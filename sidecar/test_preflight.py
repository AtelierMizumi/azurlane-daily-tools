import socket, unittest
from unittest import mock
from daily_config import DeviceCfg
import preflight
from preflight import check_waydroid, check_resolution

DEV = DeviceCfg(profile="waydroid", serial="192.0.2.10:5555", resolution=(1280, 720))

class FakeRunner:
    """Scripted runner. `answers` maps a substring of the joined args to (rc, out).

    It also models the container lifecycle (Ruling 31): `docker exec` fails
    while the container is stopped, i.e. when `inspect` answers "false" and no
    `docker start` has been run yet.
    """
    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.started = False

    def _running(self):
        inspect = self.answers.get("inspect", (0, "true"))
        return self.started or inspect[1].strip() == "true"

    def run(self, args, timeout):
        self.calls.append(args)
        if args[:2] == ["docker", "start"]:
            self.started = True
        if args[:2] == ["docker", "exec"] and not self._running():
            return (1, "Error response from daemon: container is not running")
        for key, val in self.answers.items():
            if key in " ".join(args):
                return val
        return (1, "unexpected: " + " ".join(args))

OK = {
    "waydroid status": (0, "Session:\tRUNNING\nContainer:\tRUNNING\nVendor type:\tMAINLINE"),
    "inspect": (0, "true"),
    "adb connect": (0, "connected to 192.0.2.10:5555"),
    "wm size": (0, "Physical size: 1280x720"),
}


def setUpModule():
    # The host adb probe is a real TCP connect; never let a developer's own adb
    # server change test results.
    global _patch
    _patch = mock.patch.object(preflight, "_adb_port_open", lambda *a, **k: False)
    _patch.start()


def tearDownModule():
    _patch.stop()


class SubprocessEnv(unittest.TestCase):
    """`uv run` puts sidecar/.venv first in PATH and sets VIRTUAL_ENV; the waydroid
    launcher is a python script that would then pick the venv python (no dbus)."""

    def _run_with(self, environ):
        import os, subprocess
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "RUNNING", ""))
        with mock.patch.dict(os.environ, environ, clear=True), \
             mock.patch.object(preflight.subprocess, "run", fake):
            rc, out = preflight.SubprocessRunner().run(["waydroid", "status"], 5)
        self.assertEqual((rc, out), (0, "RUNNING"))
        fake.assert_called_once()
        return fake.call_args.kwargs

    def test_env_has_no_virtualenv_and_no_venv_path_entries(self):
        kwargs = self._run_with({
            "VIRTUAL_ENV": "/home/u/sidecar/.venv",
            "PATH": "/home/u/sidecar/.venv/bin:/usr/local/bin:/usr/bin:/opt/x/.venv/bin",
            "HOME": "/home/u",
        })
        env = kwargs["env"]
        self.assertNotIn("VIRTUAL_ENV", env)
        entries = env["PATH"].split(":")
        self.assertEqual(entries, ["/usr/local/bin", "/usr/bin"])
        self.assertFalse(any("/.venv/" in e + "/" for e in entries))
        self.assertEqual(env["HOME"], "/home/u")  # everything else is inherited

    def test_env_without_venv_is_unchanged(self):
        kwargs = self._run_with({"PATH": "/usr/bin:/bin", "LANG": "C"})
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C"})

    def test_missing_path_is_tolerated(self):
        kwargs = self._run_with({"VIRTUAL_ENV": "/v/.venv"})
        self.assertNotIn("VIRTUAL_ENV", kwargs["env"])
        self.assertNotIn("PATH", kwargs["env"])

    def test_real_environ_is_not_mutated(self):
        import os
        with mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/v/.venv", "PATH": "/v/.venv/bin:/bin"}):
            self._run_with_current()
            self.assertEqual(os.environ["VIRTUAL_ENV"], "/v/.venv")
            self.assertEqual(os.environ["PATH"], "/v/.venv/bin:/bin")

    def _run_with_current(self):
        import subprocess
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch.object(preflight.subprocess, "run", fake):
            preflight.SubprocessRunner().run(["waydroid", "status"], 5)


class Preflight(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(check_waydroid(FakeRunner(OK), "alas", DEV), [])

    def test_session_stopped(self):
        r = FakeRunner({**OK, "waydroid status": (0, "Session:\tSTOPPED")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("session" in p.lower() for p in problems))

    def test_wrong_resolution(self):  # Ruling 31: resolution is checked after start
        r = FakeRunner({**OK, "wm size": (0, "Physical size: 1080x2400")})
        problems = check_resolution(r, "alas", DEV)
        self.assertTrue(any("1280x720" in p for p in problems))

    def test_uses_container_adb_not_host(self):
        r = FakeRunner(OK)
        check_waydroid(r, "alas", DEV)
        check_resolution(r, "alas", DEV)
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


class PreStartOrder(unittest.TestCase):  # Ruling 31
    def test_stopped_container_never_gets_docker_exec_before_start(self):
        r = FakeRunner({
            "waydroid status": OK["waydroid status"],
            "inspect": (0, "false"),
            "docker exec": (1, "Error: container is not running"),
        })
        self.assertEqual(check_waydroid(r, "alas", DEV), [])
        self.assertFalse(any(c[:2] == ["docker", "exec"] for c in r.calls))
        self.assertFalse(any("wm" in c for c in r.calls))

    def test_resolution_runs_adb_connect_before_wm_size(self):
        r = FakeRunner(OK)
        self.assertEqual(check_resolution(r, "alas", DEV), [])
        joined = [" ".join(c) for c in r.calls]
        self.assertEqual(len(joined), 2)
        self.assertEqual(joined[0], "docker exec alas adb connect 192.0.2.10:5555")
        self.assertEqual(joined[1], "docker exec alas adb -s 192.0.2.10:5555 shell wm size")

    def test_resolution_after_start_succeeds_on_stopped_world(self):
        r = FakeRunner({**OK, "inspect": (0, "false"), "docker start": (0, "alas")})
        r.run(["docker", "start", "alas"], 5)
        self.assertEqual(check_resolution(r, "alas", DEV), [])

    def test_resolution_before_start_is_an_exec_failure(self):
        r = FakeRunner({**OK, "inspect": (0, "false")})
        problems = check_resolution(r, "alas", DEV)
        self.assertEqual(len(problems), 1)
        self.assertIn("rc=1", problems[0])

    def test_resolution_connect_failure_reported_and_skips_wm_size(self):
        r = FakeRunner({**OK, "adb connect": (0, "failed to connect to 192.0.2.10:5555")})
        problems = check_resolution(r, "alas", DEV)
        self.assertEqual(len(problems), 1)
        self.assertIn("adb connect", problems[0])
        self.assertFalse(any("wm" in c for c in r.calls))

    def test_resolution_mismatch_names_both_sizes(self):
        r = FakeRunner({**OK, "wm size": (0, "Physical size: 1080x2400")})
        (problem,) = check_resolution(r, "alas", DEV)
        self.assertIn("1080x2400", problem)
        self.assertIn("1280x720", problem)


class DockerInspect(unittest.TestCase):  # Ruling 39
    def test_inspect_uses_type_container(self):
        r = FakeRunner(OK)
        check_waydroid(r, "alas", DEV)
        inspects = [c for c in r.calls if c[:2] == ["docker", "inspect"]]
        self.assertEqual(len(inspects), 1)
        self.assertIn("--type", inspects[0])
        self.assertEqual(inspects[0][inspects[0].index("--type") + 1], "container")


class Frozen(unittest.TestCase):  # Ruling 37
    def test_frozen_container_is_a_problem_with_remedy(self):
        r = FakeRunner({**OK, "waydroid status": (0, "Session:\tRUNNING\nContainer:\tFROZEN")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertEqual(len(problems), 1)
        self.assertIn("Waydroid container is FROZEN", problems[0])
        self.assertIn("waydroid app launch com.YoStarEN.AzurLane", problems[0])

    def test_session_stopped_message_and_remedy(self):
        r = FakeRunner({**OK, "waydroid status": (0, "Session:\tSTOPPED\nContainer:\tSTOPPED")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertEqual(len(problems), 1)
        self.assertIn("Waydroid session is not running", problems[0])
        self.assertIn("waydroid session start", problems[0])

    def test_running_substring_elsewhere_does_not_count(self):
        # Old check was a bare substring test; "RUNNING" in another line must not pass it.
        r = FakeRunner({**OK, "waydroid status": (0, "Session:\tSTOPPED\nContainer:\tRUNNING")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("session is not running" in p for p in problems))

    def test_unparsable_status_is_not_running(self):
        r = FakeRunner({**OK, "waydroid status": (0, "garbage RUNNING garbage")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("session is not running" in p for p in problems))

    def test_regex_is_case_sensitive_and_line_anchored(self):
        r = FakeRunner({**OK, "waydroid status": (0, "  Session: RUNNING\nsession: RUNNING")})
        problems = check_waydroid(r, "alas", DEV)
        self.assertTrue(any("session is not running" in p for p in problems))


class HostAdb(unittest.TestCase):  # Ruling 36
    def test_open_port_is_a_problem_with_remedy(self):
        # Fixture: inspect=false (container stopped). With the container running, the
        # port is ALAS's own adb server (network_mode: host) and is not probed.
        with mock.patch.object(preflight, "_adb_port_open", lambda *a, **k: True):
            problems = check_waydroid(FakeRunner({**OK, "inspect": (0, "false")}), "alas", DEV)
        self.assertEqual(len(problems), 1)
        self.assertIn("host adb server is running on :5037", problems[0])
        self.assertIn("adb kill-server", problems[0])

    def test_running_container_skips_the_probe(self):  # I1: ALAS's adb listens on :5037
        probe = mock.Mock(return_value=True)
        with mock.patch.object(preflight, "_adb_port_open", probe):
            problems = check_waydroid(FakeRunner({**OK, "inspect": (0, "true")}), "alas", DEV)
        self.assertEqual(problems, [])
        probe.assert_not_called()

    def test_missing_container_still_probes(self):
        probe = mock.Mock(return_value=True)
        with mock.patch.object(preflight, "_adb_port_open", probe):
            problems = check_waydroid(FakeRunner({**OK, "inspect": (1, "No such container")}),
                                      "alas", DEV)
        probe.assert_called_once()
        self.assertTrue(any("host adb server" in p for p in problems))

    def test_probe_never_invokes_adb_binary(self):
        r = FakeRunner(OK)
        check_waydroid(r, "alas", DEV)
        self.assertFalse(any(c and c[0] == "adb" for c in r.calls))

    def test_probe_true_for_listening_socket_false_for_closed(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(preflight._real_adb_port_open("127.0.0.1", port, 1.0))
        finally:
            srv.close()
        self.assertFalse(preflight._real_adb_port_open("127.0.0.1", port, 1.0))


class Diagnostics(unittest.TestCase):  # Ruling 38
    def test_waydroid_timeout_named_as_timeout(self):
        r = FakeRunner({**OK, "waydroid status": (124, "timeout after 15s: waydroid status")})
        (problem,) = check_waydroid(r, "alas", DEV)
        self.assertIn("timed out", problem)
        self.assertIn("rc=124", problem)
        self.assertNotIn("not found", problem)

    def test_waydroid_missing_binary_named_as_missing(self):
        r = FakeRunner({**OK, "waydroid status": (127, "cannot run waydroid: [Errno 2]")})
        (problem,) = check_waydroid(r, "alas", DEV)
        self.assertIn("rc=127", problem)
        self.assertIn("cannot run", problem)
        self.assertIn("waydroid", problem)

    def test_waydroid_failure_includes_rc_and_output(self):
        r = FakeRunner({**OK, "waydroid status": (1, "  boom happened \n")})
        (problem,) = check_waydroid(r, "alas", DEV)
        self.assertIn("rc=1", problem)
        self.assertIn("boom happened", problem)
        self.assertIn("waydroid session start", problem)

    def test_inspect_timeout_is_not_reported_as_not_found(self):
        r = FakeRunner({**OK, "inspect": (124, "timeout after 15s")})
        (problem,) = check_waydroid(r, "alas", DEV)
        self.assertIn("timed out", problem)
        self.assertNotIn("not found", problem)

    def test_inspect_missing_docker_binary(self):
        r = FakeRunner({**OK, "inspect": (127, "cannot run docker: [Errno 2]")})
        (problem,) = check_waydroid(r, "alas", DEV)
        self.assertIn("rc=127", problem)
        self.assertIn("docker", problem)
        self.assertNotIn("not found", problem)

    def test_missing_container_includes_output_and_remedy(self):
        r = FakeRunner({**OK, "inspect": (1, "Error: No such container: alas")})
        (problem,) = check_waydroid(r, "alas", DEV)
        self.assertIn("rc=1", problem)
        self.assertIn("No such container", problem)
        self.assertIn("container_name", problem)

    def test_resolution_timeout_and_missing_binary(self):
        for rc, word in ((124, "timed out"), (127, "cannot run")):
            r = FakeRunner({**OK, "wm size": (rc, "x")})
            (problem,) = check_resolution(r, "alas", DEV)
            self.assertIn(word, problem)
            self.assertIn(f"rc={rc}", problem)

    def test_resolution_unreadable_output_includes_text(self):
        r = FakeRunner({**OK, "wm size": (0, "nonsense")})
        (problem,) = check_resolution(r, "alas", DEV)
        self.assertIn("nonsense", problem)

    def test_every_problem_has_a_remedy(self):
        cases = [
            ("waydroid status", (1, "x")),
            ("waydroid status", (0, "Session:\tSTOPPED")),
            ("waydroid status", (0, "Session:\tRUNNING\nContainer:\tFROZEN")),
            ("inspect", (1, "x")),
        ]
        for key, val in cases:
            for p in check_waydroid(FakeRunner({**OK, key: val}), "alas", DEV):
                self.assertRegex(p, r"run `|create |fix |install ", p)
        for key, val in (("adb connect", (1, "x")), ("wm size", (0, "Physical size: 1x1")),
                         ("wm size", (1, "x"))):
            for p in check_resolution(FakeRunner({**OK, key: val}), "alas", DEV):
                self.assertRegex(p, r"run `|create |fix |install |check ", p)


if __name__ == "__main__":
    unittest.main()
