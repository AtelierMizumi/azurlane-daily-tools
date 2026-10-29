"""Crop UI templates from exploration screenshots for template matching.

One-off helper: run from sidecar/ . Sources are archived game screenshots at
1280x720 (EN server, resolution-locked Waydroid) in runs/explore-20261029/.
"""
from pathlib import Path

from PIL import Image

EXPLORE = Path(__file__).parent / "runs" / "explore-20261029"

SOURCES = {
    "e4": EXPLORE / "e4.png",                        # home, full UI
    "f1": EXPLORE / "f1.png",                        # attack hub
    "f2": EXPLORE / "f2.png",                        # commission list
    "f3": EXPLORE / "f3_reward1.png",                # WELL DONE animation
    "quit": EXPLORE / "shot_after_back.png",         # quit dialog
    "item": Path(__file__).parent / "runs" / "20261029-072459" / "04_ABORT_full.png",
    "net2": Path(__file__).parent / "runs" / "20261029-073607" / "02_ABORT_full.png",
    "title": Path(__file__).parent / "runs" / "20261029-101724" / "24_unknown_ABORT_full.png",
}

# name -> (source, (left, top, right, bottom))
CROPS = {
    "battle_btn":     ("e4",   (1130, 445, 1250, 565)),
    "commissions_btn": ("f1",  (812, 585, 948, 640)),
    "complete_btn":   ("f2",   (365, 155, 452, 183)),
    "daily_icon":     ("f2",   (30, 126, 70, 170)),     # gold clipboard icon, DAILY tab active
    "well_done":      ("f3",   (235, 95, 460, 150)),
    "quit_text":      ("quit", (385, 330, 895, 365)),   # "Are you sure you want to quit the game?"
    "info_x":         ("quit", (845, 175, 928, 218)),   # red X shared by all Info dialogs
    "confirm_r":      ("quit", (705, 485, 875, 538)),   # Confirm, two-button dialog (right)
    "confirm_c":      ("net2", (555, 485, 725, 538)),   # Confirm, single-button dialog (center)
    "item_found":     ("item", (505, 213, 778, 258)),
    "title_login":    ("title", (548, 536, 732, 568)),  # "LOG IN" bar, static on the title screen
}


def main() -> None:
    out_dir = Path(__file__).parent / "assets"
    out_dir.mkdir(exist_ok=True)
    images = {key: Image.open(path).convert("RGB") for key, path in SOURCES.items()}
    for name, (src, box) in CROPS.items():
        crop = images[src].crop(box)
        crop.save(out_dir / f"{name}.png")
        print(f"{name}.png  <- {src} {box}  ({crop.width}x{crop.height})")


if __name__ == "__main__":
    main()
