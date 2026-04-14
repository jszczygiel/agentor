import argparse
import sys
from pathlib import Path

from .committer import approve_and_commit, reject
from .config import Config, load
from .daemon import Daemon
from .git_ops import diff_vs_base
from .models import ItemStatus
from .runner import StubRunner
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
        'mark_done = true\n'
        '\n'
        '[parsing]\n'
        'mode = "checkbox"\n'
        '\n'
        '[agent]\n'
        'model = "claude-opus-4-6"\n'
        'max_attempts = 3\n'
        'pool_size = 1\n'
        '\n'
        '[git]\n'
        'base_branch = "main"\n'
        'branch_prefix = "agent/"\n'
        'auto_merge = false\n'
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


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load(_find_config(args.config))
    store = _open_store(cfg)
    try:
        daemon = Daemon(
            config=cfg,
            store=store,
            runner_factory=lambda c, s: StubRunner(c, s),
            scan_interval=args.interval,
        )
        print(f"agentor started for {cfg.project_name} "
              f"(pool_size={cfg.agent.pool_size}, interval={args.interval}s)")
        print("ctrl-c to stop")
        stats = daemon.run()
        print(f"stopped. scans={stats.scans} dispatched={stats.dispatched} "
              f"completed={stats.completed} failed={stats.failed}")
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
        fb = input("feedback for agent: ").strip()
        fresh = store.get(item.id)
        reject(store, fresh, fb or "(no feedback)")
        print("rejected.")
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
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("review", help="review items awaiting approval")
    sp.set_defaults(func=cmd_review)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
