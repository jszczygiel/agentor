# Fix top line hidden on narrow screens — 2026-04-19

## Surprises
- Bug was not in the layout math or tier thresholds — every tier already
  produces rows that fit `w`. Root cause was the missing `KEY_RESIZE`
  branch in `_loop` (and sub-mode loops). After a shrink the curses diff
  engine + terminal wrap combo leaves wide-tier cells from before the
  resize displayed in the narrow viewport, pushing row 0 off-screen.
- Instructed `git rm docs/backlog/fix-top-line-hidden-on-narrow-screens.md`
  could not be executed: the file is absent from this worktree and has no
  git history (same situation as the 2026-04-19 multi-line-feedback run).
  Backlog files on `main` are untracked locally — orchestrator presumably
  owns their removal. Nothing to stage for deletion.
- `stdscr.erase()` alone is not enough after a resize: it blanks the
  internal buffer but the diff against the terminal's cached view can
  come up empty, so `refresh()` sends nothing and the old content
  (already wrapped by the terminal) stays. `stdscr.clear()` forces a
  full repaint on the next refresh, which is what fixes it.

## Gotchas for future runs
- Any curses getch loop that isn't handled by `_render`'s auto-repaint
  (inspect, help, diff scroller, progress overlay) needs its own
  KEY_RESIZE branch — `_handle_resize(stdscr, ch)` centralises it.
- `curses.update_lines_cols()` is not universal (Windows `windows-curses`
  lacks it historically). Guard with `hasattr` before calling.
- `_prompt_multiline` uses `curses.textpad.Textbox.edit` with a
  validator. KEY_RESIZE (410) arrives inside `edit()` and would be
  inserted as a literal char without filtering. Scope kept narrow for
  this task — logged as a follow-up.

## Follow-ups
- `_prompt_multiline` swallows KEY_RESIZE: the edit window doesn't
  reflow when the user resizes while typing. Minimum-viable fix is a
  validator branch that returns 0 on 410; a proper fix recreates
  `edit_win` at the new dims. Added to `docs/IMPROVEMENTS.md`.

## Stop if
- A future attempt to "make the top line always visible" starts
  widening title strings or shrinking tier thresholds — that's the
  wrong layer. The top line is already written correctly at every
  width; what breaks is the refresh path after a resize.
