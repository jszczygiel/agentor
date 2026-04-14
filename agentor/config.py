import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourcesConfig:
    watch: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    mark_done: bool = True


@dataclass
class ParsingConfig:
    mode: str = "checkbox"  # "checkbox" | "heading"


@dataclass
class AgentConfig:
    model: str = "claude-opus-4-6"
    max_attempts: int = 3
    pool_size: int = 1  # max concurrent agents working on items
    runner: str = "stub"  # "stub" | "claude"
    # Command template for the claude runner. "{prompt}" is replaced per item.
    command: list[str] = field(default_factory=lambda: [
        "claude", "-p", "{prompt}", "--dangerously-skip-permissions",
    ])
    # Prompt sent to Claude. Placeholders: {title}, {body}, {source_file}.
    prompt_template: str = (
        "/caveman ultra\n"
        "/develop\n\n"
        "Task from the project backlog:\n\n"
        "Title: {title}\n\n"
        "Description:\n{body}\n\n"
        "Source: {source_file}\n\n"
        "Follow the /develop skill end-to-end inside this worktree. Commit "
        "your final change on this branch — a human reviewer will approve or "
        "reject the commit afterwards."
    )
    timeout_seconds: int = 1800
    build_cmd: str | None = None
    test_cmd: str | None = None


@dataclass
class GitConfig:
    base_branch: str = "main"
    branch_prefix: str = "agent/"
    auto_merge: bool = False


@dataclass
class ReviewConfig:
    port: int = 7777
    notify: bool = True


@dataclass
class Config:
    project_name: str
    project_root: Path
    sources: SourcesConfig
    parsing: ParsingConfig
    agent: AgentConfig
    git: GitConfig
    review: ReviewConfig


def load(config_path: Path) -> Config:
    """Load agentor config from a TOML file. Project root is the file's parent dir
    unless [project].root is an absolute path."""
    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    proj = raw.get("project", {})
    name = proj.get("name") or config_path.parent.name
    root_val = proj.get("root", ".")
    root = Path(root_val)
    if not root.is_absolute():
        root = (config_path.parent / root).resolve()

    return Config(
        project_name=name,
        project_root=root,
        sources=SourcesConfig(**raw.get("sources", {})),
        parsing=ParsingConfig(**raw.get("parsing", {})),
        agent=AgentConfig(**raw.get("agent", {})),
        git=GitConfig(**raw.get("git", {})),
        review=ReviewConfig(**raw.get("review", {})),
    )
