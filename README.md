# Squid Core

See [architecture](docs/ARCHITECTURE.md) for ownership/public seams.

A custom backend needs only one method; subclassing `BaseProvider` is optional:

```python
from squid_core.model_adapter import ModelAdapter
from squid_core.model_response import ModelResponse


class MyProvider:
    def create_response(self, model, messages, tools) -> ModelResponse:
        result = my_sdk.chat(model=model, messages=messages, tools=tools)
        return ModelResponse(content=result.text)


adapter = ModelAdapter(MyProvider(), model_name="my-model")
reply = adapter.respond([{"role": "user", "content": "Hello"}], [])
```

`my_sdk` represents your configured backend client; translate its native output
into Core contracts inside the provider. To run the agent loop, pass the same
object as `provider=MyProvider()` with `model_name="my-model"` to
`build_agent_run` or `build_agent_loop`. Supply runtime settings and storage
paths/collaborators as required by those helpers. Streaming, images, readiness,
and discovery are optional; richer providers use the same contract. See the
[architecture](docs/ARCHITECTURE.md) for capability details.

These packages are unpublished. Keep code-squid, squid-core and squid-providers
as siblings. From code-squid, create an environment and run:

```powershell
python -m pip install -r requirements.txt pytest
python -m pytest tests
python -m pytest ../squid-core/tests
python -m pytest ../squid-providers/tests
```

Run suites separately to avoid duplicate module names. For this repository
alone, install requirements-dev.txt to obtain its local editable dependencies
and pytest. Build with `python -m pip wheel --no-deps .`; install required local
wheels together. No inference is needed for tests.
