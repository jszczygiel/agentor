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
    # --output-format stream-json + --verbose stream per-turn events so the
    # runner can update CTX% and token counts live while the agent is still
    # running. Do NOT add --session-id or --resume here; the runner appends
    # them per-item so crashed agents can be resumed on restart.
    command: list[str] = field(default_factory=lambda: [
        "claude", "-p", "{prompt}", "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--verbose",
    ])
    # Total context window in tokens (Opus 4.6 1M variant = 1_000_000;
    # standard Opus = 200_000). Used to compute CTX% in the dashboard.
    context_window: int = 200_000
    # auto: daemon dispatches queued items as soon as a pool slot frees up.
    # manual: items stay queued until a human approves the pickup via the UI.
    pickup_mode: str = "auto"
    # Two-phase flow: agent first produces a plan (no code changes), human
    # reviews, then agent resumes in the same session to execute + commit.
    # Placeholders for both: {title}, {body}, {source_file}. The execute
    # prompt additionally receives {plan}.
    plan_prompt_template: str = (
        "/caveman ultra\n\n"
        "Task from the project backlog:\n\n"
        "Title: {title}\n\n"
        "Description:\n{body}\n\n"
        "Source: {source_file}\n\n"
        "PLANNING PHASE. Do NOT modify production code, tests, or game "
        "data. The only file you may write is TECH.md (produced by "
        "/research). Read-only grep/read of existing code is expected.\n\n"
        "Step 1 — ground yourself. Run the /research skill against this "
        "task so TECH.md captures: current behavior around the area, the "
        "key files/functions/data structures, related tests, any recent "
        "commits or TODOs that touched this code. Reference the TECH.md "
        "findings in the plan below.\n\n"
        "Step 2 — produce a DELIVERABLE-FOCUSED plan. Lead with *what the "
        "user will be able to do, see, or rely on after this ships* — "
        "concrete behavior, UX, or API, not implementation noise. A "
        "reviewer should be able to tell from section 1 alone whether this "
        "is the right thing to build.\n\n"
        "Sections (in this order):\n\n"
        "1. Deliverable — 3-8 bullets describing the *outcome*. What "
        "changes from the user/system's perspective? Include concrete "
        "examples (inputs, outputs, screens, log lines, behavior under "
        "edge cases). Skip implementation vocabulary here.\n"
        "2. Acceptance criteria — bulleted, verifiable statements. Each "
        "should answer 'how would we know this is done?' Think in terms "
        "of observable behavior, not internal structure.\n"
        "3. Context (from /research) — key file:line references, the data "
        "flow, and any existing mechanisms this builds on. Quote "
        "sparingly; link to TECH.md for depth.\n"
        "4. Approach — the strategy, and why this shape beats the "
        "alternatives you considered. Call out the one-line intuition "
        "('factor-driven target' / 'debounce on settle' / etc).\n"
        "5. Changes — numbered list of concrete edits. For each: file, "
        "function/section, what changes (1-3 substantive bullets). "
        "Include new files with their path and purpose.\n"
        "6. Data / API impact — schema, config, save format, public "
        "interface, or migration effects. Write 'none' if truly none.\n"
        "7. Tests — specific tests to add or update, by test file and "
        "case name. New test file? Say so with its path.\n"
        "8. Risks & edge cases — what could break, what could regress, "
        "concurrency / ordering / error-path concerns, rollback plan.\n"
        "9. Verification — how you (and the reviewer) will confirm "
        "success: commands, expected outputs, manual smoke steps.\n"
        "10. Open questions — ambiguities the reviewer must resolve "
        "before execution. Be specific: 'should X live in module A or "
        "B, given Y?' not 'what approach?'\n\n"
        "Write for a teammate who can give line-level feedback. Err "
        "toward concrete file paths, function names, and observable "
        "behavior. A human reviews this plan before execution runs."
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
