# azurlane-daily-tools

Tool tự động hoá daily Azur Lane chạy trên Waydroid (Arch Linux + Wayland/niri), đứng trên vai [ALAS (AzurLaneAutoScript)](https://github.com/LmeSzinc/AzurLaneAutoScript).

## Cài đặt & chạy nhanh

Yêu cầu sẵn có trên máy: Docker (đã dựng container ALAS theo phần Kiến trúc bên dưới), Waydroid ghim 1280x720, và [uv](https://docs.astral.sh/uv/).

```sh
./install.sh          # cài dependencies, tạo sidecar/daily.yaml nếu thiếu, chạy test offline
./azdaily init        # (tự chạy trong install.sh) tạo config từ daily.example.yaml
./azdaily daemon      # dashboard web (http://127.0.0.1:22268) + giữ ALAS chạy always-on
./azdaily check       # soát an toàn: preflight + config ALAS, không bật container
./azdaily run         # chạy một lượt daily, báo PASS/FAIL theo từng stage
./azdaily apply       # như run, và cho phép ghi config ALAS (container phải đang dừng)
./azdaily report      # báo cáo lịch ALAS từ log mới nhất
./azdaily test        # toàn bộ test offline (unit + state machine + bot selftest)
```

Lệnh `./azdaily` là entry point thống nhất; gọi trực tiếp từng script (`uv run python azurlane_daily.py ...`) vẫn hoạt động như cũ. Hướng dẫn đầy đủ cho người dùng: [docs/HUONG-DAN.md](docs/HUONG-DAN.md).

## Kiến trúc

```
ALAS (Docker, python 3.7)  ── adb ──►  Waydroid (1280x720, Azur Lane EN)
        │  log/, WebUI :22267
        ▼
sidecar/ (watchdog + planner)  ──►  ntfy notification
```

- **ALAS** = workhorse: commission, research, Operation Siren, exercise, hard mode, tactical class. Chạy trong container Docker `network_mode: host`, mount thư mục `AzurLaneAutoScript/`.
- **sidecar/** = phần của chúng ta: watchdog giám sát + (sắp tới) bộ tối ưu xếp tàu/classroom theo tier list.

## Trạng thái Phase 0

| Thành phần | Trạng thái |
|---|---|
| Waydroid 1280x720 | ✅ `persist.waydroid.width/height` set, xác nhận qua `wm size` |
| ALAS clone | ✅ `AzurLaneAutoScript/` (upstream master, không sửa code) |
| deploy.yaml | ✅ từ `deploy.template-docker.yaml`, tắt `ReplaceAdb`/`AutoConnect` để không phá adb SDK của host |
| Docker image `alas` | build từ `deploy/docker/Dockerfile` (python:3.7-slim-bookworm) |
| Watchdog | ✅ code xong, cần `watchdog.yaml` + ntfy topic để bật notification |
| commission_bot v2 | ✅ bot tự claim commission: 10 template, phân biệt dialog theo text, auto-reconnect adb, xử lý network drop, scroll + tab URGENT, selftest/replay/regression test. Live run v1 đã claim được; v2 chờ live run đủ vòng |

## commission_bot

```sh
cd sidecar
uv run python commission_bot.py --selftest   # validate template (bắt buộc pass trước khi live)
uv run python commission_bot.py --dry-run    # xem bot quyết định gì, không tap
uv run python commission_bot.py              # chạy thật
uv run python test_state_machine.py          # regression test state machine (không cần máy)
```

Yêu cầu: game mở ở **home screen full-UI** (không ở chế độ ẩn UI) hoặc màn commission. Config: `bot.yaml`. Template crop từ screenshot thật lưu `runs/explore-20261029/` — game đổi UI thì crop lại qua `crop_templates.py`. Giới hạn đã biết: chưa nhận diện màn hình title sau khi reload (bot đợi 90s rồi dừng an toàn); app khác chiếm foreground giữa chừng → bot dừng, không giành input.

## Chạy

```sh
# ALAS (GUI ở http://127.0.0.1:22267)
cd AzurLaneAutoScript && docker compose up -d

# Watchdog (đọc sidecar/watchdog.example.yaml, tạo watchdog.yaml)
cd sidecar && uv run watchdog.py

# Lưu ý: host adb server phải kill trước khi chạy ALAS (container dùng port 5037 do network_mode: host)
~/Android/Sdk/platform-tools/adb kill-server
```

## Quy ước quan trọng

- Không sửa code upstream ALAS trong `AzurLaneAutoScript/` — mọi thứ của ta nằm ở `sidecar/` (trừ `config/deploy.yaml` và `docker-compose.override.yml` vốn là user config/gitignored).
- ALAS yêu cầu đúng **Android display 1280x720**, không phải cửa sổ desktop. `persist.waydroid.width/height` đã ghim display — resize cửa sổ Waydroid thoải mái (nội dung chỉ scale), miễn là `docker exec alas adb -s 192.0.2.10:5555 shell wm size` vẫn trả 1280x720.
- **FROZEN pitfall**: đóng cửa sổ Waydroid → container `FROZEN` (LXC freezer) → adb treo vô hạn, không lỗi không timeout. Hồi phục user-level (không cần root): `waydroid app launch com.YoStarEN.AzurLane` (tự unfreeze qua DBus). `waydroid container unfreeze` cần root.
- Sau freeze/thaw, kết nối adb cũ chết ngầm — phải `adb disconnect` + `connect` lại. Watchdog sidecar phụ trách cả hai (keep-alive + reconnect).
- Container adb của ALAS chiếm port 5037 của host (do `network_mode: host`); kill adb host trước khi chạy ALAS. Cần adb từ host: dùng `docker exec alas adb ...` để không đụng độ.
- adb Waydroid yêu cầu RSA auth — key host đã được xác thực, container dùng chung key qua mount trong `docker-compose.override.yml` (thêm adbkey của bạn nếu đổi máy).

## Daily orchestrator (Waydroid)

Chạy một lượt daily Azur Lane trên Waydroid qua ALAS: preflight, chạy Commission, rồi báo cáo PASS/FAIL theo từng bước.

```sh
cd sidecar
uv run python azurlane_daily.py --check   # preflight + kiểm tra config ALAS, không khởi động container
uv run python azurlane_daily.py --e2e     # chạy đầy đủ, in báo cáo PASS/FAIL theo từng bước
uv run python azurlane_daily.py --apply   # như --e2e, và cho phép sửa config ALAS
```

**Cảnh báo:** `--apply` có thể ghi `AzurLaneAutoScript/config/alas.json` và `AzurLaneAutoScript/config/deploy.yaml` (đặt `Run: ["alas"]` để container tự chạy instance `alas`), mỗi file có bản sao lưu `.bak` tạo trước khi ghi. Chỉ chạy khi ALAS đã dừng. Preflight kiểm tra độ phân giải qua adb của container sau `docker start`, nên `--check` bỏ qua bước này khi container đang dừng.

- Chỉ hỗ trợ `device.profile: waydroid`; profile phone chưa được hỗ trợ.
- `daily.yaml` nằm trong `sidecar/` và là bắt buộc với `azurlane_daily.py` (thiếu file sẽ báo `FAIL daily config`). Tạo bằng cách sao chép `sidecar/daily.example.yaml` thành `sidecar/daily.yaml` rồi chỉnh giá trị (serial ví dụ `192.0.2.10` là địa chỉ RFC 5737 dành cho tài liệu — thay bằng serial Waydroid thật).

## Bản quyền & nhãn hiệu

Dự án phát hành theo **Apache License 2.0** — xem [LICENSE](LICENSE) và [NOTICE](NOTICE):

- Dùng, sửa, phân phối lại tự do, kể cả thương mại; **không kèm bất kỳ bảo hành hay trách nhiệm pháp lý nào** — rủi ro khi sử dụng (kể cả rủi ro với tài khoản game) thuộc về người dùng.
- Dự án là công cụ **fan-made phi chính thức**, không liên kết, không được chứng thực hay tài trợ bởi Manjuu/YoStar (chủ sở hữu Azur Lane) hay LmeSzinc (tác giả ALAS). "Azur Lane" và các tài sản liên quan là nhãn hiệu của chủ sở hữu tương ứng; các nhãn hiệu này **không** được cấp phép bởi dự án này.
- Tên dự án `azurlane-daily-tools` và lệnh `azdaily` thuộc tác giả. Ai fork hay phân phối lại vui lòng giữ nguyên `LICENSE` và `NOTICE` — đó là điều kiện bảo đảm quyền lợi cho cả hai bên: người dùng nhận quyền bằng sáng chế rõ ràng (Apache-2.0 mục 3), tác giả được miễn trách nhiệm và giữ nhãn hiệu của mình.
- Serial thiết bị trong ví dụ dùng địa chỉ `192.0.2.10` (RFC 5737, dải địa chỉ dành riêng cho tài liệu) — thay bằng serial Waydroid thật của bạn.

Lưu ý: tự động hoá game nằm trong vùng xám của Điều khoản dịch vụ; tác giả phát hành công cụ này cho mục đích học tập và nghiên cứu, người dùng tự chịu trách nhiệm khi sử dụng.
