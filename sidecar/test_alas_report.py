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
    """Fixture = contiguous, unmodified slice (lines 150-600) of a real ALAS log."""

    @classmethod
    def setUpClass(cls):
        cls.report = build_report(FIXTURE.read_text(encoding="utf-8", errors="replace"))

    def test_tasks_in_start_order(self):
        names = [t["name"] for t in self.report["tasks"]]
        self.assertEqual(names, ["Commission", "Restart", "Commission", "Tactical"])

    def test_first_commission_never_scanned(self):
        first = by_name(self.report, "Commission")[0]
        self.assertEqual(first["started"], "2026-10-29 20:42:14")
        self.assertEqual(first["ended"], "2026-10-29 20:42:19")
        self.assertFalse(first["commission_scan"])
        self.assertEqual(first["status"], "no_commission")
        self.assertAlmostEqual(first["duration_s"], 5.427, places=2)

    def test_second_commission_scanned_and_done(self):
        second = by_name(self.report, "Commission")[1]
        self.assertEqual(second["started"], "2026-10-29 20:44:59")
        self.assertEqual(second["ended"], "2026-10-29 20:46:40")
        self.assertTrue(second["commission_scan"])
        self.assertEqual(second["status"], "done")

    def test_restart_done_with_no_scan_flag(self):
        restart = by_name(self.report, "Restart")[0]
        self.assertEqual(restart["status"], "done")
        self.assertIsNone(restart["commission_scan"])

    def test_log_ends_during_tactical(self):
        last = self.report["tasks"][-1]
        self.assertEqual((last["name"], last["status"]), ("Tactical", "incomplete"))
        self.assertIsNone(last["ended"])
        self.assertIsNone(last["duration_s"])

    def test_next_runs_parsed_from_wrapped_lines(self):
        self.assertEqual(self.report["next_runs"],
                         {"Restart": "2026-10-30 14:00:00",
                          "Commission": "2026-10-29 21:46:38"})

    def test_counters(self):
        self.assertEqual(self.report["restarts"], 0)
        self.assertEqual(self.report["aborts"], 1)
        self.assertEqual(self.report["game_not_running"], 1)
        self.assertEqual(self.report["errors"], 1)

    def test_anomalies_only_first_commission_and_abort_summary(self):
        a = self.report["anomalies"]
        self.assertEqual(len(a), 2)
        self.assertIn("2026-10-29 20:42:19", a[0])
        self.assertIn("commission scan", a[0])
        self.assertIn("1 abort", a[1])


class CommissionStatus(unittest.TestCase):
    def test_commission_with_scan_is_done(self):
        text = log(
            (0, 0, "Scheduler: Start task `Commission`"),
            (0, 5, "═══ COMMISSION SCAN ═══"),
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
            (0, 0, "COMMISSION SCAN"),
            (1, 0, "Scheduler: Start task `Commission`"),
            (2, 0, "Scheduler: End task `Commission`"),
            (3, 0, "COMMISSION SCAN"),
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

    def test_old_commission_detect_text_is_not_a_scan(self):
        text = log(
            (0, 0, "Scheduler: Start task `Commission`"),
            (0, 5, "Commission detect"),
            (1, 0, "Scheduler: End task `Commission`"),
        )
        self.assertEqual(build_report(text)["tasks"][0]["status"], "no_commission")

    def test_lowercase_scan_text_is_not_a_scan(self):
        text = log(
            (0, 0, "Scheduler: Start task `Commission`"),
            (0, 5, "Commission scan"),
            (1, 0, "Scheduler: End task `Commission`"),
        )
        self.assertEqual(build_report(text)["tasks"][0]["status"], "no_commission")

    def test_detect_in_other_task_is_ignored(self):
        text = log(
            (0, 0, "Scheduler: Start task `Reward`"),
            (0, 1, "COMMISSION SCAN"),
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

    def test_datetime_repr_omits_zero_seconds(self):
        # Python repr drops trailing zero second: datetime.datetime(2026, 10, 30, 14, 0)
        text = log((0, 1, self.SAVE.format("Restart", "2026, 10, 30, 14, 0")))
        self.assertEqual(build_report(text)["next_runs"], {"Restart": "2026-10-30 14:00:00"})

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
