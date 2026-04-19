---
title: Auto-generate agent-log fold backlog items
category: feature
state: available
---

Every execute run writes a findings file to `docs/agent-logs/<YYYY-MM-DD>-<slug>.md` (per the execute rules in `system_prompt_template`). The stated policy in `CLAUDE.md` is "A human periodically greps `docs/agent-logs/` and folds durable lessons into CLAUDE.md / skills" — but in practice that grep-and-fold is manual, tedious, and gets skipped until the pile of logs is huge. Automate it by having the daemon notice accumulation and drop a fresh backlog item that sends the fold work back through the normal pipeline.

Behaviour: on each daemon tick after `try_fill_pool`, count unfolded log files in `docs/agent-logs/`. When the count crosses a threshold (default 10, `agent.fold_threshold`), and no fold item is already in the queue or working, the daemon creates `docs/backlog/fold-agent-lessons-YYYY-MM-DD.md` with a frontmatter header (`title: Fold agent log lessons (YYYY-MM-DD)`, `category: meta`, `state: available`) and a body that lists the log file paths to consider and the expected output (CLAUDE.md/skills diff + deletion of the consumed logs, all in one commit). `scan_once` then picks it up like any other backlog item; the agent reads the listed logs, clusters recurring Surprises/Gotchas, proposes a CLAUDE.md diff, and deletes the folded-in log files as part of the same commit so the next tick's count resets. The existing review flow gates it — no auto-merge of CLAUDE.md changes without human approval.

Track "already folded" purely by file presence (the agent deletes logs it has consumed) so there's no extra state to keep. Keep a small guard to avoid double-queuing: if an item titled `Fold agent log lessons*` is in any non-terminal status, skip creation this tick.

Verification: unit test the count-and-create logic against a fake filesystem; integration test that runs one fold cycle against a stub runner and asserts the backlog item got queued + picked up + transitioned normally. Docs update in `CLAUDE.md` to replace the "human periodically greps" line with the new auto-queue behaviour.
