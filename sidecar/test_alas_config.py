# test_alas_config.py
import json, tempfile, unittest
from pathlib import Path
from alas_config import check_alas_config, apply_alas_config, load_alas_config

GOOD = {"GameManager": {"AutoRestart": True}, "Commission": {"Scheduler": {"Enable": True}}}

class CheckConfig(unittest.TestCase):
    def test_good_passes(self):
        self.assertEqual(check_alas_config(GOOD), [])

    def test_commission_disabled_reported(self):
        bad = json.loads(json.dumps(GOOD))
        bad["Commission"]["Scheduler"]["Enable"] = False
        problems = check_alas_config(bad)
        self.assertEqual(len(problems), 1)
        self.assertIn("Commission.Scheduler.Enable", problems[0])

    def test_missing_key_reported_not_crash(self):
        self.assertEqual(len(check_alas_config({})), 2)

class ApplyConfig(unittest.TestCase):
    def test_apply_writes_and_backs_up(self):
        d = Path(tempfile.mkdtemp())
        p = d / "alas.json"
        p.write_text(json.dumps({"GameManager": {"AutoRestart": False}}))
        backup = apply_alas_config(p)
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text())["GameManager"]["AutoRestart"], False)
        self.assertEqual(check_alas_config(json.loads(p.read_text())), [])

    def test_missing_file_message(self):
        with self.assertRaisesRegex(FileNotFoundError, "mở ALAS GUI"):
            load_alas_config(Path(tempfile.mkdtemp()) / "alas.json")

if __name__ == "__main__":
    unittest.main()
