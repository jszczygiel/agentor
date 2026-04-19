import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor import git_ops
from agentor.committer import (approve_and_commit, approve_plan, defer,
                                reject, restore_deferred, retry)
from agentor.config import (AgentConfig, Config, GitConfig, ParsingConfig,
                            ReviewConfig, SourcesConfig)
from agentor.models import ItemStatus
from agentor.recovery import recover_on_startup
from agentor.runner import (CodexRunner, StubRunner,
                            _default_claude_command,
                            _mark_done_instruction, make_runner, plan_worktree,
                            write_claude_settings)
from agentor.store import Store
from agentor.watcher import scan_once


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_project(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("# project\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")


def _mk_config(root: Path, mode: str = "checkbox",
               watch: list[str] | None = None) -> Config:
    return Config(
        project_name=root.name,
        project_root=root,
        sources=SourcesConfig(
            watch=watch or ["backlog.md"], exclude=[],
        ),
        parsing=ParsingConfig(mode=mode),
        agent=AgentConfig(pool_size=1),
        git=GitConfig(base_branch="main", branch_prefix="agent/"),
        review=ReviewConfig(),
    )


class TestStubRunner(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Fix a bug\n  details\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _claim_first(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        return self.store.claim_next_queued(str(wt), br)

    def test_stub_runner_transitions_to_awaiting_review(self):
        claimed = self._claim_first()
        result = StubRunner(self.cfg, self.store).run(claimed)
        self.assertIsNone(result.error)
        self.assertTrue(result.worktree_path.exists())
        self.assertTrue(result.diff)  # untracked file should show
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        self.assertIsNotNone(refreshed.result_json)
        data = json.loads(refreshed.result_json)
        self.assertIn("summary", data)

    def test_approve_commits_and_removes_worktree(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        self.assertTrue(wt.exists())
        sha = approve_and_commit(self.cfg, self.store, item, "test commit")
        self.assertTrue(sha)
        self.assertFalse(wt.exists())
        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.MERGED)

    def test_reject_keeps_worktree_and_marks_rejected(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        reject(self.store, item, "not what I wanted")
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.REJECTED)
        self.assertTrue(wt.exists())  # worktree preserved for retry

    def test_retry_requeues(self):
        claimed = self._claim_first()
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        reject(self.store, item, "fb")
        retry(self.store, self.store.get(claimed.id))
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)


class TestFrontmatterMarkDone(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "backlog").mkdir()
        self.src = self.root / "docs" / "backlog" / "bug-a.md"
        self.src.write_text(
            "---\ntitle: Bug A\nstate: available\n---\nDescription.\n"
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "add bug")
        self.cfg = _mk_config(
            self.root, mode="frontmatter",
            watch=["docs/backlog/*.md"],
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_approve_deletes_source_file_in_frontmatter_mode(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        approve_and_commit(self.cfg, self.store, item, "fix bug A")
        # source file still exists on main (we only removed in worktree, then committed there)
        self.assertTrue(self.src.exists())
        # but on the agent branch, the file is gone
        cp = subprocess.run(
            ["git", "show", f"{item.branch}:docs/backlog/bug-a.md"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertNotEqual(cp.returncode, 0)


class TestMarkDoneInstruction(unittest.TestCase):
    """The agent's execute prompt should instruct it to delete the idea
    markdown file in its final commit under frontmatter mode. Other modes
    share a file across items, so whole-file deletion would take siblings
    with it — the helper no-ops there."""

    def _cfg(self, *, mode: str) -> Config:
        return Config(
            project_name="proj", project_root=Path("/tmp/proj"),
            sources=SourcesConfig(watch=[], exclude=[]),
            parsing=ParsingConfig(mode=mode),
            agent=AgentConfig(), git=GitConfig(), review=ReviewConfig(),
        )

    def test_frontmatter_emits_instruction(self):
        out = _mark_done_instruction(
            self._cfg(mode="frontmatter"), "docs/ideas/foo.md",
        )
        self.assertIn("docs/ideas/foo.md", out)
        self.assertIn("git rm", out)
        self.assertIn("SAME final commit", out)

    def test_checkbox_mode_emits_nothing(self):
        self.assertEqual(
            _mark_done_instruction(self._cfg(mode="checkbox"), "backlog.md"),
            "",
        )

    def test_heading_mode_emits_nothing(self):
        self.assertEqual(
            _mark_done_instruction(self._cfg(mode="heading"), "ideas.md"),
            "",
        )

    def test_missing_source_file_emits_nothing(self):
        self.assertEqual(
            _mark_done_instruction(self._cfg(mode="frontmatter"), ""),
            "",
        )


def _write_fake_cli(bin_dir: Path, name: str, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / name
    fake.write_text("#!/bin/sh\n" + script)
    st = os.stat(fake)
    os.chmod(fake, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _write_fake_claude(bin_dir: Path, script: str) -> Path:
    return _write_fake_cli(bin_dir, "claude", script)


def _write_fake_codex(bin_dir: Path, script: str) -> Path:
    return _write_fake_cli(bin_dir, "codex", script)


class TestClaudeRunner(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name) / "proj"
        self.root.mkdir()
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Add hello file\n  greet world\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _run_with_fake(self, script: str, full_cycle: bool = True,
                       max_attempts: int = 2) -> tuple:
        """Spawns the runner against a fake claude. When `full_cycle=True` (the
        default) drives plan → approve_plan → execute and returns the execute
        phase's RunResult; otherwise returns after the plan phase only."""
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=max_attempts,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        result = runner.run(claimed)
        if not full_cycle or result.error:
            return cfg, claimed, result
        fresh = self.store.get(claimed.id)
        if fresh.status != ItemStatus.AWAITING_PLAN_REVIEW:
            return cfg, claimed, result
        approve_plan(self.store, fresh)
        # re-claim for execute phase
        wt2, br2 = plan_worktree(cfg, fresh)
        claimed2 = self.store.claim_next_queued(str(wt2), br2)
        exec_result = runner.run(claimed2)
        return cfg, claimed2, exec_result

    def test_claude_runner_committed_change(self):
        script = """
set -e
if [ ! -f hello.txt ]; then
  echo "HELLO" > hello.txt
  git add hello.txt
  git -c user.email=x -c user.name=x commit -q -m "add hello"
fi
echo "/develop finished"
"""
        cfg, claimed, result = self._run_with_fake(script)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data.get("phase"), "execute")
        self.assertIn("hello.txt", data["files_changed"])

    def test_claude_runner_approve_records_existing_sha(self):
        script = """
set -e
if [ ! -f hello.txt ]; then
  echo "HELLO" > hello.txt
  git add hello.txt
  git -c user.email=x -c user.name=x commit -q -m "add hello"
fi
"""
        cfg, claimed, _ = self._run_with_fake(script)
        item = self.store.get(claimed.id)
        wt = Path(item.worktree_path)
        pre_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wt,
            capture_output=True, text=True,
        ).stdout.strip()
        sha = approve_and_commit(cfg, self.store, item, "new commit (should not fire)")
        self.assertEqual(sha, pre_sha)
        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.MERGED)
        notes = [t.note for t in self.store.transitions_for(claimed.id) if t.note]
        self.assertTrue(any("recorded existing commit" in n for n in notes),
                        f"expected 'recorded existing commit' note, got {notes}")
        self.assertFalse(wt.exists())

    def test_claude_runner_stream_json_live_updates(self):
        """The streaming path should read each stream-json event, populate
        iterations + modelUsage, and publish a `live=True` snapshot to
        result_json before the final transition."""
        # Two assistant events + a terminal result event.
        script = r"""printf '%s\n' '{"type":"system","subtype":"init","session_id":"sess-xyz"}'
printf '%s\n' '{"type":"assistant","message":{"role":"assistant","model":"claude-opus-4-6","usage":{"input_tokens":100,"cache_read_input_tokens":5000,"cache_creation_input_tokens":200,"output_tokens":50}}}'
printf '%s\n' '{"type":"assistant","message":{"role":"assistant","model":"claude-opus-4-6","usage":{"input_tokens":5,"cache_read_input_tokens":5300,"cache_creation_input_tokens":0,"output_tokens":120}}}'
printf '%s\n' '{"type":"result","subtype":"success","result":"here is the plan","num_turns":2,"total_cost_usd":0.04,"stop_reason":"end_turn","modelUsage":{"claude-opus-4-6":{"inputTokens":105,"outputTokens":170,"cacheReadInputTokens":10300,"cacheCreationInputTokens":200,"costUSD":0.04,"contextWindow":1000000}}}'
"""
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=2,
                command=[str(bin_dir / "claude"), "-p", "{prompt}",
                         "--output-format", "stream-json", "--verbose"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        result = runner.run(claimed)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_PLAN_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertEqual(data["plan"], "here is the plan")
        self.assertEqual(data["num_turns"], 2)
        self.assertEqual(data["progress"]["last_event_type"], "result")
        self.assertIn("finished: end_turn", data["progress"]["activity"])
        self.assertNotIn("total_cost_usd", data)
        iters = data["iterations"]
        self.assertEqual(len(iters), 2)
        self.assertEqual(iters[-1]["cache_read_input_tokens"], 5300)
        self.assertIn("claude-opus-4-6", data["modelUsage"])
        transcript = (
            self.root / ".agentor" / "transcripts" / f"{claimed.id}.plan.log"
        ).read_text()
        self.assertIn('"type":"assistant"', transcript)
        self.assertIn("exit: 0", transcript)

    def test_claude_runner_plan_phase_lands_in_plan_review(self):
        """First run (no prior session) is the planning phase; item lands in
        AWAITING_PLAN_REVIEW with phase=plan persisted for the execute pass."""
        script = 'echo "I will add hello.txt, then commit."\n'
        cfg, claimed, result = self._run_with_fake(script, full_cycle=False)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_PLAN_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertTrue(data.get("plan"))
        # session_id must be set so the execute phase can --resume
        self.assertIsNotNone(refreshed.session_id)

    def test_claude_runner_fails_on_nonzero_exit(self):
        script = "echo oops >&2\nexit 2\n"
        cfg, claimed, result = self._run_with_fake(
            script, full_cycle=False, max_attempts=1,
        )
        self.assertIsNotNone(result.error)
        refreshed = self.store.get(claimed.id)
        # any agent-side failure parks the item in ERRORED (no auto-retry)
        self.assertEqual(refreshed.status, ItemStatus.ERRORED)

    def test_execute_prompt_includes_mark_done_instruction_in_frontmatter(self):
        """Under frontmatter mode the execute phase prompt must carry the
        `git rm <source_file>` instruction so the agent folds idea-file
        deletion into its own commit."""
        # Seed a frontmatter idea file rather than the checkbox backlog.md
        # the TestClaudeRunner setUp prepared.
        (self.root / "backlog.md").unlink()
        idea_dir = self.root / "docs" / "ideas"
        idea_dir.mkdir(parents=True)
        idea_file = idea_dir / "bug-a.md"
        idea_file.write_text(
            "---\ntitle: Bug A\nstate: available\n---\nbody.\n"
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "add idea")
        bin_dir = Path(self.td.name) / "bin"
        prompt_log = Path(self.td.name) / "prompts.log"
        # Fake CLI appends whatever prompt text it receives to prompt_log.
        _write_fake_claude(bin_dir, f'printf "%s\\n---\\n" "$2" >> "{prompt_log}"\n')
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(
                watch=["docs/ideas/*.md"], exclude=[],
            ),
            parsing=ParsingConfig(mode="frontmatter"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        runner.run(claimed)  # plan phase — no instruction expected
        fresh = self.store.get(claimed.id)
        self.assertEqual(fresh.status, ItemStatus.AWAITING_PLAN_REVIEW)
        approve_plan(self.store, fresh)
        wt2, br2 = plan_worktree(cfg, fresh)
        claimed2 = self.store.claim_next_queued(str(wt2), br2)
        runner.run(claimed2)
        prompts = prompt_log.read_text()
        plan_block, _, exec_block = prompts.partition("\n---\n")
        self.assertNotIn("Source-file removal", plan_block,
                         "plan phase is read-only; no deletion instruction")
        self.assertIn("Source-file removal", exec_block)
        self.assertIn("docs/ideas/bug-a.md", exec_block)

    def test_do_execute_injects_primer_when_prior_execute_log_exists(self):
        """Kill-resume primer. When a `.execute.log` already exists at the
        start of the execute phase (prior attempt was killed mid-run), its
        tool-use history must be summarised into the next execute prompt so
        the resumed agent doesn't cold-start discovery."""
        bin_dir = Path(self.td.name) / "bin"
        prompt_log = Path(self.td.name) / "prompts.log"
        _write_fake_claude(
            bin_dir, f'printf "%s\\n---\\n" "$2" >> "{prompt_log}"\n',
        )
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        runner.run(claimed)  # plan phase
        fresh = self.store.get(claimed.id)
        self.assertEqual(fresh.status, ItemStatus.AWAITING_PLAN_REVIEW)

        # Seed a synthetic killed-execute-run transcript before the approve →
        # execute handoff. Three assistant turns, three tool calls.
        transcript_path = (
            self.root / ".agentor" / "transcripts"
            / f"{fresh.id}.execute.log"
        )
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        def _ev_tool_call(name, inp, tid):
            return json.dumps({
                "type": "assistant",
                "message": {
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                    "content": [{
                        "type": "tool_use", "id": tid,
                        "name": name, "input": inp,
                    }],
                },
            })

        def _ev_tool_result(tid, text):
            return json.dumps({
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result", "tool_use_id": tid,
                        "content": text, "is_error": False,
                    }],
                },
            })

        transcript_path.write_text(
            "stdout:\n"
            + _ev_tool_call(
                "Read", {"file_path": "scripts/main/game_world.gd"}, "r1",
            ) + "\n"
            + _ev_tool_result("r1", "file contents") + "\n"
            + _ev_tool_call(
                "Grep", {"pattern": "zoom_level"}, "g1",
            ) + "\n"
            + _ev_tool_result("g1", "scripts/ui/hud.gd\nscripts/camera.gd\n")
            + "\n"
            + _ev_tool_call("Bash", {"command": "ls"}, "b1") + "\n"
            + _ev_tool_result("b1", "total 0") + "\n"
        )

        approve_plan(self.store, fresh)
        wt2, br2 = plan_worktree(cfg, fresh)
        claimed2 = self.store.claim_next_queued(str(wt2), br2)
        runner.run(claimed2)

        prompts = prompt_log.read_text()
        _, _, exec_block = prompts.partition("\n---\n")
        self.assertIn("## Prior run", exec_block)
        self.assertIn("scripts/main/game_world.gd", exec_block)
        self.assertIn('"zoom_level"', exec_block)
        self.assertIn("scripts/ui/hud.gd", exec_block)
        self.assertNotIn("ls", exec_block.split("plan=")[-1])

    def test_do_execute_omits_primer_when_no_prior_execute_log(self):
        """Plan→execute handoff (no kill) must not inject a primer — there's
        no prior execute transcript on disk because plan wrote `.plan.log`."""
        bin_dir = Path(self.td.name) / "bin"
        prompt_log = Path(self.td.name) / "prompts.log"
        _write_fake_claude(
            bin_dir, f'printf "%s\\n---\\n" "$2" >> "{prompt_log}"\n',
        )
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        runner.run(claimed)
        fresh = self.store.get(claimed.id)
        approve_plan(self.store, fresh)
        wt2, br2 = plan_worktree(cfg, fresh)
        claimed2 = self.store.claim_next_queued(str(wt2), br2)
        runner.run(claimed2)

        prompts = prompt_log.read_text()
        _, _, exec_block = prompts.partition("\n---\n")
        self.assertNotIn("## Prior run", exec_block)

    def test_force_execute_item_skips_plan_phase(self):
        """An item resubmitted with `force_execute=True` (auto-resolve chain)
        carries `result_json.phase == "plan"` so the runner routes straight
        to `_do_execute` on the next dispatch — no plan subprocess, no
        `.plan.log`. Verified end-to-end: one fake-claude invocation, one
        `.execute.log`, prompt built from the execute template."""
        bin_dir = Path(self.td.name) / "bin"
        prompt_log = Path(self.td.name) / "prompts.log"
        _write_fake_claude(
            bin_dir, f'printf "%s\\n---\\n" "$2" >> "{prompt_log}"\n',
        )
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        # Create the worktree up-front so the runner's resume branch fires
        # (mirrors the post-conflict state: worktree + branch + session live).
        git_ops.worktree_add(self.root, wt, br, "main")
        # Simulate the state produced by
        # `resubmit_conflicted(..., force_execute=True)`: session_id live,
        # result_json rewritten to phase=plan so the two-phase dispatch
        # picks the _do_execute branch.
        self.store.transition(
            item.id, ItemStatus.QUEUED,
            session_id="sess-force",
            result_json='{"phase":"plan","plan":"resolve the merge conflict"}',
        )
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        result = runner.run(claimed)

        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)

        transcripts_dir = self.root / ".agentor" / "transcripts"
        plan_log = transcripts_dir / f"{claimed.id}.plan.log"
        exec_log = transcripts_dir / f"{claimed.id}.execute.log"
        self.assertFalse(plan_log.exists(),
                         "force-execute path must not produce a plan transcript")
        self.assertTrue(exec_log.exists(),
                        "execute phase must write its own transcript")

        prompts = prompt_log.read_text()
        # Fake claude was invoked exactly once.
        self.assertEqual(prompts.count("\n---\n"), 1,
                         f"expected one invocation, got: {prompts!r}")
        self.assertIn("EXEC: Add hello file", prompts)
        self.assertIn("plan=resolve the merge conflict", prompts)
        self.assertNotIn("PLAN: Add hello file", prompts)

    def test_claude_runner_timeout(self):
        # sleep > timeout
        script = "sleep 5\n"
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_claude(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=1,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                timeout_seconds=1,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        result = make_runner(cfg, self.store).run(claimed)
        self.assertIsNotNone(result.error)
        self.assertIn("timed out", result.error)


class TestCodexRunner(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name) / "proj"
        self.root.mkdir()
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Add hello file\n  greet world\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _run_with_fake(self, script: str, full_cycle: bool = True) -> tuple:
        bin_dir = Path(self.td.name) / "bin"
        _write_fake_codex(bin_dir, script)
        cfg = Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="codex", model="gpt-5-codex", pool_size=1,
                command=[str(bin_dir / "codex"), "exec", "--json",
                         "-m", "{model}", "-o", "{output_path}", "{prompt}"],
                resume_command=[
                    str(bin_dir / "codex"), "exec", "resume", "{session_id}",
                    "--json", "-m", "{model}", "-o", "{output_path}", "{prompt}",
                ],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner = make_runner(cfg, self.store)
        self.assertIsInstance(runner, CodexRunner)
        result = runner.run(claimed)
        if not full_cycle or result.error:
            return cfg, claimed, result
        fresh = self.store.get(claimed.id)
        if fresh.status != ItemStatus.AWAITING_PLAN_REVIEW:
            return cfg, claimed, result
        approve_plan(self.store, fresh)
        wt2, br2 = plan_worktree(cfg, fresh)
        claimed2 = self.store.claim_next_queued(str(wt2), br2)
        exec_result = runner.run(claimed2)
        return cfg, claimed2, exec_result

    def test_codex_runner_plan_phase_persists_thread_id(self):
        script = r"""
set -e
mode="new"
if [ "$1" = "exec" ]; then
  shift
fi
if [ "$1" = "resume" ]; then
  mode="resume"
  shift
  sess="$1"
  shift
fi
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) shift ;;
    -m) shift 2 ;;
    -o) out="$2"; shift 2 ;;
    *) prompt="$1"; shift ;;
  esac
done
if [ "$mode" = "new" ]; then
  printf '%s\n' '{"type":"thread.started","thread_id":"thread-123"}'
  printf '%s\n' '{"type":"turn.started"}'
  printf 'codex plan text' > "$out"
else
  printf '%s\n' '{"type":"thread.started","thread_id":"thread-123"}'
  printf '%s\n' '{"type":"turn.started"}'
  printf 'codex execute text' > "$out"
fi
"""
        _, claimed, result = self._run_with_fake(script, full_cycle=False)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_PLAN_REVIEW)
        self.assertEqual(refreshed.session_id, "thread-123")
        data = json.loads(refreshed.result_json)
        self.assertEqual(data["phase"], "plan")
        self.assertEqual(data["plan"], "codex plan text")
        self.assertEqual(data["progress"]["last_event_type"], "turn.started")
        self.assertEqual(data["progress"]["activity"], "turn 1 started")

    def test_codex_runner_committed_change(self):
        script = r"""
set -e
mode="new"
if [ "$1" = "exec" ]; then
  shift
fi
if [ "$1" = "resume" ]; then
  mode="resume"
  shift
  sess="$1"
  shift
fi
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) shift ;;
    -m) shift 2 ;;
    -o) out="$2"; shift 2 ;;
    *) prompt="$1"; shift ;;
  esac
done
printf '%s\n' '{"type":"thread.started","thread_id":"thread-123"}'
printf '%s\n' '{"type":"turn.started"}'
if [ "$mode" = "new" ]; then
  printf 'codex plan text' > "$out"
  exit 0
fi
echo "HELLO" > hello.txt
git add hello.txt
git -c user.email=x -c user.name=x commit -q -m "add hello"
printf 'codex execute text' > "$out"
"""
        _, claimed, result = self._run_with_fake(script)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_REVIEW)
        data = json.loads(refreshed.result_json)
        self.assertEqual(data.get("phase"), "execute")
        self.assertIn("hello.txt", data["files_changed"])

class TestDeferred(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n- [ ] two\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(pool_size=1, max_attempts=1),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_defer_from_queued_then_restore(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        defer(self.store, item)
        self.assertEqual(self.store.get(item.id).status, ItemStatus.DEFERRED)
        target = restore_deferred(self.store, self.store.get(item.id))
        self.assertEqual(target, ItemStatus.QUEUED)
        self.assertEqual(self.store.get(item.id).status, ItemStatus.QUEUED)

    def test_defer_from_awaiting_then_restore(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        StubRunner(self.cfg, self.store).run(claimed)
        item = self.store.get(claimed.id)
        self.assertEqual(item.status, ItemStatus.AWAITING_REVIEW)
        defer(self.store, item)
        self.assertEqual(self.store.get(item.id).status, ItemStatus.DEFERRED)
        target = restore_deferred(self.store, self.store.get(item.id))
        self.assertEqual(target, ItemStatus.AWAITING_REVIEW)


class TestDaemonAutoDispatch(unittest.TestCase):
    """Discovery now auto-queues every new item; the daemon picks them up
    without any operator gate. Also covers `dispatch_specific` for direct
    dispatch of a queued item by id."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n- [ ] two\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _cfg(self) -> Config:
        return Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=1),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )

    def test_scan_lands_items_at_queued(self):
        """No operator gate — one scan pass, every new item lands directly
        at QUEUED."""
        cfg = self._cfg()
        scan_once(cfg, self.store)
        self.assertEqual(len(self.store.list_by_status(ItemStatus.QUEUED)), 2)

    def test_daemon_dispatches_queued_discovery(self):
        from agentor.daemon import Daemon
        from agentor.runner import make_runner
        cfg = self._cfg()
        d = Daemon(cfg, self.store, make_runner, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading
        import time as _t
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        _t.sleep(0.3)
        d.stop_event.set()
        t.join(timeout=5)
        self.assertGreaterEqual(d.stats.dispatched, 1)

    def test_dispatch_specific_on_queued(self):
        """Operator picks a specific queued item by id — bypasses the
        oldest-first claim so you can prioritize a particular row."""
        from agentor.daemon import Daemon
        from agentor.runner import make_runner
        cfg = self._cfg()
        scan_once(cfg, self.store)
        d = Daemon(cfg, self.store, make_runner, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        queued = self.store.list_by_status(ItemStatus.QUEUED)
        self.assertEqual(len(queued), 2)
        target = queued[1]  # not the oldest — that's the whole point
        ok = d.dispatch_specific(target.id)
        self.assertTrue(ok)
        import time as _t
        for _ in range(30):
            if self.store.get(target.id).status != ItemStatus.WORKING:
                break
            _t.sleep(0.1)
        final = self.store.get(target.id)
        self.assertEqual(final.status, ItemStatus.AWAITING_REVIEW)


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] do X\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_recovery_requeues_stuck_working(self):
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        # simulate crash: item is WORKING but no worktree created
        self.assertEqual(claimed.status, ItemStatus.WORKING)
        rec = recover_on_startup(self.cfg, self.store)
        self.assertEqual(rec.requeued, [claimed.id])
        self.assertEqual(rec.resumable, [])
        self.assertEqual(self.store.get(claimed.id).status, ItemStatus.QUEUED)
        self.assertIsNone(self.store.get(claimed.id).worktree_path)

    def test_recovery_demotes_resumable_to_queued(self):
        """Item with session_id + live worktree → demoted to QUEUED while
        preserving session_id/worktree/branch so the normal dispatch loop
        picks it up (and the runner detects the resumable state)."""
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        wt.mkdir(parents=True, exist_ok=True)
        self.store.transition(
            claimed.id, ItemStatus.WORKING,
            session_id="abcd-1234", note="session assigned",
        )
        rec = recover_on_startup(self.cfg, self.store)
        self.assertEqual(rec.requeued, [])
        self.assertEqual(len(rec.resumable), 1)
        self.assertEqual(rec.resumable[0].id, claimed.id)
        final = self.store.get(claimed.id)
        self.assertEqual(final.status, ItemStatus.QUEUED)
        self.assertEqual(final.session_id, "abcd-1234")
        self.assertEqual(final.worktree_path, str(wt))
        self.assertEqual(final.branch, br)
        self.assertEqual(final.attempts, 0)


    def test_recovery_uses_previous_settled_state(self):
        """An item that had reached AWAITING_PLAN_REVIEW and was re-queued
        for execute, then crashed mid-work, should revert to QUEUED — the
        execute-phase wait. Verifies the recovery hook to
        previous_settled_status."""
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        self.store.transition(claimed.id, ItemStatus.AWAITING_PLAN_REVIEW)
        self.store.transition(claimed.id, ItemStatus.QUEUED, note="approved")
        # claim again, then "crash" without session_id
        self.store.claim_next_queued(str(wt), br)
        rec = recover_on_startup(self.cfg, self.store)
        # restored to QUEUED (the execute-phase resting state), worktree
        # fields cleared.
        item_after = self.store.get(claimed.id)
        self.assertEqual(item_after.status, ItemStatus.QUEUED)
        self.assertIsNone(item_after.worktree_path)
        self.assertIsNone(item_after.session_id)
        self.assertEqual(rec.requeued, [claimed.id])


class TestAutoRecoverySweep(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text(
            "- [ ] one\n- [ ] two\n- [ ] three\n")
        self.cfg = _mk_config(self.root)
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _seed(self, n: int, err: str) -> str:
        items = self.store.list_by_status(ItemStatus.QUEUED)
        item = items[n]
        self.store.transition(
            item.id, ItemStatus.QUEUED, last_error=err,
        )
        # bump attempts via direct SQL — claim/unclaim cycle would overwrite
        self.store.conn.execute(
            "UPDATE items SET attempts = 2 WHERE id = ?", (item.id,))
        return item.id

    def test_shutdown_error_auto_recovered(self):
        iid = self._seed(0, "do_work: claude killed: agentor shutdown")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)
        item = self.store.get(iid)
        self.assertIsNone(item.last_error)
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.status, ItemStatus.QUEUED)

    def test_max_cost_error_auto_recovered(self):
        iid = self._seed(0, "do_work: claude killed: max_cost_usd=3.0 hit")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)

    def test_dead_session_error_auto_recovered(self):
        iid = self._seed(0, "No conversation found with session ID: abc")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)

    def test_non_benign_error_left_alone(self):
        iid = self._seed(0, "do_work: SyntaxError in foo.py line 42")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertNotIn(iid, rec.auto_recovered)
        item = self.store.get(iid)
        self.assertEqual(item.last_error,
                         "do_work: SyntaxError in foo.py line 42")
        self.assertEqual(item.attempts, 2)

    def test_infra_error_auto_recovered(self):
        iid = self._seed(0, "worktree_add: fatal: not a git repository")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)
        self.assertIsNone(self.store.get(iid).last_error)

    def test_terminal_state_any_error_cleared(self):
        iid = self._seed(0, "do_work: some unique item-level error")
        self.store.transition(iid, ItemStatus.MERGED, last_error="a")
        rec = recover_on_startup(self.cfg, self.store)
        self.assertIn(iid, rec.auto_recovered)
        self.assertIsNone(self.store.get(iid).last_error)


class TestErrorClassifiers(unittest.TestCase):
    def test_infrastructure_classifier_matches_git_failures(self):
        from agentor.runner import _is_infrastructure_error
        self.assertTrue(_is_infrastructure_error(
            "fatal: not a git repository: /x/y"))
        self.assertTrue(_is_infrastructure_error(
            "fatal: a branch named 'agent/foo' already exists"))
        self.assertTrue(_is_infrastructure_error(
            "is already checked out at /x/y"))

    def test_infrastructure_classifier_skips_item_failures(self):
        from agentor.runner import _is_infrastructure_error
        self.assertFalse(_is_infrastructure_error(
            "claude exited 1: random failure"))
        self.assertFalse(_is_infrastructure_error(
            "claude killed: max_turns=30 hit"))
        self.assertFalse(_is_infrastructure_error(""))

    def test_dead_session_classifier(self):
        from agentor.runner import _is_dead_session_error
        self.assertTrue(_is_dead_session_error(
            "claude exited 1: No conversation found with session ID: abc"))
        self.assertFalse(_is_dead_session_error("anything else"))

    def test_shutdown_classifier(self):
        from agentor.runner import _is_shutdown_error
        self.assertTrue(_is_shutdown_error("claude killed: agentor shutdown"))
        self.assertFalse(_is_shutdown_error("claude exited 1"))

    def test_error_signature_strips_variable_bits(self):
        from agentor.runner import _error_signature
        # Same class → same signature, different magnitude.
        s1 = _error_signature("claude killed: max_turns=30 hit (30 turns)")
        s2 = _error_signature("claude killed: max_turns=50 hit (50 turns)")
        self.assertEqual(s1, s2)
        # Different class → different signature.
        s3 = _error_signature(
            "claude killed: max_cost_usd=3.0 hit (~$3.02)")
        self.assertNotEqual(s1, s3)


class TestTransientClassifier(unittest.TestCase):
    def test_retryable_errors(self):
        from agentor.runner import _is_transient_error
        for msg in (
            "claude exited 1: 429 rate limited",
            "claude exited 1: HTTP 500 Internal Server Error",
            "claude exited 1: HTTP 502 Bad Gateway",
            "claude exited 1: HTTP 503 Service Unavailable",
            "claude exited 1: HTTP 504 Gateway Timeout",
            "claude exited 1: Connection reset by peer",
            "claude exited 1: Connection refused",
            "claude exited 1: Temporary failure in name resolution",
            "claude exited 1: urllib3.exceptions.ReadTimeoutError: read timed out",
            "claude exited 1: overloaded_error",
        ):
            self.assertTrue(
                _is_transient_error(msg, 1.0, 100.0),
                f"{msg!r} should be transient",
            )

    def test_fatal_errors(self):
        from agentor.runner import _is_transient_error
        for msg in (
            "claude exited 1: Invalid API key provided",
            "claude exited 1: unauthorized",
            "claude exited 1: 403 Forbidden",
            "claude exited 1: quota exceeded",
            "claude exited 1: credit balance is too low",
            "claude exited 1: SyntaxError in foo.py line 42",
            "claude killed: max_turns=30 hit",
            "claude killed: max_cost_usd=3.0 hit",
            "do_work: No conversation found with session ID: abc",
            "do_work: claude killed: agentor shutdown",
            "worktree_add: fatal: not a git repository",
            "",
        ):
            self.assertFalse(
                _is_transient_error(msg, 1.0, 100.0),
                f"{msg!r} should not be transient",
            )

    def test_timeout_near_budget_is_not_transient(self):
        """A real hang (elapsed at/above 90% of the configured budget)
        should not retry — the agent isn't coming back."""
        from agentor.runner import _is_transient_error
        self.assertFalse(_is_transient_error(
            "claude timed out after 30s", elapsed=30.0, timeout_seconds=30.0))
        self.assertFalse(_is_transient_error(
            "claude timed out after 30s", elapsed=27.0, timeout_seconds=30.0))

    def test_subbudget_timeout_is_transient(self):
        """A timeout-looking error that fired well before the configured
        budget is a hiccup worth retrying."""
        from agentor.runner import _is_transient_error
        self.assertTrue(_is_transient_error(
            "HTTPConnection.read timed out", elapsed=2.0,
            timeout_seconds=100.0))

    def test_backoff_delay_grows_and_clamps(self):
        from agentor.runner import _backoff_delay, _RETRY_DELAYS
        d0 = _backoff_delay(0)
        d1 = _backoff_delay(1)
        d2 = _backoff_delay(2)
        d9 = _backoff_delay(9)
        # Each delay is within [base, base * (1 + jitter)]
        self.assertGreaterEqual(d0, _RETRY_DELAYS[0])
        self.assertGreater(d1, d0 - 1)  # grows monotonically in expectation
        self.assertGreaterEqual(d2, _RETRY_DELAYS[2])
        # Indexes past the table clamp to the final value
        self.assertGreaterEqual(d9, _RETRY_DELAYS[-1])
        self.assertLess(d9, _RETRY_DELAYS[-1] * 2)


class TestTransientRetry(unittest.TestCase):
    """Fake-claude-based integration: loop retries on transient errors,
    refunds the attempt on success, fails fast on fatal errors."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name) / "proj"
        self.root.mkdir()
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] do X\n")
        self.store = Store(self.root / ".agentor" / "state.db")
        # Replace the module-level sleep with a no-op so the backoff loop
        # doesn't actually wait. Restore on tearDown.
        from agentor import runner as runner_mod
        self._runner_mod = runner_mod
        self._orig_sleep = runner_mod._sleep
        runner_mod._sleep = lambda _s: None

    def tearDown(self):
        self._runner_mod._sleep = self._orig_sleep
        self.store.close()
        self.td.cleanup()

    def _cfg(self, bin_dir: Path, transient_retries: int,
             max_attempts: int = 2) -> Config:
        return Config(
            project_name=self.root.name, project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                runner="claude", pool_size=1, max_attempts=max_attempts,
                command=[str(bin_dir / "claude"), "-p", "{prompt}"],
                plan_prompt_template="PLAN: {title}",
                execute_prompt_template="EXEC: {title}\nplan={plan}",
                timeout_seconds=10,
                transient_retries=transient_retries,
            ),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )

    def _run(self, cfg: Config):
        scan_once(cfg, self.store)
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner_inst = make_runner(cfg, self.store)
        return claimed, runner_inst.run(claimed)

    def test_transient_then_success_does_not_charge_attempt(self):
        counter = Path(self.td.name) / "counter"
        bin_dir = Path(self.td.name) / "bin"
        # Two transient 429s then a normal plan response on the third call.
        script = f"""
N=$(cat "{counter}" 2>/dev/null || echo 0)
N=$((N+1))
echo "$N" > "{counter}"
if [ "$N" -lt 3 ]; then
  echo "Error: 429 rate limited" >&2
  exit 1
fi
echo "plan text ok"
"""
        _write_fake_claude(bin_dir, script)
        cfg = self._cfg(bin_dir, transient_retries=3)
        claimed, result = self._run(cfg)
        self.assertIsNone(result.error, msg=result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.AWAITING_PLAN_REVIEW)
        # claim_next_queued charges one attempt; in-dispatch retries don't
        # add more — a transient flap must not burn max_attempts.
        self.assertEqual(refreshed.attempts, 1)
        self.assertEqual(counter.read_text().strip(), "3")
        transcript = (
            self.root / ".agentor" / "transcripts"
            / f"{claimed.id}.plan.log"
        ).read_text()
        self.assertIn("RETRY 1/3", transcript)
        self.assertIn("RETRY 2/3", transcript)
        self.assertIn("429", transcript)

    def test_transient_budget_exhausted_surfaces_error(self):
        counter = Path(self.td.name) / "counter"
        bin_dir = Path(self.td.name) / "bin"
        script = f"""
N=$(cat "{counter}" 2>/dev/null || echo 0)
N=$((N+1))
echo "$N" > "{counter}"
echo "Error: 429 rate limited" >&2
exit 1
"""
        _write_fake_claude(bin_dir, script)
        cfg = self._cfg(bin_dir, transient_retries=2)
        claimed, result = self._run(cfg)
        self.assertIsNotNone(result.error)
        self.assertIn("429", result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.ERRORED)
        # 1 initial + 2 retries = 3 invocations.
        self.assertEqual(counter.read_text().strip(), "3")

    def test_non_transient_error_fails_fast(self):
        counter = Path(self.td.name) / "counter"
        bin_dir = Path(self.td.name) / "bin"
        script = f"""
N=$(cat "{counter}" 2>/dev/null || echo 0)
N=$((N+1))
echo "$N" > "{counter}"
echo "SyntaxError: invalid syntax at foo.py line 42" >&2
exit 1
"""
        _write_fake_claude(bin_dir, script)
        cfg = self._cfg(bin_dir, transient_retries=3)
        claimed, result = self._run(cfg)
        self.assertIsNotNone(result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.ERRORED)
        # No retry — fatal classifier short-circuits immediately.
        self.assertEqual(counter.read_text().strip(), "1")
        transcript = (
            self.root / ".agentor" / "transcripts"
            / f"{claimed.id}.plan.log"
        ).read_text()
        self.assertNotIn("RETRY", transcript)

    def test_transient_retries_zero_disables_loop(self):
        counter = Path(self.td.name) / "counter"
        bin_dir = Path(self.td.name) / "bin"
        script = f"""
N=$(cat "{counter}" 2>/dev/null || echo 0)
N=$((N+1))
echo "$N" > "{counter}"
echo "Error: 429 rate limited" >&2
exit 1
"""
        _write_fake_claude(bin_dir, script)
        cfg = self._cfg(bin_dir, transient_retries=0)
        claimed, result = self._run(cfg)
        self.assertIsNotNone(result.error)
        refreshed = self.store.get(claimed.id)
        self.assertEqual(refreshed.status, ItemStatus.ERRORED)
        self.assertEqual(counter.read_text().strip(), "1")


class TestProcRegistry(unittest.TestCase):
    def test_register_kill_unregister(self):
        from agentor.runner import ProcRegistry
        reg = ProcRegistry()
        # spawn a real long-running child so kill_all has something to kill
        p = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        reg.register("a", p)
        killed = reg.kill_all(log=lambda m: None)
        self.assertEqual(killed, 1)
        # after a brief moment, process should be dead
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("process not killed by kill_all")
        self.assertIsNotNone(p.returncode)

    def test_kill_all_skips_already_exited(self):
        from agentor.runner import ProcRegistry
        reg = ProcRegistry()
        p = subprocess.Popen(["true"], start_new_session=True)
        p.wait()
        reg.register("a", p)
        # already exited — kill_all reports 0 live, returns 0
        self.assertEqual(reg.kill_all(log=lambda m: None), 0)

    def test_unregister_removes(self):
        from agentor.runner import ProcRegistry
        reg = ProcRegistry()
        p = subprocess.Popen(["true"], start_new_session=True)
        p.wait()
        reg.register("a", p)
        reg.unregister("a")
        self.assertEqual(reg.kill_all(log=lambda m: None), 0)


class _ScriptedRunner(StubRunner):
    """Test runner whose do_work outcome is controlled by a callable. Lets
    us drive the runner through the various failure paths without spawning
    claude."""

    def __init__(self, config, store, behavior):
        super().__init__(config, store)
        self.behavior = behavior  # called with (item, worktree)
        self.calls = 0

    def do_work(self, item, worktree):
        self.calls += 1
        return self.behavior(item, worktree, self.calls)


class TestFailureLandsInErrored(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] failing task\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=3),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _dispatch(self, runner) -> None:
        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        runner.run(claimed)

    def test_first_failure_parks_item_in_errored(self):
        """Any agent-side failure goes straight to ERRORED — no retry loop,
        no attempts-bumping bounce-back to QUEUED. Daemon is free to pick the
        next queued item; operator re-queues via `revert` once fixed."""
        def always_fail(item, wt, n):
            raise RuntimeError("claude killed: max_turns=30 hit (30 turns)")

        runner = _ScriptedRunner(self.cfg, self.store, always_fail)
        self._dispatch(runner)
        errored = self.store.list_by_status(ItemStatus.ERRORED)
        self.assertEqual(len(errored), 1)
        self.assertIn("max_turns", errored[0].last_error)
        self.assertEqual(self.store.list_by_status(ItemStatus.QUEUED), [])


class TestRunnerRecordsFailures(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=3),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_do_work_exception_records_failure_row(self):
        class _Boom(StubRunner):
            def do_work(self, item, wt):
                raise RuntimeError("kaboom")

        item = self.store.list_by_status(ItemStatus.QUEUED)[0]
        wt, br = plan_worktree(self.cfg, item)
        claimed = self.store.claim_next_queued(str(wt), br)
        _Boom(self.cfg, self.store).run(claimed)
        rows = self.store.list_failures(claimed.id)
        self.assertEqual(len(rows), 1)
        self.assertIn("kaboom", rows[0]["error"])
        self.assertEqual(rows[0]["phase"], "do_work")
        self.assertEqual(rows[0]["attempt"], 1)
        self.assertIsNotNone(rows[0]["error_sig"])


class TestDaemonPause(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] task\n")
        self.cfg = Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=1, max_attempts=3),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )
        self.store = Store(self.root / ".agentor" / "state.db")
        scan_once(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_infra_error_pauses_daemon_and_sets_alert(self):
        from agentor.daemon import Daemon
        from agentor.runner import InfrastructureError

        def factory(cfg, store):
            r = StubRunner(cfg, store)

            def boom(item, wt):
                raise InfrastructureError("fatal: not a git repository")
            r.do_work = boom
            return r

        d = Daemon(self.cfg, self.store, factory, scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading as _t
        import time as _tm
        t = _t.Thread(target=d.run, daemon=True)
        t.start()
        # wait until the alert flips
        for _ in range(40):
            if d.system_alert is not None:
                break
            _tm.sleep(0.05)
        d.stop_event.set()
        t.join(timeout=5)
        self.assertIsNotNone(d.system_alert)
        self.assertTrue(d.paused)
        # dispatch refuses while paused
        self.assertFalse(d._dispatch_one())

    def test_clear_alert_resumes_dispatch(self):
        from agentor.daemon import Daemon
        d = Daemon(self.cfg, self.store, lambda c, s: StubRunner(c, s),
                   scan_interval=0.05, log=lambda m: None,
                   install_signals=False)
        d.system_alert = "previously broken"
        d.paused = True
        d.clear_alert()
        self.assertIsNone(d.system_alert)
        self.assertFalse(d.paused)


class TestResumePoolGate(unittest.TestCase):
    """Resuming WORKING items at daemon startup must respect pool_size, so
    that an operator who dropped the pool to 0 to halt work doesn't see
    in-flight sessions silently revived on the next restart."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] one\n- [ ] two\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _cfg(self, pool_size: int) -> Config:
        return Config(
            project_name="t", project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(runner="stub", pool_size=pool_size,
                              max_attempts=1),
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )

    def _seed_resumable(self, cfg: Config, n: int) -> list[str]:
        """Create n WORKING items with session_id + live worktree dir so the
        recovery sweep returns them as resumable."""
        scan_once(cfg, self.store)
        ids: list[str] = []
        for q in self.store.list_by_status(ItemStatus.QUEUED)[:n]:
            wt, br = plan_worktree(cfg, q)
            claimed = self.store.claim_next_queued(str(wt), br)
            wt.mkdir(parents=True, exist_ok=True)
            self.store.transition(
                claimed.id, ItemStatus.WORKING,
                session_id=f"sess-{claimed.id[:6]}",
                note="test: resumable session",
            )
            ids.append(claimed.id)
        return ids

    def _inert_factory(self):
        """Runner whose `.run` is a no-op — exercises the dispatch path
        without hitting git worktree machinery."""
        def factory(cfg, store):
            r = StubRunner(cfg, store)

            def noop(item):
                from agentor.runner import RunResult
                return RunResult(
                    item.id, Path(item.worktree_path or "/tmp"),
                    item.branch or "br", "noop", [], "",
                )
            r.run = noop
            return r
        return factory

    def test_pool_zero_skips_all_resumes(self):
        """With pool_size=0 the daemon can't claim anything, so resumable
        items sit in QUEUED waiting for the pool to open. Nothing is
        dispatched; session_id + worktree are preserved for later pickup."""
        from agentor.daemon import Daemon
        cfg = self._cfg(pool_size=0)
        ids = self._seed_resumable(cfg, 2)
        d = Daemon(cfg, self.store, self._inert_factory(), scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading as _t
        import time as _tm
        t = _t.Thread(target=d.run, daemon=True)
        t.start()
        _tm.sleep(0.2)
        d.stop_event.set()
        t.join(timeout=5)
        self.assertEqual(d.stats.dispatched, 0)
        for i in ids:
            it = self.store.get(i)
            self.assertEqual(it.status, ItemStatus.QUEUED)
            self.assertTrue(it.session_id)
            self.assertTrue(it.worktree_path)

    def test_pool_one_resumes_resumable_items(self):
        """With pool_size=1 the dispatch loop claims resumable items one
        at a time. Because the noop runner returns immediately, both items
        get through over the 0.2s window."""
        from agentor.daemon import Daemon
        cfg = self._cfg(pool_size=1)
        self._seed_resumable(cfg, 2)
        d = Daemon(cfg, self.store, self._inert_factory(), scan_interval=0.05,
                   log=lambda m: None, install_signals=False)
        import threading as _t
        import time as _tm
        t = _t.Thread(target=d.run, daemon=True)
        t.start()
        _tm.sleep(0.2)
        d.stop_event.set()
        t.join(timeout=5)
        self.assertGreaterEqual(d.stats.dispatched, 1)


class TestBranchCleanupHelpers(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)

    def tearDown(self):
        self.td.cleanup()

    def test_branch_checked_out_at_finds_holding_worktree(self):
        from agentor import git_ops
        wt = self.root / "wt-x"
        git_ops.worktree_add(self.root, wt, "agent/x", "main")
        held = git_ops.branch_checked_out_at(self.root, "agent/x")
        self.assertIsNotNone(held)
        self.assertEqual(held.resolve(), wt.resolve())

    def test_branch_checked_out_at_returns_none_when_unheld(self):
        from agentor import git_ops
        # branch exists but no worktree holds it
        subprocess.run(["git", "branch", "agent/y", "main"],
                       cwd=self.root, check=True)
        self.assertIsNone(
            git_ops.branch_checked_out_at(self.root, "agent/y"),
        )

    def test_fast_forward_to_base_advances_worktree(self):
        from agentor import git_ops
        wt = self.root / "wt-ff"
        git_ops.worktree_add(self.root, wt, "agent/ff", "main")
        wt_sha_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt, capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Advance main in the root checkout while feature sits at fork point.
        (self.root / "new.md").write_text("new\n")
        subprocess.run(["git", "add", "new.md"], cwd=self.root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance"],
                       cwd=self.root, check=True, capture_output=True)

        advanced, note = git_ops.fast_forward_to_base(wt, "main")

        self.assertTrue(advanced, note)
        self.assertIsNone(note)
        wt_sha_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(wt_sha_after, wt_sha_before)
        self.assertTrue((wt / "new.md").exists(),
                        "fast-forward should pull the new file into the worktree")

    def test_fast_forward_to_base_refuses_when_diverged(self):
        from agentor import git_ops
        wt = self.root / "wt-div"
        git_ops.worktree_add(self.root, wt, "agent/div", "main")
        # Feature commits.
        (wt / "feat.md").write_text("feat\n")
        subprocess.run(["git", "add", "feat.md"], cwd=wt, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat commit"],
                       cwd=wt, check=True, capture_output=True)
        # Main advances independently — histories diverge.
        (self.root / "main.md").write_text("main\n")
        subprocess.run(["git", "add", "main.md"], cwd=self.root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "main commit"],
                       cwd=self.root, check=True, capture_output=True)
        feat_sha_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt, capture_output=True, text=True, check=True,
        ).stdout.strip()

        advanced, note = git_ops.fast_forward_to_base(wt, "main")

        self.assertFalse(advanced)
        self.assertIsNotNone(note)
        feat_sha_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(feat_sha_after, feat_sha_before,
                         "ff refusal must leave the worktree untouched")


class TestRunStreamJsonSubprocess(unittest.TestCase):
    """Direct coverage for the shared stream-json subprocess helper."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        self.transcript = self.root / "transcript.log"

    def tearDown(self):
        self.td.cleanup()

    def _fake_cli(self, script: str) -> Path:
        return _write_fake_cli(self.root / "bin", "fakecli", script)

    def test_dispatches_events_and_writes_transcript(self):
        from agentor.runner import _run_stream_json_subprocess

        cli = self._fake_cli(
            r"""printf '%s\n' '{"type":"hello","id":1}'
printf '%s\n' '{"type":"hello","id":2}'
printf '%s\n' 'not-json-should-be-ignored'
"""
        )
        events: list[dict] = []

        def on_event(ev: dict) -> None:
            events.append(ev)
            return None

        stdout, stderr, rc, timed_out, cap = _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=5,
            transcript_path=self.transcript,
            proc_registry=None,
            item_key="k",
            fnfe_hint="missing",
            on_event=on_event,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(timed_out)
        self.assertIsNone(cap)
        self.assertEqual([e["id"] for e in events], [1, 2])
        body = self.transcript.read_text()
        self.assertIn('"type":"hello"', body)
        self.assertIn("exit: 0", body)

    def test_cap_reason_kills_child_and_is_returned(self):
        from agentor.runner import _run_stream_json_subprocess

        # Emit one event then sleep long enough that the helper must kill
        # us to finish — proves cap_reason short-circuits the loop.
        cli = self._fake_cli(
            r"""printf '%s\n' '{"type":"first"}'
sleep 3
"""
        )

        def on_event(ev: dict) -> str | None:
            return "stop-now" if ev.get("type") == "first" else None

        stdout, stderr, rc, timed_out, cap = _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=5,
            transcript_path=self.transcript,
            proc_registry=None,
            item_key="k",
            fnfe_hint="missing",
            on_event=on_event,
        )
        self.assertEqual(cap, "stop-now")
        self.assertFalse(timed_out)
        self.assertNotEqual(rc, 0)

    def test_timeout_sets_flag_and_kills(self):
        from agentor.runner import _run_stream_json_subprocess

        cli = self._fake_cli("sleep 3\n")

        def on_event(ev: dict) -> None:
            return None

        stdout, stderr, rc, timed_out, cap = _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=1,
            transcript_path=self.transcript,
            proc_registry=None,
            item_key="k",
            fnfe_hint="missing",
            on_event=on_event,
        )
        self.assertTrue(timed_out)
        self.assertIsNone(cap)

    def test_fnfe_raises_hint(self):
        from agentor.runner import _run_stream_json_subprocess

        with self.assertRaises(RuntimeError) as cm:
            _run_stream_json_subprocess(
                args=[str(self.root / "nope" / "does-not-exist")],
                cwd=self.root,
                timeout_seconds=1,
                transcript_path=self.transcript,
                proc_registry=None,
                item_key="k",
                fnfe_hint="this is the hint",
                on_event=lambda ev: None,
            )
        self.assertIn("this is the hint", str(cm.exception))

    def test_registers_and_unregisters_with_proc_registry(self):
        from agentor.runner import ProcRegistry, _run_stream_json_subprocess

        cli = self._fake_cli('printf "%s\\n" "{}"\n')
        reg = ProcRegistry()

        def on_event(ev: dict) -> None:
            return None

        _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=5,
            transcript_path=self.transcript,
            proc_registry=reg,
            item_key="item-abc",
            fnfe_hint="missing",
            on_event=on_event,
        )
        # After the helper returns, the process must be unregistered —
        # nothing to kill on a subsequent shutdown sweep.
        self.assertEqual(reg.kill_all(log=lambda m: None), 0)

    def test_stdin_payload_is_written(self):
        from agentor.runner import _run_stream_json_subprocess

        # Fake CLI echoes stdin back as a JSON event, then exits.
        cli = self._fake_cli(
            'read line\n'
            'printf \'{"type":"echo","line":"%s"}\\n\' "$line"\n'
        )
        events: list[dict] = []
        _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=5,
            transcript_path=self.transcript,
            proc_registry=None,
            item_key="k",
            fnfe_hint="missing",
            on_event=lambda ev: events.append(ev) or None,
            stdin_payload="hello-from-test\n",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "echo")
        self.assertEqual(events[0]["line"], "hello-from-test")

    def test_stdin_holder_injects_mid_run(self):
        from agentor.runner import (ChildStdinHolder,
                                    _run_stream_json_subprocess)

        # Fake CLI: emit one event, read a second line from stdin and echo
        # it back as a second event. Proves mid-run injection actually
        # reaches the child before it exits.
        cli = self._fake_cli(
            'printf \'{"type":"first"}\\n\'\n'
            'read injected\n'
            'printf \'{"type":"second","line":"%s"}\\n\' "$injected"\n'
        )
        holder = ChildStdinHolder()
        events: list[dict] = []

        def on_event(ev: dict):
            events.append(ev)
            if ev.get("type") == "first":
                holder.write_line("nudge-payload")
            return None

        _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=5,
            transcript_path=self.transcript,
            proc_registry=None,
            item_key="k",
            fnfe_hint="missing",
            on_event=on_event,
            stdin_holder=holder,
        )
        self.assertEqual([e["type"] for e in events], ["first", "second"])
        self.assertEqual(events[1]["line"], "nudge-payload")

    def test_watchdog_closes_stdin_after_result_event(self):
        import time as _time

        from agentor import runner as runner_mod
        from agentor.runner import (ChildStdinHolder,
                                    _run_stream_json_subprocess)

        # Fake CLI: emit a `result` event, then block on stdin read. Once the
        # watchdog closes stdin, `read` returns non-zero, the fallback branch
        # emits `late`, and the CLI exits. Proves the helper no longer hangs
        # even when the caller's `on_event` forgets to close the holder.
        cli = self._fake_cli(
            'printf \'{"type":"result"}\\n\'\n'
            'if read injected; then\n'
            '  printf \'{"type":"late","line":"%s"}\\n\' "$injected"\n'
            'else\n'
            '  printf \'{"type":"late","line":"eof"}\\n\'\n'
            'fi\n'
        )
        holder = ChildStdinHolder()
        events: list[dict] = []

        def on_event(ev: dict):
            events.append(ev)
            return None

        original_grace = runner_mod._STDIN_CLOSE_AFTER_RESULT_SECONDS
        runner_mod._STDIN_CLOSE_AFTER_RESULT_SECONDS = 1
        try:
            started = _time.monotonic()
            stdout, stderr, rc, timed_out, cap = _run_stream_json_subprocess(
                args=[str(cli)],
                cwd=self.root,
                timeout_seconds=15,
                transcript_path=self.transcript,
                proc_registry=None,
                item_key="k",
                fnfe_hint="missing",
                on_event=on_event,
                stdin_holder=holder,
            )
            elapsed = _time.monotonic() - started
        finally:
            runner_mod._STDIN_CLOSE_AFTER_RESULT_SECONDS = original_grace

        self.assertFalse(timed_out,
                         "watchdog must close stdin before outer timeout fires")
        self.assertLess(elapsed, 5,
                        f"helper should return within grace+slop, took {elapsed:.2f}s")
        self.assertEqual([e["type"] for e in events], ["result", "late"])
        self.assertIn(
            "closed stdin after result — protocol drift",
            self.transcript.read_text(),
        )

    def test_watchdog_not_armed_without_result_event(self):
        from agentor.runner import _run_stream_json_subprocess

        cli = self._fake_cli(r"""printf '%s\n' '{"type":"hello"}'
""")

        _run_stream_json_subprocess(
            args=[str(cli)],
            cwd=self.root,
            timeout_seconds=5,
            transcript_path=self.transcript,
            proc_registry=None,
            item_key="k",
            fnfe_hint="missing",
            on_event=lambda ev: None,
        )
        self.assertNotIn(
            "closed stdin after result",
            self.transcript.read_text(),
        )


class TestClaudeRunnerCheckpointInjection(unittest.TestCase):
    """Drive `_invoke_claude_streaming` with a stubbed stream-json helper
    so we can assert the emitter wires into the stdin holder without
    depending on a real claude CLI."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)
        _init_project(self.root)
        (self.root / "backlog.md").write_text("- [ ] Big refactor\n  body\n")
        self.store = Store(self.root / ".agentor" / "state.db")

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def _mk_claude_cfg(self, **agent_overrides) -> Config:
        agent = AgentConfig(
            pool_size=1, runner="claude",
            turn_checkpoint_soft=3,
            turn_checkpoint_hard=0,
            output_token_checkpoint=0,
            **agent_overrides,
        )
        return Config(
            project_name=self.root.name,
            project_root=self.root,
            sources=SourcesConfig(watch=["backlog.md"], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=agent,
            git=GitConfig(base_branch="main", branch_prefix="agent/"),
            review=ReviewConfig(),
        )

    def _stub_helper(self, events: list[dict], captured_writes: list[str],
                     captured_stdin_payload: list[str]):
        """Replacement for `_run_stream_json_subprocess` that feeds the
        caller-provided `on_event` with a canned event list and records any
        mid-run stdin writes through the supplied holder."""
        def helper(*, args, cwd, timeout_seconds, transcript_path,
                   proc_registry, item_key, fnfe_hint, on_event,
                   stdin_payload=None, stdin_holder=None):
            if stdin_payload is not None:
                captured_stdin_payload.append(stdin_payload)
            # Wire the holder to a local sink — mirrors the real helper's
            # `attach` step so `on_event` can write_line() through it.
            if stdin_holder is not None:
                class _Sink:
                    def write(self, text):
                        captured_writes.append(text)
                    def flush(self):
                        pass
                stdin_holder.attach(_Sink())
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("")
            for ev in events:
                on_event(ev)
            if stdin_holder is not None:
                stdin_holder.close()
            return "", "", 0, False, None
        return helper

    def _assistant_event(self, output_tokens: int = 10) -> dict:
        return {
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [],
            },
        }

    def test_soft_threshold_injected_via_stdin_holder(self):
        from agentor import runner as runner_mod
        from agentor.runner import ClaudeRunner

        cfg = self._mk_claude_cfg()
        runner = ClaudeRunner(cfg, self.store)

        scan_once(cfg, self.store)
        item_id = self.store.list_by_status(ItemStatus.QUEUED)[0].id
        wt = self.root / "wt"
        wt.mkdir()
        claimed = self.store.claim_next_queued(str(wt), "agent/big-refactor")

        events = [self._assistant_event() for _ in range(5)]
        captured_writes: list[str] = []
        captured_stdin_payload: list[str] = []
        stub = self._stub_helper(events, captured_writes, captured_stdin_payload)

        original = runner_mod._run_stream_json_subprocess
        runner_mod._run_stream_json_subprocess = stub
        try:
            runner._invoke_claude_streaming(
                claimed,
                ["claude", "-p", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose"],
                wt,
                self.root / ".agentor" / "transcripts" / f"{item_id}.plan.log",
                "plan",
                stdin_prompt="THE-PROMPT",
            )
        finally:
            runner_mod._run_stream_json_subprocess = original

        # Initial prompt framed as a single user JSONL line.
        self.assertEqual(len(captured_stdin_payload), 1)
        initial = json.loads(captured_stdin_payload[0].strip())
        self.assertEqual(initial["type"], "user")
        self.assertEqual(initial["message"]["content"], "THE-PROMPT")

        # Exactly one nudge injected when turn count crossed the soft
        # threshold (soft=3, five assistant events → fires once at turn 3).
        user_lines = [
            json.loads(w.strip()) for w in captured_writes if w.strip()
        ]
        self.assertEqual(len(user_lines), 1)
        self.assertEqual(user_lines[0]["type"], "user")
        self.assertIn("turn", user_lines[0]["message"]["content"].lower())

    def test_result_event_closes_stdin_holder(self):
        """Regression: claude with stream-json input keeps stdin open and
        waits for the next user message after `terminal_reason:completed`.
        The runner must close stdin on the `result` event so the CLI sees
        EOF and exits; otherwise the outer readline blocks forever and the
        item stays WORKING until timeout."""
        from agentor import runner as runner_mod
        from agentor.runner import ClaudeRunner

        cfg = self._mk_claude_cfg()
        runner = ClaudeRunner(cfg, self.store)

        scan_once(cfg, self.store)
        item_id = self.store.list_by_status(ItemStatus.QUEUED)[0].id
        wt = self.root / "wt"
        wt.mkdir()
        claimed = self.store.claim_next_queued(str(wt), "agent/big-refactor")

        holder_closed_mid_flow: list[bool] = []

        def helper(*, args, cwd, timeout_seconds, transcript_path,
                   proc_registry, item_key, fnfe_hint, on_event,
                   stdin_payload=None, stdin_holder=None):
            class _Sink:
                def write(self, text): pass
                def flush(self): pass
            stdin_holder.attach(_Sink())
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text("")
            on_event(self._assistant_event())
            on_event({"type": "result", "stop_reason": "end_turn",
                      "num_turns": 1})
            # Snapshot holder state *after* result event, before the helper
            # itself tears down. If the runner didn't close on result the
            # real CLI would hang here waiting for stdin.
            holder_closed_mid_flow.append(stdin_holder._closed)
            stdin_holder.close()
            return "", "", 0, False, None

        original = runner_mod._run_stream_json_subprocess
        runner_mod._run_stream_json_subprocess = helper
        try:
            runner._invoke_claude_streaming(
                claimed,
                ["claude", "-p", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose"],
                wt,
                self.root / ".agentor" / "transcripts" / f"{item_id}.plan.log",
                "plan",
                stdin_prompt="THE-PROMPT",
            )
        finally:
            runner_mod._run_stream_json_subprocess = original

        self.assertEqual(holder_closed_mid_flow, [True])

    def test_legacy_prompt_template_skips_injection(self):
        from agentor import runner as runner_mod
        from agentor.runner import ClaudeRunner

        cfg = self._mk_claude_cfg(
            command=["claude", "-p", "{prompt}", "--output-format",
                     "stream-json", "--verbose"],
        )
        runner = ClaudeRunner(cfg, self.store)

        scan_once(cfg, self.store)
        item_id = self.store.list_by_status(ItemStatus.QUEUED)[0].id
        wt = self.root / "wt"
        wt.mkdir()
        claimed = self.store.claim_next_queued(str(wt), "agent/big-refactor")

        events = [self._assistant_event() for _ in range(5)]
        captured_writes: list[str] = []
        captured_stdin_payload: list[str] = []
        stub = self._stub_helper(events, captured_writes, captured_stdin_payload)

        original = runner_mod._run_stream_json_subprocess
        runner_mod._run_stream_json_subprocess = stub
        try:
            # Legacy path: stdin_prompt is None → no holder, no stdin writes
            # even though the emitter still observes and crosses threshold.
            runner._invoke_claude_streaming(
                claimed,
                ["claude", "-p", "THE-PROMPT", "--output-format",
                 "stream-json", "--verbose"],
                wt,
                self.root / ".agentor" / "transcripts" / f"{item_id}.plan.log",
                "plan",
                stdin_prompt=None,
            )
        finally:
            runner_mod._run_stream_json_subprocess = original

        self.assertEqual(captured_writes, [])
        self.assertEqual(captured_stdin_payload, [])
        # Transcript still got a dry-run observation marker for the crossed
        # soft threshold so operators can see where injection would've landed.
        transcript = (
            self.root / ".agentor" / "transcripts" / f"{item_id}.plan.log"
        ).read_text()
        self.assertIn("checkpoint-observed-dry-run", transcript)


class TestClaudeSettingsHookWiring(unittest.TestCase):
    """The Claude runner must write a per-run settings JSON that registers
    a PreToolUse hook pointing at the shipped read_hook.py, so whole-file
    Read calls on large files are blocked before the tool runs."""

    def setUp(self):
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _cfg(self, threshold: int = 400,
             enforce_grep: bool = True) -> Config:
        return Config(
            project_name="proj", project_root=self.root,
            sources=SourcesConfig(watch=[], exclude=[]),
            parsing=ParsingConfig(mode="checkbox"),
            agent=AgentConfig(
                large_file_line_threshold=threshold,
                enforce_grep_head_limit=enforce_grep,
            ),
            git=GitConfig(), review=ReviewConfig(),
        )

    def _pre_by_matcher(self, data: dict) -> dict[str, dict]:
        return {e["matcher"]: e for e in data["hooks"]["PreToolUse"]}

    def test_settings_written_with_threshold(self):
        cfg = self._cfg(threshold=400, enforce_grep=False)
        path = write_claude_settings(cfg, "abcdef1234")
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "Read")
        cmd = pre[0]["hooks"][0]["command"]
        self.assertIn("AGENTOR_READ_THRESHOLD=400", cmd)
        self.assertIn("read_hook.py", cmd)
        # The command must reference an absolute hook path so claude can
        # invoke it regardless of cwd.
        hook_path_token = [t for t in cmd.split() if t.endswith("read_hook.py")][0]
        self.assertTrue(Path(hook_path_token).is_absolute())
        self.assertTrue(Path(hook_path_token).exists())

    def test_settings_disabled_when_all_hooks_off(self):
        cfg = self._cfg(threshold=0, enforce_grep=False)
        path = write_claude_settings(cfg, "abcdef1234")
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        # Hooks object present but empty — claude still accepts --settings
        # without choking, but no PreToolUse gate is registered.
        self.assertEqual(data.get("hooks"), {})

    def test_grep_matcher_registered_by_default(self):
        """Default config enables the Grep head_limit hook."""
        cfg = self._cfg()
        path = write_claude_settings(cfg, "abcdef1234")
        data = json.loads(path.read_text())
        by_matcher = self._pre_by_matcher(data)
        self.assertIn("Grep", by_matcher)
        cmd = by_matcher["Grep"]["hooks"][0]["command"]
        self.assertIn("grep_hook.py", cmd)
        hook_path_token = [t for t in cmd.split() if t.endswith("grep_hook.py")][0]
        self.assertTrue(Path(hook_path_token).is_absolute())
        self.assertTrue(Path(hook_path_token).exists())

    def test_grep_matcher_absent_when_disabled(self):
        cfg = self._cfg(enforce_grep=False)
        path = write_claude_settings(cfg, "abcdef1234")
        data = json.loads(path.read_text())
        pre = data["hooks"].get("PreToolUse", [])
        self.assertNotIn("Grep", [e.get("matcher") for e in pre])

    def test_both_hooks_registered_together(self):
        cfg = self._cfg(threshold=400, enforce_grep=True)
        path = write_claude_settings(cfg, "abcdef1234")
        data = json.loads(path.read_text())
        by_matcher = self._pre_by_matcher(data)
        self.assertEqual(set(by_matcher), {"Read", "Grep"})

    def test_default_claude_command_contains_settings_placeholder(self):
        cmd = _default_claude_command()
        self.assertIn("--settings", cmd)
        self.assertIn("{settings_path}", cmd)

    def test_default_command_formats_with_settings_path(self):
        cfg = self._cfg(threshold=400)
        settings = write_claude_settings(cfg, "item-xyz")
        args = [
            a.format(prompt="hi", model="claude", settings_path=str(settings))
            for a in _default_claude_command()
        ]
        self.assertIn(str(settings), args)


class TestStreamStateRateLimitHarvester(unittest.TestCase):
    """Passive capture of any `rate_limit`/`ratelimits`/`anthropic-ratelimit-*`
    fields the claude CLI might surface on future versions. Current CLI strips
    these so the happy path is "absent → no envelope key"."""

    def _new(self):
        from agentor.runner import _StreamState
        return _StreamState(item_id="i1", phase="execute")

    def test_absent_fields_leave_envelope_clean(self):
        state = self._new()
        state.ingest({"type": "system", "subtype": "init",
                      "session_id": "s"})
        state.ingest({"type": "result", "num_turns": 1,
                      "stop_reason": "end_turn"})
        env = state.envelope()
        self.assertNotIn("rate_limits", env)

    def test_rate_limit_on_result_event_captured(self):
        state = self._new()
        sample = {
            "session": {"used": 123, "limit": 1000,
                        "reset_at": "2026-04-19T22:00:00Z"},
            "weekly": {"used": 4500, "limit": 10000,
                       "reset_at": "2026-04-26T00:00:00Z"},
        }
        state.ingest({"type": "result", "num_turns": 1,
                      "stop_reason": "end_turn",
                      "rate_limit": sample})
        self.assertEqual(state.envelope()["rate_limits"], sample)

    def test_rate_limit_nested_in_message_usage(self):
        # Future CLI may drop the hint onto `message.usage` like Anthropic's
        # raw responses do — harvester checks both nesting points.
        state = self._new()
        sample = {"tokens_remaining": 50_000}
        state.ingest({
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 10, "output_tokens": 20,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ratelimits": sample,
                },
            },
        })
        self.assertEqual(state.envelope()["rate_limits"], sample)

    def test_latest_wins_when_multiple_samples(self):
        state = self._new()
        first = {"session": {"used": 100, "limit": 1000}}
        second = {"session": {"used": 200, "limit": 1000}}
        state.ingest({"type": "system", "subtype": "init",
                      "session_id": "s", "rate_limit": first})
        state.ingest({"type": "result", "num_turns": 1,
                      "stop_reason": "end_turn",
                      "rate_limit": second})
        self.assertEqual(state.envelope()["rate_limits"], second)

    def test_non_dict_rate_limit_ignored(self):
        state = self._new()
        state.ingest({"type": "result", "num_turns": 1,
                      "stop_reason": "end_turn",
                      "rate_limit": "not-a-dict"})
        self.assertNotIn("rate_limits", state.envelope())


if __name__ == "__main__":
    unittest.main()
