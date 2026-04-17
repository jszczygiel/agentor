import hashlib
import re
from pathlib import Path

from .models import Item

TAG_RE = re.compile(r"@(\w+):(\S+)")
CHECKBOX_RE = re.compile(r"^(\s*)- \[( |x|X)\] (.+)$")
HEADING_RE = re.compile(r"^(#{1,6}) (.+)$")
FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")


def _item_id(source_file: str, title: str, body: str) -> str:
    h = hashlib.sha1(f"{source_file}\0{title}\0{body}".encode()).hexdigest()
    return h[:12]


def _extract_tags(text: str) -> tuple[str, dict[str, str]]:
    """Strip @key:value tags from text, return (cleaned_text, tags)."""
    tags: dict[str, str] = {}
    def repl(m: re.Match) -> str:
        tags[m.group(1)] = m.group(2)
        return ""
    cleaned = TAG_RE.sub(repl, text).strip()
    return cleaned, tags


def extract_items(source_file: Path, mode: str, project_root: Path) -> list[Item]:
    """Parse a markdown file and return work items.

    checkbox mode: each unchecked "- [ ] title" is an item; indented lines after it
      (until next checkbox or blank-line-then-non-indented) form the body.
    heading mode: each "## title" is an item; content until next heading of same-or-
      higher level is the body.
    """
    text = source_file.read_text()
    rel = str(source_file.resolve().relative_to(project_root.resolve()))

    if mode == "checkbox":
        return _extract_checkbox(text, rel)
    if mode == "heading":
        return _extract_heading(text, rel)
    if mode == "frontmatter":
        return _extract_frontmatter(text, rel)
    raise ValueError(f"unknown parsing mode: {mode}")


def _extract_checkbox(text: str, source_file: str) -> list[Item]:
    lines = text.splitlines()
    items: list[Item] = []
    i = 0
    while i < len(lines):
        m = CHECKBOX_RE.match(lines[i])
        if not m:
            i += 1
            continue
        checked = m.group(2).lower() == "x"
        if checked:
            i += 1
            continue
        indent = len(m.group(1))
        title_raw = m.group(3)
        start_line = i + 1  # 1-indexed

        # gather body: lines indented deeper than the checkbox, until next checkbox
        # at same-or-shallower indent, or a top-level heading
        body_lines: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            nxt_m = CHECKBOX_RE.match(nxt)
            if nxt_m:
                nxt_indent = len(nxt_m.group(1))
                if nxt_indent <= indent:
                    break
            if HEADING_RE.match(nxt):
                break
            body_lines.append(nxt)
            j += 1

        body_raw = "\n".join(body_lines).strip()
        title, title_tags = _extract_tags(title_raw)
        body, body_tags = _extract_tags(body_raw)
        tags = {**body_tags, **title_tags}  # title tags win

        items.append(Item(
            id=_item_id(source_file, title, body),
            title=title,
            body=body,
            source_file=source_file,
            source_line=start_line,
            tags=tags,
        ))
        i = j
    return items


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str, int]:
    """Parse a YAML-ish frontmatter block at the top of the file.
    Only supports flat key: value pairs — no lists/dicts. Returns
    (fields, body, body_start_line). If no frontmatter, returns ({}, text, 1)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 1
    fields: dict[str, str] = {}
    i = 1
    while i < len(lines):
        if lines[i].strip() == "---":
            body_start = i + 1
            body = "\n".join(lines[body_start:])
            return fields, body, body_start + 1
        m = FRONTMATTER_KV_RE.match(lines[i])
        if m:
            key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            fields[key] = val
        i += 1
    # no closing --- — treat as no frontmatter
    return {}, text, 1


def _extract_frontmatter(text: str, source_file: str) -> list[Item]:
    """One file == one item. Title from frontmatter `title:`, or filename fallback.
    Skip unless `state` is absent or equals `available`."""
    fields, body_raw, body_line = _parse_frontmatter(text)
    state = fields.get("state", "available").lower()
    if state != "available":
        return []
    title = fields.get("title") or Path(source_file).stem.replace("-", " ")
    body, body_tags = _extract_tags(body_raw.strip())
    tags = {k: v for k, v in fields.items() if k not in {"title", "state"}}
    tags.update(body_tags)
    return [Item(
        id=_item_id(source_file, title, body),
        title=title,
        body=body,
        source_file=source_file,
        source_line=body_line,
        tags=tags,
    )]


def _extract_heading(text: str, source_file: str) -> list[Item]:
    lines = text.splitlines()
    items: list[Item] = []
    # find all headings + levels
    heads: list[tuple[int, int, str]] = []  # (line_idx, level, title_raw)
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            heads.append((idx, len(m.group(1)), m.group(2)))

    for k, (idx, level, title_raw) in enumerate(heads):
        # body = lines after this heading until next heading of same-or-higher level
        end = len(lines)
        for idx2, level2, _ in heads[k+1:]:
            if level2 <= level:
                end = idx2
                break
        body_raw = "\n".join(lines[idx+1:end]).strip()
        title, title_tags = _extract_tags(title_raw)
        body, body_tags = _extract_tags(body_raw)
        tags = {**body_tags, **title_tags}
        items.append(Item(
            id=_item_id(source_file, title, body),
            title=title,
            body=body,
            source_file=source_file,
            source_line=idx + 1,
            tags=tags,
        ))
    return items
