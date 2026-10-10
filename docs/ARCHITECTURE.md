# Squid Core Architecture

Source installation and package metadata
----------------------------------------

The root `pyproject.toml` uses setuptools to package only `squid_core` and its
subpackages. Source consumers can install the checkout normally or editably;
no sibling repository is needed. `README.md` supplies the package's long
description and a standalone tool-using agent example using public collaborators
and a stdlib OpenAI-compatible transport. The example uses the script's directory
as its workspace, detected platform context, and the full built-in tool registry.
The root `LICENSE` contains the MIT license, referenced through the file-based
license metadata supported by the existing `setuptools>=68` build requirement.
These packaging and documentation files do not select providers or alter runtime
behavior. Sibling checkouts are only a cross-repository development arrangement.

`MANIFEST.in` explicitly includes `docs/ARCHITECTURE.md`, `docs/SKILLS.md`,
the subsystem README guides, and `requirements-dev.txt` in source distributions.
This preserves the public reference links and documented test setup in a source
archive. README links to the architecture guide. The development log is private
to the local workflow and explicitly excluded from source distributions along
with generated file trees, agent instructions, caches, build outputs, and runtime
state. It is also excluded from version control. Skills implementation remains
`squid_core/skills.py`. The default setuptools test inclusion policy is retained.
Wheel contents remain runtime modules and
distribution metadata (including LICENSE): `include-package-data = false`
prevents these source-only docs from becoming installed package data.

README owns the public quickstart; private/local verification projects are not
consumer dependencies or public setup steps. Core contains no README example
drift checker. Ordinary required tests do not invoke live model inference.
Runtime ownership and public contracts remain unchanged.

Core owns the generic agent runtime, tools, storage, compaction, resource loading,
and response/provider contracts. It never selects a backend or imports Code
Squid, Providers, OpenAI, or httpx. Applications supply a ModelAdapter first,
otherwise a provider plus explicit model/replay inputs; missing collaborators
fail clearly. Providers imports only model_response and providers.base contracts:
BaseProvider, StreamCancelled, ModelResponse, ToolCall, TokenUsage. Native
normalization, transport tracing, vision encoding and cancellation monitors
remain provider-owned.

Squid Core has zero required third-party Python runtime dependencies. Its
`documents` packaging extra owns the optional runtime requirements:
`epub-utils==0.1.0a1`, `odfpy`, `openpyxl`, `python-docx`, `Pillow`, and `PyMuPDF`.
Readers remain Core-owned under `squid_core/file_readers/`, and the static suffix
registry always recognizes EPUB, ODS, XLSX, DOCX, PDF, PNG, JPG/JPEG, and WebP.
Missing parsers never cause known formats to fall through to UTF-8 decoding.
Readers import parsers only when needed and report missing imports separately
from malformed input, through the existing failure dictionary with
`pip install "squid-core[documents]"` guidance. Parser-independent selection and
container safety checks still run before parsing.

`image_content` constants and descriptor helpers use only the standard library.
`inspect_image_bytes` imports Pillow locally and raises `ImageValidationError`
with the same extra guidance when unavailable. Session generated-image validation
uses that same function; no cache or image integrity/decompression checks are
relaxed. Ordinary runtime construction, text reads, provider adaptation, storage,
hooks, Mods/Skills, compaction, and built-in schemas require no document libraries.

There is one provider integration path: ModelAdapter prepares a dictionary of
`model`, `messages`, and `tools`, then calls
`provider.create_response(**request)`. A plain object implementing
`create_response(model, messages, tools) -> ModelResponse` is sufficient for
normal inference; BaseProvider is an optional convenience base, not an
instantiation gate. No new request wrapper or callable-provider API is involved.
build_agent_loop uses the injected provider and explicit model name to build
this same adapter. Agent turns and text generation share its transport boundary.

Streaming and image input default to unsupported. Providers opt into streaming
with supports_streaming() and stream_chat(), which returns the same completed
ModelResponse after delivering deltas. Image support requires
supports_image_input(model) and the session asset-loader transport option.
Readiness and model discovery are optional host operations; Core never calls
is_ready() or list_models(). BaseProvider leaves these operations explicitly
unimplemented rather than inventing readiness or model identifiers. Optional
reasoning controls, tracing and cancellation parameters retain their existing
signatures; basic inference does not pass them.

Providers return Core-owned ModelResponse, ToolCall and TokenUsage contracts.
All completed transport paths reject dictionaries and SDK/response lookalikes
with ModelRequestError classified as malformed_response. Core alone parses and
validates tool arguments; malformed tool arguments still produce the existing
controlled format-error reply. Diagnostic provider names are caller labels,
never catalog IDs used to select behavior.

Shared failure classification lives in `squid_core/model_adapter.py` through
`model_request_error_details`. It walks up to five exceptions, preferring explicit
causes and otherwise following unsuppressed contexts, with cycle protection.
HTTP status and available response metadata are inspected throughout that chain;
an outer RuntimeError does not erase transport evidence. Diagnostics are bounded
and credential-redacted. Core does not read urllib error streams: transports that
consume those bodies can retain useful text in their wrapper messages. Response
objects exposing text/content can supply a separate response_body_excerpt.

Code Squid -> Core; Code Squid -> Providers -> small Core contracts.
Each package lives in a sibling Git repository. Source is under the underscore
package, tests under tests/, packaging at the root. No compatibility source
paths remain. Applications persist sparse supported config dictionaries and
construct providers before injecting them into Core.

Core's `squid_core/hooks/` contains only lifecycle contexts, `HookResponse`,
`PromptContextBlock`, `HookRunner`, and metadata/discovery/loading. The host
supplies hook objects; Core invokes their callbacks and applies only the effects
allowed at each seam. Core installs no behavioral nudges. Code Squid owns its
concrete hooks under `code_squid/hooks/` and decides which to enable.

The generic `compaction_applied` and `read_result_trimmed` notifications remain
public contracts. AgentLoop dispatches the former only after compaction has
been applied and persisted, and bridges request-only read trimming into the
latter with retained source ranges. These seams support host observations of
model-visible context independently of any built-in coverage policy. Hook
registration order, context/result transformations, transient guidance and
finalization/follow-up contracts are unchanged.


Core prompt and tool selection
------------------------------

`Agent`, `ToolExecutor`, `build_agent_run`, and `build_agent_loop` use Core's
built-in `TOOLS` registry for `tools=None`. An empty mapping disables tools;
an explicit mapping is used without merging. `Agent` and the construction
helpers restrict that mapping to the selected Skill's permitted tools. An
already permitted registry is retained without copying.
Injected agents and executors remain caller-owned collaborators. Default run
construction shares the resolved registry between its Agent and executor.

`Skill.allowed_tools` governs standalone and Mod Skills alike. Missing
`allowed-tools` retains the supplied/default registry; explicit `allowed-tools: []`
permits no tools; a non-empty list permits only those named tools in the
supplied registry. Unavailable names raise a validation error, never cause
implicit addition of Core tools. Programmatic non-empty Skill lists also
constrain tools even when `allowed_tools_explicit` is false. Mod Skills continue
to require explicit lists and validate every packaged profile before selection.

Mod discovery, loading, validation, and resolution accept `built_in_tools`.
`None` selects Core's registry; an empty or app-specific registry is used as
supplied in every phase. Protected host tools precede loose resources; Mod-local
tools/hooks precede standalone resources. Skills remain package-local. These
resource precedence and activation rules are unchanged.

Mod authoring contract and resource ownership
--------------------------------------------

Core's `squid_core/mods/` owns strict metadata validation and runtime resolution,
not a bundled Mod Builder, authoring guide, examples, or scaffolding tools.
Mod authoring resources and scaffolding belong to their host application.
They can consume Core's public loaders; Core never imports a host to author
or validate a Mod.

`mod.yaml` supports exactly `name`, `description`, `default_skill`, `accent`,
and `hooks`, with opening and closing `---` delimiters. Every package contains
one or more direct `skills/<skill-id>/SKILL.md` children; each declares explicit
`allowed-tools`, and `default_skill` names a packaged Skill. A one-Skill starter
is a valid instance of this multi-Skill contract, not a separate schema.
All packaged Skills are validated, but only the selected Skill grants tools.
Manifest-level `tools` is rejected rather than migrated or interpreted.

Custom components retain `tools/<tool-name>/tool.yaml` plus their declared Python
entrypoint, and `hooks/<hook-name>/hook.yaml` plus `hook.py`. A component folder
makes it available; the selected Skill's `allowed-tools` activates tools and the
manifest's `hooks` activates Mod-wide hooks. Prompt overrides use the three
current section names described below; no legacy section alias is accepted.

Generic APIs, Session/ToolExecutor attributes, Hook contexts, saved-tool execution
contexts, and run-started events call the filesystem working root
`workspace_root`. It need not contain Git metadata. This pre-release rename
uses `workspace_root` consistently in APIs, contexts, events, and persisted
session/undo metadata. Consumers must use that keyword and metadata key;
positional calls retain their order. Undo restore validates the recorded
`workspace_root` against the current workspace before any mutation. The undo
index still stores file paths and snapshot records, with no root field.
No legacy aliases, fallback reads, dual writes, or storage migration exist;
older development metadata may be incompatible. Filesystem layout, containment,
session replay, and undo lifecycle behavior are unchanged.

`PromptDefaults.agent_identity` supplies the identity override default. The
three fixed Skill/Mod prompt sections are `AGENT_IDENTITY`,
`TOOL_USAGE_GUIDANCE`, and `FINAL_SUMMARY_REQUIREMENTS`; each supports literal
`append` and `replace`. Current resources and consumers must use these names;
no legacy section or field aliases are accepted.

Core protects only the tool-call versus non-empty final-response protocol,
batching and result-dependent sequencing, finishing on completion, generic
verification honesty, task-neutral protocol examples, and malformed/empty reply
correction. It assembles host identity, baseline policy, Skill instructions,
overrideable guidance, and environment facts without importing the host.
Narration style, source-reading and editing preferences, final report style,
and coding examples belong to `code_squid/agent/prompts.py` in Code Squid.
Code Squid keeps narration, coding examples, and moved source/shell-discovery
preferences in its non-replaceable baseline policy alongside its coding discipline; tool and final-report guidance keep
existing Skill override behavior.

`Agent.initial_messages` passes its resolved active registry to prompt assembly.
The environment block retains OS, preferred shell, path separator, and workspace
facts. Generic command-environment and platform-specific shell execution
guidance appear only with `run_command`; capability
notes for `read_file`, `apply_patch`, and `write_file` appear only with those
tools. Core states available capabilities; the application chooses preferences.

Text/spreadsheet omission markers and request-only trimmed source/content
markers use `SQUID CORE` wording. Producers and omission-marker recognition
share the new prefix. Old branded omission markers are not recognized as the
current internal marker format; persisted original tool results are not rewritten.

Current pre-release collaborator and transcript contracts
-------------------------------------------------------

Streaming providers accept `on_reasoning_delta`; ModelAdapter forwards it
without signature inspection. Compaction calls `complete_text(messages)` when
no cancellation callback is configured, and passes `should_cancel` when one is
configured. Adapters must support that current cancellation contract; Core does
not silently drop callbacks for obsolete custom signatures.

Request observation remains optional for injected collaborators: automatic
capture requires callable `set_request_observer` and `append_model_view` methods.
Persisted assistant diagnostics render only `reasoning`; provider-native aliases
are normalized inside providers before reaching Core. Diagnostic `raw_tool_calls`
are flat id/name/arguments records, while model-visible message tool calls retain
the nested function shape. The transcript renderer supports both current sources.
No migration, reasoning fallback, or compatibility alias is provided.
