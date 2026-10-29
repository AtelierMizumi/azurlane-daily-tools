# test_alas_config.py
import json, tempfile, unittest
from pathlib import Path
from datetime import datetime
from alas_config import (check_alas_config, apply_alas_config, load_alas_config,
                         check_deploy_run, apply_deploy_run, commission_next_run_warning,
                         mission_claim_note, reward_is_due, REQUIRED)

GOOD = {"GameManager": {"GameManager": {"AutoRestart": True}}, "Commission": {"Scheduler": {"Enable": True}},
        "Reward": {"Scheduler": {"Enable": True}, "Reward": {"CollectMission": True}}}

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
        self.assertEqual(len(check_alas_config({})), len(REQUIRED))

    def test_reward_disabled_or_not_collecting_reported(self):
        bad = json.loads(json.dumps(GOOD))
        bad["Reward"]["Scheduler"]["Enable"] = False
        problems = check_alas_config(bad)
        self.assertEqual(len(problems), 1)
        self.assertIn("Reward.Scheduler.Enable", problems[0])
        bad = json.loads(json.dumps(GOOD))
        bad["Reward"]["Reward"]["CollectMission"] = False
        problems = check_alas_config(bad)
        self.assertEqual(len(problems), 1)
        self.assertIn("Reward.Reward.CollectMission", problems[0])

class ApplyConfig(unittest.TestCase):
    def test_apply_writes_and_backs_up(self):
        d = Path(tempfile.mkdtemp())
        p = d / "alas.json"
        p.write_text(json.dumps({"GameManager": {"GameManager": {"AutoRestart": False}}}))
        backup = apply_alas_config(p)
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text())["GameManager"]["GameManager"]["AutoRestart"], False)
        self.assertEqual(check_alas_config(json.loads(p.read_text())), [])

    def test_missing_file_message(self):
        with self.assertRaisesRegex(FileNotFoundError, "mở ALAS GUI"):
            load_alas_config(Path(tempfile.mkdtemp()) / "alas.json")

DEPLOY = """Deploy:
  Webui:
    # --run. Auto-run specified config when startup
    # 'null' default no specified config
    # '["alas"]' specified "alas" config
    # '["alas","alas2"]' specified "alas" "alas2" configs
    Run: null
    # SSL support
    WebuiSSLKey: null
"""


def write(text, newline=None):
    p = Path(tempfile.mkdtemp()) / "deploy.yaml"
    with open(p, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    return p


class DeployRun(unittest.TestCase):  # Ruling 32
    def test_null_run_is_a_problem(self):
        problems = check_deploy_run(write(DEPLOY))
        self.assertEqual(len(problems), 1)
        self.assertIn("Run", problems[0])
        self.assertIn('["alas"]', problems[0])

    def test_alas_list_forms_pass(self):
        for value in ('["alas"]', "['alas']", '["alas","alas2"]', '["other", "alas"]'):
            p = write(DEPLOY.replace("Run: null", f"Run: {value}"))
            self.assertEqual(check_deploy_run(p), [], value)

    def test_other_instance_only_is_a_problem(self):
        p = write(DEPLOY.replace("Run: null", 'Run: ["alas2"]'))
        self.assertEqual(len(check_deploy_run(p)), 1)

    def test_missing_run_line_is_a_problem(self):
        p = write("Deploy:\n  Webui:\n    WebuiSSLKey: null\n")
        self.assertEqual(len(check_deploy_run(p)), 1)

    def test_commented_example_lines_are_not_the_run_key(self):
        p = write("# Run: [\"alas\"]\nDeploy:\n  Run: null\n")
        self.assertEqual(len(check_deploy_run(p)), 1)

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            check_deploy_run(Path(tempfile.mkdtemp()) / "deploy.yaml")

    def test_apply_replaces_only_the_run_line_and_backs_up(self):
        p = write(DEPLOY)
        backup = apply_deploy_run(p)
        self.assertEqual(backup, p.with_name("deploy.yaml.bak"))
        self.assertEqual(backup.read_text(), DEPLOY)
        self.assertEqual(p.read_text(), DEPLOY.replace("Run: null", 'Run: ["alas"]'))
        self.assertEqual(check_deploy_run(p), [])

    def test_apply_adds_alas_to_existing_list_keeping_others(self):
        for before, after in (('["alas2"]', '["alas2", "alas"]'),
                              ("['a','b']", '["a", "b", "alas"]'),
                              ('["alas2","alas3"]', '["alas2", "alas3", "alas"]')):
            p = write(DEPLOY.replace("Run: null", f"Run: {before}"))
            apply_deploy_run(p)
            self.assertEqual(p.read_text(), DEPLOY.replace("Run: null", f"Run: {after}"), before)
            self.assertEqual(check_deploy_run(p), [])

    def test_apply_null_and_empty_set_alas_only(self):
        for before in ("null", "[]", ""):
            p = write(DEPLOY.replace("Run: null", f"Run: {before}"))
            apply_deploy_run(p)
            self.assertEqual(p.read_text(), DEPLOY.replace("Run: null", 'Run: ["alas"]'), before)

    def test_apply_other_instance_keeps_backup_of_original(self):
        text = DEPLOY.replace("Run: null", 'Run: ["alas2"]')
        p = write(text)
        backup = apply_deploy_run(p)
        self.assertEqual(backup.read_text(), text)

    def test_apply_alas_config_non_dict_intermediate_raises_valueerror(self):
        p = Path(tempfile.mkdtemp()) / "alas.json"
        p.write_text(json.dumps({"Commission": None}))
        with self.assertRaisesRegex(ValueError, "Commission"):
            apply_alas_config(p)

    def test_apply_keeps_indent_and_crlf(self):
        text = DEPLOY.replace("\n", "\r\n")
        p = write(text, newline="")
        apply_deploy_run(p)
        self.assertEqual(p.read_bytes().decode(), text.replace("Run: null", 'Run: ["alas"]'))

    def test_apply_appends_when_key_absent(self):
        p = write("Deploy:\n  Webui:\n    WebuiSSLKey: null\n")
        apply_deploy_run(p)
        self.assertEqual(check_deploy_run(p), [])
        self.assertIn("WebuiSSLKey: null", p.read_text())

    def test_check_alas_config_reports_both_when_given_deploy_path(self):
        bad = json.loads(json.dumps(GOOD))
        bad["Commission"]["Scheduler"]["Enable"] = False
        problems = check_alas_config(bad, deploy_path=write(DEPLOY))
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("Commission.Scheduler.Enable" in p for p in problems))
        self.assertTrue(any("Run" in p for p in problems))

    def test_check_alas_config_good_with_good_deploy(self):
        p = write(DEPLOY.replace("Run: null", 'Run: ["alas"]'))
        self.assertEqual(check_alas_config(GOOD, deploy_path=p), [])

    def test_scheduling_key_is_per_task_not_global(self):
        # ALAS reads `<Task>.Scheduler.Enable` (config.py get_next_task iterates
        # data.values() and Function() deep_gets "Scheduler.Enable" inside each task);
        # there is no top-level Scheduler key, so none must be written.
        self.assertIn(("Commission", "Scheduler", "Enable"), REQUIRED)
        self.assertNotIn(("Scheduler", "Enable"), REQUIRED)
        p = Path(tempfile.mkdtemp()) / "alas.json"
        p.write_text("{}")
        apply_alas_config(p)
        self.assertNotIn("Scheduler", json.loads(p.read_text()))


class NextRunWarning(unittest.TestCase):  # Ruling 33
    NOW = datetime(2026, 10, 29, 12, 0, 0)

    def _data(self, value):
        d = json.loads(json.dumps(GOOD))
        if value is not None:
            d["Commission"]["Scheduler"]["NextRun"] = value
        return d

    def test_future_gives_warning(self):
        w = commission_next_run_warning(self._data("2026-10-30 08:00:00"), self.NOW)
        self.assertTrue(w.startswith("WARNING: NextRun"))
        self.assertIn("2026-10-30 08:00:00", w)

    def test_past_or_equal_gives_none(self):
        self.assertIsNone(commission_next_run_warning(self._data("2026-10-29 11:59:59"), self.NOW))
        self.assertIsNone(commission_next_run_warning(self._data("2026-10-29 12:00:00"), self.NOW))

    def test_missing_or_garbage_gives_none(self):
        self.assertIsNone(commission_next_run_warning(self._data(None), self.NOW))
        self.assertIsNone(commission_next_run_warning(self._data("not a date"), self.NOW))
        self.assertIsNone(commission_next_run_warning({}, self.NOW))
        self.assertIsNone(commission_next_run_warning(self._data(12345), self.NOW))


class MissionClaimNote(unittest.TestCase):
    NOW = datetime(2026, 10, 29, 12, 0, 0)

    def _data(self, next_run):
        d = json.loads(json.dumps(GOOD))
        if next_run is not None:
            d["Reward"]["Scheduler"]["NextRun"] = next_run
        return d

    def test_due_runs_in_this_window(self):
        # The daily window usually finds Reward already past its 120-240 min
        # cadence, so the claim happens in the window.
        note = mission_claim_note(self._data("2026-10-29 11:00:00"), self.NOW)
        self.assertIn("is due", note)

    def test_future_slips_to_later_start(self):
        note = mission_claim_note(self._data("2026-10-29 23:49:39"), self.NOW)
        self.assertIn("2026-10-29 23:49:39", note)
        self.assertIn("later container start", note)

    def test_disabled_gives_none(self):
        d = self._data("2026-10-29 23:49:39")
        d["Reward"]["Scheduler"]["Enable"] = False
        self.assertIsNone(mission_claim_note(d, self.NOW))

    def test_not_collecting_gives_none(self):
        d = self._data("2026-10-29 23:49:39")
        d["Reward"]["Reward"]["CollectMission"] = False
        self.assertIsNone(mission_claim_note(d, self.NOW))

    def test_missing_or_garbage_gives_none(self):
        self.assertIsNone(mission_claim_note(self._data(None), self.NOW))
        self.assertIsNone(mission_claim_note(self._data("not a date"), self.NOW))
        self.assertIsNone(mission_claim_note({}, self.NOW))


if __name__ == "__main__":
    unittest.main()


class RewardIsDue(unittest.TestCase):
    NOW = datetime(2026, 10, 30, 2, 0, 0)

    def _data(self, enable=True, next_run="2026-10-29 23:00:00"):
        d = json.loads(json.dumps(GOOD))
        d["Reward"]["Scheduler"]["Enable"] = enable
        d["Reward"]["Scheduler"]["NextRun"] = next_run
        return d

    def test_enabled_and_past_is_due(self):
        self.assertTrue(reward_is_due(self._data(), self.NOW))

    def test_future_or_disabled_or_missing_is_not(self):
        self.assertFalse(reward_is_due(self._data(next_run="2026-10-30 23:00:00"), self.NOW))
        self.assertFalse(reward_is_due(self._data(enable=False), self.NOW))
        self.assertFalse(reward_is_due({}, self.NOW))
        self.assertFalse(reward_is_due(self._data(next_run=None), self.NOW))
