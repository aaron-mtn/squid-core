# Squid Core Architecture

Core owns the generic agent runtime, tools, storage, compaction, resource loading,
and response/provider contracts. It never selects a backend or imports Code
Squid, Providers, OpenAI, or httpx. Applications supply a ModelAdapter first,
otherwise a provider plus explicit model/replay inputs; missing collaborators
fail clearly. Providers imports only model_response and providers.base contracts:
BaseProvider, StreamCancelled, ModelResponse, ToolCall, TokenUsage. Native
normalization, transport tracing, vision encoding and cancellation monitors
remain provider-owned.

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
`append` and `replace`. This pre-release rename replaces `SYSTEM_PROMPT_INTRO`
and `PromptDefaults.system_prompt_intro` directly, without compatibility aliases.
Current resources and consumers must use the new names.

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
