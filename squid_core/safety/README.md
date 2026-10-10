# Safety and approval

`squid_core.safety` provides tool-action approval policy, workspace-boundary
preflight for structured file tools, command trust identities for guarded
approval, and denied-action result helpers. This is an approval/policy layer,
not a general sandbox. See the [architecture](../../docs/ARCHITECTURE.md) for
its place in the runtime.

## Approval modes and policy

`ApprovalPolicy(mode, prompter=None, *, trusted_commands=(),
persist_trusted_command=None)` is UI-agnostic. Its optional `ApprovalPrompter`
supplies `approve_action(action)`; the host implements the actual user prompt.
`trusted_commands` is an iterable of identity strings, and
`persist_trusted_command` is an optional callback accepting one identity string.
Unknown modes raise `ValueError("Unknown approval mode: ...")`.

`check(action)` returns an `ApprovalDecision` according to these modes:

| Constant | Value | Behavior |
| --- | --- | --- |
| `APPROVAL_NEVER` | `"never"` | Approves every action passed to the policy without prompting, including actions with workspace or hook approval reasons. |
| `APPROVAL_ALWAYS` | `"always"` | Requires a prompt for every action passed to the policy, regardless of command trust or whether an approval reason exists. |
| `APPROVAL_GUARDED` | `"guarded"` | Automatically approves non-`run_command` actions without a truthy `approval_reason`. For `run_command`, automatically approves a trusted literal command identity only when no separate approval reason exists. Other actions require a prompt. |

These rules apply to actions that reach the policy. A hook can block an action
before approval, and execution can still fail after approval. In particular,
`never` does not override a hook block or remove execution-time tool validation.
`always` does not calculate command identities or offer persistent command
approval through the policy.

`ApprovalDecision` is a frozen dataclass with these fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `approved: bool` | Required | Whether to allow the action. |
| `instruction: str` | `""` | User instruction preserved on denial. |
| `reason: str` | `""` | Denial reason. |
| `always_approve_command: bool` | `False` | Request to remember an eligible guarded command identity. |

Although the prompter protocol declares an `ApprovalDecision` return,
`check` also accepts a boolean and normalizes it to
`ApprovalDecision(approved=value)`. On success, the policy returns a fresh
`ApprovalDecision(approved=True)` after any trust update; the prompter's other
fields are not carried into that returned decision.

## Guarded command trust

For `run_command`, guarded policy reads `args["command"]` only when `args` is a
dictionary and the command is a string, then calls `command_identity`.
An untrusted identity without a separate approval reason produces
`"<identity> commands are not trusted"`. An unavailable identity produces
`"Command identity is missing"`.

A truthy existing `approval_reason` requires approval even for a trusted
command. That separate reason is retained rather than replaced by a command
trust reason. An untrusted literal identity can still be remembered during
that prompt. The policy adds `command_identity` and
`allow_persistent_command_approval=True` only when an untrusted literal
identity was found. It adds neither hint for a missing identity or for an
already trusted command prompted because of a separate reason.

Guarded mode checks a trust key; it does not validate command safety or inspect
arguments, subsequent commands, or shell effects.

### Command identity

`command_identity(command: str) -> str | None` extracts a normalized literal
first program token without parsing shell semantics:

- It skips leading whitespace, extracts a nonempty double-quoted or
  single-quoted first token (without the quotes), or an ordinary bare
  non-whitespace token, then applies `casefold()`.
- Empty or whitespace-only commands have no identity.
- A leading PowerShell call operator `&` is specially recognized only when
  followed by whitespace or the end of the string. After that operator, only
  spaces and tabs are skipped, and a straightforward literal token must be
  followed by whitespace or the end of the string.
- In this `&` branch, double-quoted tokens cannot contain `$` or backticks;
  single-quoted tokens are accepted as literals. Bare tokens exclude whitespace,
  `$`, backticks, quotes, and `;&|<>(){}[],`, and cannot start with `$`, `(`,
  `{`, `[`, or `@`. Dynamic expressions deliberately return `None`.

Examples:

| Command text | Identity |
| --- | --- |
| `  GIT status` | `git` |
| `'pytest' -q` | `pytest` |
| `& "C:\Program Files\Tool\tool.exe" --check` | `c:\program files\tool\tool.exe` |
| `& git status` | `git` |
| `&`, `& $command`, `& (Get-Command tool)`, `& { git status }`, `& @commands` | `None` |
| `& "$command" --check` | `None` |

The stricter dynamic-expression handling belongs to the recognized `&` branch;
it is not general shell-expression detection. For example, `&git status` takes
the ordinary-token branch and yields `&git`. The helper does not resolve an
executable, canonicalize a program path, or analyze shell syntax. Command
identity is only a trust key for approval policy, not proof that a command or
its arguments are safe.

### Remembering trust

Constructor-provided `trusted_commands` entries are normalized with `casefold()`;
empty entries are ignored. They are identity strings, not full command lines
to be parsed.

When a prompter approves an eligible guarded command with
`always_approve_command=True`, the policy calls `persist_trusted_command(identity)`
before adding that identity to its in-memory trust set. If the callback raises,
the exception propagates and the identity is not added by this update, so the
process does not falsely treat failed persistence as successful. Without a
persistence callback, the in-memory trust set is still updated. Core does not
choose a persistence format, storage location, or reload mechanism.

## Workspace preflight and execution flow

`ActionPreflight(workspace_root).check(action, *, additional_approval_reason="")`
returns a frozen `ActionPreflightDecision` containing `approval_action` and
`execution_action`. `ActionPreflight.from_runtime(tool_executor, _approval_policy)`
obtains `workspace_root` from the executor; its policy argument is unused.
If the root is `None`, workspace checks are skipped.

Preflight combines the additional approval reason first and the workspace
reason second, joining nonempty reasons with `"; "`. If either exists,
`approval_action` is a shallow copy with that combined `approval_reason`.
Otherwise it is the original action. A pre-existing action `approval_reason`
is retained only when no new combined reason is generated; it is not a third
input to the combination.

When a workspace reason exists, `execution_action` is a separate shallow copy
of the original action with `outside_workspace_approved=True`; otherwise it is
the original action. Despite its name, this marker is prepared before the
approval decision. Preflight neither approves nor executes the tool: the
AgentLoop must gate use of the execution action on approval.

The current AgentLoop flow is:

1. Run `before_tool_execute` hooks, accepting their action replacement. A hook
   block creates an unsuccessful result without preflight or approval.
2. For an unblocked action, call preflight with the hook response's
   `approval_reason` as `additional_approval_reason`.
3. Call `ApprovalPolicy.check(preflight.approval_action)`. The policy may create
   another action copy with command trust reasons and UI hints for the prompter.
4. Only on approval, display the preflight approval action and pass
   `preflight.execution_action` to `ToolExecutor.execute`. A `never` decision
   also allows this path, including its outside-workspace marker.
5. On denial, create a denied result instead of executing. Both execution and
   denial results pass through `tool_result` hooks, display, and transcript
   recording.

Thus `approval_reason`, `command_identity`, and
`allow_persistent_command_approval` are policy/UI hints. Preflight-added reasons
and policy-added command hints are not copied into the execution action.
The display receives the preflight approval action, not the policy's local
command-hint copy. Existing fields in the original action are preserved by
these shallow copies; the flow is not a general removal of all UI fields.
The executor dispatches the tool's `args`, while consuming the top-level
outside-workspace marker for its guard.

### Structured file-tool boundary

`workspace_boundary_reason(action, workspace_root)` checks exactly the names
in `FILE_TOOLS = {"read_file", "write_file", "apply_patch"}`. It returns no
reason for other tools, non-dictionary `args`, or a non-string `args["path"]`.

Only paths that `Path(path).is_absolute()` recognizes as absolute are checked.
For those paths, `is_inside_workspace` applies `expanduser().resolve()` to both
the target and workspace root, then checks containment with `relative_to`.
Symlink resolution therefore participates in containment. An inside-workspace
absolute path needs no workspace reason; an outside-workspace absolute path
produces a reason identifying the target and root and stating that approval is
required unless `approval_mode=never`.

Relative paths produce no boundary reason here. In particular, the absolute
path test occurs before expansion, so `~/file` does not become a checked
absolute path in this helper. Built-in file tools separately reject relative
file paths; that validation is not performed by this preflight helper.
Pathless `apply_patch` candidate selection also supplies no path for this
helper to check. See [tools](../tools/README.md) for file validation,
execution-time guards, and candidate-selection details.

### Hooks

`before_tool_execute` hooks may supply `approval_reason`. The hook runner joins
reasons from multiple hooks with `"; "`; preflight then combines that response
with any workspace reason. This lets hosts request approval for reasons
unrelated to command trust or workspace boundaries. Trusted command identity
does not bypass such a reason in guarded mode; `never` still bypasses policy
prompting. Hooks can also block execution independently of approval mode.
See [hooks](../hooks/README.md) for their full contract.

## Denial and missing prompters

If approval is required but no prompter exists, the policy returns
`ApprovalDecision(approved=False, reason="approval prompter required")`.
Automatically approved paths do not need a prompter.

For a prompter denial, the policy preserves `instruction` and uses the supplied
`reason`, falling back to
`USER_DENIED_TOOL_ACTION = "user denied tool action"` when the reason is empty.
`denied_result(action, reason)` returns:

```python
{"ok": False, "tool": action.get("tool", ""), "error": reason}
```

AgentLoop collects denial instructions whose `.strip()` is nonempty and records
each original instruction as a user-role message after the batch's tool-result
messages. Denials consume a tool attempt. Result hooks may update the denied
output; if the resulting output remains unsuccessful, the loop stops executing
the batch and records later untouched actions as skipped.

## Security limits and host ownership

**`run_command` is not workspace-sandboxed.** Workspace preflight does not
constrain it. Its workspace root is an initial working directory: commands
can change directory, access outside paths, and invoke arbitrary programs
permitted by the OS and user account. Guarded command approval is not filesystem
containment. See [run_command execution details](../tools/README.md).

This subsystem does not provide OS sandboxing, container isolation, command
semantic analysis, argument-level trust guarantees, network isolation,
permission dropping, filesystem virtualization, or safe execution of arbitrary
custom tools. Trusting `git`, for example, trusts that literal program identity
for guarded approval; it does not prove every invocation is harmless. Custom
tools are not automatically workspace-bounded merely because they are in the
registry. Only the three named structured file tools participate in this
workspace preflight, and it examines their explicit path argument.

Core owns approval-mode mechanics, guarded command identity checks, workspace
preflight helpers, and approval decision/result shapes. The host/application
owns the chosen approval mode, UI prompting and reason presentation, trusted
command persistence location and mechanism, broader sandboxing or OS isolation,
and application-specific policy supplied through hooks. No Code Squid-specific
UI is required.

## Small policy example

```python
from squid_core.safety import APPROVAL_GUARDED, ApprovalPolicy

policy = ApprovalPolicy(APPROVAL_GUARDED, trusted_commands={"git"})
assert policy.check({"tool": "run_command", "args": {"command": "git status"}}).approved

# An untrusted command requires a prompter; none was supplied here.
decision = policy.check({"tool": "run_command", "args": {"command": "python script.py"}})
assert not decision.approved
assert decision.reason == "approval prompter required"
```

This example checks policy only; it does not execute either command.
