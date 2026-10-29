# Hướng dẫn sử dụng — Azur Lane Daily Tools

> Tool tự động chạy daily Azur Lane trên Waydroid, bằng một lệnh duy nhất.
> Tài liệu này viết cho người sử dụng, không đòi hỏi biết code. Phần dựng hệ
> thống lần đầu xem [README.md](../README.md).

---

## 1. Bộ tool này làm được gì?

| Chức năng | Trạng thái | Nó làm gì |
|---|---|---|
| Chạy daily bằng một lệnh | ✅ Đang chạy | Bật ALAS → ALAS mở game, đăng nhập, xử lý popup, chạy commission → tắt container → báo kết quả |
| Commission (claim + start) | ✅ Đang chạy | Nhận thưởng commission xong, quét cả tab Daily lẫn Urgent, chọn commission tốt nhất đang có để start |
| Claim mission (Daily/Weekly/Event) | ✅ Đang chạy | Task **Reward** của ALAS tự vào màn Missions bấm **Collect ALL** (kèm dầu, coin, exp trang chủ) theo lịch riêng của nó |
| Các task ALAS khác đang bật | ✅ Đang chạy | Tactical, Research, Dorm, Meowfficer, Guild... — ALAS tự chạy theo lịch riêng của từng task |
| Báo cáo lịch chạy | ✅ Đang chạy | Đọc log ALAS, liệt kê từng task đã chạy lúc nào, kết quả ra sao, lần dậy kế tiếp là khi nào |
| Canh treo + thông báo | ✅ Đang chạy | Watchdog theo dõi container, WebUI, log, màn hình game; phát hiện bất thường gửi thông báo ntfy |
| Commission ưu tiên gem + hủy thông minh | 🔜 P1 | Khi slot đầy mà có commission **gem** đang chờ: hủy một commission giá trị thấp (mất dầu, không mất gì khác) để nhường slot |
| PvP an toàn | 🔜 P5 | Chỉ đánh đối thủ có power yếu hơn rõ rệt, tránh mất điểm |
| Operation Siren, farm exp, tierlist, Akashi | 🔜 P2–P7 | Xem lộ trình ở cuối tài liệu |

**Commission gem là gì, vì sao quý?** Urgent commission loại này thưởng **gems
(đá quý)** — thứ thường phải nạp tiền mới có. Nó xuất hiện ngẫu nhiên, có hạn
thời gian, và chiếm một trong 4 slot commission. Nếu cả 4 slot đều đang bận
những commission dài 8–10 tiếng thì gem commission bị bỏ lỡ. Phần P1 sẽ giải
quyết đúng tình huống này.

---

## 2. Ai làm việc gì?

Coi hệ thống như một xưởng nhỏ:

| Thành phần | Vai trò | Khi nào chạy |
|---|---|---|
| **ALAS** (container Docker) | Thợ chính: mở game, đăng nhập, bấm popup, chạy mọi tác vụ trong game | Khi orchestrator bật container lên |
| **`azurlane_daily.py`** (orchestrator) | Quản đốc: kiểm tra an toàn trước giờ G, bật/tắt ALAS, ngồi đọc log, chấm pass/fail, viết báo cáo | Khi bạn gõ lệnh |
| **`watchdog.py`** | Bảo vệ gác đêm: dò container chết, WebUI tắt, log ngừng ghi, màn hình treo — báo ntfy | Chạy nền (tùy bạn bật) |
| **`commission_bot.py`** | Thợ dự phòng: tự vào game claim commission bằng nhận dạng hình ảnh, không cần ALAS | Chạy tay khi muốn; không nằm trong đường chính |
| **`alas_report.py`** | Người ghi chép: đọc log ALAS, tóm tắt lịch + kết quả từng task | Chạy tay bất cứ lúc nào |

Nguyên tắc quan trọng nhất: **một thời điểm chỉ một "người" điều khiển
game**. Khi ALAS đang chạy (container đang bật) thì không bot nào, không bàn
phím tay nào đụng vào Waydroid.

---

## 3. Chuẩn bị trước mỗi ngày

Tool cần mấy điều kiện sau. Lỗi nào thiếu sẽ bị `--check` chỉ thẳng mặt ở bước 4.

1. **Tắt scheduler của instance `alas` trong WebUI** (`http://127.0.0.1:22267`).
   Vì sao: orchestrator sẽ tự bật ALAS lên lúc cần; để scheduler tự dậy là hai
   "người" cùng điều khiển.
2. **Tắt container**: `docker stop alas`. Orchestrator yêu cầu container đang dừng.
3. **Waydroid đang chạy, không bị FROZEN**: `waydroid status` phải hiện
   `Session: RUNNING`. Nếu `FROZEN`: gõ `waydroid app launch com.YoStarEN.AzurLane`.
4. Nếu lần này cần ghi cấu hình ALAS (xem mục 4 — chỉ lần đầu hoặc khi đổi cấu hình):
   `sudo chown thuanc177: ~/Projects/Tools/azurlane-daily-tools/AzurLaneAutoScript/config/alas.json`
   Vì sao: mỗi lần ALAS tắt, nó tự ghi lại file cấu hình này bằng quyền root
   (hành vi có sẵn của ALAS), nên lần sau phải "trao lại quyền" cho tool.

---

## 4. Quy trình chạy daily

Mở terminal vào thư mục tool:

```fish
cd ~/Projects/Tools/azurlane-daily-tools/sidecar
```

### Lần đầu tiên (hoặc sau khi đổi cấu hình ALAS)

```fish
uv run python azurlane_daily.py --e2e --apply
```

Cờ `--apply` bảo tool tự sửa hai thứ trong cấu hình ALAS (có bản `.bak` dự phòng):
bật `AutoRestart`, khai báo instance trong `Run`. Cần container đang dừng — đúng
trình tự ở mục 3 là hợp lệ.

### Các ngày bình thường

```fish
uv run python azurlane_daily.py --check   # soát an toàn, không bật gì cả
uv run python azurlane_daily.py --e2e     # chạy thật
```

`--check` PASS hết mới nên chạy `--e2e`. Trong lúc `--e2e` chạy (**tối đa
60 phút**): đừng mở game, đừng đụng Waydroid. App khác chiếm màn hình là bot
dừng an toàn.

### Kết quả đọc ở đâu?

- Trên terminal: từng dòng `PASS`/`FAIL` (giải thích ở mục 5) và dòng cuối
  `RESULT:` — `success` là tốt nhất; exit code `0` = thành công.
- File báo cáo đầy đủ: `sidecar/runs/<ngày-giờ>/summary.json`.
- Thông báo điện thoại: nếu cấu hình `ntfy_url` trong `daily.yaml` (tùy chọn).

---

## 5. Hiểu từng dòng PASS/FAIL

| Stage | Nghĩa là | Nếu FAIL thì sao |
|---|---|---|
| `preflight` | Docker truy cập được, container `alas` tồn tại, Waydroid OK, port 5037 rảnh | Làm theo phần gợi ý in kèm |
| `container state` | Container đang dừng (điều kiện để bắt đầu) | `docker stop alas` |
| `config apply` | Ghi cấu hình ALAS + backup `.bak` (chỉ khi có `--apply`) | Xem mục 6, lỗi quyền |
| `config` | Cấu hình ALAS đủ và đúng | Chạy `--e2e --apply` khi container dừng |
| `next run` | Lịch dậy kế tiếp của Commission nằm trong cửa sổ 60 phút + trạng thái task Reward (claim mission) | Chạy sau giờ được chỉ, hoặc tăng `run_minutes` |
| `docker start` | Container đã bật | Xem `docker logs alas` |
| `resolution` | Màn hình đúng 1280x720 | Kiểm tra `waydroid status`, adb; đôi khi adb chậm — chạy lại |
| `webui` | Giao diện ALAS sống | Chờ vài giây, chạy lại |
| `app started` / `login ok` | Game đã mở và đã đăng nhập | Nếu FAIL mà các stage sau PASS → báo lại (đã từng là lỗi đọc log, đã sửa) |
| `commission started` | Thấy ALAS quét commission trong log | Xem log `AzurLaneAutoScript/log/` |
| `commission done` | Commission kết thúc trong log | Tương tự |
| `commission` | Chấm điểm tổng: quét + kết thúc + không lỗi = success; kết thúc mà không quét = `no_commission` | Xem mục FAQ |
| `summary written` / `docker stop` | Đã ghi báo cáo, container đã tắt sạch | `docker stop` FAIL phải được báo — kiểm tra tay |

---

## 6. Xử lý sự cố thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `Permission denied .../config/alas.json` | ALAS ghi lại file này bằng root mỗi lần tắt | `sudo chown thuanc177: <đường dẫn>` rồi chạy lại (mục 3, bước 4) |
| `FAIL config: GameManager.AutoRestart is missing... Run is null` | Cấu hình ALAS chưa được tool ghi | `--e2e --apply` khi container đang dừng |
| `No such file or directory: 'daily.yaml'` | Đang đứng nhầm thư mục | `cd` vào `sidecar/` có `daily.yaml` |
| `FAIL resolution` hoặc skipped | Waydroid chưa READY, adb chậm, hoặc sai độ phân giải | `waydroid status`; chạy lại; kiểm tra `persist.waydroid.width/height` |
| `Session: FROZEN` | Đóng cửa sổ Waydroid làm container ngủ đông | `waydroid app launch com.YoStarEN.AzurLane` |
| Game hỏi *"Are you sure you want to quit the game?"* | Có lệnh BACK lọt vào màn home | Bấm **Cancel** (~487, 508) |
| Dialog *"Network connection failed [HostNotFound]"* | Game drop session khi idle | Bấm **Confirm**, game tự nạp lại ~30 giây |
| Một app lạ chiếm màn hình Waydroid | Ai đó đang dùng Waydroid | **Dừng bot**. Không giành input — bot tự dừng an toàn trên màn lạ là đúng |
| Container `Exited (137)` | ALAS tắt chậm, bị Docker ép tắt | Vô hại, bỏ qua |
| ALAS hẹn dậy lúc 14:00 hôm sau | Hành vi lịch của ALAS (`server_update`) | Không phải lỗi tool |

---

## 7. Các lệnh phụ

```fish
# Báo cáo lịch: task nào chạy lúc nào, lần dậy kế tiếp
uv run python alas_report.py --log ~/Projects/Tools/azurlane-daily-tools/AzurLaneAutoScript/log/2026-10-29_alas.txt

# Bot commission dự phòng (chạy tay, không cần ALAS)
uv run python commission_bot.py --dry-run      # diễn tập, không bấm thật
uv run python commission_bot.py --selftest     # kiểm tra nhận dạng ảnh
uv run python commission_bot.py --replay runs/<thư-mục-ảnh-cũ>   # test trên ảnh cũ

# Watchdog chạy nền (cần watchdog.yaml + ntfy topic)
uv run python watchdog.py
```

---

## 8. Câu hỏi thường gặp

**Có bị ban tài khoản không?**
Automation vi phạm điều khoản dịch vụ của game. Theo lịch sử cộng đồng nhiều
năm thì rủi ro thấp nhưng **không bằng không**. Quyết định dùng tool là bạn
chấp nhận rủi ro đó. Bot được thiết kế chạy chậm, có chờ đợi như người thật,
không spam thao tác.

**Chạy xong đứng im cả chục phút, sao không thấy gì?**
ALAS có lịch riêng cho từng task (`NextRun`). Nếu Commission chưa đến giờ,
ALAS sẽ chờ đúng giờ mới chạy — check ở bước `next run` sẽ cảnh báo trước
trường hợp chờ lâu hơn 60 phút. Đây là hành vi đúng, không phải treo.

**`RESULT: no_commission` nghĩa là gì?**
Commission có kết thúc trong log nhưng không hề có bước quét — thường là lần
đó không còn commission nào để chạy. Ngày nào vẫn thấy `commission started`
+ `done` thì không sao.

**Đã làm daily mà sao màn Missions vẫn còn thưởng chưa claim?**
Task **Reward** của ALAS (trông màn Missions — nút Collect ALL — kèm dầu,
coin, exp) có chu kỳ riêng 2–4 tiếng. Lần chạy đầu sau khi bật, hạn dậy của nó
có thể chưa tới trong cửa sổ chạy: stage `next run` sẽ ghi rõ
*"missions will be claimed on a later container start"*. Từ các lần chạy daily
sau, hạn này luôn đã quá hạn nên claim diễn ra ngay trong cửa sổ. Muốn xác
nhận, chạy `alas_report.py` hoặc soi log ALAS tìm dòng `Mission reward`.

**Muốn tự bấm commission bằng tay thì sao?**
Khi container ALAS đang bật hoặc bot đang chạy thì **đừng** — hai người cùng
giữ một phiên điều khiển là gây lỗi cho cả hai. Tắt hết rồi thoải mái.

**Dữ liệu của tôi đi đâu?**
Mọi thứ chạy local: log, ảnh chụp, báo cáo đều nằm trong thư mục dự án.
Cấu hình `ntfy_url` để trống thì không có gì rời khỏi máy. Repo không có
remote, không có gì được tự động gửi đi.

---

## 9. Lộ trình

| Giai đoạn | Nội dung |
|---|---|
| P0 — Nền tảng | ✅ e2e chạy thật; còn lại: fail-fast khi lịch NextRun ngoài cửa sổ chạy |
| P1 — Commission | 🔜 Gem-first + hủy commission khi slot đầy (chỉ hủy vì gem) |
| P2 — Event/SP | 🔜 Auto SP, đổi fleet theo morale |
| P3 — Farm exp khi research | 🔜 |
| P4 — Tierlist | 🔜 Chọn fleet theo meta |
| P5 — PvP an toàn | 🔜 Chỉ đánh đối thủ thắng được |
| P6 — Operation Siren | 🔜 |
| P7 — Akashi + đổi xu | 🔜 |
| P-Doc — Tài liệu | 🔜 Guide này sẽ cập nhật theo từng tính năng mới |

Chi tiết thiết kế: `docs/superpowers/specs/`, trạng thái tổng thể:
`docs/HANDOVER.md`.
