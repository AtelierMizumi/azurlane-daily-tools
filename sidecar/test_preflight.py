import unittest
from daily_config import DeviceCfg
from preflight import check_waydroid

DEV = DeviceCfg(profile="waydroid", serial="192.168.240.112:5555", resolution=(1280, 720))

class FakeRunner:
    def __init__(self, answers):
        self.answers = answers  # key: substring của args -> (rc, out)
        self.calls = []
    def run(self, args, timeout):
        self.calls.append(args)
        for key, val in self.answers.items():
            if key in " ".join(args):
                return val
        return (1, "unexpected: " + " ".join(args))

OK = {
    "waydroid status": (0, "Session:\tRUNNING\nVendor type:\tMAINLINE"),
    "inspect": (0, "true"),
    "wm size": (0, "Physical size: 1280x720"),
}

class Preflight(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(check_waydroid(FakeRunner(OK), "alas", DEV), [])

    def test_session_stopped(self):
        r = FakeRunner({**OK, "waydroid status": (0, "Session:\tSTOPPED")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("session" in p.lower() for p in problems))

    def test_wrong_resolution(self):
        r = FakeRunner({**OK, "wm size": (0, "Physical size: 1080x2400")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("1280x720" in p for p in problems))

    def test_uses_container_adb_not_host(self):
        r = FakeRunner(OK)
        check_waydroid(r, "alas", DEV)
        adb_calls = [c for c in r.calls if "adb" in c]
        self.assertTrue(adb_calls)
        self.assertTrue(all(c[:2] == ["docker", "exec"] for c in adb_calls))

    def test_missing_container_reported(self):
        r = FakeRunner({**OK, "inspect": (1, "No such object")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("alas" in p for p in problems))

    def test_stopped_container_is_not_a_problem(self):
        r = FakeRunner({**OK, "inspect": (0, "false")})
        self.assertEqual(check_waydroid(r, "alas", DEV), [])

if __name__ == "__main__":
    unittest.main()
