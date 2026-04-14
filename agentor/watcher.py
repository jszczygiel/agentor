from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .extract import extract_items
from .store import Store


@dataclass
class ScanResult:
    scanned_files: int
    new_items: int
    skipped_files: list[tuple[str, str]]  # (path, reason)


def resolve_watched_files(config: Config) -> list[Path]:
    """Expand glob patterns in config.sources.watch against project root.
    Skips anything outside project root or matching sources.exclude."""
    out: list[Path] = []
    seen: set[Path] = set()
    root = config.project_root.resolve()
    excluded: set[Path] = set()
    for pattern in config.sources.exclude:
        for p in root.glob(pattern):
            if p.is_file():
                excluded.add(p.resolve())
    for pattern in config.sources.watch:
        for p in sorted(root.glob(pattern)):
            if not p.is_file():
                continue
            rp = p.resolve()
            if rp in excluded or rp in seen:
                continue
            try:
                rp.relative_to(root)
            except ValueError:
                continue
            seen.add(rp)
            out.append(p)
    return out


def scan_once(config: Config, store: Store) -> ScanResult:
    """Single pass: read every watched file, extract items, upsert new ones.
    Existing items (matched by id) are left alone — they keep their current status."""
    scanned = 0
    new_count = 0
    skipped: list[tuple[str, str]] = []
    for f in resolve_watched_files(config):
        try:
            items = extract_items(f, config.parsing.mode, config.project_root)
        except (OSError, ValueError) as e:
            skipped.append((str(f), str(e)))
            continue
        scanned += 1
        for item in items:
            if store.upsert_discovered(item):
                new_count += 1
    return ScanResult(scanned_files=scanned, new_items=new_count, skipped_files=skipped)
