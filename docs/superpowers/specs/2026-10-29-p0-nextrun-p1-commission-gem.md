# P0 phần còn lại + P1: NextRun timing và commission gem-first

Trạng thái: **Bản nháp, chờ bạn duyệt.** Chưa implement. Chưa lập kế hoạch.

Kiến trúc đã chốt: **C (lai)** — sidecar + config ALAS là chính, không fork trừ khi bắt buộc.

## 1. Bối cảnh

- e2e đã PASS thật ngày 2026-10-29 (~77 giây, RESULT success). Orchestrator vận hành được.
- P0 còn thiếu đúng một mục: **kiểm tra timing NextRun**. Hiện stage `next run` chỉ WARNING khi `Commission.Scheduler.NextRun` (trong `alas.json`, đọc tại `sidecar/alas_config.py:141`, Ruling 33) nằm trong tương lai. Nếu NextRun nằm ngoài window `run_minutes` (60 phút), e2e vẫn chạy đủ 60 phút rồi kết thúc `no_commission` — fail muộn và vô ích.
- P1: commission ưu tiên gem trước, và khi slot đầy thì **hủy một commission giá trị thấp** để nhường slot cho gem.
- Đã xác minh (2026-10-29):
  - ALAS có filter gem-first (`Commission.CustomFilter`) nhưng **không có bất kỳ logic hủy nào** — grep toàn `module/commission` = 0 match; `RewardCommission._commission_choose` (commission.py:172) chỉ bỏ qua khi `running_count >= max_commission`.
  - Game **cho phép hủy commission đang chạy**: mất dầu đã tiêu, không nhận thưởng, tàu về ngay (AL Wiki — Commissions).
  - Pattern hủy có sẵn trong ALAS để tham khảo UI: `module/island/order.py` `reject_order()` — template + `appear_then_click` + confirm.

## 2. Phần A — P0: NextRun fail-fast

Mục tiêu: `--check` phát hiện sớm lượt chạy vô vọng thay vì WARNING rồi để e2e chạy 60 phút không kết quả.

Thay đổi tại stage `next run` của `azurlane_daily.py` (đang WARNING-only tại dòng 456):

| Điều kiện (NextRun của Commission) | Kết quả |
|---|---|
| Không có hoặc đã quá giờ | PASS — ALAS chạy ngay khi container start |
| Trong tương lai, cách ≤ `run_minutes` | PASS kèm ghi chú thời gian chờ |
| Trong tương lai, cách > `run_minutes` | **FAIL** kèm chỉ dẫn: tăng `run_minutes` trong `daily.yaml`, hoặc chạy lại sau giờ HH:MM |

- Áp dụng cho task Commission (task mà orchestrator theo dõi), không áp cho các task khác.
- e2e không đổi hành vi; chỉ check nhanh được mạnh hơn.

## 3. Phần B — P1: gem-first + hủy commission

### 3.1 Gem-first filter (config, không code)

`Commission.CustomFilter` của ALAS đặt ưu tiên gem trước. Cách đặt: qua GUI của ALAS (khuyến nghị — `--apply` không đụng filter để tránh trượt tay ghi sai). Spec này không viết code cho phần này.

### 3.2 Sidecar pre-pass hủy commission (code mới, không fork)

Module mới `sidecar/commission_cancel.py`, chạy **TRƯỚC `docker start`** trong luồng orchestrator — đúng nguyên tắc "một thiết bị một phiên điều khiển": sidecar chỉ đụng thiết bị khi container ALAS dừng.

Luồng:

1. Bật app qua adb (đường `commission_bot` v2 đã có: launch, chờ, reconnect).
2. Vào BATTLE → commission, scan cả 2 tab (daily + urgent) bằng kỹ thuật commission_bot v2: template matching + cv2 + chờ animation + chống tap-nhầm.
3. Đọc trạng thái: slot nào `running` (còn bao lâu — OCR thời gian), slot nào `pending`, genre của từng cái (gem, cube, oil, box, book...).
4. Quyết định hủy — cần đồng thời cả ba điều kiện:
   - Có commission **gem** `pending` đang chờ slot. Chỉ gem — chốt của người
     dùng 2026-10-29: **không hủy chỉ vì cube** hay genre khác;
   - Mọi slot đang `running`;
   - Tồn tại commission running "chấp nhận được mất" còn **≥ `cancel_min_remaining`**
     phút (không hủy cái sắp xong — mất dầu mà gần nhận thưởng).
5. Chọn nạn nhân theo thứ tự giá trị (đã chốt với người dùng): **gem > cube >
   oil-efficiency > box/book/part**; trong cùng genre thì hủy cái còn lâu xong
   nhất, giữ lại cái sắp xong.
6. Thực hiện hủy: tap commission running được chọn → nút hủy (UI chưa map — xem mục 4) → dialog confirm → re-scan xác nhận slot trống.
6. Kết thúc: về màn an toàn, thoát app không cần (ALAS sẽ tự xử), orchestrator tiếp tục `docker start`. ALAS scan thấy slot trống → filter gem-first start gem.

Mặc định **tắt** qua config cho đến khi explore xong và live run tin cậy.

### 3.3 Giới hạn đã chấp nhận (ghi rõ, không phải bug)

Chỉ phản ứng tại thời điểm chạy daily. Gem urgent xuất hiện **giữa ngày** khi slot đầy sẽ không được hủy tự động — ALAS không có timer cho urgent mới sinh ra, và sidecar không điều khiển thiết bị khi container ALAS đang chạy. Muốn xử lý cả trường hợp này thì phải fork ALAS (đặt logic hủy vào `_commission_choose`) — ngoài phạm vi theo kiến trúc C; xem lại nếu sau này thấy mất gem thực tế.

## 4. Explore phase (điều kiện trước khi implement P1)

UI hủy commission **chưa được map** (ALAS không có template, ta chưa từng hủy trong game). Cần một phiên explore như `runs/explore-20261029`:

- Chụp: màn detail của commission đang chạy, nút hủy/abandon, dialog confirm, trạng thái sau hủy, trạng thái slot trống.
- Deliverable: templates vào `sidecar/assets/`, flow doc, archive `runs/explore-<ngày>`.
- Cách thực hiện: người dùng hủy tay một commission thật (chấp nhận mất dầu) trong khi agent hỗ trợ chụp qua adb, hoặc agent điều khiển toàn bộ khi thiết bị rảnh — chốt ở mục câu hỏi mở.

## 5. Config mới trong `daily.yaml`

```yaml
commission:
  cancel_enabled: false          # Bật sau khi explore + live run OK
  cancel_min_remaining: 30       # Phút: chỉ hủy commission còn >= 30 phút
```

Không thêm config cho filter (để GUI). Watchdog và commission_bot không đọc phần này (tách module riêng, không làm `load_daily_config` bắt buộc thêm).

## 6. Test

- P0: unit test 3 nhánh NextRun (quá giờ / trong window / ngoài window) với fixture `alas.json`; không cần thiết bị.
- P1 pre-pass: `--selftest` + `--replay` theo style commission_bot v2, state machine test với fake adb, assert đúng tap và đúng điều kiện hủy (không hủy khi thiếu một trong 3 điều kiện).
- Live run P1 khi Waydroid rảnh, bật `cancel_enabled` từ từ.
- Không sửa test để khớp code sai.

## 7. Ngoài phạm vi

- Fork/patch ALAS (quay lại xem xét chỉ nếu explore cho thấy sidecar không làm được).
- Phản ứng gem giữa ngày (3.3).
- P2–P7, tự lên lịch cron cho orchestrator.
- Hủy commission trong lúc ALAS đang chạy.

## 8. Câu hỏi mở

Đã chốt theo phản hồi 2026-10-29: chỉ hủy khi có commission **gem** pending
(không vì cube); thứ tự giá trị nạn nhân như mục 3.2 bước 5. Yêu cầu mới cùng
ngày: **guide người dùng** (`docs/HUONG-DAN.md`) — bản nháp đã viết, chờ duyệt
cấu trúc và giọng văn.

1. **Explore hủy commission**: bạn tự hủy tay một lần trong khi agent chụp qua
   adb (khuyến nghị), hay agent điều khiển toàn bộ khi thiết bị rảnh?
2. **`Commission.CustomFilter`**: xác nhận để GUI tự quản (khuyến nghị), hay
   muốn sidecar quản lý qua `--apply`?
3. **`docs/HUONG-DAN.md`**: có phần nào cần thêm/sửa để đạt chất lượng guide
   cộng đồng mà bạn mong muốn?

## 9. Tự kiểm tra spec

- Không còn placeholder TBD; các điểm chưa rõ nằm hết ở mục 8.
- Mọi khẳng định về ALAS đều trích file:dòng: `commission.py:172` (`_commission_choose`), `alas_config.py:141` (NextRun), `island/order.py` `reject_order()`, `alas.py:177` (goto_main). Khẳng định về game-mechanics hủy commission dẫn nguồn wiki, chưa tự kiểm chứng trên thiết bị — sẽ kiểm chứng trong explore.
- Nhất quán nguyên tắc: sidecar chỉ điều khiển thiết bị khi container ALAS dừng; ALAS là bộ lập lịch duy nhất khi container chạy.
- Không thêm bắt buộc config mới cho watchdog/commission_bot hiện có.
