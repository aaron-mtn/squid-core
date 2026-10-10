# Squid Core

Squid Core is a reusable Python runtime for building tool-using AI agents. It
coordinates model responses, tool execution, and conversation state so you can
build your own agent application around a model backend of your choice.

Use it when you want an agent loop with tools, sessions/storage, compaction,
hooks, and Skills/Mods without adopting a particular application or provider.
The base package has zero required third-party Python runtime dependencies.

Core is provider-agnostic: your application supplies the provider, model, and
presentation. It does not require Code Squid, `squid-providers`, the OpenAI SDK,
or httpx.

Code Squid is an application built on Core; `squid-providers` contains optional
first-party provider integrations. These projects are developed as sibling
repositories, but Squid Core installs and works independently.

## Installation

Python 3.11 or newer is required. From a checked-out copy of this repository:

```bash
python -m pip install .
```

For an editable development install:

```bash
python -m pip install -e .
```

To install directly from GitHub, replace `<owner>` with the repository owner
and `v0.1.0` with an available release tag:

```bash
python -m pip install "git+https://github.com/<owner>/squid-core.git@v0.1.0"
```

This release does not assume PyPI publication.

The base install includes the runtime, built-in tools, and UTF-8/plain-text
reading. For EPUB, ODS, XLSX, DOCX, PDF, and supported PNG, JPG/JPEG, and WebP
images, install the optional `documents` extra:

```bash
python -m pip install ".[documents]"
# Editable equivalent:
python -m pip install -e ".[documents]"
```

Missing optional libraries produce a normal read/validation failure with install
guidance. Generated-image validation also requires the extra; image safety
checks remain enabled.

## Run a complete agent

Save this as `hello_core.py` in the directory you want the agent to work in.
It uses Core and the Python standard library to call an OpenAI-compatible
`/chat/completions` endpoint and execute real tools. Start a local model server
with a tool-calling model loaded, then set `SQUID_BASE_URL` to its API base URL
(including `/v1`) and `SQUID_MODEL` to the loaded model ID. This minimal provider
uses non-streaming text/tool responses and an endpoint that needs no API key.

```python
"""Run a tool-using Squid Core agent in the directory containing this script."""

import json
import os
from pathlib import Path
import urllib.request

from squid_core.agent import Agent
from squid_core.agent_loop import AgentLoop
from squid_core.model_adapter import ModelAdapter
from squid_core.model_response import ModelResponse, ToolCall
from squid_core.platform_context import detect_platform_context
from squid_core.session import Session
from squid_core.spawner import NullDisplay
from squid_core.tools import TOOLS, ToolExecutor, ToolResults


BASE_URL = os.getenv("SQUID_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
MODEL = os.environ["SQUID_MODEL"]


class ChatProvider:
    """Translate OpenAI-compatible chat completions into Core responses."""

    def create_response(self, model, messages, tools) -> ModelResponse:
        """Send the conversation and preserve the model's raw tool arguments."""
        request = urllib.request.Request(
            BASE_URL + "/chat/completions",
            data=json.dumps({
                "model": model, "messages": messages, "tools": tools,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            message = json.load(response)["choices"][0]["message"]
        # Core owns argument parsing, execution, and tool-result replay.
        calls = [
            ToolCall(call["id"], call["function"]["name"],
                     call["function"]["arguments"])
            for call in message.get("tool_calls") or []
        ]
        return ModelResponse(content=message.get("content"), tool_calls=calls)


workspace = Path(__file__).resolve().parent
platform_context = detect_platform_context()
session = Session(workspace)
print(f"Workspace: {workspace}")
task = input("You: ")

# Advertise and execute the same registry, using normal Core defaults.
loop = AgentLoop(
    session=session,
    agent=Agent(task, tools=TOOLS, platform_context=platform_context,
                workspace=str(workspace)),
    model_adapter=ModelAdapter(ChatProvider(), MODEL),
    tool_executor=ToolExecutor(
        workspace, session, tools=TOOLS, platform_context=platform_context,
    ),
    tool_results=ToolResults(),
    display=NullDisplay(),
)

result = loop.run()
if result.status != "complete":
    raise RuntimeError(result.reason or result.status)
print(session.messages[-1]["content"])
```

For example, in PowerShell (replace `your-model-id`):

```powershell
$env:SQUID_BASE_URL = "http://127.0.0.1:1234/v1"
$env:SQUID_MODEL = "your-model-id"
python hello_core.py
```

Or in a POSIX shell:

```bash
export SQUID_BASE_URL="http://127.0.0.1:1234/v1"
export SQUID_MODEL="your-model-id"
python hello_core.py
```

At the `You:` prompt, try: `Read hello_core.py with read_file and summarize how
the provider works.` The workspace stays the script's directory even when you
launch it from elsewhere. All four built-in tools are enabled, including file
editing and local command execution.

`Session` holds the transcript, `ModelAdapter` connects the provider to Core,
and `AgentLoop` runs until the agent completes. `NullDisplay` provides silent
presentation; the example prints the final message itself and keeps the session
in memory.

## Bring your own provider

Normal inference requires just
`create_response(model, messages, tools) -> ModelResponse` on a provider object.
Core calls it with keyword arguments. Subclassing
`squid_core.providers.BaseProvider` is optional.

The provider receives the model name, conversation messages, and a list of tool
definitions. Translate your backend's native results into Core's `ModelResponse`,
`ToolCall`, and `TokenUsage` contracts from `squid_core.model_response`. Tool
calls carry raw JSON argument strings; Core parses and validates those arguments.
Return Core contracts rather than SDK response objects or dictionaries.

Streaming, readiness checks, model discovery, and vision are optional
capabilities. Concrete integrations can live in a separate package, including
the optional first-party `squid-providers` repository. See the
[provider contracts in the architecture docs](docs/ARCHITECTURE.md) for details.

## Tools and workspaces

Core's built-in registry, exported as `squid_core.tools.TOOLS`, contains:

| Tool | Purpose |
| --- | --- |
| `read_file` | Read text and supported documents/images. |
| `write_file` | Write file contents. |
| `apply_patch` | Apply structured file edits. |
| `run_command` | Execute a local shell command. |

For `Agent`, `ToolExecutor`, and Core's run-construction helpers, `tools=None`
selects Core's built-ins, `tools={}` disables tools, and an explicit mapping uses
that supplied registry without merging in built-ins. Applications can provide
custom tools. A selected Skill can further restrict the Agent's permitted tools.
Supply the same resolved registry to the agent and executor: the agent advertises
definitions and the executor dispatches calls.

The workspace is a filesystem working root, not necessarily a Git repository.
Structured file tools require absolute paths. A directly constructed executor
rejects outside paths by default, but `AgentLoop`'s default `never` approval policy
permits them through preflight. `run_command` uses the workspace as its initial
working directory; Windows uses the selected PowerShell executable, while POSIX
uses the default subprocess shell. It is not a filesystem sandbox.
`detect_platform_context()` supplies OS, shell, and path information for prompts
and command execution, as shown above.
Applications choose the workspace and tool access, and can supply storage for
durable sessions.

## Core concepts and next steps

- `Agent`: task, prompt assembly, tool selection, and completion rules.
- `AgentLoop`: model/tool turns and run lifecycle.
- `ModelAdapter`: provider requests and response normalization.
- `Session`: conversation state and tool side effects.
- `ToolExecutor`: tool dispatch and execution guards.
- `ToolResults`: tool-result formatting for conversation messages.
- Hooks: host-supplied callbacks at lifecycle seams.
- Skills/Mods: packaged instructions, tool permissions, and custom resources.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership, public seams,
runtime flow, resource layouts, and safety invariants.

## Development and testing

From this repository, install the editable package and pytest using the existing
development requirements, then run Core's own tests:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

Document/image parser tests require the optional `documents` extra (otherwise
those tests skip). No live local or cloud model inference is required for the
test suite.

```bash
python -m pip install -e ".[documents]"
python -m compileall -q squid_core
git diff --check
```

Optional cross-repository development: keep `code-squid`, `squid-core`, and
`squid-providers` as siblings when changing the application and integrations
together. Install each project's development requirements and run its suite
separately to avoid duplicate test module names. Sibling checkouts are not
required to use or test Core on its own.

To build a local wheel from source, use `python -m pip wheel --no-deps .`.

## License

Squid Core is licensed under the [MIT License](LICENSE).
