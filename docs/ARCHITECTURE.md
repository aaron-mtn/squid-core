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
