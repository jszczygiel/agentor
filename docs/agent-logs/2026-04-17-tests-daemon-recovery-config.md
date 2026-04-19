# Tests for daemon, recovery, config — 2026-04-17

## Gotchas for future runs
- `git_ops.run(cwd=...)` uses `subprocess.run(cwd=...)` which raises `FileNotFoundError` at the Python level if `cwd` doesn't exist — `check=False` only suppresses non-zero exit codes. Tests passing an arbitrary temp dir as `project_root` work because the dir exists; but passing a fake path directly would crash before `check=False` matters.
- Recovery's `worktree_remove` is called even when `wt.exists()` is false (the function shells out anyway). For tests without a real git repo, patch `agentor.recovery.git_ops.worktree_remove` rather than relying on `check=False` swallowing the real subprocess.
- `Daemon._run_worker` runs on a worker thread started via `threading.Thread.start()`. Tests need to `join()` all `daemon.workers` before asserting on `system_alert` / `stats` — the method returns synchronously from `_dispatch_one` before the behavior fires.

## Stop if
- `_is_auto_recoverable_error` pattern list drifts from `recovery.py` constant — update the test alongside, not after.
