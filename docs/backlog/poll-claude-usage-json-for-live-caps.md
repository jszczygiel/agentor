---
title: Poll `claude usage --json` for live plan caps
state: available
category: feature
---

Dashboard token panel currently renders `NN%` only when `session_token_budget` / `weekly_token_budget` are hardcoded in `agentor.toml` (see `agentor/dashboard/formatters.py::_fmt_pct_cell` — falls back to raw token total when budget is 0). claude.ai/settings/usage knows the real plan caps server-side; agentor cannot reach them without either a brittle cookie-scrape or a documented API.

When Anthropic ships a scriptable `claude usage --json` (or equivalent `--print` mode for `/usage`), wire it in:

- Poll on the same TTL clock as `_token_windows` (2s cache in `dashboard/formatters.py`).
- Feed `five_h_budget` / `wk_budget` from the parsed JSON instead of `agent_cfg.*_token_budget`.
- Keep the toml knobs as override for operators who want a manual cap below the plan limit.
- Fail closed: if the CLI lacks the subcommand, fall through to the existing toml-budget path silently — no error spam on every tick.

Tiny diff at the call sites (`_fmt_token_compact`, `_fmt_token_row`). Gate on detecting the flag via `claude --help` at daemon startup so we don't `subprocess.run` the CLI 2×/sec when it's unsupported.
