import datetime
import json
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from . import git_ops
from .capabilities import (
    CLAUDE_CAPS,
    CODEX_CAPS,
    STUB_CAPS,
    ProviderCapabilities,
)
from .checkpoint import CheckpointConfig, CheckpointEmitter
from .config import AgentConfig, Config
from .models import ItemStatus
from .providers import ClaudeProvider, CodexProvider, Provider, make_provider
from .slug import slugify
from .store import Store, StoredItem

T = TypeVar("T")

# _publish_live coalesces result_json writes during streaming: dashboard polls
# at 500ms, SQLite UPDATE per event is wasted I/O. Terminal events bypass the
# guard so the final envelope always lands.
_PUBLISH_INTERVAL_NS = 1_000_000_000


class ChildStdinHolder:
    """Thread-safe line writer bound to a child process's stdin pipe.

    `_run_stream_json_subprocess` populates the internal handle after spawn;
    the stream-reader thread calls `write_line(payload)` to inject additional
    JSONL messages (e.g. mid-run checkpoint nudges) without racing with
    process teardown. A lock guards every write/close so `p.kill()` on the
    timeout path can't corrupt a partial write."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fh = None
        self._closed = False

    def attach(self, fh) -> None:
        with self._lock:
            self._fh = fh

    def write_line(self, text: str) -> bool:
        line = text if text.endswith("\n") else text + "\n"
        with self._lock:
            if self._closed or self._fh is None:
                return False
            try:
                self._fh.write(line)
                self._fh.flush()
                return True
            except Exception:
                return False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


class ProcRegistry:
    """Tracks live agent subprocesses so the daemon can kill them on shutdown.
    Each Popen is spawned with `start_new_session=True` so we can signal the
    whole process group — kills any sub-tools (bash, git, sub-agents) claude
    itself may have spawned. Without this the parent exits but the agent
    child lives on as an orphan, burning tokens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}

    def register(self, key: str, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs[key] = p

    def unregister(self, key: str) -> None:
        with self._lock:
            self._procs.pop(key, None)

    def kill_all(self, log=None) -> int:
        with self._lock:
            procs = list(self._procs.items())
            self._procs.clear()
        live = [(k, p) for k, p in procs if p.poll() is None]
        if not live:
            return 0
        if log:
            log(f"killing {len(live)} agent subprocess(es)")
        for _, p in live:
            _signal_group(p, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        for _, p in live:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _signal_group(p, signal.SIGKILL)
                try:
                    p.wait(timeout=2.0)
                except Exception:
                    pass
        return len(live)

    def kill_one(self, key: str) -> bool:
        """Signal a single registered subprocess — operator-initiated delete
        of a WORKING item. Same SIGTERM→wait→SIGKILL pattern as `kill_all`
        but scoped to one key. Returns True when a live process was
        signalled, False when no entry exists or it had already exited."""
        with self._lock:
            p = self._procs.pop(key, None)
        if p is None or p.poll() is not None:
            return False
        _signal_group(p, signal.SIGTERM)
        try:
            p.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            _signal_group(p, signal.SIGKILL)
            try:
                p.wait(timeout=2.0)
            except Exception:
                pass
        return True


def _signal_group(p: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            if sig == signal.SIGKILL:
                p.kill()
            else:
                p.terminate()
        except Exception:
            pass


class InfrastructureError(RuntimeError):
    """Raised when a dispatch fails for system-level reasons unrelated to the
    item itself: broken git worktree, missing repo, base branch gone, etc.

    Caught by the daemon, which pauses dispatch and surfaces a sticky alert
    to the user. The item's status is left untouched (still WORKING) and
    the dispatch attempt is not charged — fixing the infrastructure should
    be enough to let the existing item resume."""


_INFRA_NEEDLES = (
    "not a git repository",
    "fatal: invalid reference",
    "no such file or directory",
    "not a working tree",
    "fatal: bad object",
    "fatal: bad revision",
    # Stale per-item branches/worktrees from prior runs that pre-flight
    # cleanup couldn't remove (e.g. branch checked out in an unknown
    # external worktree). Treat as infra so the user gets paged instead
    # of the item silently burning attempts.
    "already exists",
    "is already checked out",
    "already used by worktree",
)


def _is_infrastructure_error(msg: str) -> bool:
    """Heuristic: distinguish git plumbing failures (broken worktree slot,
    missing branch, corrupt registration) from item-level failures
    (claude exited 1, agent timed out, etc.). Conservative — false
    negatives just fall through to the existing per-item retry path."""
    low = (msg or "").lower()
    return any(n in low for n in _INFRA_NEEDLES)


def _is_dead_session_error(msg: str) -> bool:
    """Union-needle predicate across every supported CLI.

    Used only as a disqualifier inside `_is_transient_error` — the retry
    wrapper has no `Provider` handy, so a cross-provider safety net here
    is safer than skipping the gate (matches too broadly at worst; never
    retries a dead session). The recovery sweep and the runner-level
    session-kill demote both route through `Provider.is_dead_session_error`
    instead, so a Claude item never trips on a Codex signature."""
    low = (msg or "").lower()
    needles = (
        "no conversation found with session id",
        "session not found",
        "thread not found",
        "thread/start failed",
    )
    return any(n in low for n in needles)


def _is_shutdown_error(msg: str) -> bool:
    """Detect that the failure was the daemon killing the agent on
    shutdown (^C). Not the item's fault — must not charge an attempt or
    transition state. Recovery on next startup picks the WORKING item up."""
    return "agentor shutdown" in (msg or "").lower()


_ERR_NOISE = re.compile(r"\d+|\(\$[\d.]+\)|\([^)]*\)|\s+")


def _error_signature(msg: str) -> str:
    """Strip variable bits (counts, dollar amounts, ids, whitespace) from an
    error message so two attempts that hit the same wall match. Example:
    'claude killed: max_turns=30 hit (30 turns)' →
    'claude killed: max_turns= hit'."""
    return _ERR_NOISE.sub("", (msg or "").lower())[:80]


# Exponential-backoff cadence for transient CLI failures. Module-level so
# tests can monkey-patch. `_sleep` is looked up at call time so patching
# `runner._sleep` works end-to-end.
_RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0, 30.0)
_RETRY_JITTER: float = 0.25


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


_TRANSIENT_NEEDLES = (
    "429", "rate limit", "rate_limit",
    "500 ", "502 ", "503 ", "504 ",
    "bad gateway", "gateway timeout",
    "service unavailable", "internal server error",
    "overloaded",
    "connection reset", "connection refused", "connection aborted",
    "temporary failure in name resolution",
    "name or service not known", "nodename nor servname",
    "network is unreachable",
    "eof occurred in violation",
    "read timed out",
)


# Strings that look transient at a glance but are actually fatal and should
# fail fast so the operator sees them. Auth/quota/credit won't recover on
# retry; max_turns / max_cost_usd are deliberate runaway-guard kills;
# syntax errors in the prompt/template won't self-heal.
_FATAL_NEEDLES = (
    "invalid api key", "unauthorized", "forbidden",
    "quota", "credit",
    "syntaxerror", "syntax error",
    "max_turns=", "max_cost_usd=",
)


def _is_transient_error(
    msg: str, elapsed: float, timeout_seconds: float,
) -> bool:
    """True if `msg` looks like a momentary hiccup worth retrying in-dispatch
    (HTTP 429/5xx, TCP/DNS blips, or a `timed out` when elapsed was well
    under the configured budget — i.e. a subprocess.TimeoutExpired-style
    stall, not a genuine hang). Returns False for shutdown / dead-session /
    infrastructure (dedicated paths), auth/quota/syntax/runaway-cap fatals,
    and real hangs (timeout with elapsed ≥ 90% of the budget)."""
    low = (msg or "").lower()
    if not low:
        return False
    if (_is_shutdown_error(low) or _is_dead_session_error(low)
            or _is_infrastructure_error(low)):
        return False
    if any(n in low for n in _FATAL_NEEDLES):
        return False
    if "timed out" in low or "timeout" in low:
        if timeout_seconds > 0 and elapsed >= 0.9 * timeout_seconds:
            return False
        return True
    return any(n in low for n in _TRANSIENT_NEEDLES)


def _backoff_delay(attempt: int) -> float:
    """Return the sleep duration before retry number `attempt` (0-indexed).
    Indexes past the table clamp to the last value."""
    idx = min(attempt, len(_RETRY_DELAYS) - 1)
    base = _RETRY_DELAYS[idx]
    return base + random.uniform(0.0, base * _RETRY_JITTER)


def _log_retry(
    transcript_path: Path, attempt: int, budget: int, delay: float,
    error: str,
) -> None:
    """Append a RETRY marker to the per-item transcript so operators can see
    why a run took longer than expected. Best-effort — a write failure must
    not derail the retry."""
    try:
        with transcript_path.open("a") as fh:
            fh.write(
                f"\nRETRY {attempt}/{budget} in {delay:.1f}s: "
                f"{error.strip()[-500:]}\n"
            )
    except Exception:
        pass


def _retry_transient(
    invoke: Callable[[], T], *,
    transcript_path: Path, retries: int, timeout_seconds: int,
) -> T:
    """Call `invoke()`, retrying up to `retries` times on transient errors
    with exponential backoff. Non-transient errors and budget-exhausted
    retries propagate unchanged. Each backoff is written to `transcript_path`
    so operators can see why a run took longer than expected."""
    if retries <= 0:
        return invoke()
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            return invoke()
        except RuntimeError as e:
            elapsed = time.monotonic() - t0
            if attempt >= retries:
                raise
            if not _is_transient_error(str(e), elapsed, timeout_seconds):
                raise
            delay = _backoff_delay(attempt)
            _log_retry(
                transcript_path, attempt + 1, retries, delay, str(e),
            )
            _sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


@dataclass
class RunResult:
    item_id: str
    worktree_path: Path
    branch: str
    summary: str
    files_changed: list[str]
    diff: str
    error: str | None = None


def worktree_root(config: Config) -> Path:
    return config.project_root / ".agentor" / "worktrees"


def plan_worktree(
    config: Config, item: StoredItem, store: Store | None = None,
) -> tuple[Path, str]:
    """Ephemeral per-item worktree. Path is `{project}-{slug}-{shortid}`
    under `.agentor/worktrees/`; the worktree is created at dispatch and
    removed after commit/merge. `store` is unused here — kept for call-site
    compatibility."""
    del store  # unused
    slug = slugify(item.title)
    unique = f"{config.project_name}-{slug}-{item.id[:8]}"
    branch = f"{config.git.branch_prefix}{slug}-{item.id[:8]}"
    path = worktree_root(config) / unique
    return path, branch


class Runner:
    """Base runner interface. Subclasses implement `do_work`."""

    capabilities: ProviderCapabilities = STUB_CAPS

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        # Per-CLI dead-session / wall-clock-expiry behaviour. Recovery and
        # the runner-level session-kill demote both consult this instead of
        # a hardcoded Claude substring — Codex thread expiries route through
        # the same code path.
        self.provider: Provider = make_provider(config)
        # Set by Daemon after construction. Allows in-flight subprocesses to
        # be killed on shutdown rather than orphaned.
        self.proc_registry: ProcRegistry | None = None
        self.stop_event: threading.Event | None = None

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        """Perform the agent's work inside the worktree. Return (summary, files_changed).
        Subclasses override. The base class commits no changes — committer does that."""
        raise NotImplementedError

    def write_tool_guardrails(
        self, config: Config, item_id: str,
    ) -> dict[str, str]:
        """Provider hook: produce placeholder substitutions for
        `agent.command` templates that register tool guardrails (e.g.
        PreToolUse Read/Grep hooks). Return a mapping of
        `{placeholder_name: value}` splice into the command template via
        `.format(**values)`. Return an empty dict when the provider has
        no guardrail channel (Codex) so `agent.large_file_line_threshold`
        / `agent.enforce_grep_head_limit` become observable no-ops.
        Placeholder names are namespaced per provider — Claude uses
        `{settings_path}`; future providers MUST pick a distinct key to
        avoid collisions in shared command templates."""
        return {}

    def warn_silent_guardrails(
        self, config: Config, log: Callable[[str], None],
    ) -> None:
        """Provider hook invoked once per daemon startup. Providers
        without a guardrail channel (Codex) emit a log line when any
        guardrail knob is set to a non-default value so operators learn
        at startup — not mid-review — that their config has no effect.
        Default is a no-op for providers that honour the knobs."""
        return None

    def _record_failure(
        self, item: StoredItem, phase: str, error: str,
        files_changed: list[str] | None = None,
    ) -> None:
        """Write a failure row for diagnostics. Pulls turns/duration from
        the last agent run if available (populated by runner-specific
        invoke helpers into self._last_usage). Safe for subclasses
        without that attribute — falls back to None fields."""
        usage = getattr(self, "_last_usage", None) or {}
        phase_tag = getattr(self, "_last_phase", phase)
        transcript = None
        try:
            transcript = str(
                self.config.project_root / ".agentor" / "transcripts"
                / f"{item.id}.{phase_tag}.log"
            )
        except Exception:
            transcript = None
        self.store.record_failure(
            item_id=item.id,
            attempt=item.attempts,
            phase=phase_tag,
            error=error,
            error_sig=_error_signature(error),
            num_turns=usage.get("num_turns"),
            duration_ms=usage.get("duration_ms"),
            files_changed=files_changed,
            transcript_path=transcript,
        )

    def run(self, item: StoredItem) -> RunResult:
        """Item must already be in WORKING state with worktree_path and branch set
        (daemon does this via store.claim_next_queued)."""
        assert item.status == ItemStatus.WORKING, f"runner expects WORKING, got {item.status}"
        assert item.worktree_path and item.branch
        wt_path = Path(item.worktree_path)
        branch = item.branch
        repo = self.config.project_root

        # Resume path: agent_ref persisted + worktree still on disk → skip
        # teardown/recreate so the agent picks up where it left off. Otherwise
        # do the normal pre-flight nuke so stale state doesn't leak.
        resume = bool(item.agent_ref) and wt_path.exists()
        try:
            if (self.stop_event is not None
                    and self.stop_event.is_set()):
                self.store.note_infra_failure(
                    item.id, "agentor shutdown before dispatch",
                )
                return RunResult(item.id, wt_path, branch, "", [], "",
                                 error="agentor shutdown before dispatch")
            if not resume:
                # Pre-flight cleanup. Order matters: remove the worktree
                # dir, then prune stale registrations
                # (.git/worktrees/<name>/ left after a manual rm -rf),
                # THEN delete the branch — git refuses to delete a
                # branch while still associated with a (possibly ghost)
                # worktree.
                git_ops.worktree_remove(repo, wt_path, force=True)
                if wt_path.exists():
                    shutil.rmtree(wt_path, ignore_errors=True)
                git_ops.worktree_prune(repo)
                if git_ops.branch_exists(repo, branch):
                    held_at = git_ops.branch_checked_out_at(repo, branch)
                    if held_at is not None and held_at != wt_path:
                        git_ops.worktree_remove(repo, held_at, force=True)
                        if held_at.exists():
                            shutil.rmtree(held_at, ignore_errors=True)
                        git_ops.worktree_prune(repo)
                    git_ops.branch_delete(repo, branch, force=True)

            try:
                if not resume:
                    git_ops.worktree_add(
                        repo, wt_path, branch,
                        self.config.git.base_branch,
                    )
                else:
                    # Sync: pull any commits that landed on base while this
                    # item was awaiting review. Fast-forward only — plan
                    # phase is read-only so the feature should still be at
                    # its fork point, and a successful ff guarantees no
                    # rewrites and no conflicts. If the agent has somehow
                    # committed during plan, ff will refuse; we skip the
                    # sync silently rather than merging/rebasing here so
                    # the integration path handles divergence uniformly
                    # at the final commit step (where any conflict surfaces
                    # as CONFLICTED with full recovery flow).
                    git_ops.fast_forward_to_base(
                        wt_path, self.config.git.base_branch,
                    )
            except git_ops.GitError as e:
                err = str(e)
                self._record_failure(item, "setup", err)
                if _is_infrastructure_error(err):
                    # Don't transition state, don't charge an attempt — this
                    # is the slot/repo being broken, not the item failing.
                    # Daemon will catch InfrastructureError, pause dispatch,
                    # and put a sticky alert on the dashboard.
                    self.store.note_infra_failure(item.id, err)
                    raise InfrastructureError(err)
                self.store.transition(
                    item.id, ItemStatus.ERRORED,
                    worktree_path=None, branch=None, agent_ref=None,
                    last_error=f"worktree_add: {err}",
                    note="worktree_add failed → errored",
                )
                return RunResult(item.id, wt_path, branch, "", [], "", error=err)

            try:
                summary, files_changed = self.do_work(item, wt_path)
            except Exception as e:
                last_error = f"do_work: {e}"
                self._record_failure(item, "do_work", last_error)
                if _is_shutdown_error(last_error):
                    # Operator killed us. Refund the attempt and leave the
                    # item WORKING so recovery can resume on next startup.
                    # No InfrastructureError raise — we don't want a sticky
                    # alert for an intentional ^C.
                    self.store.note_infra_failure(item.id, last_error)
                    return RunResult(item.id, wt_path, branch, "", [], "",
                                     error=last_error)
                if _is_infrastructure_error(last_error):
                    # Same treatment as the worktree_add infra path — leave
                    # state alone, refund the attempt, surface to user.
                    self.store.note_infra_failure(item.id, last_error)
                    raise InfrastructureError(last_error)
                if self.provider.is_dead_session_error(last_error) and item.agent_ref:
                    # The provider lost the session. Resuming with the same
                    # agent_ref will keep failing on every attempt until
                    # rejection — drop the agent_ref, refund the attempt, and
                    # bounce back to QUEUED so the next dispatch starts a
                    # fresh session. result_json (with the approved plan) is
                    # kept so we don't make the user re-approve.
                    git_ops.worktree_remove(repo, wt_path, force=True)
                    self.store.transition(
                        item.id, ItemStatus.QUEUED,
                        worktree_path=None, branch=None, agent_ref=None,
                        attempts=max(0, item.attempts - 1),
                        last_error=last_error,
                        note="agent session lost; restart with fresh session",
                    )
                    return RunResult(item.id, wt_path, branch, "", [], "",
                                     error=last_error)
                git_ops.worktree_remove(repo, wt_path, force=True)
                # Any agent-side failure parks the item in ERRORED so the
                # daemon immediately moves on to the next queued item. The
                # operator re-queues (via revert) once the root cause is
                # fixed — no auto-retry loop.
                self.store.transition(
                    item.id, ItemStatus.ERRORED,
                    worktree_path=None, branch=None, agent_ref=None,
                    last_error=last_error,
                    note="do_work failed → errored",
                )
                return RunResult(item.id, wt_path, branch, "", [], "", error=last_error)

            try:
                diff = git_ops.diff_vs_base(wt_path, self.config.git.base_branch)
            except git_ops.GitError as e:
                # Slot went bad between do_work returning and us reading
                # the diff (rare — usually means the worktree registration
                # was nuked under us). Treat as infra so the user sees an
                # alert instead of "worker crashed" with no recovery.
                err = f"diff_vs_base: {e}"
                self._record_failure(item, "diff", err)
                if _is_infrastructure_error(err):
                    self.store.note_infra_failure(item.id, err)
                    raise InfrastructureError(err)
                raise
            phase = getattr(self, "_last_phase", "execute")
            next_status = (ItemStatus.AWAITING_PLAN_REVIEW if phase == "plan"
                           else ItemStatus.AWAITING_REVIEW)
            result = {
                "phase": phase,
                "summary": summary,
                "files_changed": files_changed,
                "diff_len": len(diff),
            }
            if phase == "plan":
                # Keep the plan text intact so the execute phase can inject
                # it into the follow-up prompt and the review UI can show it.
                result["plan"] = summary
                questions = getattr(self, "_last_questions", []) or []
                if questions:
                    result["questions"] = questions
            else:
                # Carry the approved plan forward on the final result.
                prior = _parse_result_json(item.result_json)
                if prior.get("plan"):
                    result["plan"] = prior["plan"]
                # Record the resolved execute-tier + source so
                # tools/analyze_transcripts.py can attribute token spend
                # by chosen tier and operators can grep the fallback
                # rate. Only populated when the runner subclass set the
                # attrs (ClaudeRunner / CodexRunner do; StubRunner
                # doesn't — execute_model stays absent there).
                alias = getattr(self, "_last_execute_model", None)
                source = getattr(self, "_last_execute_model_source", None)
                if alias and source:
                    result["execute_model"] = alias
                    result["execute_model_source"] = source
                # Plan's raw tier nomination, recorded whether or not it
                # was applied (gate off, whitelist miss, tag override).
                # Lets operators measure counterfactual tier picks.
                suggestion = getattr(self, "_last_plan_suggestion", None)
                if suggestion:
                    result["plan_suggested_execute_model"] = suggestion
            envelope = getattr(self, "_last_usage", None)
            if envelope:
                # Surface turn/timing/usage fields at the top level of
                # result_json so the dashboard can read them without
                # digging into a nested 'usage' dict.
                for k, v in envelope.items():
                    result[k] = v
            note = ("plan ready for human review" if phase == "plan"
                    else "awaiting user review")
            self.store.transition(
                item.id, next_status,
                result_json=json.dumps(result),
                note=note,
            )
            return RunResult(
                item_id=item.id, worktree_path=wt_path, branch=branch,
                summary=summary, files_changed=files_changed, diff=diff,
            )
        finally:
            pass


class StubRunner(Runner):
    """Test runner that writes a trivial AGENT_NOTE.md plus a
    compliance-passing per-run findings log under `docs/agent-logs/`.
    Proves the pipeline end-to-end without spawning Claude."""

    capabilities: ProviderCapabilities = STUB_CAPS

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        note_path = worktree / f".agentor-note-{item.id[:8]}.md"
        note_path.write_text(
            f"# Stub agent note\n\n"
            f"Item: {item.title}\n\n"
            f"Body:\n{item.body}\n\n"
            f"(This is a stub runner. Replace with real agent.)\n"
        )
        note_rel = str(note_path.relative_to(worktree))

        today = datetime.date.today().isoformat()
        log_dir = worktree / "docs" / "agent-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{today}-stub-{item.id[:8]}.md"
        log_path.write_text(
            f"# {item.title} — {today}\n\n"
            f"## Surprises\n"
            f"- none (stub runner)\n\n"
            f"## Outcome\n"
            f"- Files touched: {note_rel}\n"
            f"- Tests added/adjusted: none (stub runner)\n"
            f"- Follow-ups: none\n"
        )
        log_rel = str(log_path.relative_to(worktree))

        summary = f"stub: added note for '{item.title}'"
        return summary, [note_rel, log_rel]


def _run_stream_json_subprocess(
    *,
    args: list[str],
    cwd: Path,
    timeout_seconds: int,
    transcript_path: Path,
    proc_registry: ProcRegistry | None,
    item_key: str,
    fnfe_hint: str,
    on_event,
    stdin_payload: str | None = None,
    stdin_holder: ChildStdinHolder | None = None,
) -> tuple[str, str, int | None, bool, str | None]:
    """Spawn a subprocess that emits line-delimited JSON on stdout, stream
    each event through `on_event`, and return once the child exits (or a
    cap/timeout triggers).

    `on_event(ev)` is called for every dict-shaped JSON line. If it returns
    a truthy string, the child is killed and that string is surfaced as
    `cap_reason` so callers can raise a descriptive error.

    `stdin_payload` is written (and flushed) before the read loop begins.
    When `stdin_holder` is passed, stdin stays open and `on_event` can
    inject further lines via `stdin_holder.write_line(...)`; if no holder
    is passed stdin is closed immediately after the initial payload.

    Returns (stdout_text, stderr_text, returncode, timed_out, cap_reason).
    Caller is responsible for the final shutdown-event / returncode checks —
    this helper just owns the subprocess lifecycle and byte plumbing."""
    stdin_spec = subprocess.PIPE if (
        stdin_payload is not None or stdin_holder is not None
    ) else None
    try:
        # start_new_session=True puts the child (and anything it spawns)
        # in its own process group so the daemon can SIGTERM the whole
        # tree on shutdown via os.killpg.
        p = subprocess.Popen(
            args, cwd=cwd,
            stdin=stdin_spec,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
    except FileNotFoundError:
        raise RuntimeError(fnfe_hint)
    assert p.stdout is not None and p.stderr is not None
    if proc_registry is not None:
        proc_registry.register(item_key, p)
    if stdin_holder is not None and p.stdin is not None:
        stdin_holder.attach(p.stdin)
    if stdin_payload is not None and p.stdin is not None:
        try:
            p.stdin.write(stdin_payload)
            p.stdin.flush()
        except Exception:
            pass
        if stdin_holder is None:
            try:
                p.stdin.close()
            except Exception:
                pass

    timed_out = threading.Event()

    def _on_timeout():
        timed_out.set()
        try:
            p.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout_seconds, _on_timeout)
    timer.daemon = True
    timer.start()

    # Drain stderr on a background thread so the child can't deadlock
    # writing to a full stderr buffer.
    stderr_chunks: list[str] = []

    def _drain_stderr():
        try:
            for line in iter(p.stderr.readline, ""):
                stderr_chunks.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    stdout_buf: list[str] = []
    cap_reason: str | None = None
    # Append mode so RETRY markers written by the surrounding retry loop
    # are preserved across attempts; _invoke_claude/_invoke_codex truncate
    # the file once before the first attempt.
    with transcript_path.open("a") as fh:
        fh.write(f"args: {args}\n\nstdout:\n")
    # Hold the transcript open for the duration of the read loop so the hot
    # path is write+flush per event rather than open+fstat+write+close. Flush
    # preserves the live-tail guarantee that
    # `dashboard/transcript.py::iter_events` relies on.
    transcript_fh = None
    try:
        transcript_fh = transcript_path.open("a")
        for line in iter(p.stdout.readline, ""):
            stdout_buf.append(line)
            transcript_fh.write(line)
            transcript_fh.flush()
            stripped = line.strip()
            if stripped:
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    ev = None
                if isinstance(ev, dict):
                    reason = on_event(ev)
                    if reason and not cap_reason:
                        cap_reason = reason
            if cap_reason:
                try:
                    p.kill()
                except Exception:
                    pass
                break
        p.wait(timeout=5)
    finally:
        timer.cancel()
        if proc_registry is not None:
            proc_registry.unregister(item_key)
        if stdin_holder is not None:
            stdin_holder.close()
        if transcript_fh is not None:
            try:
                transcript_fh.close()
            except Exception:
                pass
        try:
            p.stdout.close()
        except Exception:
            pass
        stderr_thread.join(timeout=2)
        try:
            p.stderr.close()
        except Exception:
            pass

    stdout_text = "".join(stdout_buf)
    stderr_text = "".join(stderr_chunks)
    with transcript_path.open("a") as fh:
        fh.write(f"\n\nstderr:\n{stderr_text}\n")
        fh.write(f"\nexit: {p.returncode}\n")
    return stdout_text, stderr_text, p.returncode, timed_out.is_set(), cap_reason


def _write_checkpoint_marker(
    transcript_path: Path, num_turns: int, output_tokens: int, nudge: str,
    injected: bool,
) -> None:
    """Append a human-readable marker to the transcript so post-hoc analysis
    can see where a checkpoint nudge landed. The marker sits outside the JSONL
    event stream (line doesn't start with `{`) so the stream walker skips it.
    Shared by ClaudeRunner (inject via stdin) and CodexRunner (dry-run only —
    codex has no open stdin, output_tokens always 0)."""
    tag = "injected" if injected else "observed-dry-run"
    marker = (
        f"\n[checkpoint-{tag} @ turn {num_turns} "
        f"output_tokens={output_tokens}]\n{nudge}\n"
    )
    try:
        with transcript_path.open("a") as fh:
            fh.write(marker)
    except Exception:
        pass


class ClaudeRunner(Runner):
    """Spawns a headless `claude -p` subprocess inside the worktree. Runs in
    two phases tied together by the persisted agent_ref (Claude's wire-format
    session_id, stored provider-neutrally on the item):

    1) plan — agent writes a development plan (no code changes), item stops at
       AWAITING_PLAN_REVIEW for human approval.
    2) execute — on approval the item returns to QUEUED, gets re-claimed, and
       resumes the same claude session to implement and commit.
    """

    capabilities: ProviderCapabilities = CLAUDE_CAPS

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prior = _parse_result_json(item.result_json)
        if prior.get("phase") == "plan":
            return self._do_execute(item, worktree, prior.get("plan", ""))
        if self.config.agent.single_phase:
            return self._do_execute(
                item, worktree, "(no plan; spec is in the task body)",
            )
        return self._do_plan(item, worktree)

    def write_tool_guardrails(
        self, config: Config, item_id: str,
    ) -> dict[str, str]:
        """Claude's PreToolUse Read/Grep hooks ride on the `--settings
        <path>` CLI flag. `write_claude_settings` writes the per-item
        JSON registering the bundled hook scripts; the returned path is
        spliced into `agent.command` via `{settings_path}`. Custom
        templates dropping that placeholder silently disable
        enforcement."""
        path = write_claude_settings(config, item_id)
        return {"settings_path": str(path)}

    def _do_plan(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prompt = self.config.agent.plan_prompt_template.format(
            title=item.title, body=item.body, source_file=item.source_file,
        )
        prompt = self._prepend_feedback(item, prompt, phase="plan")
        self._last_execute_model = None
        self._last_execute_model_source = None
        self._last_plan_suggestion = None
        _, stdout = self._invoke_claude(
            item, worktree, prompt,
        )
        # For plan phase, _derive_summary is misleading (no commits, no
        # AGENT_SUMMARY.md → falls back to the base branch commit message).
        # Pull the real plan text from the envelope's `result` field;
        # stream-json and blocking paths both populate _last_usage.
        envelope = getattr(self, "_last_usage", None) or {}
        plan_text = (
            envelope.get("result")
            or _extract_result_field(stdout)
            or "(no plan text returned)"
        )
        self._last_phase = "plan"
        self._last_questions = _extract_plan_questions(plan_text)
        return plan_text, []

    def _do_execute(
        self, item: StoredItem, worktree: Path, plan: str,
    ) -> tuple[str, list[str]]:
        prompt = self.config.agent.execute_prompt_template.format(
            title=item.title, body=item.body, source_file=item.source_file,
            plan=plan,
        )
        prompt += _mark_done_instruction(self.config, item.source_file)
        # Kill-resume primer. An existing `.execute.log` means a prior
        # attempt was interrupted (e.g. `agentor shutdown` mid-run). The
        # resumed claude session keeps its prompt cache but has no
        # structured record of which files it already Read/Grep'd, so it
        # cold-starts discovery and re-Reads — ~18k tokens wasted in the
        # worst observed case. Summarise the prior tool activity and
        # prepend it so the agent knows what not to re-fetch. The subprocess
        # will overwrite this log on start, so we read it before launching.
        primer = self.provider.build_primer(
            self._execute_transcript_path(item)
        )
        if primer:
            prompt = f"{primer}\n{prompt}"
        prompt = self._prepend_feedback(item, prompt, phase="execute")
        prompt = _prepend_plan_answers(item, prompt)
        alias, source = _resolve_execute_tier(
            self.config, self.provider, item, plan,
        )
        self._last_execute_model = alias
        self._last_execute_model_source = source
        # Record plan's raw nomination independent of resolution so we
        # can measure "would the plan have picked a smaller tier?" even
        # when `agent.auto_execute_model=false` ignores it.
        self._last_plan_suggestion = _parse_execute_tier(plan, whitelist=None)
        model_override = self.provider.model_aliases.get(alias)
        summary, stdout = self._invoke_claude(
            item, worktree, prompt, model_override=model_override,
        )
        files = _list_changes(worktree, self.config.git.base_branch)
        summary = _derive_summary(worktree, stdout, item.title)
        self._last_phase = "execute"
        return summary, files

    def _prepend_feedback(self, item: StoredItem, prompt: str, phase: str) -> str:
        """If a prior attempt was rejected, inject the reviewer's feedback at
        the top of the prompt so the agent can iterate on it. Clears the
        persisted feedback immediately so it's consumed only once and doesn't
        leak into subsequent unrelated runs."""
        if not item.feedback:
            return prompt
        hint = ("Produce a revised plan." if phase == "plan"
                else "Address this feedback during execution.")
        # Cap reviewer feedback so a giant pasted log/diff doesn't reseed
        # tens of thousands of fresh tokens on every retry. Head + tail keeps
        # both the user's intent and any concrete error tail.
        feedback = item.feedback
        max_len = 800
        if len(feedback) > max_len:
            half = (max_len - 20) // 2
            feedback = (
                f"{feedback[:half]}\n\n[…truncated…]\n\n{feedback[-half:]}"
            )
        block = (
            "REVIEWER FEEDBACK FROM A PREVIOUS REJECTED ATTEMPT:\n"
            f"{feedback}\n\n"
            f"{hint}\n\n"
        )
        # Consume feedback — clear it so the NEXT run starts clean.
        # Direct SQL so we don't have to fake a status transition.
        self.store.conn.execute(
            "UPDATE items SET feedback = NULL WHERE id = ?", (item.id,)
        )
        return block + prompt

    def _transcript_path(self, item: StoredItem, phase_tag: str) -> Path:
        return (
            self.config.project_root / ".agentor" / "transcripts"
            / f"{item.id}.{phase_tag}.log"
        )

    def _execute_transcript_path(self, item: StoredItem) -> Path:
        return self._transcript_path(item, "execute")

    def _invoke_claude(
        self, item: StoredItem, worktree: Path, prompt: str,
        model_override: str | None = None,
    ) -> tuple[str, str]:
        """Run claude and stream its stream-json events live. Publishes
        partial usage/iterations to the DB on each assistant turn so the
        dashboard's CTX% and token counters update in real time instead of
        blocking until exit. Returns (summary, raw_stdout).

        Legacy non-streaming commands (no stream-json) still work — we detect
        the output format and fall back to blocking subprocess.run.

        `model_override` replaces `agent.model` for this single invocation
        so the execute phase can run on a different tier than the plan.
        Custom `agent.command` templates that drop the `{model}`
        placeholder silently skip the override — matches the existing
        `{settings_path}` opt-out pattern."""
        template = self.config.agent.command or ClaudeProvider.default_command()
        legacy_prompt_arg = _command_has_prompt_placeholder(template)
        guardrails = self.write_tool_guardrails(self.config, item.id)
        model = model_override or self.config.agent.model
        args = [
            a.format(prompt=prompt, model=model, **guardrails)
            for a in template
        ]

        # Session id: pre-generated + persisted before the child starts so a
        # mid-run crash can be recovered via `claude --resume <id>` on the
        # next agentor startup. Reuse a previously-persisted one if present.
        # `session_id` is Claude-CLI wire vocabulary; we store it
        # provider-neutrally as agent_ref on the item.
        session_id = item.agent_ref or str(uuid.uuid4())
        had_session = bool(item.agent_ref)
        if not had_session:
            self.store.transition(
                item.id, ItemStatus.WORKING, agent_ref=session_id,
                note="session id assigned",
            )
        if had_session:
            args += ["--resume", session_id]
        else:
            args += ["--session-id", session_id]

        phase_tag = "execute" if had_session else "plan"
        transcript_path = self._transcript_path(item, phase_tag)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        # Clear once before the retry loop so the first attempt's log isn't
        # contaminated by a prior run; retry attempts append so RETRY markers
        # survive.
        transcript_path.write_text("")

        streaming = "stream-json" in args

        def invoke() -> tuple[str, str]:
            if streaming:
                stdin_prompt = None if legacy_prompt_arg else prompt
                return self._invoke_claude_streaming(
                    item, args, worktree, transcript_path, phase_tag,
                    stdin_prompt=stdin_prompt,
                )
            return self._invoke_claude_blocking(
                item, args, worktree, transcript_path,
            )

        return _retry_transient(
            invoke, transcript_path=transcript_path,
            retries=self.config.agent.transient_retries,
            timeout_seconds=self.config.agent.timeout_seconds,
        )

    def _invoke_claude_blocking(
        self, item: StoredItem, args: list[str], worktree: Path,
        transcript_path: Path,
    ) -> tuple[str, str]:
        # Use Popen + communicate (not subprocess.run) so the process is
        # registered with proc_registry and can be killed on shutdown via
        # SIGTERM to its process group. Otherwise a long blocking run would
        # orphan the claude child when agentor exits.
        try:
            p = subprocess.Popen(
                args, cwd=worktree,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"claude CLI not found. First arg was: {args[0]!r}. "
                f"Install claude or set agent.command in agentor.toml."
            )
        if self.proc_registry is not None:
            self.proc_registry.register(item.id, p)
        try:
            try:
                stdout, stderr = p.communicate(
                    timeout=self.config.agent.timeout_seconds,
                )
            except subprocess.TimeoutExpired as e:
                _signal_group(p, signal.SIGKILL)
                stdout, stderr = p.communicate()
                e_stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
                e_stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
                with transcript_path.open("a") as fh:
                    fh.write(
                        f"TIMEOUT after {self.config.agent.timeout_seconds}s\n\n"
                        f"stdout:\n{stdout or e_stdout}\n\n"
                        f"stderr:\n{stderr or e_stderr}\n"
                    )
                raise RuntimeError(
                    f"claude timed out after {self.config.agent.timeout_seconds}s"
                )
        finally:
            if self.proc_registry is not None:
                self.proc_registry.unregister(item.id)

        with transcript_path.open("a") as fh:
            fh.write(
                f"exit: {p.returncode}\n"
                f"args: {args}\n\n"
                f"stdout:\n{stdout}\n\nstderr:\n{stderr}\n"
            )
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("claude killed: agentor shutdown")
        if p.returncode != 0:
            tail = (stderr or stdout)[-500:].strip()
            raise RuntimeError(f"claude exited {p.returncode}: {tail}")
        summary = _derive_summary(worktree, stdout, item.title)
        self._last_usage = _parse_usage(stdout)
        return summary, stdout

    def _invoke_claude_streaming(
        self, item: StoredItem, args: list[str], worktree: Path,
        transcript_path: Path, phase_tag: str,
        stdin_prompt: str | None = None,
    ) -> tuple[str, str]:
        """Launch claude with Popen, read stdout line-by-line, parse each
        stream-json event, and publish live usage/iterations to the store.

        When `stdin_prompt` is set, claude is invoked with stream-json input
        mode — the prompt is framed as an initial `user` JSONL and written to
        stdin, and stdin is held open so the checkpoint emitter can inject
        additional user-role nudges mid-session."""
        state = _StreamState(item_id=item.id, phase=phase_tag)
        max_turns = int(self.config.agent.max_turns or 0)
        ckpt_cfg = CheckpointConfig(
            soft_turns=int(self.config.agent.turn_checkpoint_soft or 0),
            hard_turns=int(self.config.agent.turn_checkpoint_hard or 0),
            output_tokens=int(self.config.agent.output_token_checkpoint or 0),
            soft_template=self.config.agent.checkpoint_soft_template,
            hard_template=self.config.agent.checkpoint_hard_template,
            tokens_template=self.config.agent.checkpoint_tokens_template,
        )
        emitter = None if ckpt_cfg.all_disabled() else CheckpointEmitter(ckpt_cfg)

        # Mid-run injection is gated on BOTH the provider capability
        # declaration AND the environmental preconditions (streaming
        # stdin available = non-legacy prompt path). The capability
        # flag is the declarative source of truth per
        # `ProviderCapabilities.supports_mid_run_injection`; the
        # `stdin_prompt is not None` check preserves the legacy-prompt
        # opt-out (`{prompt}` argv placeholder → no stdin pipe to
        # write into).
        allow_injection = (
            self.capabilities.supports_mid_run_injection
            and stdin_prompt is not None
        )
        stdin_holder: ChildStdinHolder | None = None
        stdin_payload: str | None = None
        if allow_injection:
            stdin_holder = ChildStdinHolder()
            stdin_payload = _claude_initial_stdin_payload(stdin_prompt)

        def on_event(ev: dict) -> str | None:
            state.ingest(ev)
            if ev.get("type") in ("assistant", "result"):
                self._publish_live(
                    item.id, state, final=ev.get("type") == "result",
                )
            if emitter is not None and ev.get("type") == "assistant":
                nudges = emitter.observe(
                    state.num_turns, state.total_output_tokens,
                )
                for nudge in nudges:
                    _write_checkpoint_marker(
                        transcript_path, state.num_turns,
                        state.total_output_tokens, nudge,
                        injected=allow_injection,
                    )
                    if stdin_holder is not None:
                        line = json.dumps({
                            "type": "user",
                            "message": {"role": "user", "content": nudge},
                        })
                        stdin_holder.write_line(line)
            # Claude with `--input-format stream-json` keeps stdin open and
            # waits for the next user message after emitting `result`. Close
            # stdin so the CLI sees EOF and exits; otherwise the outer
            # `p.stdout.readline()` loop blocks forever and the item stays
            # WORKING until timeout.
            if ev.get("type") == "result" and stdin_holder is not None:
                stdin_holder.close()
            # Runaway guard. No cost cap — subscription-billed plans make
            # mid-stream dollar accounting misleading; max_turns is enough.
            if max_turns and state.num_turns >= max_turns:
                return f"max_turns={max_turns} hit ({state.num_turns} turns)"
            return None

        stdout_text, stderr_text, returncode, timed_out, cap_reason = (
            _run_stream_json_subprocess(
                args=args, cwd=worktree,
                timeout_seconds=self.config.agent.timeout_seconds,
                transcript_path=transcript_path,
                proc_registry=self.proc_registry,
                item_key=item.id,
                fnfe_hint=(
                    f"claude CLI not found. First arg was: {args[0]!r}. "
                    f"Install claude or set agent.command in agentor.toml."
                ),
                on_event=on_event,
                stdin_payload=stdin_payload,
                stdin_holder=stdin_holder,
            )
        )
        if timed_out:
            raise RuntimeError(
                f"claude timed out after {self.config.agent.timeout_seconds}s"
            )
        if cap_reason:
            raise RuntimeError(f"claude killed: {cap_reason}")
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("claude killed: agentor shutdown")
        if returncode not in (0, None):
            tail = (stderr_text or stdout_text)[-500:].strip()
            raise RuntimeError(f"claude exited {returncode}: {tail}")
        self._last_usage = state.envelope()
        summary = _derive_summary(worktree, stdout_text, item.title)
        return summary, stdout_text

    def _publish_live(
        self, item_id: str, state: "_StreamState", *, final: bool = False,
    ) -> None:
        """Write the current partial envelope to result_json so the dashboard
        can show live CTX% / tokens without waiting for exit."""
        now_ns = time.monotonic_ns()
        if not final and now_ns - state.last_publish_ns < _PUBLISH_INTERVAL_NS:
            return
        try:
            blob = json.dumps({
                "phase": state.phase,
                "live": True,
                **state.envelope(),
            })
            self.store.update_result_json(item_id, blob)
            state.last_publish_ns = now_ns
        except Exception:
            # A publish failure shouldn't crash the run. Dashboard just
            # stays on the previous snapshot.
            pass

class CodexRunner(Runner):
    """Spawns a headless `codex exec` subprocess inside the worktree. Keeps
    the same two-phase flow as Claude by persisting the `thread_id` emitted
    by Codex and resuming it during execution."""

    capabilities: ProviderCapabilities = CODEX_CAPS

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prior = _parse_result_json(item.result_json)
        if prior.get("phase") == "plan":
            return self._do_execute(item, worktree, prior.get("plan", ""))
        if self.config.agent.single_phase:
            return self._do_execute(
                item, worktree, "(no plan; spec is in the task body)",
            )
        return self._do_plan(item, worktree)

    def warn_silent_guardrails(
        self, config: Config, log: Callable[[str], None],
    ) -> None:
        """Codex CLI has no hook channel — `large_file_line_threshold`
        and `enforce_grep_head_limit` are dead knobs under this runner.
        Log one line per tripped knob so operators learn at startup, not
        mid-review, that their guardrail config is silently ignored."""
        defaults = AgentConfig()
        agent = config.agent
        if agent.large_file_line_threshold != defaults.large_file_line_threshold:
            log(
                "codex runner: agent.large_file_line_threshold="
                f"{agent.large_file_line_threshold} has no effect — "
                "codex has no hook channel"
            )
        if agent.enforce_grep_head_limit != defaults.enforce_grep_head_limit:
            log(
                "codex runner: agent.enforce_grep_head_limit="
                f"{agent.enforce_grep_head_limit} has no effect — "
                "codex has no hook channel"
            )

    def _do_plan(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prompt = self.config.agent.plan_prompt_template.format(
            title=item.title, body=item.body, source_file=item.source_file,
        )
        prompt = self._prepend_feedback(item, prompt, phase="plan")
        self._last_execute_model = None
        self._last_execute_model_source = None
        self._last_plan_suggestion = None
        output_path = self._last_message_path(item, "plan")
        _, stdout = self._invoke_codex(
            item, worktree, prompt, output_path,
        )
        plan_text = _read_output_message(output_path)
        if not plan_text:
            plan_text = (
                getattr(self, "_last_usage", None) or {}
            ).get("result") or _extract_codex_result(stdout) or "(no plan text returned)"
        self._last_phase = "plan"
        self._last_questions = _extract_plan_questions(plan_text)
        return plan_text, []

    def _do_execute(
        self, item: StoredItem, worktree: Path, plan: str,
    ) -> tuple[str, list[str]]:
        prompt = self.config.agent.execute_prompt_template.format(
            title=item.title, body=item.body, source_file=item.source_file,
            plan=plan,
        )
        prompt += _mark_done_instruction(self.config, item.source_file)
        prompt = self._prepend_feedback(item, prompt, phase="execute")
        prompt = _prepend_plan_answers(item, prompt)
        alias, source = _resolve_execute_tier(
            self.config, self.provider, item, plan,
        )
        self._last_execute_model = alias
        self._last_execute_model_source = source
        self._last_plan_suggestion = _parse_execute_tier(plan, whitelist=None)
        model_override = self.provider.model_aliases.get(alias)
        output_path = self._last_message_path(item, "execute")
        summary, stdout = self._invoke_codex(
            item, worktree, prompt, output_path,
            model_override=model_override,
        )
        files = _list_changes(worktree, self.config.git.base_branch)
        final_message = _read_output_message(output_path)
        if final_message:
            summary = final_message
        summary = _derive_summary(worktree, summary or stdout, item.title)
        self._last_phase = "execute"
        return summary, files

    def _prepend_feedback(self, item: StoredItem, prompt: str, phase: str) -> str:
        if not item.feedback:
            return prompt
        hint = ("Produce a revised plan." if phase == "plan"
                else "Address this feedback during execution.")
        feedback = item.feedback
        max_len = 800
        if len(feedback) > max_len:
            half = (max_len - 20) // 2
            feedback = (
                f"{feedback[:half]}\n\n[…truncated…]\n\n{feedback[-half:]}"
            )
        block = (
            "REVIEWER FEEDBACK FROM A PREVIOUS REJECTED ATTEMPT:\n"
            f"{feedback}\n\n"
            f"{hint}\n\n"
        )
        self.store.conn.execute(
            "UPDATE items SET feedback = NULL WHERE id = ?", (item.id,)
        )
        return block + prompt

    def _last_message_path(self, item: StoredItem, phase_tag: str) -> Path:
        return (
            self.config.project_root / ".agentor" / "transcripts"
            / f"{item.id}.{phase_tag}.last-message.txt"
        )

    def _invoke_codex(
        self, item: StoredItem, worktree: Path, prompt: str, output_path: Path,
        model_override: str | None = None,
    ) -> tuple[str, str]:
        phase_tag = "execute" if item.agent_ref else "plan"
        transcript_path = (
            self.config.project_root / ".agentor" / "transcripts"
            / f"{item.id}.{phase_tag}.log"
        )
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        # Clear transcript once before the retry loop; RETRY markers and
        # per-attempt logs are appended so the full history survives.
        transcript_path.write_text("")

        def invoke() -> tuple[str, str]:
            # Re-fetch the item each attempt so a thread_id persisted by a
            # prior failed attempt flips us onto the resume template (and we
            # don't orphan the codex thread created mid-stream).
            fresh = self.store.get(item.id) or item
            args = self._codex_args(
                fresh, prompt, output_path, model_override=model_override,
            )
            return self._invoke_codex_jsonl(
                fresh, args, worktree, transcript_path, output_path, phase_tag,
            )

        return _retry_transient(
            invoke, transcript_path=transcript_path,
            retries=self.config.agent.transient_retries,
            timeout_seconds=self.config.agent.timeout_seconds,
        )

    def _codex_args(
        self, item: StoredItem, prompt: str, output_path: Path,
        model_override: str | None = None,
    ) -> list[str]:
        values = {
            "prompt": prompt,
            "model": model_override or self.config.agent.model,
            # Placeholder key stays `session_id` to match the operator-facing
            # `{session_id}` token in `agent.command` / `agent.resume_command`;
            # value comes from the provider-neutral agent_ref column.
            "session_id": item.agent_ref or "",
            "output_path": str(output_path),
        }
        if item.agent_ref:
            tmpl = (
                self.config.agent.resume_command
                or CodexProvider.default_resume_command()
            )
        else:
            tmpl = self.config.agent.command or CodexProvider.default_command()
        return [a.format(**values) for a in tmpl]

    def _invoke_codex_jsonl(
        self, item: StoredItem, args: list[str], worktree: Path,
        transcript_path: Path, output_path: Path, phase_tag: str,
    ) -> tuple[str, str]:
        state = _CodexStreamState(item_id=item.id, phase=phase_tag)
        # Codex CLI has no stream-json stdin mode — the prompt is baked into
        # argv at spawn — so mid-run injection isn't possible. We still run
        # the emitter as a passive observer: when a threshold crosses, a
        # `checkpoint-observed-dry-run` marker is appended to the transcript
        # so post-hoc analysis can see where a nudge would have landed.
        # Token threshold is dormant on codex (output_tokens always 0 —
        # codex JSONL exposes no per-turn output_tokens); turn thresholds
        # are the minimum-viable gate.
        ckpt_cfg = CheckpointConfig(
            soft_turns=int(self.config.agent.turn_checkpoint_soft or 0),
            hard_turns=int(self.config.agent.turn_checkpoint_hard or 0),
            output_tokens=int(self.config.agent.output_token_checkpoint or 0),
            soft_template=self.config.agent.checkpoint_soft_template,
            hard_template=self.config.agent.checkpoint_hard_template,
            tokens_template=self.config.agent.checkpoint_tokens_template,
        )
        emitter = None if ckpt_cfg.all_disabled() else CheckpointEmitter(ckpt_cfg)
        # Mutable holder so the callback can swap in a refreshed StoredItem
        # after persisting the thread_id mid-stream.
        item_ref = [item]

        def on_event(ev: dict) -> str | None:
            state.ingest(ev)
            cur = item_ref[0]
            if state.session_id and state.session_id != cur.agent_ref:
                self.store.transition(
                    cur.id, ItemStatus.WORKING,
                    agent_ref=state.session_id,
                    note="session id assigned",
                )
                refreshed = self.store.get(cur.id)
                assert refreshed is not None
                item_ref[0] = refreshed
            self._publish_live(item_ref[0].id, state)
            if emitter is not None and ev.get("type") == "turn.started":
                for nudge in emitter.observe(state.num_turns, 0):
                    _write_checkpoint_marker(
                        transcript_path, state.num_turns, 0, nudge,
                        injected=self.capabilities.supports_mid_run_injection,
                    )
            return None

        stdout_text, stderr_text, returncode, timed_out, _ = (
            _run_stream_json_subprocess(
                args=args, cwd=worktree,
                timeout_seconds=self.config.agent.timeout_seconds,
                transcript_path=transcript_path,
                proc_registry=self.proc_registry,
                item_key=item.id,
                fnfe_hint=(
                    f"codex CLI not found. First arg was: {args[0]!r}. "
                    f"Install codex or set agent.command/agent.resume_command in agentor.toml."
                ),
                on_event=on_event,
            )
        )
        if timed_out:
            raise RuntimeError(
                f"codex timed out after {self.config.agent.timeout_seconds}s"
            )
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("codex killed: agentor shutdown")
        if returncode not in (0, None):
            tail = (stderr_text or stdout_text)[-500:].strip()
            raise RuntimeError(f"codex exited {returncode}: {tail}")
        result_text = _read_output_message(output_path) or _extract_codex_result(stdout_text)
        # Flush the final envelope past the throttle — codex JSONL has no
        # single canonical terminal event to gate on in `on_event`, so do it
        # here after the subprocess drains.
        self._publish_live(item_ref[0].id, state, final=True)
        self._last_usage = state.envelope(result_text=result_text)
        return result_text or "", stdout_text

    def _publish_live(
        self, item_id: str, state: "_CodexStreamState", *, final: bool = False,
    ) -> None:
        now_ns = time.monotonic_ns()
        if not final and now_ns - state.last_publish_ns < _PUBLISH_INTERVAL_NS:
            return
        try:
            blob = json.dumps({
                "phase": state.phase,
                "live": True,
                **state.envelope(),
            })
            self.store.update_result_json(item_id, blob)
            state.last_publish_ns = now_ns
        except Exception:
            pass


class _StreamState:
    """Accumulator for claude stream-json events. Builds the same envelope
    shape as the blocking `--output-format json` path (usage, iterations,
    modelUsage, num_turns, stop_reason) so the rest of the dashboard
    doesn't care which mode produced the data."""

    def __init__(self, item_id: str, phase: str):
        self.item_id = item_id
        self.phase = phase
        self.session_id: str | None = None
        self.last_event_at: float | None = None
        self.last_event_type: str | None = None
        self.activity: str | None = None
        self.iterations: list[dict] = []
        self.num_turns: int = 0
        self.stop_reason: str | None = None
        self.duration_ms: int | None = None
        self.duration_api_ms: int | None = None
        # Throttle cursor for _publish_live (coalesces SQLite writes).
        self.last_publish_ns: int = 0
        # modelUsage is keyed by model id, mirroring claude's final envelope.
        # Used for token accounting and context-window detection, not cost.
        self.model_usage: dict[str, dict] = {}
        # Last seen result text (set by the terminal 'result' event).
        self.result_text: str | None = None
        # Cumulative output tokens across all assistant turns. Published
        # so the checkpoint emitter can gate on "doing too much in-context"
        # without recomputing from `iterations` on every event.
        self.total_output_tokens: int = 0
        # Latest-wins capture of any `rate_limit`/`ratelimits`/`anthropic-
        # ratelimit-*` field the claude CLI might surface in future versions.
        # The current CLI strips the response headers, so this stays None in
        # practice and the dashboard falls back to budget-derived %. Kept as
        # a passive harvester so that if Anthropic later exposes quota hints
        # in stream-json, we start recording them without further work.
        self.rate_limits: dict | None = None

    def _harvest_rate_limits(self, ev: dict) -> None:
        """Look for rate-limit hints on the event and nested message/usage
        dicts. Latest-wins — each call overwrites any prior sample. Flat
        dict lookup only (no recursive walk) to keep per-event cost O(1)."""
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else None
        msg_usage = msg.get("usage") if isinstance(msg, dict) else None
        for scope in (ev, msg, ev.get("usage"), msg_usage):
            if not isinstance(scope, dict):
                continue
            for key in ("rate_limit", "rate_limits", "ratelimits",
                        "anthropic-ratelimit", "anthropic_ratelimit"):
                val = scope.get(key)
                if isinstance(val, dict) and val:
                    self.rate_limits = val
                    return

    def ingest(self, ev: dict) -> None:
        etype = ev.get("type")
        self.last_event_at = time.time()
        self.last_event_type = str(etype or "unknown")
        self._harvest_rate_limits(ev)
        if etype == "system" and ev.get("subtype") == "init":
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            self.activity = "session initialized"
            return
        if etype == "assistant":
            msg = ev.get("message") or {}
            usage = msg.get("usage") or {}
            if not isinstance(usage, dict):
                return
            model = msg.get("model") or "unknown"
            self.num_turns += 1
            self.activity = f"assistant turn {self.num_turns} finished on {model}"
            self.iterations.append({
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "cache_read_input_tokens": int(
                    usage.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens", 0) or 0),
                "model": model,
            })
            # Aggregate token usage per model. `contextWindow` is filled in
            # from the terminal `result` event when claude reports it.
            mu = self.model_usage.setdefault(model, {
                "inputTokens": 0, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                "contextWindow": 0,
            })
            mu["inputTokens"] += int(usage.get("input_tokens", 0) or 0)
            mu["outputTokens"] += int(usage.get("output_tokens", 0) or 0)
            mu["cacheReadInputTokens"] += int(
                usage.get("cache_read_input_tokens", 0) or 0)
            mu["cacheCreationInputTokens"] += int(
                usage.get("cache_creation_input_tokens", 0) or 0)
            self.total_output_tokens += int(usage.get("output_tokens", 0) or 0)
            return
        if etype == "result":
            if ev.get("num_turns") is not None:
                self.num_turns = int(ev["num_turns"])
            if ev.get("stop_reason"):
                self.stop_reason = ev["stop_reason"]
                self.activity = f"finished: {self.stop_reason}"
            if ev.get("duration_ms") is not None:
                self.duration_ms = int(ev["duration_ms"])
            if ev.get("duration_api_ms") is not None:
                self.duration_api_ms = int(ev["duration_api_ms"])
            if isinstance(ev.get("modelUsage"), dict):
                # Adopt claude's authoritative per-model breakdown — it
                # reports `contextWindow` which we can't derive ourselves.
                self.model_usage = ev["modelUsage"]
            result = ev.get("result")
            if isinstance(result, str):
                self.result_text = result

    def envelope(self) -> dict:
        """Produce the same envelope shape _parse_usage would build off the
        blocking JSON path, so dashboard code stays agnostic of mode."""
        flat_usage = {
            "input_tokens": sum(i["input_tokens"] for i in self.iterations),
            "output_tokens": sum(i["output_tokens"] for i in self.iterations),
            "cache_read_input_tokens": sum(
                i["cache_read_input_tokens"] for i in self.iterations),
            "cache_creation_input_tokens": sum(
                i["cache_creation_input_tokens"] for i in self.iterations),
        }
        out: dict = {
            "usage": flat_usage,
            "iterations": self.iterations,
            "modelUsage": self.model_usage,
            "num_turns": self.num_turns,
        }
        if self.stop_reason:
            out["stop_reason"] = self.stop_reason
        if self.duration_ms is not None:
            out["duration_ms"] = self.duration_ms
        if self.duration_api_ms is not None:
            out["duration_api_ms"] = self.duration_api_ms
        if self.session_id:
            out["agent_ref"] = self.session_id
        if self.result_text:
            out["result"] = self.result_text
        if self.rate_limits:
            out["rate_limits"] = self.rate_limits
        progress: dict[str, object] = {}
        if self.last_event_at is not None:
            progress["last_event_at"] = self.last_event_at
        if self.last_event_type:
            progress["last_event_type"] = self.last_event_type
        if self.activity:
            progress["activity"] = self.activity
        if progress:
            out["progress"] = progress
        return out


class _CodexStreamState:
    """Minimal JSONL accumulator for `codex exec --json` output."""

    def __init__(self, item_id: str, phase: str):
        self.item_id = item_id
        self.phase = phase
        self.session_id: str | None = None
        self.last_event_at: float | None = None
        self.last_event_type: str | None = None
        self.activity: str | None = None
        self.num_turns: int = 0
        self.result_text: str | None = None
        self.last_error: str | None = None
        # Throttle cursor for _publish_live (coalesces SQLite writes).
        self.last_publish_ns: int = 0

    def ingest(self, ev: dict) -> None:
        etype = ev.get("type")
        self.last_event_at = time.time()
        self.last_event_type = str(etype or "unknown")
        if etype == "thread.started" and ev.get("thread_id"):
            self.session_id = str(ev["thread_id"])
            self.activity = "thread started"
            return
        if etype == "turn.started":
            self.num_turns += 1
            self.activity = f"turn {self.num_turns} started"
            return
        if etype == "error" and ev.get("message"):
            self.last_error = str(ev["message"])
            self.activity = f"error: {self.last_error[:120]}"
            return
        for key in ("message", "last_message", "result"):
            val = ev.get(key)
            if isinstance(val, str) and val.strip():
                self.result_text = val
                snippet = " ".join(val.strip().split())
                self.activity = f"message received: {snippet[:120]}"

    def envelope(self, result_text: str | None = None) -> dict:
        out: dict = {
            "usage": {},
            "iterations": [],
            "modelUsage": {},
            "num_turns": self.num_turns,
        }
        if self.session_id:
            out["agent_ref"] = self.session_id
        if result_text or self.result_text:
            out["result"] = result_text or self.result_text
        if self.last_error:
            out["stop_reason"] = self.last_error
        progress: dict[str, object] = {}
        if self.last_event_at is not None:
            progress["last_event_at"] = self.last_event_at
        if self.last_event_type:
            progress["last_event_type"] = self.last_event_type
        if self.activity:
            progress["activity"] = self.activity
        if progress:
            out["progress"] = progress
        return out


def _extract_result_field(stdout: str) -> str | None:
    """Pull claude's final `result` text from a --output-format json stdout.
    This is what the user should see for plan phase, not a commit message."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        last_open = text.rfind("{")
        if last_open < 0:
            return None
        try:
            obj = json.loads(text[last_open:])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    result = obj.get("result")
    return result if isinstance(result, str) and result.strip() else None


def _extract_codex_result(stdout: str) -> str | None:
    """Best-effort extraction of the final message from Codex JSONL output."""
    last: str | None = None
    for line in (stdout or "").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("last_message", "message", "result"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                last = val
    return last


def _parse_result_json(blob: str | None) -> dict:
    """Safe load of an item's stored result_json. Returns {} on any failure."""
    if not blob:
        return {}
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _prepend_plan_answers(item: StoredItem, prompt: str) -> str:
    """If the reviewer answered any of the agent's plan-phase questions,
    surface their Q/A pairs at the very top of the execute prompt. The
    parallel block to `_prepend_feedback` but fed from `result_json`
    instead of the `feedback` column, and is idempotent — answers live in
    result_json and don't need clearing (the execute phase overwrites the
    blob on completion)."""
    data = _parse_result_json(item.result_json)
    questions = data.get("questions") or []
    answers = data.get("answers") or []
    if not questions or not any(
        isinstance(a, str) and a.strip() for a in answers
    ):
        return prompt
    lines = ["REVIEWER ANSWERS TO YOUR PLAN QUESTIONS:"]
    for i, q in enumerate(questions):
        a = answers[i] if i < len(answers) else ""
        a = a.strip() if isinstance(a, str) else ""
        lines.append(f"- Q: {q}")
        lines.append(
            f"  A: {a if a else '(no answer — proceed with your best judgment)'}"
        )
    lines.append("")
    return "\n".join(lines) + "\n" + prompt


_QUESTIONS_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*open\s+questions?\s*$"
)
_NEXT_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+\S")
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_EXECUTE_TIER_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*execute\s+tier\s*$"
)
_SUGGESTED_MODEL_RE = re.compile(
    r"(?im)^\s*suggested_model\s*:\s*([A-Za-z0-9_-]+)\s*$"
)


def _parse_execute_tier(
    plan_text: str, whitelist: list[str] | None = None,
) -> str | None:
    """Extract the plan's nominated execute-phase model tier from the
    `## Execute tier` trailer. Returns a lowercase alias (e.g. `"haiku"`)
    when the trailer is present AND the suggestion is in `whitelist` (any
    case). Returns None on missing heading, malformed body, or whitelist
    miss — callers treat None as a soft fallback to the global default.
    No raise.

    `whitelist=None` disables the gate (any well-formed alias is
    returned). Callers that want a hard gate pass the active provider's
    `model_aliases.keys()` — `_resolve_execute_tier` does this."""
    if not plan_text:
        return None
    m = _EXECUTE_TIER_HEADING_RE.search(plan_text)
    if not m:
        return None
    rest = plan_text[m.end():]
    nxt = _NEXT_HEADING_RE.search(rest)
    block = rest[:nxt.start()] if nxt else rest
    sm = _SUGGESTED_MODEL_RE.search(block)
    if not sm:
        return None
    alias = sm.group(1).strip().lower()
    if whitelist is None:
        return alias
    allowed_lower = {a.lower() for a in whitelist}
    if alias not in allowed_lower:
        return None
    return alias


def _resolve_execute_tier(
    config: Config, provider: Provider, item: StoredItem, prior_plan: str,
) -> tuple[str, str]:
    """Resolve the alias + source for the execute-phase model dispatch.

    Precedence:
      1. `@model:<alias>` tag on the item (operator override, wins
         unconditionally — but still whitelist-gated so typos like
         `@model: claude-haiku-4-5` (full ID) fall through with a warning
         rather than silently pinning an unintended value).
      2. Plan's `## Execute tier` trailer — only when
         `agent.auto_execute_model=True`.
      3. Global default — the alias matching `agent.model` per the
         provider's reverse lookup, else the first alias in
         `provider.model_aliases`, else the raw `agent.model` string.

    The whitelist is `agent.execute_model_whitelist` when non-empty,
    else the active provider's full `model_aliases.keys()`. Alias
    vocabulary is per-provider — `@model:haiku` on a Codex-routed item
    falls through with a warning because Codex ships `mini/full`.

    Returns `(alias, source)` where `source ∈ {"tag","plan","default"}`.
    """
    configured = list(config.agent.execute_model_whitelist or [])
    whitelist = configured or list(provider.model_aliases.keys())
    allowed = {a.lower() for a in whitelist}

    tag_raw = (item.tags or {}).get("model")
    if tag_raw:
        tag = tag_raw.strip().lower()
        if tag in allowed:
            return tag, "tag"
        print(
            f"[runner] ignoring @model tag {tag_raw!r} on item {item.id} — "
            f"not in {provider.__class__.__name__} alias whitelist {sorted(allowed)!r}. "
            "Use a short alias, not the full model id.",
        )

    if config.agent.auto_execute_model and prior_plan:
        nominated = _parse_execute_tier(prior_plan, whitelist)
        if nominated:
            return nominated, "plan"

    # Fallback: map agent.model back to an alias via the provider's
    # reverse lookup so the recorded value matches the alias vocabulary
    # used elsewhere. If the model id isn't known to this provider, fall
    # through to its first-listed alias rather than a hardcoded "opus"
    # (which would be wrong under any non-Claude provider).
    default_alias = provider.model_to_alias(config.agent.model)
    if default_alias is None:
        default_alias = (
            next(iter(provider.model_aliases)) if provider.model_aliases
            else config.agent.model
        )
    return default_alias, "default"


def _extract_plan_questions(plan: str) -> list[str]:
    """Return `?`-terminated bullets under the first `## Open Questions`
    heading (any level, case-insensitive). Returns [] when the heading is
    absent. Only bulleted lines (`-`, `*`, `+`, `1.`, `1)`) are considered;
    unbulleted prose in the block is ignored. Lines that don't end in `?`
    are dropped — guards against the agent stashing non-questions under the
    heading."""
    if not plan:
        return []
    m = _QUESTIONS_HEADING_RE.search(plan)
    if not m:
        return []
    rest = plan[m.end():]
    nxt = _NEXT_HEADING_RE.search(rest)
    block = rest[:nxt.start()] if nxt else rest
    out: list[str] = []
    for raw in block.splitlines():
        match = _BULLET_LINE_RE.match(raw)
        if not match:
            continue
        text = match.group(1).strip()
        if text.endswith("?"):
            out.append(text)
    return out


def _parse_usage(stdout: str) -> dict | None:
    """Extract the usage envelope from a `--output-format json` claude run.
    Returns a dict with `usage`, `modelUsage`, `num_turns`, `duration_ms`,
    `duration_api_ms`, `stop_reason` when available. Parses defensively —
    stdout may be plain text or have trailing log noise. Returns None if
    nothing usable can be pulled out."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        last_open = text.rfind("{")
        if last_open < 0:
            return None
        try:
            obj = json.loads(text[last_open:])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    # Flatten fields the dashboard cares about. `usage` stays as a nested
    # dict because existing readers (_tokens_used variants) pick from it.
    out: dict = {}
    # `session_id` kept for back-compat with result_json blobs written before
    # the agent_ref rename; new writes use `agent_ref`.
    for k in ("usage", "modelUsage", "num_turns",
              "duration_ms", "duration_api_ms", "stop_reason",
              "agent_ref", "session_id", "result"):
        if k in obj and obj[k] is not None:
            out[k] = obj[k]
    return out or None


def _read_output_message(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    return text or None


def _claude_initial_stdin_payload(prompt: str) -> str:
    """Frame the initial prompt as a single `user` stream-json line.
    Trailing newline included — the runner writes this verbatim to claude's
    stdin before the read loop starts."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": prompt},
    }) + "\n"


def _command_has_prompt_placeholder(args: list[str]) -> bool:
    """Legacy command templates carry `{prompt}` as an arg; the new
    stream-json stdin path does not. Used to pick single-shot vs
    injection-capable invocation."""
    return any("{prompt}" in a for a in args)


def _read_hook_path() -> Path:
    """Absolute path to the shipped PreToolUse Read hook script."""
    return (Path(__file__).resolve().parent / "read_hook.py")


def _grep_hook_path() -> Path:
    """Absolute path to the shipped PreToolUse Grep hook script."""
    return (Path(__file__).resolve().parent / "grep_hook.py")


def write_claude_settings(
    config: Config, item_id: str,
) -> Path:
    """Write a Claude settings JSON registering the bundled PreToolUse
    hooks into `<project>/.agentor/claude-settings/<item_id>.json`. Each
    enforcement can be toggled independently:
      - Read offset/limit gate: `agent.large_file_line_threshold` (>0).
      - Grep head_limit gate: `agent.enforce_grep_head_limit`.
    The file always exists (claude needs a readable --settings path); if
    every toggle is off, the hooks list is simply empty."""
    threshold = int(config.agent.large_file_line_threshold or 0)
    enforce_grep = bool(config.agent.enforce_grep_head_limit)
    settings_dir = (
        config.project_root / ".agentor" / "claude-settings"
    )
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / f"{item_id}.json"
    pretool: list[dict] = []
    if threshold > 0:
        read_cmd = (
            f"AGENTOR_READ_THRESHOLD={threshold} "
            f"python3 {_read_hook_path()}"
        )
        pretool.append({
            "matcher": "Read",
            "hooks": [{"type": "command", "command": read_cmd}],
        })
    if enforce_grep:
        grep_cmd = f"python3 {_grep_hook_path()}"
        pretool.append({
            "matcher": "Grep",
            "hooks": [{"type": "command", "command": grep_cmd}],
        })
    settings: dict = {"hooks": {"PreToolUse": pretool} if pretool else {}}
    settings_path.write_text(json.dumps(settings, indent=2))
    return settings_path


def _mark_done_instruction(config: Config, source_file: str) -> str:
    """Idea-file housekeeping. Frontmatter mode only — in checkbox/heading
    modes the source is a shared list and whole-file deletion would take
    sibling items with it. Returns an empty string when not applicable so
    the caller can append unconditionally."""
    if not (config.parsing.mode == "frontmatter" and source_file):
        return ""
    return (
        "\n\nSource-file removal (mandatory):\n"
        f"This item originates from `{source_file}`. Delete that source "
        f"markdown when the item is done — include `git rm {source_file}` "
        "(or equivalent delete + `git add`) in the SAME final commit as "
        "your implementation, one commit, not a trailing cleanup commit.\n"
    )


def _list_changes(worktree: Path, base_branch: str) -> list[str]:
    committed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}..HEAD"],
        cwd=worktree, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    uncommitted_raw = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree, capture_output=True, text=True,
    ).stdout.splitlines()
    uncommitted = [ln[3:] for ln in uncommitted_raw if len(ln) > 3]
    return sorted(set(committed + uncommitted))


def _derive_summary(worktree: Path, stdout: str, title: str) -> str:
    """Prefer AGENT_SUMMARY.md in worktree, else last commit message,
    else last non-empty stdout line."""
    summary_file = worktree / "AGENT_SUMMARY.md"
    if summary_file.exists():
        return summary_file.read_text().strip()[:500]
    cp = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=worktree,
        capture_output=True, text=True,
    )
    msg = cp.stdout.strip()
    if msg:
        return msg[:500]
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else f"(no summary) {title}")[:500]


def make_runner(config: Config, store: Store) -> Runner:
    kind = config.agent.runner.lower()
    if kind == "stub":
        return StubRunner(config, store)
    if kind == "claude":
        return ClaudeRunner(config, store)
    if kind == "codex":
        return CodexRunner(config, store)
    raise ValueError(f"unknown agent.runner: {kind!r}")
