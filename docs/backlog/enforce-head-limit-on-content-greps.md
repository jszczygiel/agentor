---
title: Enforce head_limit on content-mode Greps via pre-tool hook
category: feature
state: available
---

The system prompt advises *"On content Greps always pass `head_limit` (default 50)"* (`agentor/config.py:68-70`) but the rule isn't enforced. A 2026-04-19 review of `/Users/szczygiel/StudioProjects/lancelot/.agentor/transcripts/` (93 logs) found **100 ungated content-mode Greps across 30 runs** — a 5× regression from the prior batch (20 / 12 runs). Worst offenders:

- `cc9b8c6f4f4e.plan` — 12 ungated greps
- `eefb16871c97.plan` — 9
- `07340a3f34a9.exec` — 7
- `a1898a95e9f8.exec` — 6
- `a5a95ebdbb5b.plan` — 6
- `58fb573e6d4f.plan` — 6

Each ungated content-mode Grep can return hundreds of match lines that all land in context. Even with cache hits the output tokens are hot.

Replace advisory text with an **enforced PreToolUse hook** that rejects a `Grep` call when:

1. `output_mode == "content"` (explicit or defaulted to content — check the tool definition for the actual default), AND
2. `head_limit` is absent.

Rejection message: *"Content-mode Grep must pass `head_limit` (default 50) — omit only with `output_mode: count` or `files_with_matches`."* Agent re-issues with a cap.

Scope:

- Do not block `output_mode: count` or `output_mode: files_with_matches` (they're already bounded).
- Do not block when `head_limit` is present at any value — trust agent judgment on the cap.
- Apply equally in plan and execute phases.

Verification:

- Unit test: synthesize tool-input payloads for (content, no head_limit) → reject; (content, head_limit=20) → allow; (files_with_matches, no head_limit) → allow; (count, no head_limit) → allow.
- Shadow-mode run against one of the 100-ungated-grep transcripts and confirm ≥90% would have been rejected and re-issued.

Pair with `enforce-read-offset-limit-on-large-files.md` — both are PreToolUse hooks that upgrade existing advisory prompt rules to enforced rules. Can share hook-plumbing code.

Evidence file: `/Users/szczygiel/StudioProjects/lancelot/tmp/agentor_analysis_2026-04-19.md`.
