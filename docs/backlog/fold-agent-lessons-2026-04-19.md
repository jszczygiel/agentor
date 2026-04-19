---
title: Fold agent log lessons (2026-04-19)
category: meta
state: available
---

Auto-generated on 2026-04-19. `docs/agent-logs/` has accumulated 55 findings files; fold their durable lessons into CLAUDE.md (or the relevant skill file) and delete the consumed logs so the count resets.

## Logs to consider

- `docs/agent-logs/2026-04-17-audit-enum-drift-merge.md`
- `docs/agent-logs/2026-04-17-audit-enum-drift.md`
- `docs/agent-logs/2026-04-17-auto-resolve-conflicts.md`
- `docs/agent-logs/2026-04-17-ci-ruff-mypy.md`
- `docs/agent-logs/2026-04-17-dashboard-split.md`
- `docs/agent-logs/2026-04-17-deduplicate-transcript-parsing.md`
- `docs/agent-logs/2026-04-17-document-prior-run-gotchas.md`
- `docs/agent-logs/2026-04-17-enter-opens-pickup-review-merge-2.md`
- `docs/agent-logs/2026-04-17-enter-opens-pickup-review-merge.md`
- `docs/agent-logs/2026-04-17-enter-opens-pickup-review.md`
- `docs/agent-logs/2026-04-17-fix-test-config-f401-imports.md`
- `docs/agent-logs/2026-04-17-group-store-methods.md`
- `docs/agent-logs/2026-04-17-prioritize-backlog-items.md`
- `docs/agent-logs/2026-04-17-remove-unpause-footer.md`
- `docs/agent-logs/2026-04-17-rework-conflict-summary.md`
- `docs/agent-logs/2026-04-17-shared-stream-json-subprocess.md`
- `docs/agent-logs/2026-04-17-split-approve-feedback.md`
- `docs/agent-logs/2026-04-17-tests-daemon-recovery-config.md`
- `docs/agent-logs/2026-04-18-approve-cycles-through-queue.md`
- `docs/agent-logs/2026-04-18-dashboard-hang.md`
- `docs/agent-logs/2026-04-18-merge-main-into-remove-pickup-mode.md`
- `docs/agent-logs/2026-04-18-remove-pickup-mode-toggle.md`
- `docs/agent-logs/2026-04-19-auto-generate-agent-log-fold.md`
- `docs/agent-logs/2026-04-19-clarify-dashboard-navigation-after-submit-merge.md`
- `docs/agent-logs/2026-04-19-compact-token-indicator-merge.md`
- `docs/agent-logs/2026-04-19-compact-token-indicator.md`
- `docs/agent-logs/2026-04-19-cumulative-token-usage-panel.md`
- `docs/agent-logs/2026-04-19-dashboard-priority-indicator.md`
- `docs/agent-logs/2026-04-19-delete-in-deferred.md`
- `docs/agent-logs/2026-04-19-dispatch-stagger.md`
- `docs/agent-logs/2026-04-19-drop-doc-name-from-list-response.md`
- `docs/agent-logs/2026-04-19-enable-delete-across-inspect.md`
- `docs/agent-logs/2026-04-19-enforce-read-offset-limit.md`
- `docs/agent-logs/2026-04-19-fast-fail-stale-session.md`
- `docs/agent-logs/2026-04-19-fast-forward-user-checkout-after-merge.md`
- `docs/agent-logs/2026-04-19-feedback-input-noop.md`
- `docs/agent-logs/2026-04-19-feedback-multiline-bug-idea-note.md`
- `docs/agent-logs/2026-04-19-fix-top-line-hidden-narrow.md`
- `docs/agent-logs/2026-04-19-grep-head-limit-hook.md`
- `docs/agent-logs/2026-04-19-increase-feedback-overlay-height.md`
- `docs/agent-logs/2026-04-19-inject-turn-budget-checkpoints.md`
- `docs/agent-logs/2026-04-19-multi-line-feedback-input.md`
- `docs/agent-logs/2026-04-19-plan-review-feedback-delete.md`
- `docs/agent-logs/2026-04-19-preserve-file-context-across-resumed-sessions.md`
- `docs/agent-logs/2026-04-19-priority-keybinding-in-inspect.md`
- `docs/agent-logs/2026-04-19-remove-backlog-status-from-enum.md`
- `docs/agent-logs/2026-04-19-responsive-dashboard.md`
- `docs/agent-logs/2026-04-19-serialize-auto-merge.md`
- `docs/agent-logs/2026-04-19-skip-plan-on-auto-resolve.md`
- `docs/agent-logs/2026-04-19-strip-approve-mode-noise.md`
- `docs/agent-logs/2026-04-19-surface-auto-resolve-chain.md`
- `docs/agent-logs/2026-04-19-surface-checkout-advance-reason.md`
- `docs/agent-logs/2026-04-19-sync-subprocess-curses-audit.md`
- `docs/agent-logs/2026-04-19-transient-retries.md`
- `docs/agent-logs/2026-04-19-unify-inspect-actions.md`

## Expected output (one commit)

- A CLAUDE.md (and/or skills) diff that captures recurring Surprises / Gotchas — cluster, don't copy verbatim.
- `git rm` on every log file listed above that you folded in. Keep anything still too raw to promote, but prefer to fold rather than hoard.
- One commit containing both the docs update and the deletions. The normal review flow gates the merge — do not auto-merge CLAUDE.md changes.
