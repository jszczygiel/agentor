# Fast-forward user checkout after auto-merge — 2026-04-19

## Surprises
- After `merge_feature_into_base` CAS-advances `refs/heads/<base>`, `HEAD` at `project.root` symbolically follows the ref — so post-CAS `rev-parse HEAD` returns the new sha and `status --porcelain` shows every file in the base diff as a spurious staged change. Any guard based on HEAD-equals-base_sha_before or clean-tree must be evaluated **before** the CAS runs; post-CAS evaluation silently skips the happy path.
- `git merge --ff-only` was the first instinct for the advance but it fires the `post-merge` hook. `git reset --hard <new_sha>` is hook-free and achieves the same index+worktree sync since HEAD symbolically already points to the new ref.

## Gotchas for future runs
- Test harness creates an untracked `.agentor/` state dir at `project.root`. Any test that exercises user-checkout cleanliness must seed a `.gitignore` containing `.agentor/` (and commit it) or every `status --porcelain` check trips. Same trap for the markdown source files referenced by `sources.watch` — commit them in setUp if you need a clean tree.

## Follow-ups
(none)

## Stop if
- The caller proposes to run `advance_user_checkout_allowed` *after* `merge_feature_into_base`. The guards are unreliable post-CAS — see the first Surprise bullet.
