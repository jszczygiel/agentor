import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .slug import slugify
from .store import Store, StoredItem


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
    """Detect CLI resume failures caused by a missing persisted session."""
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

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        # Set by Daemon after construction. Allows in-flight subprocesses to
        # be killed on shutdown rather than orphaned.
        self.proc_registry: ProcRegistry | None = None
        self.stop_event: threading.Event | None = None

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        """Perform the agent's work inside the worktree. Return (summary, files_changed).
        Subclasses override. The base class commits no changes — committer does that."""
        raise NotImplementedError

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

        # Resume path: session_id persisted + worktree still on disk → skip
        # teardown/recreate so the agent picks up where it left off. Otherwise
        # do the normal pre-flight nuke so stale state doesn't leak.
        resume = bool(item.session_id) and wt_path.exists()
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
                    worktree_path=None, branch=None, session_id=None,
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
                if _is_dead_session_error(last_error) and item.session_id:
                    # The provider lost the session. Resuming with the same id
                    # will keep failing on every attempt until rejection —
                    # drop the session_id, refund the attempt, and bounce
                    # back to QUEUED so the next dispatch starts a fresh
                    # session. result_json (with the approved plan) is
                    # kept so we don't make the user re-approve.
                    git_ops.worktree_remove(repo, wt_path, force=True)
                    self.store.transition(
                        item.id, ItemStatus.QUEUED,
                        worktree_path=None, branch=None, session_id=None,
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
                    worktree_path=None, branch=None, session_id=None,
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
            else:
                # Carry the approved plan forward on the final result.
                prior = _parse_result_json(item.result_json)
                if prior.get("plan"):
                    result["plan"] = prior["plan"]
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
    """Test runner that writes a trivial AGENT_NOTE.md. Proves the pipeline
    end-to-end without spawning Claude."""

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        note_path = worktree / f".agentor-note-{item.id[:8]}.md"
        note_path.write_text(
            f"# Stub agent note\n\n"
            f"Item: {item.title}\n\n"
            f"Body:\n{item.body}\n\n"
            f"(This is a stub runner. Replace with real agent.)\n"
        )
        summary = f"stub: added note for '{item.title}'"
        return summary, [str(note_path.relative_to(worktree))]


class ClaudeRunner(Runner):
    """Spawns a headless `claude -p` subprocess inside the worktree. Runs in
    two phases tied together by session_id:

    1) plan — agent writes a development plan (no code changes), item stops at
       AWAITING_PLAN_REVIEW for human approval.
    2) execute — on approval the item returns to QUEUED, gets re-claimed, and
       resumes the same claude session to implement and commit.
    """

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prior = _parse_result_json(item.result_json)
        if prior.get("phase") == "plan":
            return self._do_execute(item, worktree, prior.get("plan", ""))
        if self.config.agent.single_phase:
            return self._do_execute(
                item, worktree, "(no plan; spec is in the task body)",
            )
        return self._do_plan(item, worktree)

    def _do_plan(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prompt = self.config.agent.plan_prompt_template.format(
            title=item.title, body=item.body, source_file=item.source_file,
        )
        prompt = self._prepend_feedback(item, prompt, phase="plan")
        _, stdout = self._invoke_claude(item, worktree, prompt)
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
        summary, stdout = self._invoke_claude(item, worktree, prompt)
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

    def _invoke_claude(
        self, item: StoredItem, worktree: Path, prompt: str,
    ) -> tuple[str, str]:
        """Run claude and stream its stream-json events live. Publishes
        partial usage/iterations to the DB on each assistant turn so the
        dashboard's CTX% and token counters update in real time instead of
        blocking until exit. Returns (summary, raw_stdout).

        Legacy non-streaming commands (no stream-json) still work — we detect
        the output format and fall back to blocking subprocess.run."""
        args = [
            a.format(prompt=prompt, model=self.config.agent.model)
            for a in (self.config.agent.command or _default_claude_command())
        ]

        # Session id: pre-generated + persisted before the child starts so a
        # mid-run crash can be recovered via `claude --resume <id>` on the
        # next agentor startup. Reuse a previously-persisted one if present.
        session_id = item.session_id or str(uuid.uuid4())
        had_session = bool(item.session_id)
        if not had_session:
            self.store.transition(
                item.id, ItemStatus.WORKING, session_id=session_id,
                note="session id assigned",
            )
        if had_session:
            args += ["--resume", session_id]
        else:
            args += ["--session-id", session_id]

        phase_tag = "execute" if had_session else "plan"
        transcript_path = (
            self.config.project_root / ".agentor" / "transcripts"
            / f"{item.id}.{phase_tag}.log"
        )
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        streaming = "stream-json" in args
        if streaming:
            return self._invoke_claude_streaming(
                item, args, worktree, transcript_path, phase_tag,
            )
        return self._invoke_claude_blocking(
            item, args, worktree, transcript_path,
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
                transcript_path.write_text(
                    f"TIMEOUT after {self.config.agent.timeout_seconds}s\n\n"
                    f"stdout:\n{stdout or e.stdout or ''}\n\n"
                    f"stderr:\n{stderr or e.stderr or ''}\n"
                )
                raise RuntimeError(
                    f"claude timed out after {self.config.agent.timeout_seconds}s"
                )
        finally:
            if self.proc_registry is not None:
                self.proc_registry.unregister(item.id)

        transcript_path.write_text(
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
    ) -> tuple[str, str]:
        """Launch claude with Popen, read stdout line-by-line, parse each
        stream-json event, and publish live usage/iterations to the store."""
        import threading
        try:
            # start_new_session=True puts claude (and anything it spawns)
            # in its own process group so the daemon can SIGTERM the whole
            # tree on shutdown via os.killpg. Without this, killing the
            # Popen leaves grand-children (sub-agents, bash, git) running.
            p = subprocess.Popen(
                args, cwd=worktree,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"claude CLI not found. First arg was: {args[0]!r}. "
                f"Install claude or set agent.command in agentor.toml."
            )
        if self.proc_registry is not None:
            self.proc_registry.register(item.id, p)

        # Kill the child on timeout. Timer fires from its own thread; using
        # p.kill() (SIGKILL) is safe because the main loop will detect EOF
        # on stdout and exit cleanly.
        timed_out = threading.Event()

        def _on_timeout():
            timed_out.set()
            try:
                p.kill()
            except Exception:
                pass

        timer = threading.Timer(
            self.config.agent.timeout_seconds, _on_timeout,
        )
        timer.daemon = True
        timer.start()

        # Drain stderr on a background thread so the child can't deadlock
        # writing to a full stderr buffer. Accumulate into a list for the
        # transcript.
        stderr_chunks: list[str] = []

        def _drain_stderr():
            try:
                for line in iter(p.stderr.readline, ""):
                    stderr_chunks.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        state = _StreamState(item_id=item.id, phase=phase_tag)
        stdout_buf: list[str] = []
        cap_reason: str | None = None
        max_turns = int(self.config.agent.max_turns or 0)
        transcript_lines = [f"args: {args}\n", "stdout:\n"]
        transcript_path.write_text("".join(transcript_lines))
        try:
            for line in iter(p.stdout.readline, ""):
                stdout_buf.append(line)
                with transcript_path.open("a") as fh:
                    fh.write(line)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                state.ingest(ev)
                # Publish to DB on every assistant turn and on the final
                # result event. Cheaper than per-line, enough for live UX.
                if ev.get("type") in ("assistant", "result"):
                    self._publish_live(item.id, state)
                # Runaway guard — stop if the agent is looping past its
                # turn budget. No cost cap: a user-level subscription
                # makes mid-stream dollar accounting misleading, and
                # max_turns already bounds runaway behaviour effectively.
                if max_turns and state.num_turns >= max_turns:
                    cap_reason = (
                        f"max_turns={max_turns} hit ({state.num_turns} turns)"
                    )
                if cap_reason:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    break
            p.wait(timeout=5)
        finally:
            timer.cancel()
            if self.proc_registry is not None:
                self.proc_registry.unregister(item.id)
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
        if timed_out.is_set():
            raise RuntimeError(
                f"claude timed out after {self.config.agent.timeout_seconds}s"
            )
        if cap_reason:
            raise RuntimeError(f"claude killed: {cap_reason}")
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("claude killed: agentor shutdown")
        if p.returncode not in (0, None):
            tail = (stderr_text or stdout_text)[-500:].strip()
            raise RuntimeError(f"claude exited {p.returncode}: {tail}")
        self._last_usage = state.envelope()
        summary = _derive_summary(worktree, stdout_text, item.title)
        return summary, stdout_text

    def _publish_live(self, item_id: str, state: "_StreamState") -> None:
        """Write the current partial envelope to result_json so the dashboard
        can show live CTX% / tokens without waiting for exit."""
        try:
            blob = json.dumps({
                "phase": state.phase,
                "live": True,
                **state.envelope(),
            })
            self.store.update_result_json(item_id, blob)
        except Exception:
            # A publish failure shouldn't crash the run. Dashboard just
            # stays on the previous snapshot.
            pass


class CodexRunner(Runner):
    """Spawns a headless `codex exec` subprocess inside the worktree. Keeps
    the same two-phase flow as Claude by persisting the `thread_id` emitted
    by Codex and resuming it during execution."""

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prior = _parse_result_json(item.result_json)
        if prior.get("phase") == "plan":
            return self._do_execute(item, worktree, prior.get("plan", ""))
        if self.config.agent.single_phase:
            return self._do_execute(
                item, worktree, "(no plan; spec is in the task body)",
            )
        return self._do_plan(item, worktree)

    def _do_plan(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        prompt = self.config.agent.plan_prompt_template.format(
            title=item.title, body=item.body, source_file=item.source_file,
        )
        prompt = self._prepend_feedback(item, prompt, phase="plan")
        output_path = self._last_message_path(item, "plan")
        _, stdout = self._invoke_codex(item, worktree, prompt, output_path)
        plan_text = _read_output_message(output_path)
        if not plan_text:
            plan_text = (
                getattr(self, "_last_usage", None) or {}
            ).get("result") or _extract_codex_result(stdout) or "(no plan text returned)"
        self._last_phase = "plan"
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
        output_path = self._last_message_path(item, "execute")
        summary, stdout = self._invoke_codex(item, worktree, prompt, output_path)
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
    ) -> tuple[str, str]:
        phase_tag = "execute" if item.session_id else "plan"
        transcript_path = (
            self.config.project_root / ".agentor" / "transcripts"
            / f"{item.id}.{phase_tag}.log"
        )
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        args = self._codex_args(item, prompt, output_path)
        return self._invoke_codex_jsonl(
            item, args, worktree, transcript_path, output_path, phase_tag,
        )

    def _codex_args(
        self, item: StoredItem, prompt: str, output_path: Path,
    ) -> list[str]:
        values = {
            "prompt": prompt,
            "model": self.config.agent.model,
            "session_id": item.session_id or "",
            "output_path": str(output_path),
        }
        if item.session_id:
            tmpl = (
                self.config.agent.resume_command
                or _default_codex_resume_command()
            )
        else:
            tmpl = self.config.agent.command or _default_codex_command()
        return [a.format(**values) for a in tmpl]

    def _invoke_codex_jsonl(
        self, item: StoredItem, args: list[str], worktree: Path,
        transcript_path: Path, output_path: Path, phase_tag: str,
    ) -> tuple[str, str]:
        try:
            p = subprocess.Popen(
                args, cwd=worktree,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, start_new_session=True,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"codex CLI not found. First arg was: {args[0]!r}. "
                f"Install codex or set agent.command/agent.resume_command in agentor.toml."
            )
        if self.proc_registry is not None:
            self.proc_registry.register(item.id, p)

        timed_out = threading.Event()

        def _on_timeout():
            timed_out.set()
            try:
                p.kill()
            except Exception:
                pass

        timer = threading.Timer(self.config.agent.timeout_seconds, _on_timeout)
        timer.daemon = True
        timer.start()

        stderr_chunks: list[str] = []

        def _drain_stderr():
            try:
                for line in iter(p.stderr.readline, ""):
                    stderr_chunks.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        state = _CodexStreamState(item_id=item.id, phase=phase_tag)
        stdout_buf: list[str] = []
        transcript_path.write_text(f"args: {args}\n\nstdout:\n")
        try:
            for line in iter(p.stdout.readline, ""):
                stdout_buf.append(line)
                with transcript_path.open("a") as fh:
                    fh.write(line)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                state.ingest(ev)
                if state.session_id and state.session_id != item.session_id:
                    self.store.transition(
                        item.id, ItemStatus.WORKING,
                        session_id=state.session_id,
                        note="session id assigned",
                    )
                    item = self.store.get(item.id)
                self._publish_live(item.id, state)
            p.wait(timeout=5)
        finally:
            timer.cancel()
            if self.proc_registry is not None:
                self.proc_registry.unregister(item.id)
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
        if timed_out.is_set():
            raise RuntimeError(
                f"codex timed out after {self.config.agent.timeout_seconds}s"
            )
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("codex killed: agentor shutdown")
        if p.returncode not in (0, None):
            tail = (stderr_text or stdout_text)[-500:].strip()
            raise RuntimeError(f"codex exited {p.returncode}: {tail}")
        result_text = _read_output_message(output_path) or _extract_codex_result(stdout_text)
        self._last_usage = state.envelope(result_text=result_text)
        return result_text or "", stdout_text

    def _publish_live(self, item_id: str, state: "_CodexStreamState") -> None:
        try:
            blob = json.dumps({
                "phase": state.phase,
                "live": True,
                **state.envelope(),
            })
            self.store.update_result_json(item_id, blob)
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
        # modelUsage is keyed by model id, mirroring claude's final envelope.
        # Used for token accounting and context-window detection, not cost.
        self.model_usage: dict[str, dict] = {}
        # Last seen result text (set by the terminal 'result' event).
        self.result_text: str | None = None

    def ingest(self, ev: dict) -> None:
        etype = ev.get("type")
        self.last_event_at = time.time()
        self.last_event_type = str(etype or "unknown")
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
            out["session_id"] = self.session_id
        if self.result_text:
            out["result"] = self.result_text
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
            out["session_id"] = self.session_id
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
    for k in ("usage", "modelUsage", "num_turns",
              "duration_ms", "duration_api_ms", "stop_reason", "session_id",
              "result"):
        if k in obj and obj[k] is not None:
            out[k] = obj[k]
    return out or None


def _read_output_message(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    return text or None


def _default_codex_command() -> list[str]:
    return [
        "codex", "exec", "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m", "{model}",
        "-o", "{output_path}",
        "{prompt}",
    ]


def _default_claude_command() -> list[str]:
    return [
        "claude", "-p", "{prompt}", "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--verbose",
    ]


def _default_codex_resume_command() -> list[str]:
    return [
        "codex", "exec", "resume", "{session_id}", "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m", "{model}",
        "-o", "{output_path}",
        "{prompt}",
    ]


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
