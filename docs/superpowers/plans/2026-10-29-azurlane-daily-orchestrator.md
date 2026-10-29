# Azur Lane daily orchestrator (Waydroid) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Một lệnh chạy được chuỗi daily Azur Lane trên Waydroid từ đầu đến cuối: preflight → kiểm tra config ALAS → bật ALAS → theo dõi log → dừng ALAS → báo kết quả, kèm lệnh `--e2e` in PASS/FAIL từng bước.

**Architecture:** `azurlane_daily.py` là bộ điều phối mỏng, không tap màn hình. ALAS (Docker, `alas`) làm app/login/popup/commission. Mọi phụ thuộc bên ngoài (docker, waydroid, HTTP, đồng hồ, sleep) được truyền vào qua tham số để unit test không cần thiết bị. Parser log và kiểm tra config là module thuần.

**Tech Stack:** Python 3.10+ (sidecar hiện tại), `pyyaml`, stdlib `unittest` (không thêm dependency), `uv run`, Docker CLI, `waydroid` CLI.

**Spec:** `docs/superpowers/specs/2026-10-29-azurlane-daily-orchestrator-design.md`

## Global Constraints

- Profile lần này chỉ là `waydroid`. Profile `phone` phải báo lỗi "not supported yet" (spec mục 2, quyết định 1 = chưa).
- Độ phân giải Waydroid: `1280x720`, lấy từ config, không hardcode ngoài giá trị mặc định.
- Serial mặc định `192.168.240.112:5555`, lấy từ config. Không được còn literal serial ở code mới.
- Không sửa code upstream trong `AzurLaneAutoScript/`, trừ file config instance `config/alas.json` khi chạy `--apply` (có backup).
- Thư mục `azurlane-daily-tools` không phải git repo. Các bước "Commit" trong skill được thay bằng checkpoint: toàn bộ test phải xanh trước khi sang task kế tiếp. Không commit nếu bạn chưa yêu cầu.
- Preflight dùng `docker exec alas adb ...`, không dùng host adb, để không tranh port 5037 với ALAS.
- Mặc định `commission_bot` giữ nguyên, không xóa (spec mục 9.2).
- Chạy test: `cd sidecar && uv run python -m unittest test_daily_config test_alas_config test_log_parser test_preflight test_azurlane_daily -v`.

## Review Focus

1. **ALAS đang chạy sẵn khi script khởi động** → script phải báo rõ và không tự `docker stop` container mà người khác đang dùng. Test: `run_daily` khi container đã Running → stage `already_running`, không gọi stop trừ khi script đã start.
2. **Hết timeout giữa chừng** → container ALAS vẫn phải được dừng (không để chạy mãi). Test: timeout → `docker stop` được gọi và result = `timeout`.
3. **Chưa từng chạy ALAS nên chưa có `config/alas.json`** → báo lỗi rõ kèm hướng dẫn mở ALAS GUI một lần, không crash với traceback. Test: `check_alas_config` với file thiếu.
4. **Log rotate / có nhiều file log trong `log/`** → parser đọc file mới nhất theo mtime, không đọc file cũ. Test: `newest_log_file` chọn đúng file khi có hai file.
5. **`waydroid status` báo STOPPED hoặc không có output** → preflight dừng với thông báo "Waydroid session chưa chạy", không chờ vô hạn. Test: output STOPPED → problem có chữ "session".

---

### Task 1: Config loader

**Files:**
- Create: `sidecar/daily_config.py`
- Create: `sidecar/daily.example.yaml`
- Test: `sidecar/test_daily_config.py`

**Interfaces:**
- Consumes: không có.
- Produces:
  - `@dataclass(frozen=True) class DeviceCfg: profile: str; serial: str; resolution: tuple[int, int]`
  - `@dataclass(frozen=True) class DailyConfig: device: DeviceCfg; alas_container: str; alas_webui_url: str; alas_repo_dir: Path; run_minutes: int; ntfy_url: str | None`
  - `load_daily_config(path: Path) -> DailyConfig` — raise `ValueError` với thông báo rõ khi thiếu key, profile không phải `waydroid`, hoặc `resolution` không đúng dạng `WxH`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_daily_config -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'daily_config'`

- [ ] **Step 3: Implement `load_daily_config(path: Path) -> DailyConfig` in `sidecar/daily_config.py`**

Dùng `yaml.safe_load`. Với mỗi key bắt buộc, đọc qua hàm nhỏ `_require(d, "a.b")` để thông báo lỗi ghi đúng đường dẫn key. `resolution` parse bằng regex `^(\d+)x(\d+)$`. `run_minutes` mặc định 60 nếu thiếu, còn các key khác là bắt buộc.

- [ ] **Step 4: Create `sidecar/daily.example.yaml`** với nội dung đúng như `GOOD` ở Step 1, kèm comment tiếng Việt cho từng key.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd sidecar && uv run python -m unittest test_daily_config -v`
Expected: `Ran 4 tests ... OK`

---

### Task 2: ALAS config check and apply

**Files:**
- Create: `sidecar/alas_config.py`
- Test: `sidecar/test_alas_config.py`

**Interfaces:**
- Consumes: không có (làm việc trên dict JSON).
- Produces:
  - `REQUIRED: dict[tuple[str, ...], object]` — `{("GameManager","AutoRestart"): True, ("Commission","Scheduler","Enable"): True}`
  - `check_alas_config(data: dict) -> list[str]` — trả danh sách vấn đề, rỗng nghĩa là đạt.
  - `apply_alas_config(path: Path) -> Path` — ghi giá trị đúng vào file, trả đường dẫn backup. Raise `FileNotFoundError` với thông báo "mở ALAS GUI một lần để tạo config".
  - `load_alas_config(path: Path) -> dict` — raise `FileNotFoundError` cùng thông báo đó.

- [ ] **Step 1: Confirm the JSON nesting used by ALAS (read-only)**

Run: `cd AzurLaneAutoScript && rg -n "deep_set|deep_get|json.dump" module/config/config.py module/config/utils.py | head -20`
Expected: thấy hàm `deep_get`/`deep_set` tách key bằng `.`. Nếu xác nhận khác (ví dụ key phẳng), sửa `REQUIRED` cho khớp trước khi tiếp tục và ghi rõ vào spec.

- [ ] **Step 2: Write the failing test**

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_alas_config -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'alas_config'`

- [ ] **Step 4: Implement the four names in `sidecar/alas_config.py`**

`check_alas_config` duyệt `REQUIRED`, dùng `dict.get` lồng nhau để không crash khi thiếu key, và trả thông báo chứa đường dẫn có dấu chấm. `apply_alas_config` copy file sang `alas.json.bak` trước khi ghi, rồi set từng key trong `REQUIRED` bằng cùng hàm nested set.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd sidecar && uv run python -m unittest test_alas_config -v`
Expected: `Ran 5 tests ... OK`

---

### Task 3: Log parser

**Files:**
- Create: `sidecar/log_parser.py`
- Test: `sidecar/test_log_parser.py`

**Interfaces:**
- Consumes: không có.
- Produces:
  - `@dataclass class RunState: app_started: bool; login_ok: bool; commission_started: bool; commission_done: bool; errors: list[str]`
  - `parse_lines(lines: Iterable[str]) -> RunState`
  - `newest_log_file(log_dir: Path) -> Path | None` — chọn file `*.txt` có mtime lớn nhất.

- [ ] **Step 1: Find the real ALAS log markers (read-only)**

Run:
```
cd AzurLaneAutoScript
rg -n "logger.hr\('App start'|Login success|Commission detect|Choose daily commission" module/
rg -n "task.*end|Task.*finished|Scheduler.*Enable|next run" module/daemon/alas.py module/config/config.py | head -20
```
Expected: xác định được (a) chuỗi "App start", "Login success" (đã thấy trong `module/handler/login.py:61`), "Commission detect" (`module/commission/commission.py:70`), và (b) dòng log khi tác vụ commission kết thúc. Ghi rõ chuỗi (b) và file:dòng vào docstring của `log_parser.py`. Nếu không tìm được dấu hiệu kết thúc rõ ràng, dừng và báo lại người dùng thay vì đoán.

- [ ] **Step 2: Write the failing test**

```python
# test_log_parser.py
import tempfile, time, unittest
from pathlib import Path
from log_parser import parse_lines, newest_log_file

HAPPY = [
    "═══ App start ═══",
    "Login success",
    "═══ Commission detect ═══",
    "Choose daily commission",
    # DONE_MARKER  <- thay bằng chuỗi xác định ở Task 3 Step 1
]

class ParseLines(unittest.TestCase):
    def test_happy_path(self):
        s = parse_lines(HAPPY)
        self.assertTrue(s.app_started)
        self.assertTrue(s.login_ok)
        self.assertTrue(s.commission_started)
        self.assertEqual(s.errors, [])

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
```

Thay `DONE_MARKER` bằng chuỗi thật từ Step 1, rồi thêm assert `commission_done` là True cho test happy path.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_log_parser -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'log_parser'`

- [ ] **Step 4: Implement `parse_lines` and `newest_log_file` in `sidecar/log_parser.py`**

Dùng regex cho từng marker, không so sánh chuỗi đầy đủ, vì logger có thể thêm ký tự khung `═`. Lỗi gồm `GameStuckError|GameNotRunningError|GameTooManyClickError|Traceback|CRITICAL`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd sidecar && uv run python -m unittest test_log_parser -v`
Expected: `Ran 5 tests ... OK`

---

### Task 4: Waydroid preflight

**Files:**
- Create: `sidecar/preflight.py`
- Test: `sidecar/test_preflight.py`

**Interfaces:**
- Consumes: `DeviceCfg` từ Task 1.
- Produces:
  - `class Runner(Protocol): def run(self, args: list[str], timeout: float) -> tuple[int, str]` (stdout gộp stderr)
  - `class SubprocessRunner` implement `Runner` bằng `subprocess.run(capture_output=True, text=True)`.
  - `check_waydroid(runner: Runner, container: str, device: DeviceCfg) -> list[str]` — trả danh sách vấn đề.

- [ ] **Step 1: Write the failing test**

```python
# test_preflight.py
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

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_preflight -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'preflight'`

- [ ] **Step 3: Implement `Runner`, `SubprocessRunner`, and `check_waydroid` in `sidecar/preflight.py`**

`check_waydroid` làm bốn việc theo thứ tự: (1) `waydroid status`, phải chứa `RUNNING`; (2) `docker inspect -f {{.State.Running}} <container>`, phải là `true`; (3) `docker exec <container> adb -s <serial> shell wm size`, phải chứa `Physical size: WxH` đúng với `resolution`; (4) bỏ qua bước 2 và 3 nếu bước 1 đã thất bại, để tránh thông báo lỗi chồng chéo. Dùng `timeout=15` cho mỗi lệnh.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd sidecar && uv run python -m unittest test_preflight -v`
Expected: `Ran 4 tests ... OK`

---

### Task 5: Orchestrator core

**Files:**
- Create: `sidecar/azurlane_daily.py`
- Test: `sidecar/test_azurlane_daily.py`

**Interfaces:**
- Consumes: `DailyConfig` (Task 1), `check_alas_config` và `load_alas_config` (Task 2), `parse_lines`, `newest_log_file`, `RunState` (Task 3), `check_waydroid`, `Runner` (Task 4).
- Produces:
  - `run_daily(cfg: DailyConfig, runner: Runner, http_get: Callable[[str], int], clock: Callable[[], float], sleep: Callable[[float], None], alas_config_path: Path, log_dir: Path, apply: bool = False) -> dict`
  - Dict trả về có các key: `result` ∈ {`success`, `preflight_failed`, `already_running`, `config_failed`, `webui_timeout`, `alas_error`, `timeout`}, `stages: list[tuple[str, bool, str]]` (tên bước, đạt hay không, chi tiết), `started`, `finished`.
  - Luôn gọi `docker stop <container>` trong `finally` nếu script đã gọi `docker start` (xem Review Focus 1 và 2).

- [ ] **Step 1: Write the failing test**

```python
# test_azurlane_daily.py
import json, tempfile, unittest
from pathlib import Path
from daily_config import DailyConfig, DeviceCfg
from azurlane_daily import run_daily
from test_preflight import FakeRunner, OK

def make_cfg():
    return DailyConfig(
        device=DeviceCfg("waydroid", "192.168.240.112:5555", (1280, 720)),
        alas_container="alas", alas_webui_url="http://x",
        alas_repo_dir=Path("."), run_minutes=1, ntfy_url=None)

GOOD_ALAS = {"GameManager": {"AutoRestart": True}, "Commission": {"Scheduler": {"Enable": True}}}

class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt

def alas_json(d, data):
    p = Path(d) / "alas.json"
    p.write_text(json.dumps(data))
    return p

class Orchestrator(unittest.TestCase):
    def test_success_path_stops_container(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner({**OK, "docker start": (0, "alas"), "docker stop": (0, "alas")})
        clock = Clock()
        # webui ready, log shows commission done
        log_dir = Path(d)
        (log_dir / "run.txt").write_text("App start\nLogin success\nCommission detect\n<DONE>\n")
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), log_dir)
        self.assertEqual(out["result"], "success")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_timeout_still_stops_container(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner({**OK, "docker start": (0, "alas"), "docker stop": (0, "alas")})
        clock = Clock()
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), Path(d))  # log rỗng -> timeout
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_already_running_not_stopped(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner(OK)  # inspect trả true -> đang chạy sẵn
        out = run_daily(make_cfg(), runner, lambda u: 200, Clock(), lambda s: None,
                        alas_json(d, GOOD_ALAS), Path(d))
        self.assertEqual(out["result"], "already_running")
        self.assertFalse(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_missing_alas_config_reports_not_crash(self):
        out = run_daily(make_cfg(), FakeRunner({**OK, "inspect": (0, "false")}), lambda u: 200,
                        Clock(), lambda s: None, Path(tempfile.mkdtemp()) / "alas.json", Path("."))
        self.assertEqual(out["result"], "config_failed")

if __name__ == "__main__":
    unittest.main()
```

Lưu ý: `<DONE>` trong test được thay bằng chuỗi thật từ Task 3 Step 1 trước khi chạy.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_azurlane_daily -v`
Expected: FAIL với `ModuleNotFoundError: No module named 'azurlane_daily'`

- [ ] **Step 3: Implement `run_daily` in `sidecar/azurlane_daily.py`**

Thứ tự bước: preflight → kiểm tra config (nếu `apply` thì ghi) → kiểm tra container không chạy sẵn (nếu đang chạy thì trả `already_running` mà không stop) → `docker start` → chờ WebUI (`http_get` trả 200, tối đa 120s, sleep 5s mỗi vòng) → đọc log mỗi 30s cho đến `run_minutes` → `finally`: nếu đã start thì `docker stop`. Ghi `stages` cho từng bước. Không dùng `time.sleep` trực tiếp, chỉ dùng `sleep` và `clock` được truyền vào.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd sidecar && uv run python -m unittest test_azurlane_daily -v`
Expected: `Ran 4 tests ... OK`

---

### Task 6: CLI modes `--check`, `--apply`, `--e2e`

**Files:**
- Modify: `sidecar/azurlane_daily.py` (thêm hàm `main` và `format_e2e_report`)
- Test: `sidecar/test_azurlane_daily.py` (thêm class mới)

**Interfaces:**
- Consumes: `run_daily` (Task 5).
- Produces:
  - `format_e2e_report(stages: list[tuple[str, bool, str]]) -> str` — mỗi dòng là `PASS|FAIL  <tên bước>  <chi tiết>`.
  - `main(argv: list[str] | None = None) -> int` — exit 0 nếu `result == "success"` (hoặc preflight/config OK khi `--check`), còn lại trả 1.

- [ ] **Step 1: Write the failing test**

Thêm vào `test_azurlane_daily.py`:

```python
class Cli(unittest.TestCase):
    def test_report_lines(self):
        from azurlane_daily import format_e2e_report
        text = format_e2e_report([("preflight", True, "ok"), ("alas", False, "timeout")])
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("PASS"))
        self.assertTrue(lines[1].startswith("FAIL"))
        self.assertIn("timeout", lines[1])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_azurlane_daily.Cli -v`
Expected: FAIL với `ImportError: cannot import name 'format_e2e_report'`

- [ ] **Step 3: Implement `format_e2e_report` and `main`**

`main` đọc `--config` (mặc định `daily.yaml`), `--check` (chỉ preflight và kiểm tra config, không start), `--apply` (cho phép ghi config), `--e2e` (chạy đầy đủ và in báo cáo). Không có `--e2e` và không có `--check` thì vẫn chạy đầy đủ, in báo cáo ngắn. Nối `SubprocessRunner`, `time.monotonic`, `time.sleep`, và `urllib.request.urlopen` vào `run_daily` tại đây.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd sidecar && uv run python -m unittest test_azurlane_daily -v`
Expected: tất cả test của Task 5 và Task 6 đều `OK`

---

### Task 7: Serial từ config cho watchdog và commission_bot

**Files:**
- Modify: `sidecar/watchdog.py:177` (thay literal serial)
- Modify: `sidecar/commission_bot.py` (DEFAULTS `serial`)
- Test: `sidecar/test_daily_config.py` (thêm một test nhỏ)

**Interfaces:**
- Consumes: `load_daily_config` (Task 1).
- Produces: không có API mới. `watchdog` và `commission_bot` đọc serial từ `daily.yaml` nếu tồn tại, ngược lại dùng giá trị mặc định cũ.

- [ ] **Step 1: Write the failing test**

Thêm vào `test_daily_config.py`:

```python
def test_serial_is_read_not_hardcoded(self):
    cfg = self._load(GOOD.replace("192.168.240.112:5555", "10.0.0.9:5555"))
    self.assertEqual(cfg.device.serial, "10.0.0.9:5555")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sidecar && uv run python -m unittest test_daily_config -v`
Expected: test mới PASS vì Task 1 đã đọc serial từ config. Bước này xác nhận hàm đọc đúng trước khi sửa watchdog.

- [ ] **Step 3: Update the two call sites**

Trong `watchdog.py`, thay `"192.168.240.112:5555"` bằng `load_daily_config(cfg_dir / "daily.yaml").device.serial` nếu file tồn tại. Trong `commission_bot.py`, thêm vào `DEFAULTS` một bước đọc tương tự, giữ literal làm fallback khi không có `daily.yaml`.

- [ ] **Step 4: Verify no literal serial remains in the new path**

Run: `cd sidecar && rg -n "192.168.240.112" --glob '!runs/**' --glob '!.venv/**' --glob '!docs/**' .`
Expected: chỉ còn literal fallback trong `commission_bot.py` và file `daily.example.yaml`.

---

### Task 8: Verification gate and handoff

**Files:**
- Modify: `sidecar/README.md` (nếu có) hoặc `README.md` của project, thêm mục ngắn về lệnh mới.

**Interfaces:**
- Consumes: mọi module ở Task 1–7.
- Produces: không có.

- [ ] **Step 1: Run the full unit suite**

Run: `cd sidecar && uv run python -m unittest test_daily_config test_alas_config test_log_parser test_preflight test_azurlane_daily -v`
Expected: tất cả OK, không có skip. Ghi lại số test pass/fail/skip.

- [ ] **Step 2: Run the legacy regression tests**

Run: `cd sidecar && uv run python test_state_machine.py && uv run python commission_bot.py --selftest`
Expected: `STATE MACHINE TEST PASS` và selftest 12/12 OK.

- [ ] **Step 3: Run the device-free preflight against the real container**

Run (trên máy bạn, ngoài sandbox): `cd sidecar && uv run python azurlane_daily.py --check`
Expected: in từng bước PASS/FAIL. Bất kỳ FAIL nào phải kèm lệnh khắc phục cụ thể.

- [ ] **Step 4: Run the e2e and report**

Run (trên máy bạn): `cd sidecar && uv run python azurlane_daily.py --e2e`
Expected: in báo cáo PASS cho mọi bước, exit code 0. Dán output vào cuộc trò chuyện để mình đọc.

- [ ] **Step 5: Checkpoint**

Không commit (xem Global Constraints). Liệt kê file đã tạo/sửa và số test pass/fail/skip cho bạn.

---

## Ghi chú các điểm chưa kiểm chứng

- Dòng log "commission hoàn tất" chưa xác định (Task 3 Step 1). Nếu không tìm được, plan dừng ở Task 3 và báo lại, không đoán.
- Cấu trúc JSON lồng (`Commission.Scheduler.Enable`) cần xác nhận ở Task 2 Step 1 trước khi viết code.
- E2E thực tế chỉ chạy được trên máy bạn, vì sandbox không kết nối được Waydroid và docker.
