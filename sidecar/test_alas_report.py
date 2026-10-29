import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import alas_report
from alas_report import build_report, format_report, main

FIXTURE = Path(__file__).parent / "fixtures" / "alas_sample.log"
TS = "2026-10-29 20:{:02d}:{:02d}.000 | INFO | {}"


def log(*items):
    """items: (minute, second, text) -> synthetic ALAS log text."""
    return "\n".join(TS.format(m, s, t) for m, s, t in items) + "\n"


def by_name(report, name):
    return [t for t in report["tasks"] if t["name"] == name]


class FixtureReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report(FIXTURE.read_text(encoding="utf-8", errors="replace"))

    def test_tasks_in_start_order(self):
        names = [t["name"] for t in self.report["tasks"]]
        self.assertEqual(
            names,
            ["Commission", "Restart", "Commission", "Tactical", "Research", "Dorm",
             "Meowfficer", "Guild", "Restart", "Guild"],
        )

    def test_first_commission_is_no_commission(self):
        first = by_name(self.report, "Commission")[0]
        self.assertEqual(first["started"], "2026-10-29 20:42:14")
        self.assertEqual(first["ended"], "2026-10-29 20:42:19")
        self.assertFalse(first["commission_scan"])
        self.assertEqual(first["status"], "no_commission")
        self.assertAlmostEqual(first["duration_s"], 5.427, places=2)

    def test_non_commission_task_is_done_with_no_scan_flag(self):
        tactical = by_name(self.report, "Tactical")[0]
        self.assertEqual(tactical["status"], "done")
        self.assertIsNone(tactical["commission_scan"])

    def test_last_guild_is_incomplete(self):
        last = self.report["tasks"][-1]
        self.assertEqual((last["name"], last["status"]), ("Guild", "incomplete"))
        self.assertIsNone(last["ended"])
        self.assertIsNone(last["duration_s"])

    def test_counters(self):
        self.assertEqual(self.report["restarts"], 1)
        self.assertEqual(self.report["aborts"], 1)
        self.assertEqual(self.report["game_not_running"], 1)
        self.assertEqual(self.report["errors"], 2)

    def test_anomalies_mention_no_commission_and_summary(self):
        joined = "\n".join(self.report["anomalies"])
        self.assertIn("Commission", joined)
        self.assertIn("commission scan", joined)
        self.assertIn("restart", joined.lower())
        # last task incomplete is not an anomaly
        self.assertFalse([a for a in self.report["anomalies"] if "incomplete" in a.lower()])


class CommissionStatus(unittest.TestCase):
    def test_commission_with_detect_is_done(self):
        text = log(
            (0, 0, "Scheduler: Start task `Commission`"),
            (0, 5, "═══ Commission detect ═══"),
            (1, 0, "Scheduler: End task `Commission`"),
        )
        task = build_report(text)["tasks"][0]
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["commission_scan"])
        self.assertEqual(task["duration_s"], 60.0)
        self.assertEqual(task["started"], "2026-10-29 20:00:00")
        self.assertEqual(task["ended"], "2026-10-29 20:01:00")

    def test_detect_outside_window_does_not_count(self):
        text = log(
            (0, 0, "Commission detect"),
            (1, 0, "Scheduler: Start task `Commission`"),
            (2, 0, "Scheduler: End task `Commission`"),
            (3, 0, "Commission detect"),
        )
        task = build_report(text)["tasks"][0]
        self.assertEqual(task["status"], "no_commission")

    def test_screenshot_pattern_is_no_commission(self):
        text = log(
            (0, 0, "Scheduler: Start task `Commission`"),
            (0, 3, "Game not running"),
            (0, 4, "Task call: Restart"),
            (0, 5, "Scheduler: End task `Commission`"),
        )
        report = build_report(text)
        task = report["tasks"][0]
        self.assertEqual(task["status"], "no_commission")
        self.assertFalse(task["commission_scan"])
        self.assertEqual(report["game_not_running"], 1)
        self.assertTrue(any("no commission scan" in a.lower() or "commission scan" in a
                            for a in report["anomalies"]))

    def test_detect_in_other_task_is_ignored(self):
        text = log(
            (0, 0, "Scheduler: Start task `Reward`"),
            (0, 1, "Commission detect"),
            (0, 2, "Scheduler: End task `Reward`"),
        )
        task = build_report(text)["tasks"][0]
        self.assertEqual(task["status"], "done")
        self.assertIsNone(task["commission_scan"])


class Incomplete(unittest.TestCase):
    def test_start_without_end_is_incomplete(self):
        text = log((0, 0, "Scheduler: Start task `Research`"))
        report = build_report(text)
        task = report["tasks"][0]
        self.assertEqual(task["status"], "incomplete")
        self.assertIsNone(task["ended"])
        self.assertIsNone(task["duration_s"])
        self.assertEqual(report["anomalies"], [])  # last task: not an anomaly

    def test_incomplete_before_another_task_is_anomaly(self):
        text = log(
            (0, 0, "Scheduler: Start task `Research`"),
            (1, 0, "Scheduler: Start task `Dorm`"),
            (2, 0, "Scheduler: End task `Dorm`"),
        )
        report = build_report(text)
        self.assertEqual(report["tasks"][0]["status"], "incomplete")
        self.assertEqual(report["tasks"][1]["status"], "done")
        self.assertTrue(any("Research" in a and "incomplete" in a.lower()
                            for a in report["anomalies"]))


class NextRun(unittest.TestCase):
    SAVE = "Save config ./config/alas.json, {}.Scheduler.NextRun=datetime.datetime({})"

    def test_wrapped_datetime_is_joined(self):
        # SAVE appends the closing ")", so build the wrapped line by hand.
        head = self.SAVE.format("Dorm", "2026, 10, 30,")[:-1]
        text = TS.format(0, 1, head) + "\n 2, 1, 41)\n"
        self.assertEqual(build_report(text)["next_runs"], {"Dorm": "2026-10-30 02:01:41"})

    def test_last_saved_value_wins(self):
        text = log(
            (0, 1, self.SAVE.format("Dorm", "2026, 10, 29, 23, 0, 0")),
            (0, 2, self.SAVE.format("Dorm", "2026, 10, 30, 1, 2, 3")),
        )
        self.assertEqual(build_report(text)["next_runs"]["Dorm"], "2026-10-30 01:02:03")

    def test_next_run_before_latest_end_is_anomaly(self):
        text = log(
            (0, 0, "Scheduler: Start task `Dorm`"),
            (5, 0, "Scheduler: End task `Dorm`"),
            (5, 1, self.SAVE.format("Dorm", "2026, 10, 29, 20, 1, 0")),
        )
        anomalies = build_report(text)["anomalies"]
        self.assertTrue(any("Dorm" in a and "NextRun" in a for a in anomalies))

    def test_next_run_after_end_is_fine(self):
        text = log(
            (0, 0, "Scheduler: Start task `Dorm`"),
            (5, 0, "Scheduler: End task `Dorm`"),
            (5, 1, self.SAVE.format("Dorm", "2026, 10, 30, 2, 1, 41")),
        )
        self.assertEqual(build_report(text)["anomalies"], [])

    def test_restart_far_ahead_is_anomaly(self):
        text = log((0, 1, self.SAVE.format("Restart", "2026, 10, 31, 14, 0, 0")))
        anomalies = build_report(text)["anomalies"]
        self.assertTrue(any("Restart scheduled far ahead; check server_update delay" in a
                            for a in anomalies))

    def test_restart_within_24h_is_fine(self):
        text = log((0, 1, self.SAVE.format("Restart", "2026, 10, 30, 14, 0, 0")))
        self.assertEqual(build_report(text)["anomalies"], [])


class Counters(unittest.TestCase):
    def test_restarts_aborts_errors_counted(self):
        text = log(
            (0, 0, "Restarting com.YoStarEN.AzurLane to fix it"),
            (0, 1, "Restarting com.YoStarEN.AzurLane to fix it"),
            (0, 2, "Aborted (core dumped)"),
            (0, 3, "Aborted (core dumped)"),
            (0, 4, "Aborted (core dumped)"),
        ).replace("| INFO | Restarting", "| WARNING | Restarting")
        text += TS.format(0, 5, "x").replace("INFO", "ERROR") + "\n"
        report = build_report(text)
        self.assertEqual(report["restarts"], 2)
        self.assertEqual(report["aborts"], 3)
        self.assertEqual(report["errors"], 1)
        self.assertTrue(any("2" in a and "3" in a for a in report["anomalies"]))

    def test_clean_log_has_no_summary_anomaly(self):
        report = build_report(log((0, 0, "Scheduler: Start task `Dorm`"),
                                  (1, 0, "Scheduler: End task `Dorm`")))
        self.assertEqual((report["restarts"], report["aborts"]), (0, 0))
        self.assertEqual(report["anomalies"], [])

    def test_empty_text(self):
        report = build_report("")
        self.assertEqual(report["tasks"], [])
        self.assertEqual(report["next_runs"], {})


class Format(unittest.TestCase):
    def test_plain_text_no_ansi(self):
        report = build_report(FIXTURE.read_text(encoding="utf-8", errors="replace"))
        out = format_report(report)
        self.assertNotIn("\x1b", out)
        self.assertIn("no_commission", out)
        self.assertIn("Tactical", out)
        self.assertIn("restarts", out)
        self.assertIn("aborts", out)
        self.assertGreaterEqual(len(out.splitlines()), len(report["tasks"]) + len(report["anomalies"]))


class Main(unittest.TestCase):
    def test_missing_file_returns_1(self):
        with tempfile.TemporaryDirectory() as d:
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = main(["--log", str(Path(d) / "nope.log")])
            self.assertEqual(rc, 1)
            self.assertIn("nope.log", err.getvalue())
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_reads_and_prints_without_touching_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "alas.log"
            p.write_bytes(FIXTURE.read_bytes() + b"\xff\xfe bad utf8\n")
            before = {f.name: (f.stat().st_mtime_ns, f.read_bytes()) for f in Path(d).iterdir()}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["--log", str(p)])
            after = {f.name: (f.stat().st_mtime_ns, f.read_bytes()) for f in Path(d).iterdir()}
            self.assertEqual(rc, 0)
            self.assertEqual(before, after)
            self.assertIn("no_commission", out.getvalue())

    def test_module_has_no_write_calls(self):
        src = Path(alas_report.__file__).read_text(encoding="utf-8")
        for needle in ("open(", "write_text", "write_bytes", ".unlink", "os.remove"):
            if needle == "open(":
                self.assertNotIn('"w"', src)
                self.assertNotIn("'w'", src)
            else:
                self.assertNotIn(needle, src)


if __name__ == "__main__":
    unittest.main()
