# Squid Core

Squid Core contains the building blocks needed to build custom AI agents.

It handles the back-and-forth between the model and tools, keeps track of the
session, and provides the systems used to customize how an agent behaves.

While building Code Squid, I wanted its core systems to be reusable for other
kinds of agents instead of being tied only to coding. I built them with that
separation in mind, and Squid Core is the result of pulling those systems out
into their own package.

Squid Core has an extension system called Mods, which can bundle Skills, tools,
and hooks together.

Squid Core does not depend on a particular model provider or interface. You can
connect your own provider, choose the tools and behavior you want, and build a
different kind of agent on top of it.

The base package has no required third-party Python dependencies.

Code Squid is built on Squid Core. `squid-providers` contains the first-party
model integrations used by Code Squid, but Squid Core itself can be used without
either one.

## Installation

Squid Core requires Python 3.11 or newer.

Install from PyPI:

```bash
pip install squid-core
```

For optional document and image reading support:

```bash
pip install "squid-core[documents]"
```

The `documents` extra adds support for reading EPUB, ODS, XLSX, DOCX, PDF, PNG, JPG/JPEG, and WebP files.

## Quick start

This example runs a small tool-using agent against a local model in LM Studio.

Start LM Studio's local server with a tool-calling model loaded, then save the
following as `hello_squid.py`:

```python
import json
import os
import urllib.request
from pathlib import Path

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
    def create_response(self, model, messages, tools) -> ModelResponse:
        # Send Squid Core's conversation and tool definitions to LM Studio.
        request = urllib.request.Request(
            BASE_URL + "/chat/completions",
            data=json.dumps({
                "model": model,
                "messages": messages,
                "tools": tools,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        # Pull the assistant message out of the OpenAI-compatible response.
        with urllib.request.urlopen(request, timeout=120) as response:
            message = json.load(response)["choices"][0]["message"]

        # Convert LM Studio's OpenAI-compatible response into Squid Core's format.
        tool_calls = [
            ToolCall(
                call["id"],
                call["function"]["name"],
                call["function"]["arguments"],
            )
            for call in message.get("tool_calls") or []
        ]

        return ModelResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
        )


# Use the folder containing this script as the agent's workspace.
workspace = Path(__file__).resolve().parent

# Tell Squid Core what OS, shell, and path conventions it is working with.
platform_context = detect_platform_context()

# Session stores the conversation and tool results for this run.
session = Session(workspace)

task = input("You: ")

# Define the agent's task and which tools it is allowed to use.
agent = Agent(
    task,
    tools=TOOLS,
    platform_context=platform_context,
    workspace=str(workspace),
)

# ToolExecutor is the part that actually runs tool calls.
tool_executor = ToolExecutor(
    workspace,
    session,
    tools=TOOLS,
    platform_context=platform_context,
)

# AgentLoop handles the back-and-forth between the model and tools.
loop = AgentLoop(
    session=session,
    agent=agent,
    model_adapter=ModelAdapter(ChatProvider(), MODEL),
    tool_executor=tool_executor,
    tool_results=ToolResults(),
    display=NullDisplay(),
)

result = loop.run()

if result.status != "complete":
    raise RuntimeError(result.reason or result.status)

print(session.messages[-1]["content"])
```

Set the `SQUID_MODEL` environment variable to the model you have loaded in LM Studio,
then run the script. This example uses PowerShell:

```powershell
$env:SQUID_MODEL = "your-model-id"
python hello_squid.py
```

The directory containing `hello_squid.py` is used as the workspace.

Once it starts, you can give it a task like:

```text
Read hello_squid.py and tell me what this thing is doing.
```

The agent has access to Squid Core's four built-in tools: `read_file`, `write_file`,
`apply_patch`, and `run_command`.

## Bring your own provider

Squid Core does not care which model provider you use.

For a basic provider, you only need one method:

```python
create_response(model, messages, tools) -> ModelResponse
```

Squid Core gives your provider the model name, the conversation, and the tools
the agent can use. Your provider sends that to the model and gives Squid Core
back a `ModelResponse`.

If the model wants to call a tool, return those calls as `ToolCall` objects.
Squid Core handles parsing the arguments and running the tool.

Your provider can inherit from `BaseProvider`, but it does not need to.

Things like streaming, model discovery, readiness checks, and vision are optional.
You can add them if your provider supports them.

Code Squid's built-in provider integrations live in `squid-providers`.

## Tools and workspaces

Squid Core comes with four built-in tools:

| Tool | Purpose |
| --- | --- |
| `read_file` | Reads text, code, and supported documents and images. |
| `write_file` | Creates or replaces files. |
| `apply_patch` | Makes targeted edits to existing files. |
| `run_command` | Runs local shell commands. |

These are available through `squid_core.tools.TOOLS`. You can use the built-in
set, replace it with your own tools, or add custom tools alongside them.

A workspace is just the folder the agent is working in. It can be any folder.

`run_command` starts in the workspace, and the file tools use it as the main
working area for file operations.

The workspace is not a security sandbox. Tool access and file access are controlled
by the tools and settings you give the agent.

## Skills and Mods

Skills change how an agent behaves for a particular kind of task.

A Skill can replace specific parts of the system prompt and change which tools
the agent has access to. It can remove tools that are not useful for the task or
make additional tools available.

Mods package those pieces together. A Mod can include a Skill, custom tools, and
hooks that run at different points while the agent is working.

Mods can also define an accent color, which can be used for things like avatar
colors and other Mod-specific visuals.

## Basic Squid Core structure

- `Agent` builds the prompt, tracks the task, and controls which tools the model
  is allowed to use.
- `AgentLoop` runs the back-and-forth between the model and tools until the task
  finishes.
- `ModelAdapter` sends requests through the selected provider and turns provider
  responses into the format Squid Core expects.
- `Session` stores the conversation history and tool results for the run.
- `ToolExecutor` receives tool calls and runs the matching tool.
- `ToolResults` formats tool output so it can be added back into the conversation.
- Hooks run code at specific points while the agent is working.
- Skills change parts of the system prompt and the tools available to the agent.
- Mods package Skills, custom tools, hooks, and Mod metadata together.
- Compaction keeps long sessions manageable by reducing older context.
- Session storage saves and reloads sessions.
- Approvals control when tool actions need permission before they run.
- Events report what is happening during a run.

## Building on Squid Core

Squid Core is meant to be used as the base for other agents, not just Code Squid.

You can bring your own provider, tools, Skills, Mods, hooks, prompts, and storage
while keeping the same agent loop and session system underneath.

For a deeper look at the architecture and the parts meant to be reused, see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Testing

Run the test suite with:

```bash
python -m pytest tests
```

## License

Squid Core is licensed under the [MIT License](LICENSE).