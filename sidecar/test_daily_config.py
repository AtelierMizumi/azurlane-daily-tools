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

if __name__ == "__main__":
    unittest.main()
