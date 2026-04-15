import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import git_ops
from .config import Config
from .models import ItemStatus
from .slug import slugify
from .store import Store, StoredItem


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


def plan_worktree(config: Config, item: StoredItem) -> tuple[Path, str]:
    slug = slugify(item.title)
    unique = f"{slug}-{item.id[:8]}"
    branch = f"{config.git.branch_prefix}{unique}"
    path = worktree_root(config) / unique
    return path, branch


class Runner:
    """Base runner interface. Subclasses implement `do_work`."""

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    def do_work(self, item: StoredItem, worktree: Path) -> tuple[str, list[str]]:
        """Perform the agent's work inside the worktree. Return (summary, files_changed).
        Subclasses override. The base class commits no changes — committer does that."""
        raise NotImplementedError

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

        if not resume:
            # Pre-flight cleanup. Order matters: remove the worktree dir, then
            # prune stale registrations (.git/worktrees/<name>/ left after a
            # manual rm -rf), THEN delete the branch — git refuses to delete a
            # branch while it is still associated with a (possibly ghost)
            # worktree.
            git_ops.worktree_remove(repo, wt_path, force=True)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
            git_ops.worktree_prune(repo)
            if git_ops.branch_exists(repo, branch):
                git_ops.branch_delete(repo, branch, force=True)

        try:
            if not resume:
                git_ops.worktree_add(repo, wt_path, branch, self.config.git.base_branch)
        except git_ops.GitError as e:
            err = str(e)
            if item.attempts >= self.config.agent.max_attempts:
                self.store.transition(
                    item.id, ItemStatus.REJECTED,
                    worktree_path=None, branch=None,
                    last_error=f"worktree_add exhausted after {item.attempts} attempts: {err}",
                    note="max_attempts reached",
                )
            else:
                self.store.transition(
                    item.id, ItemStatus.QUEUED,
                    worktree_path=None, branch=None, session_id=None,
                    last_error=f"worktree_add: {err}",
                )
            return RunResult(item.id, wt_path, branch, "", [], "", error=err)

        try:
            summary, files_changed = self.do_work(item, wt_path)
        except Exception as e:
            last_error = f"do_work: {e}"
            git_ops.worktree_remove(repo, wt_path, force=True)
            if item.attempts >= self.config.agent.max_attempts:
                self.store.transition(
                    item.id, ItemStatus.REJECTED,
                    worktree_path=None, branch=None,
                    last_error=last_error,
                    note="max_attempts reached",
                )
            else:
                self.store.transition(
                    item.id, ItemStatus.QUEUED,
                    worktree_path=None, branch=None, session_id=None,
                    last_error=last_error,
                )
            return RunResult(item.id, wt_path, branch, "", [], "", error=last_error)

        diff = git_ops.diff_vs_base(wt_path, self.config.git.base_branch)
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
            # Keep the plan text intact so the execute phase can inject it
            # into the follow-up prompt and the review UI can display it.
            result["plan"] = summary
        else:
            # Carry the approved plan forward on the final result for audit.
            prior = _parse_result_json(item.result_json)
            if prior.get("plan"):
                result["plan"] = prior["plan"]
        envelope = getattr(self, "_last_usage", None)
        if envelope:
            # Surface cost/turn/timing fields at the top level of result_json
            # so the dashboard's `_cost_total` / `_cost_breakdown` /
            # `_build_detail_lines` can read them without digging into a
            # nested 'usage' dict.
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
        if not item.last_error:
            return prompt
        hint = ("Produce a revised plan." if phase == "plan"
                else "Address this feedback during execution.")
        block = (
            "REVIEWER FEEDBACK FROM A PREVIOUS REJECTED ATTEMPT:\n"
            f"{item.last_error}\n\n"
            f"{hint}\n\n"
        )
        # Consume feedback — clear last_error so the NEXT run starts clean.
        # Direct SQL so we don't have to fake a status transition.
        self.store.conn.execute(
            "UPDATE items SET last_error = NULL WHERE id = ?", (item.id,)
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
        args = [a.format(prompt=prompt) for a in self.config.agent.command]

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
        try:
            cp = subprocess.run(
                args, cwd=worktree, capture_output=True, text=True,
                timeout=self.config.agent.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            transcript_path.write_text(
                f"TIMEOUT after {self.config.agent.timeout_seconds}s\n\n"
                f"stdout:\n{e.stdout or ''}\n\nstderr:\n{e.stderr or ''}\n"
            )
            raise RuntimeError(
                f"claude timed out after {self.config.agent.timeout_seconds}s"
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"claude CLI not found. First arg was: {args[0]!r}. "
                f"Install claude or set agent.command in agentor.toml."
            )

        transcript_path.write_text(
            f"exit: {cp.returncode}\n"
            f"args: {args}\n\n"
            f"stdout:\n{cp.stdout}\n\nstderr:\n{cp.stderr}\n"
        )
        if cp.returncode != 0:
            tail = (cp.stderr or cp.stdout)[-500:].strip()
            raise RuntimeError(f"claude exited {cp.returncode}: {tail}")
        summary = _derive_summary(worktree, cp.stdout, item.title)
        self._last_usage = _parse_usage(cp.stdout)
        return summary, cp.stdout

    def _invoke_claude_streaming(
        self, item: StoredItem, args: list[str], worktree: Path,
        transcript_path: Path, phase_tag: str,
    ) -> tuple[str, str]:
        """Launch claude with Popen, read stdout line-by-line, parse each
        stream-json event, and publish live usage/iterations to the store."""
        import threading
        try:
            p = subprocess.Popen(
                args, cwd=worktree,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"claude CLI not found. First arg was: {args[0]!r}. "
                f"Install claude or set agent.command in agentor.toml."
            )

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
        try:
            for line in iter(p.stdout.readline, ""):
                stdout_buf.append(line)
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
            p.wait(timeout=5)
        finally:
            timer.cancel()
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
        transcript_path.write_text(
            f"exit: {p.returncode}\n"
            f"args: {args}\n\n"
            f"stdout:\n{stdout_text}\n\nstderr:\n{stderr_text}\n"
        )
        if timed_out.is_set():
            raise RuntimeError(
                f"claude timed out after {self.config.agent.timeout_seconds}s"
            )
        if p.returncode not in (0, None):
            tail = (stderr_text or stdout_text)[-500:].strip()
            raise RuntimeError(f"claude exited {p.returncode}: {tail}")
        self._last_usage = state.envelope()
        summary = _derive_summary(worktree, stdout_text, item.title)
        return summary, stdout_text

    def _publish_live(self, item_id: str, state: "_StreamState") -> None:
        """Write the current partial envelope to result_json so the dashboard
        can show live CTX% / tokens / cost without waiting for exit."""
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


class _StreamState:
    """Accumulator for claude stream-json events. Builds the same envelope
    shape as the blocking `--output-format json` path (usage, iterations,
    modelUsage, total_cost_usd, num_turns, stop_reason) so the rest of the
    dashboard doesn't care which mode produced the data."""

    def __init__(self, item_id: str, phase: str):
        self.item_id = item_id
        self.phase = phase
        self.session_id: str | None = None
        self.iterations: list[dict] = []
        self.num_turns: int = 0
        self.stop_reason: str | None = None
        self.duration_ms: int | None = None
        self.duration_api_ms: int | None = None
        self.total_cost_usd: float = 0.0
        # modelUsage is keyed by model id, mirroring claude's final envelope.
        self.model_usage: dict[str, dict] = {}
        # Last seen result text (set by the terminal 'result' event).
        self.result_text: str | None = None

    def ingest(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            return
        if etype == "assistant":
            msg = ev.get("message") or {}
            usage = msg.get("usage") or {}
            if not isinstance(usage, dict):
                return
            model = msg.get("model") or "unknown"
            self.num_turns += 1
            self.iterations.append({
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "cache_read_input_tokens": int(
                    usage.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens", 0) or 0),
                "model": model,
            })
            # Aggregate into modelUsage.
            mu = self.model_usage.setdefault(model, {
                "inputTokens": 0, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                "costUSD": 0.0, "contextWindow": 0,
            })
            mu["inputTokens"] += int(usage.get("input_tokens", 0) or 0)
            mu["outputTokens"] += int(usage.get("output_tokens", 0) or 0)
            mu["cacheReadInputTokens"] += int(
                usage.get("cache_read_input_tokens", 0) or 0)
            mu["cacheCreationInputTokens"] += int(
                usage.get("cache_creation_input_tokens", 0) or 0)
            return
        if etype == "result":
            # Final envelope — trust its numbers over the aggregate we built
            # (it's what claude reports for billing).
            if ev.get("total_cost_usd") is not None:
                self.total_cost_usd = float(ev["total_cost_usd"])
            if ev.get("num_turns") is not None:
                self.num_turns = int(ev["num_turns"])
            if ev.get("stop_reason"):
                self.stop_reason = ev["stop_reason"]
            if ev.get("duration_ms") is not None:
                self.duration_ms = int(ev["duration_ms"])
            if ev.get("duration_api_ms") is not None:
                self.duration_api_ms = int(ev["duration_api_ms"])
            if isinstance(ev.get("modelUsage"), dict):
                # Claude's own modelUsage includes costUSD and contextWindow
                # which we can't derive ourselves.
                self.model_usage = ev["modelUsage"]
            result = ev.get("result")
            if isinstance(result, str):
                self.result_text = result

    def envelope(self) -> dict:
        """Produce the same envelope shape _parse_usage would build off the
        blocking JSON path, so dashboard code stays agnostic of mode."""
        # Aggregate `usage` from iterations for dashboards that read the
        # flat usage dict.
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
        if self.total_cost_usd:
            out["total_cost_usd"] = self.total_cost_usd
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
    """Extract the entire cost/usage envelope from a `--output-format json`
    claude run. Returns a dict with `usage`, `total_cost_usd`, `modelUsage`,
    `num_turns`, `duration_ms`, `duration_api_ms`, `stop_reason` when
    available. Parses defensively — stdout may be plain text or have trailing
    log noise. Returns None if nothing usable can be pulled out."""
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
    for k in ("usage", "total_cost_usd", "modelUsage", "num_turns",
              "duration_ms", "duration_api_ms", "stop_reason", "session_id",
              "result"):
        if k in obj and obj[k] is not None:
            out[k] = obj[k]
    return out or None


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
    raise ValueError(f"unknown agent.runner: {kind!r}")
