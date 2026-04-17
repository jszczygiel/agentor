---
title: Add tests for daemon, recovery, and config modules
state: available
category: test
---

No coverage today for `agentor/daemon.py`, `agentor/recovery.py`, or
`agentor/config.py`. Recovery is crash-path code and the highest risk —
start there. Cover: WORKING+live-session → resumable, WORKING+dead-session
→ revert to prior settled status, benign stale `last_error` cleared. Then
daemon dispatch (pool-size cap, infra-failure sticky alert) and config
loading (unknown-key warning, relative vs absolute `project.root`, macOS
`/tmp` → `/private/tmp` resolve).
