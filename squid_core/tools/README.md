# Tools

A Squid Core tool pairs a model-facing function definition with a Python
execution callable. The host supplies a registry; Core advertises its definitions,
parses model calls, dispatches execution, and returns results to the conversation.
`squid_core.tools` exports `TOOLS`, `ToolExecutor`, and `ToolResults`.

## Registry and selection

The registry is a dictionary keyed by tool name. Each entry contains
`"definition"` (a zero-argument callable returning a definition dictionary) and
`"execute"` (a callable receiving the workspace root positionally, followed by
tool arguments as keyword arguments). Definitions use the function-tool shape:
`{"type": "function", "function": {"name": ..., "description": ...,
"parameters": ...}}`. Keep the registry key and definition's function name equal.
`parameters` describes an object using JSON Schema properties and required fields.

For `Agent`, `ToolExecutor`, and Core's run-construction helpers:

- `tools=None` selects the four Core built-ins in `TOOLS`.
- `tools={}` disables tools.
- An explicit registry supplies exactly those entries; built-ins are not merged
  automatically. To extend them, construct a mapping such as `{**TOOLS, **custom}`.

`Agent` applies the selected Skill's tool permissions, calls each entry's
`definition()` in `tool_definitions()`, and uses the selected registry when
assembling its prompt. The run helpers also apply Skill permissions to execution.
When constructing collaborators directly, pass the same permitted registry to
the Agent and executor (for example, pass `agent.tools` to the executor).
`ToolExecutor` itself does not select a Skill.

## Built-ins

| Tool | Arguments and behavior |
| --- | --- |
| `read_file` | Requires absolute `path`. Reads UTF-8 text, DOCX, EPUB, XLSX, ODS, PDF, and PNG/JPG/JPEG/WebP images through the file-reader subsystem. Optional `ranges` select 1-based inclusive text lines (`start_line`, `end_line`); `pages` select PDF pages (`start_page`, `end_page`); `sheet` and `rows` (`start_row`, `end_row`) select spreadsheet content. Images are whole-image reads. Reader limits, optional-library availability, image capability checks, and storage collaborators affect results. Unknown suffixes fall back to UTF-8 text. |
| `write_file` | Requires absolute `path` and full `content`. Creates or replaces a UTF-8 file; parent directories must already exist. Returns character-count metadata. |
| `apply_patch` | Takes absolute `path`, `old_text`, and `new_text` to replace exactly one occurrence in an existing UTF-8 file. Missing or ambiguous matches return diagnostics rather than editing. The executor retains run-local candidates; a subsequent `match_id` call selects a candidate and revalidates its exact text at the stored offset. Successful writes invalidate candidates for that path. This is text replacement, not unified-diff application. |
| `run_command` | Requires `command` and boolean `verification`. Runs a shell command from the workspace root, returning combined stdout/stderr, return code, and output truncation metadata. `verification` labels whether the command checks task results; it does not change command execution. |

## Execution and model-visible results

`ModelAdapter` converts raw JSON tool arguments into actions containing `tool`,
`args`, `tool_call_id`, and `raw_arguments`. `ToolExecutor.execute(action)` looks
up the entry by `action["tool"]` and calls
`entry["execute"](self.workspace_root, **args)` after execution-time guards.
Unknown tools return `{"ok": False, "tool": name, "error": ...}`.
Exceptions during execution become the same error shape.

For the original built-in callables, the executor injects command platform and
cancellation collaborators, patch candidate storage, or read settings and image
collaborators. This injection depends on callable identity. It also handles
optional undo snapshots for write/patch actions and records successful file
reads/writes in the Session by tool name.

`ToolResults.message_from_action_result(action, result)` produces a message with
`role="tool"`, the action's `tool_call_id`, and `content=json.dumps(result,
indent=2)`. Its batch method preserves order and rejects mismatched action/result
counts. The agent loop records these messages for subsequent model turns.

## Workspace and command boundaries

`Agent.workspace` is prompt context; it does not set execution paths.
`ToolExecutor.workspace_root` is the command working directory and file-tool
boundary. Run-construction helpers resolve their workspace root to an absolute
Path; direct executor callers should supply an absolute Path themselves.

The three structured file tools require absolute paths and use those paths
directly, rather than joining them to the workspace. The executor rejects targets
outside the resolved workspace by default (including resolved symlink targets).
The guard is keyed to `read_file`, `write_file`, and `apply_patch`; arbitrary
custom tool names receive no automatic filesystem boundary. Calling a built-in
execution function directly also bypasses the executor guard.

The executor permits outside paths when `allow_outside_workspace=True` or when
the host execution action carries `outside_workspace_approved=True`. The agent
loop handles preflight/approval separately; run helpers enable outside access
when the configured approval mode is `never`. Candidate patch selection uses
the retained target rather than trusting a new caller path.

`run_command` starts each command with `cwd=workspace_root`; shell commands can
change directory or access paths outside it. This is not a filesystem sandbox.
On Windows it invokes the selected PowerShell executable with `-NoProfile
-Command`. On POSIX it uses `subprocess.Popen(..., shell=True)`, hence the default
subprocess shell, even if `PlatformContext` describes a different login shell.
Use syntax appropriate to the executing shell.

Commands have a 60-second timeout and support host cancellation, with bounded,
best-effort process-tree cleanup. Normal completion can leave explicitly detached
processes running. Output is decoded as UTF-8 with replacement and truncated to
beginning/end excerpts at 12,000 characters. Success is based on process exit
code; the tool does not independently prove that task results are correct.

## Programmatic custom tools

No subclass or registration decorator is required:

```python
from squid_core.agent import Agent
from squid_core.tools import ToolExecutor


def echo_definition():
    return {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Return supplied text.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }


def echo(workspace_root, text):
    if not isinstance(text, str):
        return {"ok": False, "tool": "echo", "error": "text must be a string"}
    return {"ok": True, "tool": "echo", "message": text}


registry = {"echo": {"definition": echo_definition, "execute": echo}}
# workspace_root (Path) and session are supplied by the embedding application.
agent = Agent("Echo hello", tools=registry, workspace=str(workspace_root))
executor = ToolExecutor(workspace_root, session, tools=agent.tools)
```

Return a JSON-serializable dictionary with `ok` and preferably `tool`; include
useful data on success or `error` on failure. Direct registry callables do not
receive automatic string-to-result normalization or output truncation.
Serialization happens in `ToolResults`, outside the executor's exception handler.

The adapter checks JSON-object arguments, known definition names, required-field
presence (including alternatives from `oneOf`/`anyOf`), and boolean property
types. It does **not** implement full JSON Schema validation: validate other
types, values, extra arguments, and safety constraints in your callable.
Direct `ToolExecutor.execute()` calls bypass adapter validation and must provide
a well-formed action. Missing/unexpected callable arguments normally become
execution errors; malformed actions or registry entries can fail before the
executor's exception handler.

## Skills and Mods

A selected Skill can restrict the available registry; it cannot add missing
tools. An omitted permission list retains host tools, an explicit empty list
disables them, and references to unavailable tools raise `ValueError`.

Mods may package custom tools. The Mod runtime discovers their metadata and
builds registry entries through `tools/user_tools.py`; Python tool source is
loaded lazily on execution. These adapters call packaged functions as
`function(args, context=context)` and normalize their results. Activation still
depends on the resolved registry and selected Skill permissions; discovery alone
does not enable a tool.

See [the architecture documentation](../../docs/ARCHITECTURE.md) for runtime
ownership, public seams, and the wider Skills/Mods system.
