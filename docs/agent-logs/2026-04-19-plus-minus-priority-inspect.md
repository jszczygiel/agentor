# +/- priority keys in inspect view — 2026-04-19

## Gotchas for future runs
- Main-dashboard loop already binds `+`/`-` globally to agent pool size. Inspect view has its own input loop, so adding `+`/`-` there is conflict-free — but do not extend the top-level help "global actions" block with `+`/`-` for priority; keep the inspect-specific mnemonics under the "inspect actions" block to avoid implying they work from the main table.
- `+` / `-` are non-alpha so the `chr(ch).lower()` dispatch below the priority branch doesn't swallow them, but the match must still live *above* that line alongside `ord("P")` / `ord("O")` to keep identical semantics (no `_inspect_dispatch` routing, no status change).
