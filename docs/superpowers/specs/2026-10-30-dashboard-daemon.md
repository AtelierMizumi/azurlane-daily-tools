# P-Dash: daemon + web dashboard cho azurlane-daily-tools

Trạng thái: **Đã chốt 2026-10-30 (người dùng điều chỉnh: always-on + port 22268), đang triển khai.**

Nguồn yêu cầu: người dùng (2026-10-30) — muốn tool chạy như một tiến trình
terminal/daemon, rồi mở browser xem trạng thái mọi thứ đang diễn ra, thay vì
chạy trong terminal mà không biết gì đang xảy ra. Điều chỉnh khi duyệt:
**AL cần chạy gần như mọi lúc máy bật** (không phải 1 lượt/ngày) và port
phải tránh các port dev phổ biến.

## 1. Mục tiêu

- `./azdaily daemon` khởi động một tiến trình duy nhất: web server + supervisor.
- **Chế độ always-on (mặc định, đúng nhu cầu "AL chạy mọi lúc máy bật")**:
  daemon đảm bảo container ALAS đang chạy khi daemon start; ALAS là bộ lập
  lịch duy nhất (R41/R42) tự chạy mọi task theo NextRun của nó; watchdog
  (keep-alive FROZEN, kiểm tra container/WebUI/log/màn hình treo) chạy
  **trong tiến trình daemon**. Daemon không tự ý dừng container.
- **Chế độ run-once** (giữ nguyên): `azdaily run` chạy một lượt có kiểm soát
  với pipeline stage + dừng container sau — dashboard có thể gọi.
- Trên browser (`http://127.0.0.1:22268`) thấy live: run pipeline (nếu đang
  chạy), lịch ALAS + NextRun từng task + trạng thái claim mission, lịch sử,
  log tail ALAS, trạng thái container/Waydroid/watchdog.
- Nút web: **Start ALAS / Stop ALAS** (điều khiển container), **Check /
  Run now / Apply** (pipeline run-once), **Stop run** (terminate + docker stop).
- Không thêm dependency Python mới.

Thành công: `./azdaily daemon` lúc bật máy (systemd user unit) → AL chạy suốt
phiên làm việc, mở `http://127.0.0.1:22268` thấy mọi thứ; tắt máy không cần
dọn gì (container dừng theo Docker daemon).

## 2. Kiến trúc

```
azdaily daemon (tiến trình duy nhất)
├── HTTP server — stdlib http.server (ThreadingHTTPServer), không dep mới
├── Supervisor thread — always-on: đảm bảo container chạy; watchdog checks
│   theo interval (tái dùng logic watchdog.py trong tiến trình)
├── Run subprocess — "azdaily run" qua CLI (cách ly; state vẫn còn nếu chết)
└── Web UI — dashboard.html tĩnh (vanilla JS, poll JSON mỗi 3 giây)
```

- **State flow**: orchestrator (azurlane_daily.py) hiện gom stages rồi chỉ ghi
  `summary.json` ở cuối. Bổ sung: sau mỗi lần append stage, dump luôn
  `runs/<ts>/state.json` (cùng cấu trúc payload với summary). Dashboard đọc
  file — nguồn sự thật là file, daemon chết/restart vẫn thấy đúng trạng thái.
- **Run từ web** = daemon spawn subprocess `azdaily.py run [--apply]`.
  Stop = terminate subprocess rồi `docker stop <container>` + state
  `stopped_by_user` (orchestrator không bắt SIGTERM nên daemon phải dọn).
- **Lock**: một pipeline run tại một thời điểm; POST /api/run khi đang chạy → 409.
- **Config mới** trong `daily.yaml`:

```yaml
daemon:
  listen: 127.0.0.1:22268  # port 22268: cạnh ALAS WebUI (22267), tránh port dev (3000/5173/8000/8080...)
  token: null              # bắt buộc khi bind ngoài localhost
  always_on: true          # giữ container chạy trong lúc daemon chạy (ALAS tự lập lịch)
  run_at: null             # chỉ dùng khi always_on=false: giờ spawn run mỗi ngày
  watchdog: true           # chạy watchdog checks trong daemon (đừng chạy watchdog.py riêng nữa)
  watchdog_interval: 60    # giây giữa các vòng watchdog
```

- `azdaily daemon` nhận `--port/--host/--token` override; in URL ra terminal lúc start; tự mở browser nếu chạy foreground trên desktop (mặc định có).

## 3. API

| Endpoint | Phương thức | Trả về |
|---|---|---|
| `/api/status` | GET | run pipeline hiện tại (state.json), container/Waydroid/watchdog trạng thái, lịch ALAS + mission note, cấu hình daemon |
| `/api/history` | GET | danh sách `runs/*/summary.json` (mới trước): thời gian, result, duration |
| `/api/logtail` | GET | ~50 dòng cuối của log ALAS mới nhất |
| `/api/container` | POST | body `{"action": "start"|"stop"}` — điều khiển container ALAS |
| `/api/run` | POST | body `{"mode": "check"|"run"|"apply"}`; 409 nếu đang chạy |
| `/api/stop` | POST | dừng subprocess + `docker stop`; 409 nếu không có run |

- Bảo mật: mặc định bind localhost; bind ngoài localhost phải có token (mọi
  request); POST kiểm tra Origin; không HTTPS (SSH tunnel nếu cần từ xa).
  Điều khiển container qua web chỉ hoạt động khi token đã đặt HOẶC bind
  localhost (chống CSRF từ trang khác trên cùng máy bằng Origin check).

## 4. Web UI (dashboard.html, vanilla JS)

1. **Header**: badge tổng (IDLE / ALAS RUNNING / PIPELINE RUNNING / FAILED...), badge container + Waydroid + watchdog, giờ.
2. **Điều khiển ALAS**: Start / Stop container, hiển thị always-on đang bật.
3. **Pipeline run-once**: stage list canonical, PASS/FAIL live, elapsed; nút Check/Run now/Apply/Stop.
4. **Lịch ALAS**: bảng task + NextRun (alas_report trên log mới nhất) + mission claim note.
5. **Lịch sử**: bảng runs (ngày, result, thời lượng).
6. **Log tail**: 50 dòng cuối, monospace, auto-refresh.

## 5. systemd (chạy nền thật)

Ship `sidecar/azdaily-daemon.service.example` (systemd **user** unit:
`ExecStart=%h/Projects/Tools/azurlane-daily-tools/azdaily daemon`,
`Restart=on-failure`). Hướng dẫn enable trong guide.

## 6. Test

- Unit test offline: `_emit_stage` ghi state.json mỗi stage; API trên port
  ngẫu nhiên localhost (status/history/logtail/run lock/token/origin);
  scheduler de-dupe + always_on/run_at tương tác (inject clock); config
  parsing daemon section (giá trị mặc định cho config cũ).
- UI kiểm tra bằng tay + screenshots; tích hợp thật trên máy người dùng.

## 7. Ngoài phạm vi v1

- Stream video scrcpy, WebSocket/SSE, HTTPS, multi-user.
- Panel riêng cho commission_bot; dashboard điều khiển game trực tiếp.
- Chạy daemon tự khởi động cùng OS ngoài systemd user unit.

## 8. Quyết định đã chốt

1. **Always-on là chế độ chính** (người dùng: AL chạy gần như mọi lúc bật máy); `run_at` chỉ là fallback khi tắt always_on.
2. **Port mặc định 22268** — liền kề ALAS WebUI (22267) cho dễ nhớ, tránh các port dev phổ biến (3000, 5173, 4200, 5000, 8000, 8080, 9000).
3. Watchdog **tích hợp vào daemon** theo mặc định (`daemon.watchdog: true`); hướng dẫn ngừng chạy watchdog.py riêng để tránh hai tiến trình cùng canh.
4. Tự mở browser khi chạy foreground: có.
