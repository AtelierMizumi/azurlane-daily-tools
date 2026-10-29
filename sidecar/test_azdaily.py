import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import azdaily


class Version(unittest.TestCase):
    def test_version_prints_and_returns_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = azdaily.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(azdaily.VERSION, buf.getvalue())

    def test_version_matches_pyproject(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # python 3.10
            self.skipTest("tomllib needs python 3.11+")
        data = tomllib.loads((azdaily.SIDE / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["version"], azdaily.VERSION)


class Dispatch(unittest.TestCase):
    def test_unknown_command_exits_two(self):
        with self.assertRaises(SystemExit) as cm:
            azdaily.main(["frobnicate"])
        self.assertEqual(cm.exception.code, 2)

    def test_flags_after_command_reach_the_command(self):
        # REMAINDER-style argparse would reject these; manual dispatch must not.
        seen = []
        with mock.patch.dict(azdaily.COMMANDS,
                             {"__probe": ("probe", lambda rest: seen.append(rest) or 0)}):
            code = azdaily.main(["__probe", "--port", "123", "--no-browser"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [["--port", "123", "--no-browser"]])

    def test_help_lists_commands(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            azdaily.main(["--help"])
        for name in azdaily.COMMANDS:
            self.assertIn(name, buf.getvalue())


class ConfigPath(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)

    def test_flag_wins(self):
        self.assertEqual(azdaily._config_path(["--config", "/tmp/x.yaml"]),
                         Path("/tmp/x.yaml"))
        self.assertEqual(azdaily._config_path(["--config=/tmp/y.yaml"]),
                         Path("/tmp/y.yaml"))

    def test_cwd_daily_yaml_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            Path("daily.yaml").write_text("device: {}\n")
            self.assertEqual(azdaily._config_path([]), Path("daily.yaml"))

    def test_falls_back_to_sidecar_copy(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            self.assertEqual(azdaily._config_path([]), azdaily.SIDE / "daily.yaml")


class Init(unittest.TestCase):
    def setUp(self):
        self.side = Path(tempfile.mkdtemp())
        (self.side / "daily.example.yaml").write_text("device: {}\n")
        (self.side / "watchdog.example.yaml").write_text("alas: {}\n")

    def test_creates_daily_yaml_and_prints_next_steps(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = azdaily.cmd_init([], side=self.side)
        self.assertEqual(code, 0)
        self.assertEqual((self.side / "daily.yaml").read_text(), "device: {}\n")
        self.assertIn("device.serial", buf.getvalue())

    def test_refuses_to_overwrite(self):
        with contextlib.redirect_stdout(io.StringIO()):
            azdaily.cmd_init([], side=self.side)
        (self.side / "daily.yaml").write_text("custom: yes\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = azdaily.cmd_init([], side=self.side)
        self.assertEqual(code, 1)
        self.assertEqual((self.side / "daily.yaml").read_text(), "custom: yes\n")
        self.assertIn("kept existing daily.yaml", buf.getvalue())

    def test_watchdog_flag_also_inits_watchdog(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            azdaily.cmd_init(["--watchdog"], side=self.side)
        self.assertTrue((self.side / "watchdog.yaml").exists())

    def test_missing_example_reports_error(self):
        empty = Path(tempfile.mkdtemp())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = azdaily.cmd_init([], side=empty)
        self.assertEqual(code, 1)
        self.assertIn("cannot create", buf.getvalue())
        self.assertFalse((empty / "daily.yaml").exists())


class CommandsTable(unittest.TestCase):
    def test_every_command_is_wired(self):
        for name in ("init", "check", "run", "apply", "report", "bot", "watchdog", "test"):
            self.assertIn(name, azdaily.COMMANDS)


if __name__ == "__main__":
    unittest.main()
