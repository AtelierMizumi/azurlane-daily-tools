# test_watchdog_serial.py
import tempfile, unittest
from pathlib import Path

import watchdog

DAILY = """
device:
  profile: waydroid
  serial: 10.0.0.9:5555
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


class ResolveSerial(unittest.TestCase):
    def _path(self, text):
        p = Path(tempfile.mkdtemp()) / "daily.yaml"
        if text is not None:
            p.write_text(text)
        return p

    def test_daily_present_uses_its_serial(self):
        self.assertEqual(watchdog.resolve_serial(self._path(DAILY)), "10.0.0.9:5555")

    def test_daily_absent_uses_fallback(self):
        self.assertEqual(watchdog.resolve_serial(self._path(None)), watchdog.FALLBACK_SERIAL)

    def test_daily_malformed_falls_back_without_raising(self):
        self.assertEqual(watchdog.resolve_serial(self._path("device: [unclosed")),
                         watchdog.FALLBACK_SERIAL)

    def test_daily_missing_key_falls_back_without_raising(self):
        self.assertEqual(watchdog.resolve_serial(self._path("device: {profile: waydroid}")),
                         watchdog.FALLBACK_SERIAL)


if __name__ == "__main__":
    unittest.main()
