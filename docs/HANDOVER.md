# Handover: Azur Lane daily tool (ALAS + sidecar)

Tài liệu này dành cho người (hoặc model) tiếp tục công việc. Đọc hết trước khi chạy lệnh nào.

## 1. Mục tiêu

Chạy chuỗi daily Azur Lane trên Waydroid bằng một lệnh, không cần người can thiệp:
bật container ALAS → ALAS mở app, đăng nhập, xử lý popup → ALAS chạy các tác vụ đang bật → sidecar theo dõi log → dừng container → báo kết quả.

Mở rộng sau đó (chưa làm): tự động PvP an toàn, commission ưu tiên gem, event/SP map, Operation Siren, Akashi, farm exp. Xem mục 8.

## 2. Vị trí

| Thứ | Đường dẫn |
|---|---|
| Repo chính (checkout gốc) | `~/Projects/Tools/azurlane-daily-tools` (branch `master`, tip `eb367a7`) |
| Worktree đang làm việc | `~/Projects/Tools/azurlane-daily-tools/.claude/worktrees/azur-final-fix` (branch `worktree-azur-final-fix`, tip sau commit handover) |
| Mã sidecar | `<worktree>/sidecar/` |
| ALAS (repo upstream riêng, không track ở đây) | `~/Projects/Tools/azurlane-daily-tools/AzurLaneAutoScript/` |
| Spec | `docs/superpowers/specs/2026-10-29-azurlane-daily-orchestrator-design.md` |
| Plan | `docs/superpowers/plans/2026-10-29-azurlane-daily-orchestrator.md` |
| Ledger (ignored, chỉ đọc) | `<repo>/.superpowers/sdd/2026-10-29-azurlane-daily-orchestrator/progress.md` (checkout gốc) |
| Ledger fix wave | `<worktree>/.superpowers/sdd/2026-10-29-azurlane-daily-orchestrator/progress-fixwave.md` |
| Báo cáo fix wave | `<worktree>/.superpowers/sdd/2026-10-29-azurlane-daily-orchestrator/final-fix-report.md` |
| Báo cáo lịch ALAS | `<worktree>/.superpowers/sdd/2026-10-29-azurlane-daily-orchestrator/alas-report-report.md` |

`.superpowers/` bị git-ignore. Ledger và báo cáo nằm ở đó, không nằm trong commit.

## 3. Lệnh

Tất cả lệnh chạy trong `<worktree>/sidecar`:

```fish
# Test (không cần thiết bị)
uv run python -m unittest test_daily_config test_watchdog_serial test_alas_config test_log_parser test_preflight test_azurlane_daily test_alas_report
uv run python test_state_machine.py
uv run python commission_bot.py --selftest

# Chỉ kiểm tra preflight và config (không start container)
uv run python azurlane_daily.py --check

# Chạy đầy đủ, kèm báo cáo từng bước
uv run python azurlane_daily.py --e2e

# Ghi config ALAS (cần container đang dừng; có .bak)
uv run python azurlane_daily.py --apply

# Báo cáo lịch từ log ALAS (chỉ đọc)
uv run python alas_report.py --log ~/Projects/Tools/azurlane-daily-tools/AzurLaneAutoScript/log/2026-10-29_alas.txt
```

Trạng thái khi bàn giao: 202 unit test pass, state machine PASS, selftest exit 0.

Cập nhật 2026-10-29 tối: **e2e thật đã PASS** — `--e2e --apply` kết thúc `RESULT: success`, exit 0, ~77 giây (container stopped → apply config → docker start → ALAS chạy Commission → stop sạch), summary tại `sidecar/runs/20261029-225222/summary.json`. Sau fix marker "App is already running" thì 204 unit test pass (parser đã được validate trên log thật của run).

## 4. Những gì đã làm (theo commit)

Branch `worktree-azur-final-fix`, từ `master`:

- `91d2c5f` baseline snapshot (trước khi làm).
- Task 1–8 của plan (config, ALAS config check, log parser, preflight, orchestrator core, CLI, serial từ config, README).
- `826d990`, `61ca340`: final fix wave (thứ tự kiểm tra preflight, `--apply` ghi `Run` và `.bak`, summary + ntfy, các stage e2e, probe port 5037, FROZEN, diagnostics, `.gitignore`).
- `11f9008`: preflight không kế thừa môi trường venv của `uv`.
- `2ffb69c`: commission kết thúc mà không quét thì là `no_commission`, không phải success.
- `2babcff`, `751c87e`: báo cáo lịch ALAS `alas_report.py`; sửa marker commission thành `COMMISSION SCAN`; fixture là đoạn log thật liên tục.

## 5. Quy tắc quan trọng (ruling)

Ruling đầy đủ nằm trong ledger. Các ruling có ảnh hưởng lớn nhất:

- **R3**: `check_waydroid` chỉ kiểm tra container tồn tại. Trạng thái chạy do orchestrator quyết định (`already_running`).
- **R9, R15, R16**: `--apply` chỉ chạy khi container đang dừng và trước `docker start`. Kiểm tra already-running trước.
- **R10, R13, R46**: thành công = có commission được quét (`COMMISSION SCAN`) + có End + không có lỗi. End không kèm quét thì là `no_commission`.
- **R11, R12**: chỉ đọc dòng log sinh ra sau khi run bắt đầu. Dòng cũ không được đưa vào parser.
- **R15**: `docker stop` luôn chạy trong `finally` nếu đã `docker start`.
- **R17**: lỗi trong log không kết thúc sớm, chỉ đánh giá khi hết thời gian.
- **R18, R22**: `docker stop` lỗi phải được báo; exit 0 chỉ khi success và stop sạch.
- **R23–R26**: serial đọc từ `daily.yaml` nếu có; nếu không thì literal fallback. Watchdog và commission_bot không bắt buộc `daily.yaml`.
- **R30**: `daily.yaml` bắt buộc cho orchestrator.
- **R31**: pre-start không gọi `docker exec` để đọc `wm size`. Kiểm tra độ phân giải sau khi start.
- **R32**: `--apply` ghi `deploy.yaml` (`Run` thêm `"alas"`, giữ các instance khác) và khóa `Commission.Scheduler.Enable`, có `.bak`.
- **R36**: probe port 5037 bằng socket, không gọi binary adb. Bỏ qua khi container đang chạy (container dùng `network_mode: host`).
- **R41**: ALAS không có khóa scheduler toàn cục. Lên lịch theo từng tác vụ.
- **R42**: khi instance tự chạy, mọi tác vụ đang bật đều chạy. Đây là chủ ý theo spec.
- **R43**: `Emulator.Serial` của ALAS chưa được so với `device.serial` tự động. Phải kiểm tra thủ công trước khi chạy.
- **R44**: kiểm tra độ phân giải ngay sau start có thể báo lỗi giả nếu adb chậm.

Các ruling 19–29 chỉ nằm trong prompt dispatch, không có trong ledger:
- R19: main nối các thành phần thật, có thể thay thế trong test.
- R20: `--check` chỉ kiểm tra, không start container.
- R21: chế độ mặc định dùng cùng định dạng báo cáo với `--e2e`.
- R22: exit 0 chỉ khi success và không có `docker stop` lỗi.
- R27: `--check` và `--e2e` trên máy thật là PENDING. **ĐÃ xong 2026-10-29: cả hai PASS.**
- R28: README (đã sửa bởi R30).
- R29: commit riêng cho README.

## 6. Trạng thái e2e và việc người dùng phải làm

E2E thật **đã PASS** ngày 2026-10-29 (xem mục 3). Danh sách điều kiện dưới đây giữ lại làm tham khảo cho lần chạy sau:

1. Dừng scheduler ALAS trong GUI, rồi `docker stop alas`. Orchestrator yêu cầu container đang dừng. Lưu ý: lần stop đầu ALAS shutdown chậm nên bị SIGKILL (Exited 137) — vẫn ổn.
2. `AzurLaneAutoScript/config/alas.json` phải tồn tại. **ĐÃ ĐẠT** (GUI đã tạo).
3. `deploy.yaml` phải ghi được (thuộc user, không phải root). **ĐÃ ĐẠT** qua chown. `log/` chỉ cần đọc được, không cần ghi.
4. `Alas.Emulator.Serial` trong `alas.json` bằng `device.serial` trong `daily.yaml`. **ĐÃ ĐẠT** (cả hai `192.168.240.112:5555`).
5. `sidecar/daily.yaml` tồn tại. **ĐÃ ĐẠT** (trong worktree, git-ignored).
6. Waydroid session RUNNING và không FROZEN. **ĐÃ ĐẠT**.
7. `adb kill-server` trên host — nếu `--check` PASS preflight thì port 5037 đã rảnh, bước này không cần nữa.

Sau đó chạy `--check`, rồi `--e2e`. Dán output và exit code để đánh giá.

## 7. Đã biết và chưa xử lý (deferred)

Các mục này được đánh dấu SHIP-AS-DEFERRED. Có thể làm sau:

- `finished` được ghi sau `docker stop` (đã sửa ở fix wave).
- Dòng `RESULT:` nằm trên stdout cùng với các dòng PASS/FAIL.
- `run_minutes` = 0 hoặc số âm không bị từ chối rõ ràng.
- `ntfy_url` không được kiểm tra kiểu.
- `apply_alas_config`: `.bak` bị ghi đè khi apply lần hai.
- JSON không phải object gây AttributeError (đã đổi thành ValueError ở `_set`).
- Thông báo lỗi preflight khi Docker daemon chết có thể gây hiểu nhầm (đã cải thiện ở fix wave).
- `watchdog` và `commission_bot` dùng `load_daily_config` đầy đủ. Lỗi không liên quan có thể làm chúng âm thầm dùng serial literal.
- Test còn một số temp dir không dọn dẹp (test helper từ brief).

## 8. Roadmap tính năng (chưa làm)

Người dùng yêu cầu thêm các automation sau. Đây là nhiều sub-project, mỗi cái cần spec riêng:

- **P0. Nền tảng:** e2e chạy được, timing lịch chờ (NextRun) đúng, báo cáo theo tác vụ. Đã có báo cáo lịch `alas_report.py`. Còn lại: e2e thật và kiểm tra NextRun.
- **P1. Commission:** ưu tiên gem trước (hủy một commission để nhận gem), rồi cube, rồi hiệu suất dầu + coin trên một đơn vị thời gian. ALAS đã có bộ lọc gem-first (`Commission_CustomFilter`). Còn thiếu: logic hủy commission và công thức hiệu suất theo giờ. Cần kiểm tra `module/commission/commission.py` trước.
- **P2. Event và SP map:** auto SP, auto 3 lần không X2 oil, đổi fleet khi morale thấp, luân phiên fleet 5 và 6. ALAS có `Campaign` nhưng chưa rõ phần đổi fleet theo morale.
- **P3. Research farm exp:** đội farm exp khi research chạy. Chưa có trong ALAS.
- **P4. Tierlist:** chọn đội theo tướng meta trên trang tierlist. Cần quyết định nguồn dữ liệu: snapshot cố định hay lấy trực tiếp.
- **P5. PvP an toàn:** chỉ chọn đối thủ thắng được, so sánh fleet power và meta. ALAS `Exercise_OpponentChooseMode` có `max_exp`, `easiest`, `leftmost`, `easiest_else_exp`, không có chế độ theo sức mạnh. Cần OCR đọc sức mạnh đối thủ. Rủi ro mất tàu cao nhất.
- **P6. Operation Siren:** handover tự động, chọn map theo khả năng, ưu tiên map có hộp stamina. ALAS có `OpsiAshBeacon`, `OpsiFleet`, `OpsiHazard1Leveling`.
- **P7. Akashi và đổi xu:** đổi đồ tự động, đổi xu hằng tháng. Chưa tìm thấy khóa cấu hình "Akashi" trong ALAS, cần kiểm tra thêm.
- **P-Doc. Hướng dẫn người dùng** (yêu cầu 2026-10-29): guide tiếng Việt theo chất lượng hướng dẫn cộng đồng — mỗi automation làm gì, quy trình daily, troubleshooting, FAQ. Bản nháp: `docs/HUONG-DAN.md`, chờ duyệt rồi commit; cập nhật theo từng tính năng mới.

**Quyết định kiến trúc đã chốt 2026-10-29: C** (trình bày ba phương án cho người dùng duyệt).

Nguyên tắc điều phối: thiết bị chỉ có một phiên điều khiển. ALAS là bộ lập lịch duy nhất. Không chạy nhiều bot song song.

## 9. Ràng buộc và thói quen người dùng

- Trả lời bằng tiếng Việt, ngắn gọn, đưa kết luận quan trọng xuống cuối.
- Không thêm dòng attribution (Co-Authored-By) vào commit hay PR. Đây là yêu cầu hiện tại của người dùng.
- Không commit hay push khi chưa được yêu cầu. Repo không có remote.
- Không sửa code upstream ALAS trong `AzurLaneAutoScript/` (spec mục 8). Ngoại lệ duy nhất: `--apply` ghi file config runtime, có `.bak`.
- Test-first khi có test suite. Không sửa test để khớp code sai.
- Không dùng `rm -rf` trên đường dẫn chưa kiểm tra.
- Với nhiều file tài liệu, hook `block-doc-commit.sh` chặn các lệnh git có chuỗi đuôi tài liệu (`.docx`, `.pdf`, ...) trong cùng dòng lệnh, kể cả khi chỉ là pattern tìm kiếm. Tách lệnh.

## 10. Gotchas

- Worktree bị khóa (`locked`). Không xóa worktree khi chưa có quyết định merge hoặc giữ.
- Trong session worktree, checkout gốc là read-only. Ledger chính ở checkout gốc chỉ đọc được.
- Các file character device `.bashrc`, `.gitconfig`, `.mcp.json`, ... ở gốc worktree là bind-mount của sandbox, không phải file dự án. Không commit chúng.
- `uv run` đưa `.venv` vào PATH và VIRTUAL_ENV. Script `waydroid` là python và sẽ dùng nhầm venv (không có dbus). Đã sửa trong `preflight._clean_env()`. Subprocess mới nên dùng cùng hàm này.
- Marker commission là `COMMISSION SCAN` (uppercase), không phải `Commission detect`. Log thật không có `Commission detect`.
- NextRun đôi khi là datetime 5 trường (khi giây = 0). Đã xử lý trong `alas_report.py`.
- Restart của ALAS có thể bị delay tới ngày hôm sau 14:00 (`server_update='00:00'`). Đây là hành vi ALAS, không phải lỗi tool.
- Mỗi lần ALAS shutdown (`docker stop`) đều ghi lại `config/alas.json` bằng root qua bind mount (xác nhận thật 2 lần: 22:40:33, 22:51:59). `deploy.yaml` ALAS không đụng nên giữ quyền user. **Trước mỗi lần cần `--apply`, chown lại `alas.json`.**
- `daily.yaml` git-ignored, không theo merge. Chạy orchestrator từ checkout nào thì checkout đó phải có `sidecar/daily.yaml` (sao chép từ worktree nếu cần).
- ALAS in `App is already running, goto main page` (alas.py:177) khi game còn sống — parser đã tính đây là app started + login ok. Đường `App start`/`Login success` (game chết, restart thật) chưa từng chạy qua thật.
- Task `Reward` của ALAS là claimer có sẵn cho màn Missions ("Collect ALL", `module/reward/reward.py:309`) với chu kỳ 120–240 phút — lần chạy daily đầu sau khi bật có thể chưa tới hạn của nó (đã gặp thật: window 22:51–22:52, NextRun Reward 23:49). Orchestrator ghi chú trạng thái ở stage `next run` (`mission_claim_note`); từ lần chạy daily thứ hai trở đi NextRun của Reward luôn đã quá hạn nên claim chạy trong window. `--apply` đảm bảo `Reward.Scheduler.Enable` và `Reward.Reward.CollectMission` (thêm vào REQUIRED).
- Sandbox không kết nối được Waydroid, docker hay adb trên máy người dùng. Mọi kiểm tra thiết bị phải do người dùng chạy.
- Lệnh `sed`/`grep` với biến và nhiều bước bị guard từ chối trong session worktree. Tách thành các lệnh đơn giản, dùng đường dẫn tuyệt đối.

## 11. Việc tiếp theo, theo thứ tự

1. ~~Người dùng dừng scheduler và container, hoàn thành mục 6, chạy `--check` rồi `--e2e`.~~ **Đã xong 2026-10-29, e2e PASS.**
2. ~~Dựa trên kết quả e2e, sửa lỗi nếu có. Nếu PASS, chốt merge hoặc giữ branch.~~ **Đã merge `worktree-azur-final-fix` vào `master` (fast-forward), worktree giữ nguyên.**
3. ~~Chốt A/B/C.~~ **Đã chốt C (2026-10-29).** Đang viết spec cho P0 (phần còn lại) và P1, chờ duyệt rồi lập kế hoạch.
4. Kiểm tra `Commission` trong ALAS (`commission.py`) để xác định đường hủy commission (P1). **Đã kiểm tra sơ bộ:** ALAS không có logic hủy; hook point là `_commission_choose` khi `running_count >= max_commission`; game cho hủy commission đang chạy (mất dầu, không nhận thưởng); pattern hủy tham khảo `module/island/order.py` `reject_order()`. Chi tiết trong spec P1.
5. Với P5 (PvP), cần kiểm tra OCR đọc sức mạnh đối thủ trước khi thiết kế. **Đã kiểm tra:** `module/exercise/opponent.py` đã OCR sẵn power + level của cả 4 đối thủ; P5 khả năng chỉ cần config (`easiest` + `Exercise_LowHpThreshold`), gần như không cần patch.
6. Duyệt spec `docs/superpowers/specs/2026-10-29-p0-nextrun-p1-commission-gem.md` (đã chốt: chỉ hủy vì gem; thứ tự giá trị nạn nhân) và bản nháp guide `docs/HUONG-DAN.md`, rồi lập plan P0-phần-còn-lại + P1. Hai câu hỏi mở còn lại: ai làm explore hủy (user tay + agent chụp, hay agent toàn quyền), `Commission.CustomFilter` để GUI hay để `--apply`.
