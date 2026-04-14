import argparse
import sys
from pathlib import Path

from .config import Config, load
from .models import ItemStatus
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
