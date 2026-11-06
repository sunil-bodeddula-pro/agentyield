# AGENTS.md

Instructions for AI coding agents working on AgentYield.

## What this is

AgentYield measures how much code and functionality AI agents ship, how well it holds up, and how fast, for a team or organisation. It reads GitHub, stores filtered facts as JSON files (folder, `gs://`, `s3://`), and will compute metrics and serve a dashboard. Python 3.12+, PyYAML is the only runtime dependency.

## Commands

```bash
.venv/bin/python -m unittest discover -s tests -v       # tests: mock GitHub only, never the network
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy   # CI runs all three
```

Run all of them before saying a change is done, and report the results honestly.

## Layout

| Path | What it is |
| :--- | :--- |
| `src/agentyield/config.py`, `schema.py` | Finding, reading and validating `agentyield.yaml` (line numbers, JSON Schema) |
| `src/agentyield/github.py` | Read-only GitHub REST client on the standard library |
| `src/agentyield/guardrails.py` | The sensitive-data filter |
| `src/agentyield/facts.py` | One PR and its timeline -> the stored record |
| `src/agentyield/sync.py` | Incremental sync and backfill; per-commit line counts for mixed PRs |
| `src/agentyield/attribution.py`, `derive.py` | Agent, assisted or human per PR; the derived layer and admin corrections |
| `src/agentyield/ops.py` | `config diff` / `apply`, status |
| `src/agentyield/store.py` | JSON file store and its folder, GCS and S3 backends; the writer lease |
| `tests/mockgh.py` | Fixture organisation with planted secrets, and a fake GitHub server |

## Rules

- **Privacy first.** Nothing reaches the store without passing `guardrails.Filter`. Never store PR bodies, commit messages, review text, emails or code. A new text field needs a filter call and a planted value in `tests/mockgh.py`.
- **Leak tests must stay green.** `PLANTED` values must never appear in the store or command output. Add to them; never weaken them.
- **Only configured repositories.** Never read a repository the config doesn't list or select.
- **Read-only on GitHub.** No write calls.
- **No new runtime dependencies** beyond PyYAML; cloud clients stay optional extras imported lazily.
- **Tests never use the network or real repositories.** Use the fake GitHub.
- **Typed code.** mypy runs in strict mode.
- **Keep the store format stable** under `v1/`; a breaking change needs `v2/` and a migration.

## Style

Match the surrounding code: short docstrings that say why, comments only where the reason isn't obvious. Line length 120; ruff formats.
