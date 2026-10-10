# Mods

A Squid Core Mod is an on-disk package of runtime resources and metadata. It
contains one or more Skills and can package custom tools and hooks, or reference
shared tools and hooks under the host's application home. A **Skill** supplies
selected instructions, prompt overrides, and tool permissions; a **Mod** is the
container that bundles and resolves these resources. Resolving a Mod prepares
resources for the host/Core; it does not construct or execute an Agent run.

The public API in `squid_core.mods` exports `Mod`, `ResolvedMod`,
`ModMetadataError`, `ModResolutionError`, `discover_mods`, `load_mod`,
`validate_mod`, and `resolve_mod`.

## Package layout

Discovery uses the application home supplied by the caller:

```text
<app_home>/
└── mods/
    └── review-pack/
        ├── mod.yaml
        ├── skills/
        │   └── review/
        │       └── SKILL.md
        ├── tools/                 # optional packaged tools
        │   └── summarize/
        │       ├── tool.yaml
        │       └── tool.py        # default entrypoint; metadata may rename it
        └── hooks/                 # optional packaged hooks
            └── audit/
                ├── hook.yaml
                └── hook.py
```

Only direct `skills/<skill-id>/SKILL.md` children are discovered. A Mod must
contain at least one such Skill; directories without `SKILL.md` are ignored.
Tools and hooks are looked up by referenced name, rather than by recursively
scanning the package. The resolver defines no other resource directories or
files. Shared fallback resources live at `<app_home>/tools/<name>/` and
`<app_home>/hooks/<name>/`. Standalone `<app_home>/skills/` is never a fallback
for a packaged Skill.

## `mod.yaml`

The manifest has exactly these implemented fields:

| Field | Requirement and meaning |
| --- | --- |
| `name` | Required. Must match the Mod directory name and `[a-z0-9]+(?:-[a-z0-9]+)*`. |
| `description` | Required, non-empty after trimming. |
| `default_skill` | Required. Uses the same name pattern as `name` and must identify a packaged Skill directory. |
| `accent` | Optional presentation metadata, default `cyan`. Accepted values: `cyan`, `blue`, `green`, `yellow`, `orange`, `red`, `pink`, `purple`, `white`, `gray`. |
| `hooks` | Optional ordered list of hook names, default empty. Each name must match `[a-z0-9_-]+`; duplicates are invalid. |

There is no Mod version field, tool declaration field, arbitrary resource-path
field, or per-Skill hook declaration. Unknown keys, including `tools` and
`version`, are rejected. Tool references come from each packaged Skill's
`allowed-tools`; hook references come from the manifest's `hooks`.

This is a limited custom YAML-like parser, **not full YAML**:

- The first line must strip to `---`. Exactly one subsequent line must strip
  to `---` as the closing delimiter. Only whitespace may follow it; there is
  no Markdown body. Files are read as UTF-8 with line endings normalized.
- Blank lines and full-line comments beginning with `#` after trimming are
  ignored inside the delimiters. Duplicate fields are errors.
- Mapping lines must be unindented and contain a colon. The first colon
  separates the trimmed key and value.
- Scalar fields are literal strings with outer quote characters stripped.
  There is no YAML escape processing, scalar type conversion, or inline
  comment removal.
- `hooks: []` is empty. `hooks: ["audit", "trace"]` uses JSON list syntax,
  so non-empty inline lists require JSON double-quoted strings. A block list
  uses an empty `hooks:` followed by lines starting with exactly `  - `.
  Items are trimmed, outer quote characters stripped, and empty block items
  rejected. An empty block list is also valid.
- Nested mappings, block scalars, anchors, aliases, and general YAML syntax
  are not interpreted. Some unsupported scalar syntax can remain literal
  text rather than producing a parser error; use the forms shown here.

## Discovery and validation

`discover_mods(app_home, *, built_in_tools=None)` returns a dictionary keyed
by immediate Mod directory name under `<app_home>/mods`. A missing or
non-directory Mods root returns `{}`. Directories are processed in
`(name.lower(), name)` order; children without a `mod.yaml` file are skipped.

Discovery reads manifests, parses packaged Skills, and validates resource
metadata. It does not import custom tool or hook Python. A malformed manifest
normally yields a `Mod` with `valid=False` and an `errors` tuple, rather than
aborting discovery. Manifest parse/read errors produce placeholder metadata
with the directory name, empty description/default Skill, default accent, and
no Skills or hooks. Other validation errors preserve the loaded metadata and
successfully parsed Skills.

`load_mod(name, app_home, *, built_in_tools=None)` performs the same metadata
loading and validation for one name. It rejects unsafe names and a resolved
Mod directory outside the Mods root with `ModResolutionError`. A missing
manifest is an unknown Mod. An existing invalid Mod is returned as metadata;
`load_mod` alone does not reject it solely because `valid` is false.

`validate_mod(mod, app_home, *, built_in_tools=None)` returns a tuple of error
strings without importing custom Python. Its checks include:

- Manifest names, directory agreement, description, default Skill syntax,
  accent, hook name syntax, and duplicate hook names.
- At least one direct packaged Skill and an existing packaged default Skill.
- Every packaged Skill's parse validity, including required name/description
  and recognized prompt-override rules. The Skill directory ID must match the
  Mod name pattern, and its declared name must equal that ID.
- Every packaged Skill must explicitly declare `allowed-tools`; an explicit
  empty list is valid. Duplicate tool references and names outside
  `[a-z0-9_-]+` are rejected.
- Referenced tools must resolve against the protected registry, a packaged
  tool, or a valid shared tool. Referenced custom tool metadata must have a
  safe matching name, a non-empty filename without path separators for its
  entrypoint, a Python identifier for its function, and valid `args`/`safety`
  mapping shapes. `args` cannot use top-level JSON Schema
  `type: object`, `properties`, or `required` structure.
- Every declared hook must resolve to a directory with both `hook.yaml` and
  `hook.py`, readable/parseable metadata, a name matching the reference, and
  an integer version (default `1`).

All packaged Skills are parsed and their tool dependencies checked, including
inactive Skills. An invalid inactive Skill or its referenced tool can prevent
resolution of the default Skill. Hooks are Mod-wide, so all manifest hook
references are checked regardless of Skill selection.

This is not validation of every file in the package. **Unreferenced packaged
tool and hook directories are not validated.** Tool validation reads metadata
but does not check that the entrypoint file exists, compile/import its source,
or check its callable. Hook validation checks source-file existence, but not
source syntax or `create_hook`. Manifest validation errors short-circuit
resource validation; tool selection stops at the first failure for each Skill,
and hook selection stops at its first failure. Diagnostics are not an
exhaustive inventory of every possible package error.

## Resolution and returned resources

```python
resolved = resolve_mod(
    "review-pack",
    app_home,
    selected_skill=None,
    workspace_root=None,
    session_id=None,
    built_in_tools=None,
)
```

Resolution loads and revalidates the package before selecting a Skill. It
raises `ModResolutionError` for an invalid package. `selected_skill=None`
chooses `default_skill`; an explicit value must be a key in the packaged
Skills dictionary. Unknown names, including an empty string, raise an unknown
selected Skill error. Explicit selection cannot bypass an invalid default or
fall back to a standalone Skill.

The returned frozen `ResolvedMod` dataclass contains:

| Field | Value |
| --- | --- |
| `metadata` | A `Mod` with `name`, `description`, `default_skill`, `accent`, ordered hook-name tuple, package `path`, all successfully parsed packaged `skills`, `valid=True`, and `errors=()`. |
| `skill` | The one selected `Skill`, marked `source="mod"`, `editable=False`. These labels are metadata, not filesystem write protection. |
| `tools` | Registry restricted to the selected Skill's `allowed_tools`, in that tuple's order. An empty permission list produces `{}`. |
| `hooks` | Tuple of instantiated hooks, in manifest order. |

Only the selected Skill contributes instructions, prompt overrides, and tool
permissions when supplied to run construction. Other Skills' permissions are
never unioned into the active registry. See [Skills](../../docs/SKILLS.md) for
the shared Skill format and prompt behavior.

Resolution does not build an Agent, create a session, call a model, execute
tools, or dispatch hook lifecycle methods. It does execute hook module code
and factories while preparing the hook instances.

## Packaged tools

A referenced packaged tool uses the same `tool.yaml` loader as a shared user
tool. `entrypoint` identifies a filename in its directory (default `tool.py`),
and `function` identifies the callable (default `run`). Tool metadata can also
contain description, version, argument specifications, and safety metadata.
The example below illustrates the metadata format; see
[Tools](../tools/README.md) for executor behavior.

Registry construction remains lazy. Each custom entry has an `execute`
callable, a zero-argument `definition` callable producing a model tool
definition, and a `source` label (`"mod"` for packaged, `"user"` for shared).
Each execution resolves the entrypoint within the tool directory, checks that
it is a file, imports it into a fresh module, and calls:

```python
result = function(args, context=context)
```

`args` is the argument dictionary. `context` contains string `workspace_root`
and `tool_name`, plus `session_id` when a truthy ID was supplied to resolution.
A workspace supplied to resolution is captured by the registry wrapper;
otherwise the wrapper uses the workspace passed to `execute` at call time.

Dictionary results are copied and default missing `tool`/`ok` fields. Strings,
`None`, and other values become successful result dictionaries with a message
and empty data. Top-level string fields are truncated through the shared
head/tail helper when necessary. Missing source/callables, import failures,
and tool exceptions become `{"ok": False, "tool": name, "error": ...}`
results at execution time, rather than Mod resolution failures.

## Packaged hooks

The manifest's ordered `hooks` list selects the hooks for every Skill in the
Mod. Each reference uses `hooks/<name>/hook.yaml` and the fixed
`hooks/<name>/hook.py`, falling back to the same directory under application
home only when the packaged directory is absent.

Selection passes `enabled_override=True` to the metadata loader. Listing a
hook in the manifest therefore enables it even if `hook.yaml` says
`enabled: false`; the stored `enabled` field's boolean type is not checked on
this overridden path. Hooks not listed are neither selected nor loaded.

During resolution, `load_hook_infos` imports selected source and requires a
callable `create_hook`. It calls `create_hook(config=...)` when the signature
accepts `config` or arbitrary keyword arguments, otherwise `create_hook()`.
Config contains the selected name, description, overridden enabled value,
version, directory Path, and source (`"mod"` or `"user"`). Returned objects
are collected without validating their lifecycle methods or return type.
Import and factory failures raise `HookLoadError`.

The host can supply `HookRunner(list(resolved.hooks))` to run construction.
The runner traverses hooks in supplied order; blocking lifecycle decisions
can stop later hooks at applicable seams. Mods add no priority sorting or
automatic merge with other user hooks. See [Hooks](../hooks/README.md) for
the lifecycle contract.

## Precedence and collisions

These rules apply to referenced resources in the selected Mod, not to a
global registry combining every discovered Mod:

| Situation | Implemented behavior |
| --- | --- |
| Tool name in the protected registry | Uses that registry entry. A same-name packaged path that exists causes a validation error, even if it is not a directory. A same-name shared tool does not replace it. |
| Custom tool in a packaged directory and shared directory | Packaged directory wins. Invalid packaged metadata or missing `tool.yaml` is an error; there is no retry with the shared tool. |
| Custom tool without a packaged directory | Uses valid shared metadata; a missing reference is an error. A packaged non-directory path is treated as absent for this lookup. |
| Hook in a packaged directory and shared directory | Packaged directory wins. Invalid/missing packaged files are errors, with no shared retry. |
| Hook without a packaged directory | Uses the shared directory; a missing reference is an error. A packaged non-directory path is treated as absent. |
| Same Skill ID in standalone Skills | Ignored by Mod resolution; only packaged Skills are eligible. |
| Same local Skill/tool/hook IDs in separate Mods | No cross-Mod merge or collision check; lookups are scoped to the selected package. |
| Repeated `hooks` or packaged Skill `allowed-tools` names | Validation error, not replacement or repeated activation. |

`built_in_tools=None` protects Core's `read_file`, `write_file`, `apply_patch`,
and `run_command` registry. An explicitly supplied mapping replaces that
default protected registry; `{}` protects no built-ins and adds none.
Use the same registry for discovery, validation, and resolution so availability
checks agree. All packaged Skills' references are checked for protected-name
collisions, not just the active Skill's. An unreferenced same-name packaged
path is not checked.

Shared tool discovery scans direct directories in `(name.lower(), name)`
order, skips per-tool `UserToolError` failures, and keys results by metadata
name; tool names must match their directory, so valid same-name siblings are
not merged. The selector calls shared tool discovery even when references
will use protected or packaged tools. A shared tools root that exists but is
not a directory can therefore invalidate a package even with empty permissions.
Shared hooks are looked up only by manifest reference. There is no generic
precedence hierarchy beyond these lookups.

## Host integration

Core owns manifest parsing, discovery, validation, resolution, resource loading
helpers, and the returned runtime resources. The embedding application owns
the application home, Mod and Skill selection, protected registry, UI/settings
for enablement, and broader policy. Code Squid is one possible host; its
product defaults are not requirements of this subsystem.

Generic `build_agent_run` does not discover or accept a Mod name. The host
passes `skill=resolved.skill`, `tools=resolved.tools`, and
`hook_runner=HookRunner(list(resolved.hooks))` along with its other run
collaborators. Default construction uses the permitted tools for both Agent
exposure and execution; its default HookRunner is empty unless supplied.
Injected agents/executors remain the host's responsibility to keep consistent
with these resources. See [Architecture](../../docs/ARCHITECTURE.md) for the
wider ownership and construction seams.

## Small example

Place these four files under `<app_home>/mods/review-pack/`.

`mod.yaml`:

```yaml
---
name: review-pack
description: Review code with a packaged summary helper.
default_skill: review
hooks: []
---
```

`skills/review/SKILL.md`:

```markdown
---
name: review
description: Read code and summarize findings.
allowed-tools: read_file summarize
---

Read the requested code. Report concrete findings with file references.
Use summarize to format a short findings summary.
```

`tools/summarize/tool.yaml`:

```yaml
name: summarize
description: Format a short findings summary.
args:
  text:
    type: string
    required: true
```

`tools/summarize/tool.py`:

```python
"""Format a findings summary for the packaged review Skill."""


def run(args, context=None):
    """Return the provided summary text."""
    return {"ok": True, "message": args["text"], "data": {}}
```

`resolve_mod("review-pack", app_home)` with the default protected registry
returns the `review` Skill, a registry containing built-in `read_file` and
the lazy packaged `summarize` entry, and an empty hook tuple. Metadata has
default accent `cyan`. Tool source is not imported until `summarize` executes;
no Agent run starts. Additional packaged Skills could be selected explicitly
without changing the Mod-wide hooks.

## Author-facing errors and limits

`ModMetadataError` identifies manifest parser/required-field failures.
Normal discovery/loading captures these as invalid metadata. Resolution
aggregates validation diagnostics into `ModResolutionError` with a
`Mod <name> is invalid:` message and error bullets. Unsafe/unknown Mod names
and unknown explicit Skill selections also raise `ModResolutionError`.

Packaged Skill parse failures (`ValueError`) and read failures (`OSError`)
are collected as validation diagnostics. Referenced tool `UserToolError` and
hook `HookLoadError` metadata failures are likewise collected. Duplicate
references, mismatched names, unavailable resources, and protected tool
collisions prevent resolution; they do not silently select another resource.

Metadata validity does not guarantee executable source validity. Hook import
or factory failures propagate as `HookLoadError` during resolution. Tool
implementation failures instead return failed tool results when called.

The invalid-metadata behavior is bounded by the exceptions caught in the
implementation: Mod loading catches `ModMetadataError` and `OSError`, and
resource validation catches the types listed above. It is not a blanket
exception handler. For example, a UTF-8 decoding failure in `mod.yaml` or
referenced tool/hook metadata can propagate and abort discovery, rather than
becoming a `valid=False` entry. Errors enumerating the Mods root can also
propagate. Packaged Skill decoding failures are `ValueError` subclasses and
are collected by the Skill validation path.
