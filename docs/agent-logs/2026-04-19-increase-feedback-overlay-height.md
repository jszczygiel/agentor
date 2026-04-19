# increase-visible-line-count-in-input-mode — 2026-04-19

## Surprises
- Source markdown `docs/backlog/increase-visible-line-count-in-input-mod.md` is untracked in the parent repo working tree — never committed to git. So `git rm` from this worktree is a no-op; the file isn't visible here. Removal of the source item must happen in the parent repo's working tree (outside this worktree's scope).

## Gotchas for future runs
- Backlog items can originate from untracked markdown in the parent repo. When execute-phase instructions mandate a `git rm` of the source file and the file doesn't appear in the worktree, check `git status` of the parent repo — the file may be an untracked scratch file, not a tracked doc. The worktree cannot delete what git never knew about.

## Follow-ups
- None — code change is scoped and the test suite was extended alongside it.
