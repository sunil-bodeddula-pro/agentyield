# AgentYield

**How much code and functionality do your AI agents ship, how well does it hold up, and how fast does it reach production?** AgentYield reads your code host, works out which pull requests and lines an agent wrote, and compares them with human work in the same repositories: agent share of code, functionality shipped, effectiveness (merge rate, rework, reverts) and shipping speed.

It runs on a laptop or a remote machine, with no database: data lives in JSON files in a folder or a `gs://` / `s3://` bucket.

> **Status: early development.** Batches 1 and 2 of 9 are done: configuration, GitHub sync, the sensitive-data filter, and attribution (who wrote each PR). Metrics and the dashboard come in batches 3 and 4. See the [roadmap](docs/ROADMAP.md).

## What works today

- `agentyield.yaml`: which repositories to track (a list, or a GitHub custom property), agent rules and guardrails, validated with line numbers.
- `agentyield config validate | show | diff | apply | schema`: config changes are previewed, then applied on purpose.
- `agentyield sync`: fetches only PRs updated since the last sync, with conditional requests (an unchanged repository costs no rate limit).
- `agentyield backfill <owner/repo> --since 2025-01-01`: older history on demand.
- `agentyield status`: what is tracked, how far back, when it last synced.
- One JSON file per PR: times, sizes, commits (with their conventional-commit type), reviews, force-pushes, the back-and-forth before merge, and which agent rules matched.
- `agentyield attribution [--prs]`: every PR labelled **agent**, **assisted**, **human** or **excluded** (dependency bots), with the agent, its share of the changed lines, a confidence and the evidence.
- `agentyield label set owner/repo#42 agent --agent claude-code --note "…"`: an admin correction that always wins and survives re-syncs (`label clear`, `label list`).
- `agentyield recompute`: rebuilds the derived files from stored facts after a rule change, with no GitHub calls.

## How attribution works

| Signal | Example | Confidence |
| :--- | :--- | :--- |
| PR opened by an agent's bot account | `bot_logins` in the config | High |
| Commits with an agent co-author or bot author | `Co-Authored-By: Claude <noreply@anthropic.com>` | High, per commit |
| Agent branch prefix | `claude/`, `copilot/`, `codex/` | Medium |
| Agent label or PR description marker | `ai-agent`, "Generated with Claude Code" | Medium |

A PR is **agent** when an agent opened it, or wrote at least `metrics.agent_share_threshold` (default 50%) of its changed lines; **assisted** when some commits carry an agent but less than the threshold. The share is measured in lines for PRs that mix agent and human commits (generated, vendored and lock files don't count), and by commit count when line counts are missing. `status` shows attribution coverage: the share of PRs with any agent signal.

## Quick start (your own repositories)

```bash
pip install -e .                       # from a checkout; PyPI later
gh auth login                          # or export GITHUB_TOKEN
agentyield init --repo you/your-repo   # writes ~/.config/agentyield/agentyield.yaml
agentyield config apply                # backfills the last 180 days
agentyield sync                        # later: only what changed
agentyield status
```

## Configuration

Lookup order: `--config`, then `AGENTYIELD_CONFIG`, then `./agentyield.yaml`, `~/.config/agentyield/agentyield.yaml` (Windows: `%APPDATA%\agentyield\agentyield.yaml`), `/etc/agentyield/agentyield.yaml`. A config can also be read from `github://owner/repo/path/agentyield.yaml@branch`, `gs://` or `s3://`, so an organisation keeps one config in a private platform repository.

```yaml
version: 1
store: ~/.agentyield/data          # or gs://bucket/path, s3://bucket/path
repositories:
  include:
    - name: acme/checkout-service
      team: payments
    - name: acme/security-patches
      restricted: true             # numbers only: no titles, branch names or labels kept
  select:                          # or: every repository with this custom property set
    org: acme
    property: agentyield-team
  exclude: [acme/legacy]
agents:
  in-house-bot:
    bot_logins: ["acme-agent[bot]"]
    trailers: ["Co-Authored-By: Acme Agent"]
guardrails:
  deny_terms_file: deny-terms.txt  # customer names, codenames; one per line
  restricted_labels: [security, embargo]
```

`agentyield config schema` prints the full JSON Schema for editors.

## Guardrails

Nothing is stored before it passes the filter, and most text is never stored at all.

- **Never stored:** PR descriptions, commit messages, review text, author emails, code, and the names of human co-authors (they are counted, not named). Only tool identities, such as an agent's co-author name or a bot account, are kept.
- **Filtered:** titles, branch names and labels lose secrets (tokens, keys, credentials in URLs, high-entropy strings), personal data (emails, phone numbers, IPs, card numbers, home-folder paths, internal hostnames) and your deny terms.
- **Restricted:** repositories marked `restricted`, and PRs with a restricted label, keep numbers only.
- **Fails closed:** if the filter errors, the text is dropped.
- **Tested:** the test suite plants secrets and personal data in every field of a mock GitHub and fails if any of them reaches the store or the command output.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v   # mock GitHub only; no network
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

Coding agents: read [AGENTS.md](AGENTS.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
