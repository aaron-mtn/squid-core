# Squid Core

See [architecture](docs/ARCHITECTURE.md) for ownership/public seams.

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
