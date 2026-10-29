# P0 phần còn lại + P1: NextRun timing và commission gem-first

Trạng thái: **Đã chốt điều hướng 2026-10-30, chờ lập kế hoạch.** P0-A có thể triển khai ngay; P1 chờ explore UI hủy.

## 1. Bối cảnh

- e2e đã PASS thật ngày 2026-10-29 (~77 giây, RESULT success). Orchestrator vận hành được.
- P0 còn thiếu đúng một mục: **kiểm tra timing NextRun**. Hiện stage `next run` chỉ WARNING khi `Commission.Scheduler.NextRun` (trong `alas.json`, đọc tại `sidecar/alas_config.py:141`, Ruling 33) nằm trong tương lai. Nếu NextRun nằm ngoài window `run_minutes` (60 phút), e2e vẫn chạy đủ 60 phút rồi kết thúc `no_commission` — fail muộn và vô ích.
- P1: commission ưu tiên gem trước, và khi slot đầy thì **hủy một commission giá trị thấp** để nhường slot cho gem.
- Đã xác minh (2026-10-29):
  - ALAS có filter gem-first (`Commission.CustomFilter`) nhưng **không có bất kỳ logic hủy nào** — grep toàn `module/commission` = 0 match; `RewardCommission._commission_choose` (commission.py:172) chỉ bỏ qua khi `running_count >= max_commission`.
  - Game **cho phép hủy commission đang chạy**: mất dầu đã tiêu, không nhận thưởng, tàu về ngay (AL Wiki — Commissions).
  - Pattern hủy có sẵn trong ALAS để tham khảo UI: `module/island/order.py` `reject_order()` — template + `appear_then_click` + confirm.

Kiến trúc chốt: **C (lai)** — sidecar + config ALAS là chính. Ràng buộc thêm từ người dùng (2026-10-30): **không can thiệp sâu vào logic ALAS** — nó đã được nghiên cứu và refine từ trước; mọi tính năng mới ưu tiên (1) config ALAS, (2) sidecar, fork chỉ khi bất khả kháng.

### 1b. Đối chiếu hệ sinh thái fork ALAS (2026-10-30) — vì sao không fork

Đã kiểm tra bằng GitHub API (repo public, số liệu ngày 2026-10-30):

- **Upstream `LmeSzinc/AzurLaneAutoScript`**: pushed 2026-10-28, vẫn merge bug_fix PR thường xuyên (#6070–#6086 trong tuần cuối 10/2026) — **core reward modules (surface ta phụ thuộc: login/device, commission, reward/mission, dorm, meowfficer, guild, tactical, research) vẫn được maintain**.
- **`foxwhite25/AzurLaneAutoScriptThatWorks`** (fork được nhắc trong handover): commit cuối 2024-08, chỉ là fork cộng dồn event assets/War Archives — **đã chết hơn 2 năm, không có giá trị tham chiếu**.
- **`guoh064/AzurLaneAutoScript`** (fork cộng đồng sống nhất, 37 sao, desc "từ bỏ hy vọng vào lme"): commit 2026-10-27 tập trung vào **island transport/collect, shop info-bar, minigame, supply pack** — **không module nào trùng với đường chạy daily của ta**; đồng thời nó merge upstream liên tục, chứng tỏ thượng nguồn vẫn sống.
- Kết luận: **không có bằng chứng nào cho thấy phần ALAS ta dùng bị outdated**. Rủi ro thật của ta là template drift khi game đổi UI (đã có watchdog + ntfy + abort-safe), không phải abandonware. Giữ chạy ALAS stock; đánh giá lại chỉ khi upstream thực sự ngừng nhận bug_fix.

### 1c. Bước ngoặt 2026-10-30: chuyển sang fork ALAS (ghi đè phần mềm lượng phương án trên)

Người dùng quyết định (sau khi thấy GUI gốc của ALAS): **fork ALAS, patch theo ý riêng, lưu trên GitHub** —
`https://github.com/AtelierMizumi/AzurLaneAutoScript`, branch `azdaily`. Hệ quả:

- Trigger P1 (check commission sau sortie) chuyển vào **trong ALAS**: patch `791c4944` thêm option
  `Commission > Check After Sorties` — hook ở `alas.py` `loop()` sau khi task sortie kết thúc, `cross_set`
  `Commission.Scheduler.NextRun = now`. Mặc định tắt. **Thiết kế interlude (mục 3.2.2) bị thay thế** — không
  còn docker stop/start xen giữa; mọi thứ nằm trong scheduler của ALAS, hiện native trong GUI người dùng thích.
- Hành động **hủy** commission vẫn cần template UI (mục 4 capture explore) — sau này cắm thẳng vào
  `_commission_choose` của fork thay vì sidecar.
- Sidecar dashboard bị **hạ cấp thành tùy chọn** (người dùng thích GUI ALAS); các patch tiếp theo ưu tiên
  dạng native ALAS (argument.yaml + module logic) để hiện trong GUI.
- Quy tắc rebase: branch `azdaily` giữ patch nhỏ, tái base lên `origin/master` (upstream) định kỳ; nút
  Update của ALAS trỏ vào fork qua `config/deploy.yaml` mục `Git`.

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

Module mới `sidecar/commission_cancel.py`, điều phối bởi **daemon** (P-Dash).
Nguyên tắc "một thiết bị một phiên điều khiển" giữ nguyên: sidecar chỉ đụng
màn hình khi container ALAS **đã dừng**, nên mỗi lần check là một *interlude*:

```
trigger → docker stop alas → sidecar scan + hủy (nếu cần) → docker start alas
```

### 3.2.1 Trigger — check sau mỗi 3 lần auto đi map (chốt của người dùng 2026-10-30)

Commission gem thường spawn **sau khi đi ải**, nên run-once mỗi ngày là không đủ.
Daemon đếm số lần task **Campaign** của ALAS kết thúc trong log
(`Scheduler: End task `Campaign`` — cùng cơ chế parse với `alas_report.py`);
**mỗi 3 lần kết thúc → chạy một interlude check**. Giới hạn an toàn:

- **Sàn thời gian**: cách lần check trước ≥ 20 phút (chống thrash khi farm dồn dập).
- **ALAS đang rảnh**: task cuối trong log phải có trạng thái kết thúc (không
  `incomplete`) — không chen ngang giữa task.
- Không chạy khi pipeline run-once đang hoạt động (lock có sẵn của daemon).
- Vô hiệu hóa qua config: `commission.cancel_enabled: false`.

### 3.2.2 Luồng interlude

1. `docker stop alas` (graceful; ALAS xong việc hiện tại rồi dừng).
2. Sidecar (dùng nền tảng commission_bot v2: launch/chờ/reconnect, template +
   cv2, chống tap-nhầm): vào BATTLE → commission, scan cả 2 tab (daily + urgent).
3. Nhận các commission đã xong (claim — bot v2 đã làm được).
4. Đọc trạng thái: slot `running` (OCR thời gian còn lại), slot `pending`,
   genre của từng cái (gem nhận dạng bằng icon template, không cần OCR tên).
5. Quyết định hủy — cần đồng thời cả ba điều kiện:
   - Có commission **gem** `pending` đang chờ slot. Chỉ gem — chốt của người
     dùng 2026-10-29: **không hủy chỉ vì cube** hay genre khác;
   - Mọi slot đang `running`;
   - Tồn tại commission running "chấp nhận được mất" còn **≥ `cancel_min_remaining`**
     phút (không hủy cái sắp xong — mất dầu mà gần nhận thưởng).
6. Chọn nạn nhân theo thứ tự giá trị (đã chốt): **gem > cube > oil-efficiency >
   box/book/part**; cùng genre thì hủy cái còn lâu xong nhất, giữ cái sắp xong.
7. Thực hiện hủy: tap commission running được chọn → nút hủy (UI sẽ map qua
   explore — mục 4) → dialog confirm → re-scan xác nhận slot trống → về home.
8. **Trick chown-via-docker**: chạy container một-lệnh mount repo để chown
   `config/alas.json` về user (giải tận gốc gotcha root-flip sau mỗi lần ALAS
   shutdown). Daemon ghi `Commission.Scheduler.NextRun = now` (chỉ khi đã hủy/
   trả slot) để ALAS scan ngay khi khởi động lại và **tự start gem bằng flow
   chọn fleet battle-tested của nó** — sidecar không tự chọn fleet.
9. `docker start alas`. ALAS scan → filter gem-first → start gem.

### 3.3 Vì sao sidecar chỉ hủy, còn start giao cho ALAS

- Flow start commission trong ALAS (`COMMISSION_ADVICE` → recommend fleet →
  confirm) là flow nhiều bước nhất và ALAS đã xử lý đủ edge case (commission
  flashing bug, wrong-commission check). Sidecar tự làm = viết lại dễ vỡ.
- Việc ghi `NextRun` là write config runtime — đúng ngoại lệ duy nhất được
  phép theo ràng buộc dự án (giống `--apply`), và chown-via-docker biến nó
  thành thao tác không cần sudo.
- Chi phí mỗi interlude: ~2–4 phút ALAS nghỉ (stop + scan + start). Với sàn
  20 phút, tổn thất thời gian chạy tối đa ở mức chấp nhận được.

## 4. Explore phase (điều kiện trước khi implement P1) — làm bằng capture mode

UI hủy commission **chưa được map** (ALAS không có template, ta chưa từng hủy
trong game). Thay vì hẹn giờ đứng chụp, `commission_bot.py` có cờ mới:

```fish
./azdaily bot --capture 15      # chụp màn hình mỗi 2 giây trong 15 phút
```

- Ảnh lưu `runs/explore-<ngày>/NNN.png`. Người dùng chỉ cần **chơi như bình
  thường và hủy tay một commission bất kỳ** trong lúc capture chạy ở background
  (agent chạy hộ cũng được) — không cần phối hợp thời gian chính xác.
- Từ bộ ảnh, agent crop template: nút hủy/abandon, dialog confirm, icon gem
  trên card pending, trạng thái sau hủy — tập hợp vào `sidecar/assets/` và
  flow doc, archive `runs/explore-<ngày>/`.
- Tương tự cần vài ảnh màn **start** commission đang chạy để xác định field
  OCR thời gian còn lại (nạn nhân cần biết còn bao lâu).

## 5. Config mới trong `daily.yaml`

```yaml
commission:
  cancel_enabled: false          # Bật sau khi explore + live run OK
  cancel_min_remaining: 30       # Phút: chỉ hủy commission còn >= 30 phút
```

Không thêm config cho filter (để GUI). Watchdog và commission_bot không đọc phần này (tách module riêng, không làm `load_daily_config` bắt buộc thêm).

## 6. Test

- P0: unit test 3 nhánh NextRun (quá giờ / trong window / ngoài window) với fixture `alas.json`; không cần thiết bị.
- P1 interlude: `--selftest` + `--replay` theo style commission_bot v2, state machine test với fake adb, assert đúng tap và đúng điều kiện hủy (không hủy khi thiếu một trong 3 điều kiện); unit test cho trigger counter (đếm Campaign end, sàn 20 phút, vô hiệu khi `cancel_enabled: false`).
- Live run P1 khi Waydroid rảnh, bật `cancel_enabled` từ từ.
- Không sửa test để khớp code sai.

## 7. Ngoài phạm vi

- Fork/patch ALAS (đã chốt không fork — mục 1b; start commission giao cho ALAS).
- Chèn tap giữa các task của ALAS đang chạy (mọi tương tác sidecar chỉ xảy ra khi container dừng).
- P2–P7, tự lên lịch cron cho orchestrator.

## 8. Quyết định đã chốt và câu hỏi còn mở

Đã chốt (người dùng, 2026-10-29/30):

1. Chỉ hủy khi có commission **gem** pending (không vì cube); thứ tự giá trị
   nạn nhân như mục 3.2 bước 5.
2. **Chọn mặc định cho 2 câu hỏi còn lại** (người dùng chưa phản bác — nếu muốn
   đổi thì sửa lại mục này trước khi lập plan P1):
   - Explore hủy commission: **capture mode** — người dùng chơi + hủy tay một
     commission trong khi `commission_bot --capture` chụp nền; không cần hẹn giờ.
   - `Commission.CustomFilter`: **để GUI của ALAS tự quản** — `--apply` không
     đụng filter, tránh trượt tay ghi sai chuỗi ưu tiên.
3. Không fork ALAS — chốt lại 2026-10-30 sau khi đối chiếu hệ sinh thái fork
   (mục 1b): upstream vẫn maintain core modules; fork cộng đồng không chạm
   vào surface ta dùng.
4. Trigger P1: **check commission sau mỗi 3 lần Campaign kết thúc** (chốt của
   người dùng 2026-10-30), kèm sàn 20 phút và điều kiện ALAS rảnh — mục 3.2.1.
5. Guide người dùng (`docs/HUONG-DAN.md`) đã có và được giữ cập nhật theo
   từng tính năng; repo public tại
   `https://github.com/AtelierMizumi/azurlane-daily-tools` (Apache-2.0).

Không còn blocker lịch cho P1: chỉ cần một phiên chơi game trong đó người
dùng hủy tay một commission (và để một commission đang chạy mở màn detail)
trong khi `./azdaily bot --capture 15` chạy nền. Sau khi có bộ ảnh, agent
crop template rồi lập plan implement.

## 9. Tự kiểm tra spec

- Không còn placeholder TBD; các điểm chưa rõ nằm hết ở mục 8.
- Mọi khẳng định về ALAS đều trích file:dòng: `commission.py:172` (`_commission_choose`), `alas_config.py:141` (NextRun), `island/order.py` `reject_order()`, `alas.py:177` (goto_main). Khẳng định về game-mechanics hủy commission dẫn nguồn wiki, chưa tự kiểm chứng trên thiết bị — sẽ kiểm chứng trong explore.
- Nhất quán nguyên tắc: sidecar chỉ điều khiển thiết bị khi container ALAS dừng; ALAS là bộ lập lịch duy nhất khi container chạy.
- Không thêm bắt buộc config mới cho watchdog/commission_bot hiện có.
