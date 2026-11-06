# Roadmap

| # | Batch | What it delivers | Status |
| :--- | :--- | :--- | :--- |
| 1 | Config, sync and guardrails | `agentyield.yaml` with validation; `config validate/show/diff/apply`; incremental GitHub sync and backfill with PR timelines; JSON file store (folder, gs://, s3://); the sensitive-data filter; mock GitHub with planted secrets | Done |
| 2 | Attribution | Agent, assisted or human per PR, with a confidence per signal; agent share by lines; admin corrections; `recompute` | Done |
| 3 | Metrics engine | Agent share of code; functionality shipped; speed, review and reliability metrics; matched agent-against-human comparison; rolling trends | Next |
| 4 | Dashboard | `agentyield serve` and `build`: organisation, team, repository and admin views | Planned |
| 5 | Quality signals | First-run CI pass, coverage on changed lines, code-scanning findings | Planned |
| 6 | Review-comment classifier | What reviewers keep catching that agents miss | Planned |
| 7 | Issue trackers and cost | Jira and Linear; optional vendor analytics for cost | Planned |
| 8 | Feedback loop and digests | Suggested `AGENTS.md` rules, before-and-after comparisons, weekly digests | Planned |
| 9 | Packaging and release | PyPI, Docker image, publishing to private GitHub Pages or a bucket | Planned |
