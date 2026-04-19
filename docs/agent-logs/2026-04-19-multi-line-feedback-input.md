# multi-line feedback input — 2026-04-19

## Surprises
- Instruction to `git rm docs/backlog/multi-line-feedback-input.md` could not be followed: file is not present in this worktree and has no git history. Presumed consumed earlier by orchestrator — nothing to stage for deletion.
- Backlog description's line numbers were stale (`modes.py:85-86`, `:536`, `:705`); real feedback prompts live at `modes.py:276`, `:286`, `:311`. Confirms the project-rule gotcha: grep symbols, not line numbers.

## Gotchas for future runs
- `curses.textpad.Textbox` defaults `stripspaces=True`, which silently eats trailing whitespace on every line — including blank paragraph separators in a multi-line editor. Set `stripspaces = False` after construction when the caller cares about vertical structure.
- Ctrl-C (keycode 3) must be remapped inside the Textbox validator; raising `KeyboardInterrupt` through `box.edit` would leak out of the dashboard loop.

## Follow-ups
- `modes.py:784` bug/idea note prompt is also long-form and currently single-line. Left on `_prompt_text` to stay in scope; logged to `docs/IMPROVEMENTS.md`.
