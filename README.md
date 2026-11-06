# AgentYield

**How much code and functionality do your AI agents ship, how well does it hold up, and how fast does it reach production?** AgentYield reads your code host, works out which pull requests and lines an agent wrote, and compares them with human work in the same repositories: agent share of code, functionality shipped, effectiveness (merge rate, rework, reverts) and shipping speed.

**It works with any agent.** AgentYield never talks to an agent or reads anyone's machine; it reads GitHub. Agent work is recognised from what agents leave there: bot accounts, commit co-author lines, branch prefixes, labels and PR markers. Rules for Claude Code, Copilot and Codex are built in, and any other agent (Cursor, Devin, Gemini, Amazon Q, Jules, your in-house bots) is one entry under `agents:`.

It runs on a laptop or a remote machine, with no database: data lives in JSON files in a folder or a `gs://` / `s3://` bucket.

> **Status: early development.** Batches 1 to 8 of 9 are done: configuration, GitHub sync, the sensitive-data filter, attribution, the metrics engine, the dashboard with GitHub sign-in, quality signals, the review-comment classifier, features and spend, and the feedback loop with weekly digests. See the [roadmap](docs/ROADMAP.md).

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
- `agentyield metrics [--scope team:payments | repo:acme/app] [--json]`: the numbers below for agent, assisted and human work side by side, a matched comparison, and the last 4 weeks against the 4 before.
- `agentyield suggest`, `agentyield experiments`, `agentyield digest`: rules to add to your agents' instructions, before-and-after comparisons, and a weekly digest (below).

## What reviewers flag

Human review comments (not bots', not the PR author's replies) are sorted into categories: correctness, scope, design, error handling, security, performance, concurrency, tests, docs, style, build and CI, conventions, steering (instructions to an agent, such as "@copilot fix the tests") and other. The dashboard compares the share of each for agent and human PRs, so you can see what reviewers keep catching in agent work.

| `review.classifier` | What it uses | Where comment text goes |
| :--- | :--- | :--- |
| `rules` (default) | Built-in keyword rules | Nowhere: classified in place |
| `openai-compatible` | Any chat-completions endpoint: `base_url` and `model`, plus `api_key_env` if needed. Works with local servers (Ollama, vLLM, LM Studio) and hosted providers | To that endpoint, after filtering |
| `anthropic` | Claude through Anthropic's SDK (`pip install 'agentyield[anthropic]'`) | To Anthropic under your account, after filtering |

```yaml
review:
  classifier: openai-compatible
  base_url: http://localhost:11434/v1   # e.g. Ollama's OpenAI-compatible API
  model: qwen2.5:14b
```

Comment text is filtered before any classifier sees it and dropped afterwards; only the category is stored (and a short filtered excerpt if `guardrails.comment_excerpts` is on). Comments already classified aren't sent again. If the classifier is down, sync carries on and the comments are classified next time.

**Accuracy:** `agentyield classifier eval` measures exact-match accuracy, per-category recall and precision, and the most common mix-ups, and the admin view shows the result. The built-in rules score 76% (85 of 112) on the bundled sample, but that sample was written by the same authors as the rules, so treat it as a smoke test. For a number that holds for your team, label 100 or so of your own comments as JSONL (`{"text": "...", "label": "tests"}`) and run `agentyield classifier eval --sample yours.jsonl`.

## Features from Jira and Linear

PRs are linked to tracker issues by their keys (`CHK-12`, `ENG-7`) in the title, branch name or description, for the project or team prefixes you list. Each feature (a Jira epic, or a Linear project) then shows its PRs, the agent's share of its merged code, whether agents or people led it, whether it's done, and its lead time from first commit to the last merge or the tracker's done date.

```yaml
functionality:
  jira:
    base_url: https://acme.atlassian.net    # reads JIRA_EMAIL and JIRA_API_TOKEN
    projects: [CHK, PAY]
    feature: epic                           # or: parent
  linear:
    teams: [ENG]                            # reads LINEAR_API_KEY
    feature: project                        # or: parent
```

Only each issue's key, type, state, dates, parent or project, and filtered title and labels are stored; descriptions and comments are never fetched. Done issues are cached; open ones are re-read at most every six hours. Static copies carry feature totals but no titles.

## Spend (optional)

Agent spend per merged PR and per feature, from any tool:

```yaml
cost:
  sources:
    - type: file                 # any vendor: CSV or JSONL with date, person, amount_usd[, tool]
      path: spend.csv
    - type: claude_code          # one built-in connector: Claude Code Analytics Admin API
      admin_key_env: ANTHROPIC_ADMIN_KEY
  people:                        # map emails in the sources to GitHub logins
    - person: jane@acme.com
      login: jane-gh
```

A person's spend on a day is shared equally across the agent and assisted PRs they authored that had commits that day (or were open that day); anything that can't be matched is reported as unallocated rather than guessed. Daily spend per person is never stored: only the per-PR allocations and unallocated daily totals are, and the people map (with its emails) stays in the live config, never in the store. Tools without a per-use cost, such as seat-based subscriptions, can be included by exporting their cost per person per day to the file.

## Feedback loop and digests

**Suggested agent rules.** When reviewers keep flagging one category on agent PRs clearly more than on human PRs (by default at least 3 comments in the last 8 weeks, and at least 1.5 times the human share), or agent PRs keep failing CI on the first push, or reviewers keep steering agents in comments, AgentYield drafts a rule for your agents' instructions file: `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `copilot-instructions.md` or whatever your agents read.

```bash
agentyield suggest --scope team:payments      # Markdown, ready to review and paste; --json for the numbers
```

The drafts come from templates, not a model, with the evidence behind each one. They are suggestions for a person to review: AgentYield never writes to a repository.

**Before and after.** Record a change, such as a new agent, model or instructions file, and AgentYield compares agent work in the weeks before and after it, with sample sizes:

```yaml
experiments:
  - name: Tests-first rule in AGENTS.md
    date: 2026-10-01
    scope: team:payments           # org (default), team:<name> or repo:<owner/name>
    note: Added after reviewers kept asking for tests
feedback:
  weeks: 8                         # review comments behind each suggestion
  min_comments: 3
  experiment_weeks: 4              # weeks compared on each side of a change
```

```bash
agentyield experiments
```

Other things change at the same time, so read these as signals, not proof. Both appear on the dashboard as **Suggested agent rules** and **Before and after changes**; a change recorded for a repository shows only to people who can read it.

**Weekly digest.** A short summary per scope: agent share of merged code, PRs merged, time to merge and merge rate against the 4 weeks before, first-push CI, features done, what reviewers flag most, and the top suggested rule. It carries totals only, like a static copy: no PR or feature titles and no comment text.

```yaml
outputs:
  digest:
    scopes: [org, team:payments]
    webhook_env: AGENTYIELD_WEBHOOK    # a Slack-compatible incoming webhook (Slack, Mattermost, Rocket.Chat)
    email:                             # and/or plain SMTP
      host: smtp.acme.com
      port: 587
      from: agentyield@acme.com
      to: [eng-leads@acme.com]
      username_env: SMTP_USER
      password_env: SMTP_PASSWORD
    schedule: "Mon 09:00"              # in metrics.working_hours' time zone
```

```bash
agentyield digest                  # print it
agentyield digest --send           # send it now
```

`agentyield serve` sends it on schedule, once a week, after a sync. Recipients are read from the live config file, not the copy kept in the store.

## Dashboard

```bash
agentyield serve                 # live dashboard on http://127.0.0.1:8780, syncing every 15 minutes
agentyield build --out site      # static copy: totals only, no PR list or titles
```

KPI cards (agent share of merged code, PRs merged by who wrote them, time to merge, merge rate, back-and-forth, functionality shipped), weekly trend charts, effectiveness and back-and-forth by group, the matched comparison, every metric in one table, a paginated PR list, and suggested agent rules, before-and-after changes, and an admin view of sync state and attribution coverage. Choose the organisation, a team or a repository at the top. Dark and light themes; works on a phone.

### Who sees what

| Viewer | Sees |
| :--- | :--- |
| Anyone signed in | Organisation and team totals |
| People who can read a repository on GitHub | That repository's own view and its PRs in the list, re-checked every 15 minutes with their own GitHub token |
| Logins in `outputs.dashboard.admins` | The admin view |
| A static copy's readers | Totals only: a static file can't check who is reading it |

### Sign in with GitHub

For a shared server, create a GitHub App (read-only: pull requests, contents, issues, metadata), install it on your organisation, and configure:

```yaml
github:
  auth:                                  # the server reads GitHub as the App
    app_id_env: AGENTYIELD_APP_ID
    private_key_env: AGENTYIELD_APP_KEY  # the PEM itself, or a path to it
    installation_id_env: AGENTYIELD_APP_INSTALLATION
outputs:
  dashboard:
    host: 0.0.0.0
    base_url: https://agentyield.example.com
    admins: [platform-lead]
    auth:
      mode: github                       # people sign in with the same App
      # reads AGENTYIELD_CLIENT_ID, AGENTYIELD_CLIENT_SECRET and AGENTYIELD_SESSION_SECRET (32+ characters)
```

Set the App's callback URL to `<base_url>/auth/callback`. Without sign-in, `serve` listens on 127.0.0.1 only and refuses another address.

## Metrics

| Group | Metrics |
| :--- | :--- |
| Code | Agent share of merged code (lines, without generated, vendored and lock files); lines and PRs merged |
| Functionality | Issues closed (`Closes #12`), features shipped (closed issues or PRs with a feature label), user-facing changes (`feat`/`fix`), releases and time to release |
| Speed | Working hours to first review and to merge (median and 85th percentile) |
| Back-and-forth | Review round trips, commits after the first review, force-pushes |
| Effectiveness | Merge rate, reverts within 30 days, follow-up fixes within 14 days (a later `fix` PR touching the same files), human review coverage, self-merges |
| Quality | Passed CI on the first push, fix-up commits after a CI failure, coverage on changed lines, new code-scanning findings per PR, code still alive after 30+ days |
| Delivery | Hours from first commit to production, production deployments and the failed-deployment rate |
| Trend | Rolling 4-week figures for the last 12 weeks; last 4 weeks against the 4 before |

- **Working hours:** times between events count only `metrics.working_hours` (default Mon to Fri, 09:00 to 18:00 UTC), so weekends don't inflate review times.
- **Small samples:** a rate or median from fewer than `metrics.min_sample` PRs (default 10) is shown as `-`, not as a number.
- **Matched comparison:** agents tend to take smaller tasks, so agent PRs are also compared with human PRs of the same repository and size band, weighted by where the agent work is.
- **Quality sources:** CI from check runs and commit statuses on every commit; coverage from a status or check you name; findings from GitHub code scanning (repositories without it are skipped); deployments to `metrics.production_environments` (default `production`, `prod`).
- **Code survival (opt-in):** `metrics.code_survival: true` measures, with `git blame` on a blob-less local mirror in `cache_dir`, how many lines a merged PR added are still there 30+ days later. Needs `git`. Paths and code stay in the local mirror; only counts are stored. Rebase merges can't be traced and show as unknown.
- **Approximations, stated plainly:** follow-up fixes are matched by file, not line; a PR's release is the first release published after it merged.

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
metrics:
  coverage:
    status_context: codecov/patch  # where coverage on changed lines is reported
  production_environments: [production]
  code_survival: true              # git blame on a local mirror (needs git)
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
