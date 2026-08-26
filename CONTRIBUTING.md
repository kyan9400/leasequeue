# Contributing

LeaseQueue accepts focused fixes and improvements that preserve its small operational surface.

## Development

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
```

Open a pull request with a concise problem statement, the chosen behavior, and tests for state-transition changes. Update `CHANGELOG.md` when behavior visible to operators or workers changes.

## Design rules

- A job transition and its event record must commit in one transaction.
- Lease-bearing commands must reject missing, expired, or mismatched tokens.
- New request bodies must use strict Pydantic models and bounded fields.
- Never expose stored idempotency keys or active lease tokens from read endpoints.
