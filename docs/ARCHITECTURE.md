# Squid Core Architecture

Core owns the generic agent runtime, tools, storage, compaction, resource loading,
and response/provider contracts. It never selects a backend or imports Code
Squid, Providers, OpenAI, or httpx. Applications supply a ModelAdapter first,
otherwise a provider plus explicit model/replay inputs; missing collaborators
fail clearly. Providers imports only model_response and providers.base contracts:
BaseProvider, StreamCancelled, ModelResponse, ToolCall, TokenUsage. Native
normalization, transport tracing, vision encoding and cancellation monitors
remain provider-owned.

Code Squid -> Core; Code Squid -> Providers -> small Core contracts.
Each package lives in a sibling Git repository. Source is under the underscore
package, tests under tests/, packaging at the root. No compatibility source
paths remain. Applications persist sparse supported config dictionaries and
construct providers before injecting them into Core.
