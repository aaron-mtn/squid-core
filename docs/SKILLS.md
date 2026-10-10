# Skills

A Squid Core `Skill` combines a name, description, ordinary model instructions,
tool permissions, and optional prompt-section overrides. It is more than prompt
text: selecting a Skill changes both the assembled system prompt and the tool
registry used by Core's default run construction.

The implementation is the repository's `squid_core/skills.py` module.
Its public entry points are `Skill`,
`PromptSectionOverride`, `parse_skill_file`, `discover_skills`, and
`select_skill_tools`. A host can load a `SKILL.md` or construct a `Skill`
programmatically, then pass that selected object to `Agent` or
`build_agent_run`. Discovery alone does not select or activate a Skill.

## Loading and file format

`parse_skill_file(path, source="user", editable=True)` reads a supplied `Path`
as UTF-8. It does not require that the filename itself be `SKILL.md`.
`discover_skills(app_home=...)` looks specifically for direct children at
`<app_home>/skills/<directory>/SKILL.md`; it does not search deeper directories
or consult Mods. A missing Skills directory returns `{}`.

Discovery processes matching paths in sorted order and returns a dictionary
keyed by the declared Skill name. Standalone names need not match their directory
names, and the standalone parser imposes no name-pattern restriction beyond a
non-empty name. If multiple files declare the same name, the later sorted file
replaces the earlier value. A malformed file aborts standalone discovery rather
than being skipped.

The file starts with a line containing exactly `---`, followed by front matter,
another exact `---` line, and the Markdown instructions body. The closing
delimiter must have a following newline. Leading whitespace, a leading blank
line, or a UTF-8 BOM before the opening delimiter is not accepted. Python's text
reading normalizes line endings before parsing.

The front matter is a small custom parser, not a general YAML implementation:

| Field | Meaning |
| --- | --- |
| `name` | Required, non-empty scalar identifying the Skill. |
| `description` | Required, non-empty scalar shown in the Active Skill block. |
| `allowed-tools` | Optional tool names; omission and an explicit empty value have different permission behavior. |

Each non-blank metadata line is split at its first colon. Keys and values are
trimmed; quote characters at the ends of scalar values are stripped without
YAML escape processing. An empty scalar starts a possible list: subsequent
indented, non-blank lines must use `- item`. Empty items are rejected. A list
used for `name` or `description` is treated as a missing required scalar.

Unknown metadata fields are parsed but not used. Repeated keys overwrite earlier
values. There is no YAML comment handling, block-scalar syntax, nested mapping
support, or general inline-list parsing. For example, `description: |` becomes
the literal scalar `|`, not a multiline description. Do not rely on YAML
features merely because the delimiter error calls this YAML front matter.

For `allowed-tools`, the supported forms are:

- A whitespace-separated scalar, such as
  `allowed-tools: read_file run_command`.
- An indented list of `- read_file`, `- run_command`, and similar entries under
  an empty `allowed-tools:` key. Each entry must contain exactly one
  whitespace-delimited tool name.
- The exact scalar `[]`, or an empty `allowed-tools:` value, for an explicitly
  empty permission list.

Non-empty inline YAML arrays such as `[read_file, run_command]` are not decoded
as arrays; their text is split on whitespace and produces incorrect tool names.
Tool names are case-sensitive registry keys. The standalone parser neither
checks their availability nor rejects duplicate names.

The body may be empty. Parsing separates ordinary instructions from recognized
prompt overrides and strips surrounding whitespace from both. The resulting
`Skill` records the supplied `path`, `source`, and `editable` values as well as
its prompt and permission data. Standalone discovery sets `source="user"` and
`editable=True`; these labels are metadata, not filesystem write protection.

## Ordinary instructions and prompt assembly

The selected Skill's description and ordinary instructions are model-visible
even when they contain no prompt override headings. Core inserts them as their
own system-prompt block:

```text
## Active Skill: <name>

Description:
<description>

Skill instructions:
<ordinary instructions>
```

This block appears after the execution environment and before tool usage
guidance. Untagged instructions are not ignored. When no Skill is selected,
the block is omitted. Override content is removed from the ordinary instructions
and inserted into its respective extension slot instead.

`squid_core/agent/prompts.py` assembles the system prompt in this order:

```text
System prompt
|-- agent identity                   <- Skill may append/replace
|-- host baseline policy             <- not Skill-overridable
|-- execution environment            <- Core-generated
|-- Active Skill                     <- description + ordinary instructions
|-- tool usage guidance              <- Skill may append/replace
|-- protected response protocol      <- not Skill-overridable
|-- protected verification guidance  <- not Skill-overridable
|-- final summary requirements       <- Skill may append/replace
`-- protected examples               <- not Skill-overridable
```

Whitespace-only sections are omitted; remaining sections are joined with two
newlines, and the complete prompt is stripped. The environment section is
included only when both platform context and workspace are supplied. `Agent`
passes its permitted tool registry into prompt assembly, so environment
capability statements reflect that registry.

## Prompt-section overrides

Core owns exactly three Skill extension slots. A host supplies their initial
content through `PromptDefaults`:

| Skill section | `PromptDefaults` field | Bare Core default |
| --- | --- | --- |
| `AGENT_IDENTITY` | `agent_identity` | `You are an agent working on a user-authored task.` |
| `TOOL_USAGE_GUIDANCE` | `tool_usage_guidance` | Empty string |
| `FINAL_SUMMARY_REQUIREMENTS` | `final_summary_requirements` | Empty string |

`PromptDefaults.baseline_policy` also defaults to an empty string, but has no
Skill override slot. `PromptDefaults.agent_name` defaults to `Agent`; it is
neither an override slot nor a separate section in this system-prompt assembly.

Declare an override with an unindented, level-two heading containing exactly
the section name and mode, for example `## TOOL_USAGE_GUIDANCE append`.
Section names and modes are case-sensitive. Each of the three sections supports:

- `append`: literally `default + "\n\n" + override.content`.
- `replace`: literally `override.content`, discarding that slot's default.

There is no semantic merging or policy reconciliation. Empty override content
is accepted; an empty replacement removes that slot from the assembled prompt.
The final prompt's outer stripping and omission of blank sections still apply.

Everything before the first recognized override heading is ordinary Skill
instructions. Everything after an override heading belongs to that override
until the next recognized override heading or the end of the file. An ordinary
Markdown heading does not close an override. Place ordinary instructions before
overrides; there is no closing marker that switches back to ordinary instructions.
If a section is declared multiple times, the last declaration wins.

The parser recognizes a candidate only when the line starts with `## ` and has
exactly two whitespace-separated tokens, with an uppercase section token
containing an underscore. Other headings remain ordinary text, or part of the
current override. Consequently, a heading missing its mode or containing extra
tokens does not necessarily produce a validation error. The parser processes
lines without tracking Markdown code fences: a matching heading inside a fence
is still interpreted as an override.

The baseline policy, Core-generated environment, protected response protocol,
protected verification guidance, and protected examples have no Skill override
slots. The protected protocol and examples govern valid tool/final responses;
verification guidance requires honest reporting of what was actually checked.
Skill prompt overrides cannot replace these sections. This is a prompt assembly
boundary, not a guarantee that a model will obey every instruction.

These mechanics belong to reusable Core. Code Squid is one example of a host
that can supply richer prompt defaults through the same mechanism; using Skills
does not require Code Squid.

## Tool permissions and run construction

`allowed_tools` is a tuple of permitted names. `allowed_tools_explicit` records
whether the file contained the `allowed-tools` metadata key. Core needs both to
distinguish an omitted list from an explicitly empty one:

| Skill permissions | Parsed values | Result of `select_skill_tools` |
| --- | --- | --- |
| No selected Skill | Not applicable | Retain the supplied registry. |
| Missing `allowed-tools` | `allowed_tools=()`, `allowed_tools_explicit=False` | Retain the supplied registry. |
| `allowed-tools: []` or empty `allowed-tools:` | `allowed_tools=()`, `allowed_tools_explicit=True` | Permit no tools. |
| Non-empty permissions | Non-empty tuple; parsed files set the explicit flag to `True` | Permit only the named tools, in declared order. |

A programmatically constructed Skill with a non-empty tuple still restricts
tools even if `allowed_tools_explicit=False`. The flag changes the meaning of
an empty tuple, not whether a non-empty tuple takes effect.

Selection checks every named permission against the available registry and
raises `ValueError` with `<path>: unknown Tool '<name>'` if a name is missing.
A Skill can restrict tools; it cannot create missing tools or automatically
import them. Unrestricted selection returns the original registry. A restriction
also returns that same object if its keys already exactly match the permission
tuple in order; otherwise it builds a filtered dictionary. Standalone duplicate
permissions are not rejected, but dictionary keys collapse repeated names.

`Agent` chooses Core's built-in `TOOLS` for `tools=None`, accepts `tools={}`
as an empty registry, and uses any explicit mapping without merging built-ins.
It applies `select_skill_tools`, advertises only the resulting definitions, and
uses the selected Skill to assemble its initial system message.

`build_agent_run` restricts its supplied/default registry before constructing
the default Agent. `build_agent_loop` also restricts its supplied/default
registry using `agent.skill`, including when called directly. Default run
construction passes the permitted registry to the default tool executor as well
as the Agent, aligning model exposure with execution.

Injected agents and executors are used directly, rather than rewritten to match
the helper's selections. The host must keep those collaborators consistent.
Similarly, when constructing an executor directly, pass `agent.tools` (or the
same permitted mapping); `ToolExecutor` itself does not select a Skill. See
[Tools](../squid_core/tools/README.md) for registry and executor details.

## Standalone Skills and Skills in Mods

A Skill can exist independently of a Mod. Mods package one or more direct
`skills/<skill-id>/SKILL.md` children and designate a packaged default Skill.
`resolve_mod(..., selected_skill=None)` uses that default; a supplied name selects
one packaged Skill. Its result exposes the selected `skill` and resolved `tools`
for the host to pass into run construction. An unknown selection is rejected.

Packaged Skills use the same parser and prompt behavior, with additional Mod
validation:

- Every packaged Skill must explicitly declare `allowed-tools`; `[]` is valid.
- The directory ID must match lowercase letters/numbers separated by single
  hyphens, and the declared Skill name must match that ID.
- Duplicate permission names are rejected. Permission names must match
  `[a-z0-9_-]+`.
- Packaged Skills are marked `source="mod"`, `editable=False`.

Mod validation parses all packaged Skills and checks all of their referenced
tool metadata, including Skills that will not be selected. Invalid inactive
Skills can therefore prevent Mod resolution. Packaged Skill parse/read failures
are collected as validation errors; resolution of an invalid Mod raises
`ModResolutionError`. Standalone discovery instead propagates the failure.

Only the selected Skill contributes instructions, overrides, and active tool
permissions. Permissions are not combined across packaged Skills. Mod resolution
can build registry entries for referenced custom tools from tool metadata; that
is Mod resource resolution, not the Skill parser creating tools. A missing tool
reference makes the Mod invalid. Mod manifest format, broader package layout,
tool/hook packaging, precedence, and full resolution policy are separate concerns.

## Example

Save this as `<app_home>/skills/review/SKILL.md` for standalone discovery:

```markdown
---
name: review
description: Inspect code and report concrete findings.
allowed-tools: read_file
---

Read the requested code. Explain findings with file references.
Distinguish verified behavior from assumptions.

## FINAL_SUMMARY_REQUIREMENTS append

Include a concise list of findings and any verification limits.
```

The description and two ordinary instruction lines appear in
`## Active Skill: review`. The permission tuple is `("read_file",)` with
`allowed_tools_explicit=True`, restricting the supplied/default registry to
`read_file`; selection fails if that registry lacks it. The final paragraph
becomes an append override for `final_summary_requirements`, following any host
content in that slot. It is not duplicated in the Active Skill instructions.
The same file satisfies the packaged Skill naming and permission requirements
when placed in a Mod's `skills/review/` directory.

## Errors and validation limits

Skill format errors raise `ValueError`, generally including the path and, for
line-level parsing errors, a line number. Front matter line numbers count from
file line 2; prompt heading diagnostics count from the instructions body.
Useful failures include:

- Missing opening front matter or missing closing delimiter.
- Metadata lines without a colon, empty keys, malformed indented list items,
  or empty list items.
- Missing, blank, or list-valued `name` or `description`.
- `allowed-tools` list entries containing multiple whitespace-separated names.
- Unknown prompt section names in recognized candidate headings, such as
  `## MADE_UP_SECTION replace`.
- Explicitly unsupported sections: `VISIBLE_NARRATION_REQUIREMENTS`,
  `REQUIRED_RESPONSE_FORMAT`, `CORRECTION_MESSAGES`, and `COMPACTION_PROMPT`.
- Unsupported modes in recognized headings, such as
  `## TOOL_USAGE_GUIDANCE overwrite`. Only lowercase `append` and `replace`
  are accepted. Prompt assembly also rejects an unsupported mode in a
  programmatically supplied override when that slot is consumed.
- Unavailable tool names at tool selection time, rather than standalone file
  parsing time; Mods validate tool references during package validation.

File reading and UTF-8 decoding errors propagate from `parse_skill_file`.
Programmatic dataclass construction does not run file-parser validation.
Unknown metadata keys, repeated metadata keys, repeated prompt sections, empty
instruction bodies, and headings outside the recognition pattern are not
rejected simply for those reasons. These are current parser limits; author
Skills using the explicit syntax above rather than relying on silent fallback.

## Ownership

Core owns Skill parsing and validation, the three prompt extension slots, the
Active Skill block, and registry restriction behavior. The host/application owns
which Skill is selected, its `PromptDefaults` content, the supplied tool registry,
and broader application policy. Core's discovery and resolution helpers accept
application paths and selections; default run construction does not discover a
Skill on the host's behalf.

See [Architecture](ARCHITECTURE.md) for wider runtime ownership,
dependency direction, and public seams.
