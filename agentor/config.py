import sys
import tomllib
from dataclasses import dataclass, field, fields
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
    runner: str = "stub"  # "stub" | "claude" | "codex"
    # Advanced override for the selected runner's base command template.
    # Normal configs should not need this; each runner has built-in Python
    # defaults. Supported placeholders: {prompt}, {model}, {output_path}.
    command: list[str] = field(default_factory=list)
    # Advanced override used by the codex runner when resuming an existing
    # session. Supported placeholders: {session_id}, {prompt}, {model},
    # {output_path}. Normal configs should not need this.
    resume_command: list[str] = field(default_factory=list)
    # Total context window in tokens (Opus 4.6 1M variant = 1_000_000;
    # standard Opus = 200_000). Used to compute CTX% in the dashboard.
    context_window: int = 200_000
    # auto: daemon dispatches queued items as soon as a pool slot frees up.
    # manual: items stay queued until a human approves the pickup via the UI.
    pickup_mode: str = "auto"
    # When true, skip the plan phase entirely — agent goes straight to
    # execute on first claim. Saves a full Claude run for items where the
    # backlog text is already a sufficient spec and human plan-review adds
    # no value. The execute prompt is rendered with plan="(no plan; spec is
    # in the task body)" so existing template stays valid.
    single_phase: bool = False
    # Two-phase flow: agent first produces a plan (no code changes), human
    # reviews, then agent resumes in the same session to execute + commit.
    # Placeholders for both: {title}, {body}, {source_file}. The execute
    # prompt additionally receives {plan}.
    plan_prompt_template: str = (
        "Task from the project backlog:\n\n"
        "Title: {title}\n\n"
        "Description:\n{body}\n\n"
        "Source: {source_file}\n\n"
        "PLANNING PHASE. Read-only grep/read is fine. Do NOT modify any "
        "files. Do NOT spawn sub-agents (no /research, no /develop, no "
        "Task tool). Grep first; Read 3-8 files directly to ground "
        "yourself, then write a concise plan. For large files (>10k tokens) "
        "use `Read` with `offset`/`limit` or grep for the relevant region "
        "instead of reading whole files — full reads will fail and abort "
        "the plan.\n\n"
        "Plan structure (terse, not verbose):\n"
        "1. Deliverable — 3-5 bullets, observable outcome.\n"
        "2. Acceptance — 2-4 bullets, how to verify.\n"
        "3. Changes — numbered file:function edits, one line each.\n"
        "4. Tests — test files/cases to add or touch.\n"
        "5. Risks — what could break.\n"
        "6. Open questions — only if reviewer must resolve.\n\n"
        "A human reviews this plan before execution runs. Keep it tight."
    )
    execute_prompt_template: str = (
        "The plan below was reviewed and approved by a human. Execute it end-"
        "to-end in this worktree and commit your final change on this branch. "
        "Stick to the plan; if reality forces a deviation, note it briefly in "
        "your commit message.\n\n"
        "Approved plan:\n{plan}\n\n"
        "Task (for reference):\nTitle: {title}\nDescription:\n{body}\n"
    )
    # Back-compat placeholder; no longer used by the two-phase runner.
    prompt_template: str = ""
    timeout_seconds: int = 1800
    # Hard cap on agent turns. 0 disables. Live stream watches num_turns
    # and kills the child when exceeded.
    max_turns: int = 0
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


def _filter_known(cls, data: dict, section: str) -> dict:
    """Drop unknown keys before constructing a dataclass, with a warning
    so stale configs (e.g. a removed option like max_cost_usd) don't
    crash the loader. Users keep their existing files working and get a
    nudge to clean up."""
    known = {f.name for f in fields(cls)}
    filtered = {}
    for k, v in (data or {}).items():
        if k in known:
            filtered[k] = v
        else:
            print(f"[config] ignoring unknown key [{section}].{k} "
                  f"(removed or misspelled)", file=sys.stderr)
    return filtered


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
        sources=SourcesConfig(**_filter_known(
            SourcesConfig, raw.get("sources", {}), "sources")),
        parsing=ParsingConfig(**_filter_known(
            ParsingConfig, raw.get("parsing", {}), "parsing")),
        agent=AgentConfig(**_filter_known(
            AgentConfig, raw.get("agent", {}), "agent")),
        git=GitConfig(**_filter_known(
            GitConfig, raw.get("git", {}), "git")),
        review=ReviewConfig(**_filter_known(
            ReviewConfig, raw.get("review", {}), "review")),
    )
