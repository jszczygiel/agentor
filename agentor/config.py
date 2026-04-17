import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class SourcesConfig:
    watch: list[str] = field(
        default_factory=lambda: ["docs/backlog/*.md", "docs/ideas/*.md"]
    )
    exclude: list[str] = field(
        default_factory=lambda: ["**/README.md"]
    )


@dataclass
class ParsingConfig:
    mode: str = "frontmatter"  # "checkbox" | "heading" | "frontmatter"


@dataclass
class AgentConfig:
    model: str = "claude-opus-4-6"
    max_attempts: int = 3
    pool_size: int = 0  # max concurrent agents working on items
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
        "PLANNING PHASE. Read, Grep, Glob, and `/research` are all allowed "
        "— use them to ground the plan. Do NOT edit any project files "
        "(writing scratch notes into `tmp/` is fine). Do NOT commit. "
        "Do NOT use `/develop` — that's the execute phase.\n\n"
        "Token-economy rules (strict):\n"
        "- For large files (>10k tokens) use `Read` with `offset`/`limit` "
        "or grep for the relevant region — never whole-file reads of big "
        "modules.\n"
        "- Do NOT re-Read a file you've already Read in this session. Use "
        "the prior read. If you need to re-locate content, Grep by a "
        "stable symbol then Read only the narrow range.\n"
        "- On content Greps, always pass `head_limit` (default 50, "
        "raise only with cause). Skip head_limit only when "
        "`output_mode=count` or `files_with_matches`.\n"
        "- Before firing a Bash command that will dump a log/transcript, "
        "plan how to filter it (head/tail/grep) so the output you pull "
        "into context is <200 lines.\n\n"
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
        "The plan below was produced in a prior turn of this same session "
        "and has been reviewed and approved by a human. Execute it end-to-"
        "end in this worktree and commit the final change on this branch. "
        "A human reviewer approves or rejects the committed result "
        "afterwards.\n\n"
        "Approved plan:\n{plan}\n\n"
        "Task (for reference):\nTitle: {title}\nDescription:\n{body}\n\n"
        "Execution guidelines:\n\n"
        "1. Ground yourself briefly: read CLAUDE.md for coding standards "
        "and build/test commands. Skip re-running `/research` — the plan "
        "was already grounded in prior research during the planning "
        "phase.\n\n"
        "Token-economy rules (strict, apply throughout):\n"
        "- Do NOT re-Read a file you've already Read in this session. "
        "Your context already has it; re-Reading burns cache and tokens "
        "for no signal. If you need to locate something, Grep and Read "
        "only the narrow `offset`/`limit` range.\n"
        "- On content Greps, always pass `head_limit` (default 50). "
        "Omit only when `output_mode=count` or `files_with_matches`.\n"
        "- For test runs and other log-producing Bash commands, pipe "
        "through `grep`/`tail`/`head` so the output pulled into context "
        "is <200 lines. Full headless-test dumps do not belong in "
        "context — grep failures + summary only.\n\n"
        "2. Work through the plan step by step. Build INCREMENTALLY — "
        "build and verify after each logical unit. Do not batch all "
        "changes before compiling. Validate one file before scaling the "
        "pattern to the rest.\n\n"
        "3. Tests are mandatory for every code change. Updating existing "
        "tests to compile does NOT count as coverage — add new test cases "
        "that exercise the new behavior. If code is hard to test, "
        "refactor it.\n\n"
        "4. Scope guard: if you discover an out-of-scope issue, log it to "
        "`docs/IMPROVEMENTS.md` (create if missing) rather than fixing "
        "inline. Stay focused on the approved plan.\n\n"
        "5. If reality forces a deviation from the plan, note it briefly "
        "in the commit message.\n\n"
        "6. Final verification before committing: run the build command "
        "and the test suite documented in CLAUDE.md. Fix any failures.\n\n"
        "7. Optional review: if the diff touches 3+ files and a "
        "`code-reviewer` subagent is available in this project, delegate "
        "a review pass and apply its Must-Fix findings before "
        "committing.\n\n"
        "8. Findings log (mandatory, lightweight). Before committing, "
        "write a per-run findings file at "
        "`docs/agent-logs/<YYYY-MM-DD>-<short-slug>.md`. Keep it terse — "
        "3-8 bullets total across these sections (omit any that are "
        "empty):\n\n"
        "    # <title> — <YYYY-MM-DD>\n\n"
        "    ## Surprises\n"
        "    - things that didn't match the plan or CLAUDE.md\n\n"
        "    ## Gotchas for future runs\n"
        "    - codebase quirks worth codifying in CLAUDE.md later\n\n"
        "    ## Follow-ups\n"
        "    - out-of-scope items also logged to docs/IMPROVEMENTS.md\n\n"
        "    ## Stop if\n"
        "    - symptoms that should halt a future similar attempt\n\n"
        "   Skip sections with nothing to say — an empty run produces no "
        "file. Include this file in your commit. A human will periodically "
        "grep `docs/agent-logs/` and fold durable lessons into CLAUDE.md "
        "/ skills.\n\n"
        "9. Commit on this branch. Do NOT push, do NOT merge — a human "
        "reviewer and agentor's committer handle integration. Use a "
        "concise conventional-commit-style message summarizing the "
        "change.\n"
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
    # How to integrate the feature branch into `base_branch` on approval.
    # "merge" (default) creates a --no-ff merge commit. "rebase" replays
    # the feature commits onto base for a linear history — if the rebase
    # conflicts, the feature worktree is left in its pre-rebase state and
    # the item is parked in CONFLICTED.
    merge_mode: str = "merge"


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
