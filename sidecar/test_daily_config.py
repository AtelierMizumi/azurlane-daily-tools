# test_daily_config.py
import tempfile, unittest
from pathlib import Path
from daily_config import load_daily_config

GOOD = """
device:
  profile: waydroid
  serial: 192.168.240.112:5555
  resolution: 1280x720
alas:
  container_name: alas
  webui_url: http://127.0.0.1:22267
  repo_dir: ../AzurLaneAutoScript
timeouts:
  run_minutes: 60
notify:
  ntfy_url: null
"""

class LoadConfig(unittest.TestCase):
    def _load(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / "daily.yaml"
        p.write_text(text)
        return load_daily_config(p)

    def test_good_config(self):
        cfg = self._load(GOOD)
        self.assertEqual(cfg.device.profile, "waydroid")
        self.assertEqual(cfg.device.resolution, (1280, 720))
        self.assertEqual(cfg.alas_container, "alas")
        self.assertEqual(cfg.run_minutes, 60)
        self.assertIsNone(cfg.ntfy_url)

    def test_phone_profile_rejected(self):
        with self.assertRaisesRegex(ValueError, "not supported yet"):
            self._load(GOOD.replace("profile: waydroid", "profile: phone"))

    def test_bad_resolution_rejected(self):
        with self.assertRaisesRegex(ValueError, "resolution"):
            self._load(GOOD.replace("1280x720", "1280-720"))

    def test_missing_key_named_in_error(self):
        with self.assertRaisesRegex(ValueError, "alas.container_name"):
            self._load(GOOD.replace("  container_name: alas\n", ""))

    def test_serial_is_read_not_hardcoded(self):
        cfg = self._load(GOOD.replace("192.168.240.112:5555", "10.0.0.9:5555"))
        self.assertEqual(cfg.device.serial, "10.0.0.9:5555")


class CommissionBotSerial(unittest.TestCase):
    def _daily(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "daily.yaml"
        if text is not None:
            p.write_text(text)
        return p

    def test_serial_from_daily_present(self):
        import commission_bot
        p = self._daily(GOOD.replace("192.168.240.112:5555", "10.0.0.9:5555"))
        self.assertEqual(commission_bot.serial_from_daily(p), "10.0.0.9:5555")

    def test_serial_from_daily_absent_is_none(self):
        import commission_bot
        self.assertIsNone(commission_bot.serial_from_daily(self._daily(None)))

    def test_serial_from_daily_malformed_is_none(self):
        import commission_bot
        self.assertIsNone(commission_bot.serial_from_daily(self._daily("device: [unclosed")))

    def test_load_config_uses_daily_serial_when_bot_yaml_silent(self):
        import commission_bot
        p = self._daily(GOOD.replace("192.168.240.112:5555", "10.0.0.9:5555"))
        cfg = commission_bot.load_config(None, daily_path=p)
        self.assertEqual(cfg["serial"], "10.0.0.9:5555")

    def test_load_config_bot_yaml_serial_wins(self):
        import commission_bot
        p = self._daily(GOOD.replace("192.168.240.112:5555", "10.0.0.9:5555"))
        bot = Path(tempfile.mkdtemp()) / "bot.yaml"
        bot.write_text("serial: 10.1.1.1:5555\n")
        cfg = commission_bot.load_config(str(bot), daily_path=p)
        self.assertEqual(cfg["serial"], "10.1.1.1:5555")

    def test_load_config_falls_back_to_default_without_daily(self):
        import commission_bot
        cfg = commission_bot.load_config(None, daily_path=self._daily(None))
        self.assertEqual(cfg["serial"], commission_bot.DEFAULTS["serial"])


if __name__ == "__main__":
    unittest.main()
