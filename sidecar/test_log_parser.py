import tempfile, time, unittest
from pathlib import Path
from log_parser import parse_lines, newest_log_file

# Strings verified in AzurLaneAutoScript source (see log_parser.py docstring).
HAPPY = [
    "═══ App start ═══",
    "Login success",
    "═══ Commission detect ═══",
    "Choose daily commission",
    "Scheduler: End task `Commission`",
]

class ParseLines(unittest.TestCase):
    def test_happy_path(self):
        s = parse_lines(HAPPY)
        self.assertTrue(s.app_started)
        self.assertTrue(s.login_ok)
        self.assertTrue(s.commission_started)
        self.assertTrue(s.commission_done)
        self.assertEqual(s.errors, [])

    def test_timestamped_prefix_still_matches(self):
        s = parse_lines(["2026-10-29 10:00:00.000 | INFO | Scheduler: End task `Commission`"])
        self.assertTrue(s.commission_done)

    def test_other_task_end_is_not_commission_done(self):
        s = parse_lines(["Scheduler: End task `Reward`"])
        self.assertFalse(s.commission_done)

    def test_start_without_end_is_not_done(self):
        s = parse_lines(HAPPY[:-1] + ["Scheduler: Start task `Commission`"])
        self.assertTrue(s.commission_started)
        self.assertFalse(s.commission_done)

    def test_stuck_error_recorded(self):
        s = parse_lines(["GameStuckError: game stuck"])
        self.assertEqual(len(s.errors), 1)

    def test_traceback_recorded(self):
        s = parse_lines(["Traceback (most recent call last):"])
        self.assertTrue(s.errors)

class NewestLog(unittest.TestCase):
    def test_picks_newest(self):
        d = Path(tempfile.mkdtemp())
        old = d / "a.txt"; old.write_text("old")
        time.sleep(0.05)
        new = d / "b.txt"; new.write_text("new")
        self.assertEqual(newest_log_file(d), new)

    def test_empty_dir_none(self):
        self.assertIsNone(newest_log_file(Path(tempfile.mkdtemp())))

if __name__ == "__main__":
    unittest.main()
