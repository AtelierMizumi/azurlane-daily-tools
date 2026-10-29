# Azur Lane daily orchestrator: thiết kế

Trạng thái: **Bản nháp, chờ bạn duyệt.** Chưa implement. Chưa lập kế hoạch.

## 1. Mục tiêu

Chạy được chuỗi daily Azur Lane từ đầu đến cuối bằng một lệnh, không cần người can thiệp:

mở app → nhấn start và đăng nhập tài khoản đã lưu → đóng popup/thông báo/cập nhật → chạy daily (commission) → dừng → báo kết quả.

Thành công nghĩa là: một lệnh e2e cho kết quả PASS trên thiết bị đã khai báo, và bộ unit test chạy được không cần thiết bị.

## 2. Phạm vi thiết bị

Hệ thống phải chạy được trên hai loại thiết bị, chọn qua config:

| Thiết bị | Độ phân giải | Ghi chú |
|---|---|---|
| Waydroid (hiện tại) | 1280x720, cố định | Có nhánh riêng: trạng thái FROZEN, `waydroid app launch` |
| Điện thoại Android hiện đại (qua adb USB hoặc wireless debugging) | Thường khác 1280x720 (ví dụ 1080x2400) | **Chưa hỗ trợ.** Xem ràng buộc bên dưới |

Controller (script và ALAS container) vẫn chạy trên PC. Điện thoại chỉ là thiết bị được điều khiển qua adb.

**Ràng buộc đã kiểm chứng trong mã nguồn:**
- ALAS, đường uiautomator2, kiểm tra độ phân giải và dừng nếu không phải 1280x720 (`AzurLaneAutoScript/module/device/method/uiautomator_2.py:420-439`).
- `commission_bot` dùng template và tọa độ cố định 1280x720 (`sidecar/commission_bot.py`, `sidecar/assets/`).

Vì vậy điện thoại chỉ chạy được nếu có một trong các cách sau (quyết định ở mục 9):
- (a) Đặt độ phân giải hiển thị của điện thoại thành 1280x720 bằng `wm size`. Chưa kiểm chứng trên máy thật, có thể méo hoặc có viền đen vì tỉ lệ khác 16:9.
- (b) Chờ đến khi có chiến lược scaling. Ngoài phạm vi lần này.

**Yêu cầu bắt buộc cho mọi thiết bị:**
- Không hardcode serial. Serial lấy từ config. Hiện `192.168.240.112:5555` đang nằm trong `watchdog.py:177` và `commission_bot.py` DEFAULTS, phải chuyển vào config dùng chung.
- Nhánh xử lý theo loại thiết bị. Logic riêng của Waydroid không được áp dụng cho điện thoại.

## 3. Kiến trúc

- Một script mới `sidecar/azurlane_daily.py`, chạy bằng `uv run python azurlane_daily.py`.
- Script **không tự tap màn hình**. Nó điều phối ALAS.
- ALAS đảm nhiệm: mở app, nhấn "press to start" (`LOGIN_CHECK`), đăng nhập, popup, thông báo, bảo trì, cập nhật, commission (`AzurLaneAutoScript/module/handler/login.py`, `module/commission/commission.py`).
- `commission_bot` không nằm trong đường chạy chính. Không xóa trước khi có sự đồng ý (mục 9).

## 4. Luồng chạy

1. **Preflight**
   - Container `alas` tồn tại và Docker truy cập được.
   - Host adb server đã dừng (tránh tranh port 5037, theo README).
   - Thiết bị adb đã kết nối và độ phân giải đúng với profile.
   - Nếu lỗi: dừng ngay, in lệnh khắc phục cụ thể.
2. **Kiểm tra config ALAS**
   - Mặc định chỉ kiểm tra và báo: `GameManager_AutoRestart = True` (`config_generated.py:513`), các tác vụ commission bật, `Scheduler_Enable` (mặc định `False` ở `config_generated.py:13`, cần xác định có cần bật cho lần chạy thủ công không).
   - Cờ `--apply` mới được ghi thay đổi, luôn sao lưu file config trước khi ghi.
3. **Bật ALAS** và chờ WebUI `http://127.0.0.1:22267` trả HTTP 200.
4. **Theo dõi log** trong `AzurLaneAutoScript/log/`. Đọc các dòng log để nhận biết tác vụ đã bắt đầu, đã xong, hay lỗi. Timeout tổng mặc định 60 phút.
5. **Dừng ALAS** khi xong. Game không bị đóng.
6. **Báo kết quả**: ghi `summary.json` vào `runs/<timestamp>/`, gửi ntfy (dùng cùng topic với `bot.yaml`).

## 5. Xử lý lỗi

| Tình huống | Hành vi |
|---|---|
| Preflight thất bại | Dừng, in lệnh khắc phục (ví dụ thêm user vào group docker, hoặc `waydroid session start`) |
| Độ phân giải không khớp profile | Dừng, báo kích thước thực tế và kích thước cần có |
| ALAS báo lỗi hoặc stuck trong log | Ghi lại vào summary, báo ntfy, không tự restart vòng lặp |
| Hết timeout | Dừng ALAS, trạng thái `timeout` |
| adb mất kết nối | Ghi log, dừng; không tự động kết nối lại trong script điều phối |

## 6. Config

Một file config chung (ví dụ `sidecar/daily.yaml`) gồm:
- `device.profile`: `waydroid` hoặc `phone`
- `device.serial`: chuỗi kết nối adb
- `device.resolution`: kích thước mong đợi, ví dụ `1280x720`
- `alas.container_name`, `alas.webui_url`, `alas.repo_dir`
- `timeouts.run_minutes`
- `notify.ntfy_url`

`watchdog.yaml` và `bot.yaml` lấy serial và ntfy từ đây, không khai báo lại.

## 7. Test

**Unit test (không cần thiết bị), chạy bằng `uv run pytest` hoặc script hiện có:**
- Parser log dùng các dòng log thật từ `AzurLaneAutoScript/log/2026-10-29_gui.txt` làm fixture.
- Kiểm tra config với file tạm, gồm cả trường hợp thiếu key và cờ `--apply` ghi đúng và có backup.
- Luồng trạng thái điều phối với docker và HTTP giả: thành công, lỗi preflight, timeout, ALAS lỗi.
- Test chọn đúng nhánh theo `device.profile`.

**E2E (cần thiết bị), một lệnh:**
- `uv run python azurlane_daily.py --e2e`
- In từng bước PASS/FAIL và trả exit code 0/1. Các bước: preflight, bật ALAS, app đã mở, đăng nhập thành công (log), commission đã chạy (log), dừng ALAS, ghi summary.
- Chạy riêng cho từng profile.

**Trạng thái kiểm chứng:**
- Unit test: do mình viết và chạy trong sandbox.
- E2E Waydroid: cần bạn chạy trên máy. Sandbox không kết nối được Waydroid hay docker.
- E2E điện thoại: chưa thể kiểm chứng, phụ thuộc quyết định ở mục 9.

## 8. Ngoài phạm vi

- Sửa code upstream của ALAS.
- Tự viết lại đăng nhập, popup, hoặc commission (đã có trong ALAS).
- Scaling độ phân giải trong ALAS.
- Chạy controller trên chính điện thoại (Termux, v.v.).
- Các tác vụ ALAS khác ngoài daily (research, exercise, Operation Siren, ...) ngoài việc bật trong config.

## 9. Câu hỏi mở

1. **Điện thoại có cần chạy trong lần này không?**
   - (1) Chưa. Lần này chỉ làm Waydroid. Điện thoại là giai đoạn sau. *(khuyến nghị: chốt đường chạy trên Waydroid trước)*
   - (2) Có, bằng cách đặt `wm size 1280x720`. Chấp nhận rủi ro méo hình, cần test trên máy thật.
2. **`commission_bot`**: giữ làm dự phòng, hay xóa sau khi e2e qua ALAS? Mặc định: giữ, chưa xóa.
3. **Bật lại `Scheduler_Enable`** cho lần chạy thủ công: có cần không? Cần xác minh trong mã trước khi lập kế hoạch.

## 10. Tự kiểm tra spec

- Không còn placeholder "TBD" hay "TODO" trong các mục đã chốt. Các điểm chưa rõ đã được đưa vào mục 9.
- Mục 2 và mục 6 nhất quán: serial và độ phân giải đều lấy từ `device.*`.
- Mục 3 và mục 4 nhất quán: script điều phối, không tap trực tiếp.
- Mọi khẳng định về ALAS đều dẫn đến file và dòng cụ thể trong repo. Khẳng định chưa kiểm chứng được ghi rõ là chưa kiểm chứng.
