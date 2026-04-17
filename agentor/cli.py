import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .committer import approve_and_commit, reject
from .config import Config, load
from .daemon import Daemon
from .dashboard import run_dashboard
from .git_ops import diff_vs_base
from .models import ItemStatus
from .runner import make_runner
from .store import Store
from .watcher import scan_once

DEFAULT_CONFIG_NAMES = ("agentor.toml", ".agentor/config.toml")


def _find_config(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).resolve()
        if not p.exists():
            raise SystemExit(f"config not found: {p}")
        return p
    cwd = Path.cwd()
    for name in DEFAULT_CONFIG_NAMES:
        p = cwd / name
        if p.exists():
            return p
    raise SystemExit(
        f"no config found in {cwd}. Pass --config or create agentor.toml."
    )


def _open_store(cfg: Config) -> Store:
    db_path = cfg.project_root / ".agentor" / "state.db"
    return Store(db_path)


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or ".").resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_path = target / "agentor.toml"
    if cfg_path.exists() and not args.force:
        print(f"exists (use --force to overwrite): {cfg_path}", file=sys.stderr)
        return 1
    cfg_path.write_text(
        '[project]\n'
        f'name = "{target.name}"\n'
        'root = "."\n'
        '\n'
        '[sources]\n'
        'watch = ["docs/backlog.md", "docs/ideas.md"]\n'
        'exclude = []\n'
        '\n'
        '[parsing]\n'
        'mode = "checkbox"\n'
        '\n'
        '[agent]\n'
        'runner = "claude"\n'
        'model = "claude-opus-4-6"\n'
        'max_attempts = 3\n'
        'pool_size = 0\n'
        '\n'
        '[git]\n'
        'base_branch = "main"\n'
        'branch_prefix = "agent/"\n'
        '\n'
        '[review]\n'
        'port = 7777\n'
        'notify = true\n'
    )
    print(f"created {cfg_path}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    try:
        result = scan_once(cfg, store)
    finally:
        store.close()
    print(f"{cfg.project_name}: scanned {result.scanned_files} files, "
          f"{result.new_items} new items")
    for path, reason in result.skipped_files:
        print(f"  skipped {path}: {reason}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    try:
        print(f"project: {cfg.project_name}  root: {cfg.project_root}")
        print(f"pool_size: {cfg.agent.pool_size}  mode: {cfg.parsing.mode}")
        print()
        totals: dict[str, int] = {}
        for status in ItemStatus:
            totals[status.value] = store.count_by_status(status)
        print("counts:")
        for k, v in totals.items():
            if v:
                print(f"  {k:<18} {v}")

        if args.list:
            print("\nitems:")
            for status in ItemStatus:
                rows = store.list_by_status(status)
                if not rows:
                    continue
                print(f"  [{status.value}]")
                for r in rows:
                    print(f"    {r.id}  {r.title}  ({r.source_file})")
    finally:
        store.close()
    return 0


REPL_HELP = """commands:
  s, status        — counts + pool state
  l, list          — list items grouped by status
  r, review        — review items awaiting approval
  log              — tail recent daemon log lines
  h, help, ?       — this help
  q, quit, exit    — stop daemon and exit
"""


def _make_daemon_logger(log_path: Path, ring: deque, to_stdout: bool) -> Callable[[str], None]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", buffering=1)

    def log(msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        log_file.write(line + "\n")
        ring.append(line)
        if to_stdout:
            print(f"[daemon] {msg}", flush=True)

    return log


def _print_daemon_status(cfg: Config, store: Store, daemon: Daemon) -> None:
    print(f"project: {cfg.project_name}  pool_size: {cfg.agent.pool_size}  "
          f"interval: {daemon.scan_interval}s  workers: {len(daemon.workers)}")
    print(f"stats: scans={daemon.stats.scans} dispatched={daemon.stats.dispatched} "
          f"completed={daemon.stats.completed} failed={daemon.stats.failed}")
    for status in ItemStatus:
        n = store.count_by_status(status)
        if n:
            print(f"  {status.value:<18} {n}")


def _print_list(store: Store) -> None:
    for status in ItemStatus:
        rows = store.list_by_status(status)
        if not rows:
            continue
        print(f"[{status.value}]")
        for r in rows:
            print(f"  {r.id}  {r.title}  ({r.source_file})")


def _repl(cfg: Config, store: Store, daemon: Daemon, log_ring: deque) -> None:
    print(REPL_HELP)
    while True:
        try:
            line = input("agentor> ").strip().lower()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print()
            return

        if line in ("q", "quit", "exit"):
            return
        if line in ("", "h", "help", "?"):
            print(REPL_HELP)
        elif line in ("s", "status"):
            _print_daemon_status(cfg, store, daemon)
        elif line in ("l", "list"):
            _print_list(store)
        elif line in ("r", "review"):
            items = store.list_by_status(ItemStatus.AWAITING_REVIEW)
            if not items:
                print("no items awaiting review.")
            else:
                print(f"{len(items)} item(s) awaiting review.\n")
                for item in items:
                    _review_one(cfg, store, item)
        elif line == "log":
            if not log_ring:
                print("(no log lines yet)")
            else:
                for ln in list(log_ring):
                    print(ln)
        else:
            print(f"unknown command: {line!r}. type 'help'.")


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    log_ring: deque = deque(maxlen=200)
    log_path = cfg.project_root / ".agentor" / "agentor.log"

    ui = args.ui
    logger = _make_daemon_logger(log_path, log_ring, to_stdout=(ui == "repl"))

    daemon = Daemon(
        config=cfg,
        store=store,
        runner_factory=make_runner,
        scan_interval=args.interval,
        log=logger,
        install_signals=False,
    )
    t = threading.Thread(target=daemon.run, name="agentor-daemon", daemon=True)

    if ui == "repl":
        print(f"agentor started for {cfg.project_name} "
              f"(pool_size={cfg.agent.pool_size}, runner={cfg.agent.runner}, "
              f"interval={args.interval}s)")
        print(f"log: {log_path}")
    t.start()
    try:
        if ui == "dashboard":
            try:
                run_dashboard(cfg, store, daemon, log_ring)
            except Exception as e:
                # curses can't init (no TTY, weird terminfo, etc.) — fall back
                # to the REPL with a one-line note.
                print(f"dashboard unavailable ({e}); falling back to REPL.")
                # daemon's logger was muted for dashboard mode — re-enable
                # stdout so the REPL user sees activity.
                daemon.log = _make_daemon_logger(log_path, log_ring, to_stdout=True)
                _repl(cfg, store, daemon, log_ring)
        else:
            _repl(cfg, store, daemon, log_ring)
    finally:
        print("stopping daemon...")
        daemon.stop_event.set()
        t.join(timeout=60)
        store.close()
        s = daemon.stats
        print(f"stopped. scans={s.scans} dispatched={s.dispatched} "
              f"completed={s.completed} failed={s.failed}")
    return 0


def cmd_errors(args: argparse.Namespace) -> int:
    """List items that currently carry a last_error. Same data source as
    the dashboard errors filter and the `!` marker, but for non-TTY /
    scripting use."""
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    try:
        err_ids = store.ids_with_errors()
        if not err_ids:
            print("no items with errors.")
            return 0
        # Use a stable order: newest updated first.
        rows: list = []
        for id_ in err_ids:
            item = store.get(id_)
            if item is not None:
                rows.append(item)
        rows.sort(key=lambda r: r.updated_at, reverse=True)
        for r in rows:
            err_head = (r.last_error or "").splitlines()[0][:120]
            print(f"{r.id}  {r.status.value:<18} attempts={r.attempts}  "
                  f"{r.title[:50]}")
            print(f"  → {err_head}")
    finally:
        store.close()
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    """Revert an item to its previous settled state. Looks at the
    transitions log to find what the item was doing before the most recent
    failure cascade (e.g. AWAITING_PLAN_REVIEW before a slot-broken bounce
    loop ended in REJECTED). Resets attempts/last_error/worktree/branch/
    session_id so the next dispatch starts fresh."""
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    try:
        item = store.get(args.item_id)
        if item is None:
            print(f"no such item: {args.item_id}", file=sys.stderr)
            return 1
        prev = store.previous_settled_status(args.item_id)
        if prev is None:
            print(f"no prior settled state found for {args.item_id}",
                  file=sys.stderr)
            return 1
        print(f"item:    {item.id}  {item.title!r}")
        print(f"current: {item.status.value}  attempts={item.attempts}")
        print(f"target:  {prev.value}")
        if not args.yes:
            answer = input("revert? [y/N] ").strip().lower()
            if answer != "y":
                print("aborted.")
                return 0
        store.transition(
            item.id, prev,
            worktree_path=None, branch=None, session_id=None,
            attempts=0, last_error=None, feedback=None,
            note=f"manual revert from {item.status.value}",
        )
        print(f"reverted {item.id} → {prev.value}")
    finally:
        store.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    try:
        items = store.list_by_status(ItemStatus.AWAITING_REVIEW)
        if not items:
            print("no items awaiting review.")
            return 0
        print(f"{len(items)} item(s) awaiting review.\n")
        for item in items:
            _review_one(cfg, store, item)
    finally:
        store.close()
    return 0


def _review_one(cfg: Config, store: Store, item) -> None:
    print("=" * 72)
    print(f"id:     {item.id}")
    print(f"title:  {item.title}")
    print(f"source: {item.source_file}:{item.source_line}")
    print(f"branch: {item.branch}")
    print(f"wt:     {item.worktree_path}")
    if item.result_json:
        import json as _json
        res = _json.loads(item.result_json)
        print(f"summary: {res.get('summary')}")
        print(f"files:   {res.get('files_changed')}")
    print()
    diff = diff_vs_base(Path(item.worktree_path), cfg.git.base_branch)
    print(diff or "(empty diff)")
    print()
    choice = input("[a]pprove / [r]eject / [s]kip ? ").strip().lower()
    if choice == "a":
        msg = input("commit message (blank = default): ").strip()
        if not msg:
            msg = f"{item.title}\n\nAgent work for item {item.id}."
        sha = approve_and_commit(cfg, store, item, msg)
        print(f"committed {sha[:8]} on {item.branch}")
    elif choice == "r":
        fb = input("feedback for agent (empty = terminal reject): ").strip()
        fresh = store.get(item.id)
        assert fresh is not None
        if fb:
            from .committer import reject_and_retry
            reject_and_retry(store, fresh, fb)
            print("re-queued for retry with feedback.")
        else:
            reject(store, fresh, "(no feedback)")
            print("rejected (terminal).")
    else:
        print("skipped.")
    print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentor", description="Agent work orchestrator")
    p.add_argument("--config", "-c", help="path to agentor.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="scaffold an agentor.toml in a project dir")
    sp.add_argument("path", nargs="?", help="target directory (default: cwd)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("scan", help="scan watched files, enqueue new items")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("status", help="show queue status")
    sp.add_argument("--list", "-l", action="store_true", help="list items")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("start", help="run daemon loop")
    sp.add_argument("--interval", type=float, default=5.0,
                    help="scan interval in seconds (default 5)")
    sp.add_argument("--ui", choices=["dashboard", "repl"], default="dashboard",
                    help="UI mode (default: dashboard; falls back to repl if "
                         "curses can't init)")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("review", help="review items awaiting approval")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("errors",
                        help="list items with a last_error set")
    sp.set_defaults(func=cmd_errors)

    sp = sub.add_parser("revert",
                        help="revert an item to its previous settled state")
    sp.add_argument("item_id", help="item id (or unique prefix shown in dashboard)")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip confirmation prompt")
    sp.set_defaults(func=cmd_revert)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
