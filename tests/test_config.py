import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.config import AgentConfig, GitConfig, _filter_known, load


class TestFilterKnown(unittest.TestCase):
    def test_keeps_known_keys(self):
        got = _filter_known(AgentConfig, {"runner": "stub", "pool_size": 4}, "agent")
        self.assertEqual(got, {"runner": "stub", "pool_size": 4})

    def test_drops_unknown_with_warning(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            got = _filter_known(
                AgentConfig,
                {"runner": "stub", "max_cost_usd": 5.0},
                "agent",
            )
        self.assertEqual(got, {"runner": "stub"})
        err = buf.getvalue()
        self.assertIn("unknown key [agent].max_cost_usd", err)
        self.assertIn("removed or misspelled", err)

    def test_empty_and_none(self):
        self.assertEqual(_filter_known(GitConfig, {}, "git"), {})
        self.assertEqual(_filter_known(GitConfig, None, "git"), {})


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.td = TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _write(self, name: str, body: str) -> Path:
        p = self.dir / name
        p.write_text(body)
        return p

    def test_minimal_config_uses_defaults(self):
        cfg_path = self._write("agentor.toml", "")
        cfg = load(cfg_path)
        # Name defaults to parent directory's name.
        self.assertEqual(cfg.project_name, self.dir.name)
        # Root defaults to "." → resolves to config file's parent.
        self.assertEqual(cfg.project_root, self.dir.resolve())
        self.assertEqual(cfg.agent.runner, "stub")
        self.assertEqual(cfg.agent.pool_size, 0)
        self.assertEqual(cfg.parsing.mode, "frontmatter")
        self.assertEqual(cfg.git.base_branch, "main")
        self.assertEqual(cfg.git.merge_mode, "merge")
        self.assertEqual(cfg.sources.watch,
                         ["docs/backlog/*.md", "docs/ideas/*.md"])

    def test_project_name_explicit(self):
        cfg_path = self._write(
            "agentor.toml",
            '[project]\nname = "myproj"\n',
        )
        cfg = load(cfg_path)
        self.assertEqual(cfg.project_name, "myproj")

    def test_relative_project_root_resolves_against_config_parent(self):
        sub = self.dir / "subproj"
        sub.mkdir()
        cfg_path = self._write(
            "agentor.toml",
            '[project]\nroot = "subproj"\n',
        )
        cfg = load(cfg_path)
        self.assertEqual(cfg.project_root, sub.resolve())
        self.assertTrue(cfg.project_root.is_absolute())

    def test_absolute_project_root_kept_verbatim(self):
        target = self.dir / "elsewhere"
        target.mkdir()
        cfg_path = self._write(
            "agentor.toml",
            f'[project]\nroot = "{target.resolve()}"\n',
        )
        cfg = load(cfg_path)
        self.assertEqual(cfg.project_root, target.resolve())

    @unittest.skipUnless(sys.platform == "darwin",
                         "macOS-specific /tmp → /private/tmp symlink")
    def test_macos_tmp_symlink_resolved(self):
        """On macOS `/tmp` is a symlink to `/private/tmp`. Config should
        produce a resolved root so downstream path logic (relative_to,
        glob filtering) doesn't trip over the alias."""
        # The TemporaryDirectory on macOS typically sits under /var/folders,
        # not /tmp — to exercise the real symlink we need to construct a
        # config living under /tmp.
        tmp_dir = Path("/tmp") / f"agentor-cfgtest-{os.getpid()}"
        tmp_dir.mkdir(exist_ok=True)
        try:
            cfg_path = tmp_dir / "agentor.toml"
            cfg_path.write_text("")
            cfg = load(cfg_path)
            # /tmp resolves to /private/tmp on darwin.
            self.assertEqual(
                str(cfg.project_root),
                str(tmp_dir.resolve()),
            )
            self.assertTrue(
                str(cfg.project_root).startswith("/private/"),
                f"expected /private/... got {cfg.project_root}",
            )
        finally:
            cfg_path.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_unknown_key_in_agent_section_warns_not_raises(self):
        cfg_path = self._write(
            "agentor.toml",
            '[agent]\nrunner = "stub"\nmax_cost_usd = 5.0\n',
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            cfg = load(cfg_path)
        self.assertEqual(cfg.agent.runner, "stub")
        self.assertIn("unknown key [agent].max_cost_usd", buf.getvalue())

    def test_unknown_key_across_sections(self):
        cfg_path = self._write(
            "agentor.toml",
            '[sources]\nbogus = 1\n\n[git]\nnope = "x"\n',
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            load(cfg_path)
        err = buf.getvalue()
        self.assertIn("[sources].bogus", err)
        self.assertIn("[git].nope", err)

    def test_known_keys_honored(self):
        cfg_path = self._write(
            "agentor.toml",
            '[agent]\n'
            'runner = "claude"\n'
            'pool_size = 3\n'
            'single_phase = true\n'
            '[git]\n'
            'base_branch = "develop"\n'
            'merge_mode = "rebase"\n'
            '[parsing]\n'
            'mode = "heading"\n',
        )
        cfg = load(cfg_path)
        self.assertEqual(cfg.agent.runner, "claude")
        self.assertEqual(cfg.agent.pool_size, 3)
        self.assertTrue(cfg.agent.single_phase)
        self.assertEqual(cfg.git.base_branch, "develop")
        self.assertEqual(cfg.git.merge_mode, "rebase")
        self.assertEqual(cfg.parsing.mode, "heading")

    def test_pickup_mode_is_ignored_as_unknown_key(self):
        """The pickup_mode knob was removed — stale configs carrying it
        must not crash the loader, just warn via _filter_known."""
        cfg_path = self._write(
            "agentor.toml",
            '[agent]\n'
            'runner = "stub"\n'
            'pickup_mode = "manual"\n',
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            cfg = load(cfg_path)
        self.assertEqual(cfg.agent.runner, "stub")
        self.assertIn("unknown key [agent].pickup_mode", buf.getvalue())
        self.assertFalse(hasattr(cfg.agent, "pickup_mode"))

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load(self.dir / "does-not-exist.toml")

    def test_invalid_toml_raises(self):
        cfg_path = self._write("bad.toml", "this is = = not toml\n")
        import tomllib
        with self.assertRaises(tomllib.TOMLDecodeError):
            load(cfg_path)


if __name__ == "__main__":
    unittest.main()
