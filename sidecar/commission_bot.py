"""Commission claim bot — autonomous daily-commission collector for Azur Lane EN on Waydroid.

Drives the game purely through adb: screencap -> cv2 template match -> tap.
Single self-contained file; only stdlib + numpy + opencv-python-headless.

Flow (all coordinates for the pinned 1280x720 Waydroid display):
  home -> BATTLE -> attack hub -> COMMISSIONS
  -> scroll the DAILY list, tap every orange "Complete" button
  -> WELL DONE animation (auto-dismisses; skipped by tap after 2 sightings)
  -> "Item Found" popup (tap to continue)
  -> optional URGENT tab visit
  -> back out to home.

Safety rules:
  * Never sends the Android BACK key: on the home screen it opens the
    quit-game dialog. All backward navigation uses the in-game arrow (55,55).
  * Any unrecognized screen aborts the run (full-resolution PNG saved) instead
    of guessing — except for one bounded "grace" back-step right after a
    deliberate URGENT-tab switch.
  * Quit dialog -> Cancel; any other Info dialog (network family) -> Confirm,
    then waits out the game reload before resuming.

Modes:
  default        live run against the emulator
  --dry-run      classify + log actions, never tap
  --selftest     validate templates against the archived screenshots, exit 1 on failure
  --replay DIR   classify a directory of saved screenshots, no adb involved

Usage:
  uv run python commission_bot.py [--config bot.yaml] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import yaml
from daily_config import load_daily_config

log = logging.getLogger("commission_bot")

SIDE = Path(__file__).resolve().parent

DEFAULTS: dict = {
    "serial": "192.0.2.10:5555",
    "adb": None,                # null = auto-detect (env ADB, SDK path, PATH)
    "assets": str(SIDE / "assets"),
    "max_steps": 60,
    "claim_urgent": True,       # visit the URGENT tab after DAILY is exhausted
    "scroll_list": True,        # scroll the commission list once per visit
    "ntfy": None,               # e.g. https://ntfy.sh/my-topic -> POST run summary
    "thresholds": {
        "quit_text": 0.75,
        "info_x": 0.75,
        "confirm_r": 0.75,
        "confirm_c": 0.75,
        "well_done": 0.70,
        "item_found": 0.80,
        "complete_btn": 0.80,
        "daily_icon": 0.80,
        "commissions_btn": 0.80,
        "battle_btn": 0.72,
        "title_login": 0.80,
    },
    "timings": {               # seconds slept after each action class
        "short": 1.5,          # popup taps
        "claim": 2.0,          # Complete tap (reward animation follows)
        "reward": 2.0,         # well_done / item_found observation cadence
        "nav": 2.5,            # screen transitions (BATTLE, COMMISSIONS, back)
        "reload": 90.0,        # max wait for the game to reload after a network Confirm
        "unknown_poll": 4.0,   # re-check interval while the screen is unrecognized (loading)
        "loading": 90.0,       # max total time to wait for an unrecognized screen to resolve
    },
}

# fixed interaction points (dialog geometry is shared by all Info dialogs)
BACK_ARROW = (55, 55)       # in-game back, top-left diamond (commission & hub screens)
CANCEL_POINT = (489, 509)   # Cancel of a two-button Info dialog
URGENT_TAB = (48, 255)      # URGENT tab on the commission screen
CENTER = (640, 360)

# states where a screenshot counts as "game is usable again" after a reload
ACTIONABLE = {"battle_btn", "commissions_btn", "complete_btn", "daily_icon",
              "well_done", "item_found"}


# --------------------------------------------------------------------------- adb
class AdbError(RuntimeError):
    pass


def detect_adb(explicit: str | None) -> str:
    candidates = [
        explicit,
        os.environ.get("ADB"),
        str(Path.home() / "Android/Sdk/platform-tools/adb"),
        shutil.which("adb"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise AdbError("adb binary not found (set adb: in bot.yaml or $ADB)")


class Adb:
    """adb client with automatic reconnect; screencap/tap/swipe only."""

    def __init__(self, serial: str, binary: str):
        self.serial = serial
        self.binary = binary

    def _run(self, *args: str, timeout: float = 30.0) -> bytes:
        cmd = [self.binary, "-s", self.serial, *args]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args[:2])}... rc={proc.returncode}: "
                               f"{proc.stderr.decode(errors='replace')!r}")
        return proc.stdout

    def _reconnect(self) -> None:
        log.warning("adb connection issue, reconnecting")
        for args in (["disconnect"], ["connect", self.serial]):
            try:
                self._run(*args, timeout=10)
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning("adb %s failed: %s", args[0], exc)
            time.sleep(1.0)

    def screencap(self) -> np.ndarray:
        last: Exception | None = None
        for _ in range(3):
            try:
                raw = self._run("exec-out", "screencap", "-p")
                img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    raise RuntimeError(f"screencap returned {len(raw)} bytes, not a PNG")
                return img
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                last = exc
                self._reconnect()
        raise AdbError(f"screencap failed after reconnects: {last}. If the Waydroid "
                       "container is FROZEN, run: waydroid app launch com.YoStarEN.AzurLane")

    def tap(self, x: int, y: int) -> None:
        self._run("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int) -> None:
        self._run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))


# ---------------------------------------------------------------------- detection
def load_templates(assets: str | Path) -> dict[str, np.ndarray]:
    templates: dict[str, np.ndarray] = {}
    for name in DEFAULTS["thresholds"]:
        path = Path(assets) / f"{name}.png"
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"missing template: {path}")
        templates[name] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return templates


def best_match(gray: np.ndarray, tmpl: np.ndarray) -> tuple[float, int, int, int, int]:
    h, w = tmpl.shape[:2]
    res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return score, loc[0], loc[1], w, h


def all_matches(gray: np.ndarray, tmpl: np.ndarray, threshold: float) -> list[tuple[int, int, int, int]]:
    """Distinct matches above threshold, sorted top-down (rows share the same x)."""
    h, w = tmpl.shape[:2]
    res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= threshold)
    picked: list[tuple[int, int]] = []
    for x, y in sorted(zip(xs.tolist(), ys.tolist()), key=lambda p: p[1]):
        if all(abs(x - px) > w // 2 or abs(y - py) > h for px, py in picked):
            picked.append((x, y))
    return [(x, y, w, h) for x, y in picked]


def looks_orange(shot: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """True if most of the matched region is orange — rejects look-alike gray buttons."""
    x, y, w, h = box
    roi = cv2.cvtColor(shot[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    mask = (roi[:, :, 0] >= 5) & (roi[:, :, 0] <= 35) \
        & (roi[:, :, 1] > 100) & (roi[:, :, 2] > 120)
    return float(mask.mean()) > 0.35


def classify(shot: np.ndarray, templates: dict[str, np.ndarray],
             thr: dict[str, float]) -> tuple[str, list[tuple[int, int, int, int]], float]:
    """Map a screenshot to (state, boxes, score).

    States: quit_dialog, network_dialog, well_done, item_found, complete_btn,
    daily_icon (commission list, DAILY tab), commissions_btn (attack hub),
    battle_btn (home), unknown.
    For network_dialog, boxes[0] is the Confirm button to tap.
    """
    gray = cv2.cvtColor(shot, cv2.COLOR_BGR2GRAY)

    score, _, _, _, _ = best_match(gray, templates["quit_text"])
    if score >= thr["quit_text"]:
        return "quit_dialog", [], score

    score, x, y, w, h = best_match(gray, templates["info_x"])
    if score >= thr["info_x"]:
        # every Info dialog needs Confirm; find which layout it is
        for name in ("confirm_r", "confirm_c"):
            s, cx, cy, cw, ch = best_match(gray, templates[name])
            if s >= thr[name]:
                return "network_dialog", [(cx, cy, cw, ch)], score
        return "unknown", [], score  # dialog of unknown type: don't guess buttons

    score, _, _, _, _ = best_match(gray, templates["title_login"])
    if score >= thr["title_login"]:
        return "title_screen", [], score

    score, _, _, _, _ = best_match(gray, templates["well_done"])
    if score >= thr["well_done"]:
        return "well_done", [], score

    score, _, _, _, _ = best_match(gray, templates["item_found"])
    if score >= thr["item_found"]:
        return "item_found", [], score

    score, x, y, w, h = best_match(gray, templates["complete_btn"])
    if score >= thr["complete_btn"]:
        boxes = [b for b in all_matches(gray, templates["complete_btn"], thr["complete_btn"])
                 if looks_orange(shot, b)]
        if boxes:
            return "complete_btn", boxes, score

    score, _, _, _, _ = best_match(gray, templates["daily_icon"])
    if score >= thr["daily_icon"]:
        return "daily_icon", [], score

    score, x, y, w, h = best_match(gray, templates["commissions_btn"])
    if score >= thr["commissions_btn"]:
        return "commissions_btn", [(x, y, w, h)], score

    score, x, y, w, h = best_match(gray, templates["battle_btn"])
    if score >= thr["battle_btn"]:
        return "battle_btn", [(x, y, w, h)], score

    return "unknown", [], score


# --------------------------------------------------------------------------- bot
class CommissionBot:
    def __init__(self, adb: Adb, templates: dict[str, np.ndarray], cfg: dict, dry: bool):
        self.adb = adb
        self.templates = templates
        self.thr = cfg["thresholds"]
        self.t = cfg["timings"]
        self.cfg = cfg
        self.dry = dry
        self.run_dir = SIDE / "runs" / time.strftime("%Y%m%d-%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.reset()

    def reset(self) -> None:
        self.claimed = 0
        self.phase = "in"           # in = navigate/claim, out = heading home
        self.navigated_in = False   # tapped BATTLE at start?
        self.scrolled = False       # list scroll used on this visit
        self.urgent_tried = False
        self.grace_back = False     # one bounded back-step after the URGENT switch
        self.reload_deadline = 0.0  # while set: tolerate loading screens
        self.last_sig: tuple | None = None
        self.same_count = 0
        self.steps = 0
        self.stop_reason: str | None = None
        self.reward_streak = 0
        self.unknown_waited = 0.0   # seconds spent on an unrecognized screen (loading wait)

    # -- primitives ------------------------------------------------------------
    def tap(self, pt: tuple[int, int], what: str) -> None:
        log.info("tap %-28s @ %s", what, pt)
        if not self.dry:
            self.adb.tap(*pt)

    def swipe_scroll(self) -> None:
        log.info("scroll commission list")
        if not self.dry:
            self.adb.swipe(640, 560, 640, 260, 400)

    def wait(self, key: str) -> None:
        time.sleep(self.t[key])

    def shot(self, state: str, image: np.ndarray, full: bool = False) -> None:
        suffix = "_ABORT_full" if full else ""
        path = self.run_dir / f"{self.steps:02d}_{state}{suffix}.png"
        cv2.imwrite(str(path), image)

    # -- main loop ---------------------------------------------------------------
    def run(self) -> dict:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            for self.steps in range(1, self.cfg["max_steps"] + 1):
                shot = self.adb.screencap()
                state, boxes, score = classify(shot, self.templates, self.thr)
                self.shot(state, shot, full=(state == "unknown"))
                if not self._step(state, boxes, score, shot):
                    break
            else:
                self.stop_reason = "max_steps"
        finally:
            result = self.stop_reason or "max_steps"
            summary = {
                "started": started, "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                "result": result, "steps": self.steps, "claimed": self.claimed,
                "run_dir": str(self.run_dir),
            }
            (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
            log.info("summary: %s", summary)
            self._notify(summary)
        return summary

    def _step(self, state, boxes, score, shot) -> bool:
        """Execute one action; return False when the run must stop."""
        # still waiting out a game reload after a network reconnect?
        if self.reload_deadline:
            if state in ACTIONABLE:
                log.info("game responsive again after reload")
                self.reload_deadline = 0.0
            elif state == "network_dialog":
                log.warning("network dialog again during reload — confirming once more")
                self.reload_deadline = time.monotonic() + self.t["reload"]
                self.tap((boxes[0][0] + boxes[0][2] // 2, boxes[0][1] + boxes[0][3] // 2),
                         "network Confirm")
                self.wait("short")
                return True
            elif state == "quit_dialog":
                self.reload_deadline = 0.0  # fall through to the quit handler
            elif time.monotonic() < self.reload_deadline:
                log.info("waiting for game reload... (%.0fs left)",
                         self.reload_deadline - time.monotonic())
                time.sleep(4)
                return True
            else:
                log.error("reload timeout: game did not reach a known screen. It is "
                          "probably on the title screen — tap it once manually or add "
                          "a title-screen template, then re-run.")
                self.stop_reason = "reload_timeout"
                return False

        # stuck guard: identical state+target too many times means taps aren't landing.
        # 'unknown' is exempt: it has its own bounded wait (loading screens).
        if state != "well_done":
            self.reward_streak = 0
        if state != "unknown":
            self.unknown_waited = 0.0
        sig = (state, tuple(boxes[0][:2]) if boxes else None)
        self.same_count = self.same_count + 1 if sig == self.last_sig else 0
        self.last_sig = sig
        if self.same_count >= 5 and state != "unknown":
            log.error("state %s repeated %dx without progress, aborting", state, self.same_count)
            self.stop_reason = "stuck_state"
            return False

        if state == "quit_dialog":
            log.warning("quit dialog detected -> Cancel (conf=%.2f)", score)
            self.tap(CANCEL_POINT, "dialog Cancel")
            self.wait("short")
        elif state == "network_dialog":
            x, y, w, h = boxes[0]
            log.warning("network dialog -> Confirm (conf=%.2f), then waiting out reload", score)
            self.reload_deadline = time.monotonic() + self.t["reload"]
            self.tap((x + w // 2, y + h // 2), "network Confirm")
            self.wait("short")
        elif state == "title_screen":
            # title / PRESS TO START: tap the centre (never the Log Out / Recovery buttons)
            log.info("title screen -> tap to start (conf=%.2f)", score)
            self.tap(CENTER, "title start")
            self.wait("nav")
        elif state == "well_done":
            self.reward_streak += 1
            if self.reward_streak >= 3:
                log.info("reward screen stuck, tap center to skip")
                self.reward_streak = 0
                self.tap(CENTER, "skip reward")
            else:
                log.info("reward animation, observing (%d)", self.reward_streak)
            self.wait("reward")
        elif state == "item_found":
            log.info("item found popup -> continue")
            self.grace_back = False
            self.tap((640, 480), "item continue")
            self.wait("short")
        elif state == "complete_btn":
            x, y, w, h = boxes[0]  # topmost
            self.claimed += 1
            log.info("claiming commission #%d (%d visible, conf=%.2f)",
                     self.claimed, len(boxes), score)
            self.grace_back = False
            self.tap((x + w // 2, y + h // 2), "Complete")
            self.wait("claim")
        elif state == "daily_icon":
            # commission list is open on the DAILY tab, nothing claimable visible
            if self.cfg["scroll_list"] and not self.scrolled:
                self.scrolled = True
                self.swipe_scroll()
                self.wait("short")
            elif self.cfg["claim_urgent"] and not self.urgent_tried:
                self.urgent_tried = True
                self.grace_back = True
                self.tap(URGENT_TAB, "URGENT tab")
                self.wait("nav")
            else:
                log.info("DAILY exhausted -> back out")
                self.phase = "out"
                self.grace_back = False
                self.tap(BACK_ARROW, "back arrow")
                self.wait("nav")
        elif state == "commissions_btn":
            x, y, w, h = boxes[0]
            if self.phase == "in":
                log.info("attack hub -> COMMISSIONS (conf=%.2f)", score)
                self.navigated_in = True
                self.tap((x + w // 2, y + h // 2), "COMMISSIONS")
            else:
                log.info("attack hub on the way home -> back")
                self.tap(BACK_ARROW, "back arrow")
            self.wait("nav")
        elif state == "battle_btn":
            if self.phase == "out" or self.claimed or self.navigated_in:
                log.info("home reached: done, %d commission(s) claimed in %d steps",
                         self.claimed, self.steps)
                self.stop_reason = "success"
                return False
            x, y, w, h = boxes[0]
            log.info("home -> BATTLE (conf=%.2f)", score)
            self.navigated_in = True
            self.tap((x + w // 2, y + h // 2), "BATTLE")
            self.wait("nav")
        else:  # unknown
            if self.grace_back:
                log.warning("unrecognized screen right after URGENT switch -> one back-step")
                self.grace_back = False
                self.tap(BACK_ARROW, "back arrow (grace)")
                self.wait("nav")
            elif self.unknown_waited < self.t["loading"]:
                # most likely a loading/transition screen: observe, never tap a guess
                self.unknown_waited += self.t["unknown_poll"]
                log.info("unrecognized screen (loading?) — waiting %.0f/%.0fs (conf=%.2f)",
                         self.unknown_waited, self.t["loading"], score)
                time.sleep(self.t["unknown_poll"])
            else:
                log.error("unrecognized screen (best conf=%.2f) — aborting; "
                          "full screenshot in %s", score, self.run_dir)
                self.stop_reason = "unknown_screen"
                return False
        return True

    def _notify(self, summary: dict) -> None:
        if not self.cfg.get("ntfy"):
            return
        try:
            req = urllib.request.Request(
                self.cfg["ntfy"],
                data=f"commission_bot: {summary['result']} — {summary['claimed']} claimed "
                     f"in {summary['steps']} steps".encode(),
                headers={"Title": "Azur Lane commission bot"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            log.warning("ntfy notify failed: %s", exc)


# ------------------------------------------------------------------ selftest etc
CASES = [  # (screenshot, expected state) — archived under runs/
    ("runs/explore-20261029/e4.png", "battle_btn"),
    ("runs/explore-20261029/f1.png", "commissions_btn"),
    ("runs/explore-20261029/f2.png", "complete_btn"),
    ("runs/explore-20261029/f3_reward1.png", "well_done"),
    ("runs/20261029-072459/04_ABORT_full.png", "item_found"),
    ("runs/explore-20261029/shot_after_back.png", "quit_dialog"),
    ("runs/20261029-073607/01_cancel_btn.jpg", "network_dialog"),
    ("runs/20261029-073607/02_ABORT_full.png", "network_dialog"),
    ("runs/explore-20261029/e9_commission.png", "unknown"),
    ("runs/explore-20261029/e3_back_home.png", "unknown"),
    ("runs/explore-20261029/shot1.png", "unknown"),
    ("runs/20261029-101724/24_unknown_ABORT_full.png", "title_screen"),
]


def selftest(cfg: dict) -> bool:
    templates = load_templates(cfg["assets"])
    ok = True
    for rel, expected in CASES:
        shot = cv2.imread(str(SIDE / rel))
        if shot is None:
            print(f"SKIP {rel} (missing)")
            continue
        state, boxes, score = classify(shot, templates, cfg["thresholds"])
        good = state == expected
        ok &= good
        print(f"{'OK ' if good else 'FAIL'} {Path(rel).name:26s} {expected:16s} -> {state:16s} "
              f"conf={score:.3f} boxes={len(boxes)}")
    return ok


def replay(directory: str, cfg: dict) -> None:
    templates = load_templates(cfg["assets"])
    paths = sorted(p for p in Path(directory).iterdir()
                   if p.suffix.lower() in (".png", ".jpg"))
    for p in paths:
        shot = cv2.imread(str(p))
        if shot is None:
            continue
        state, boxes, score = classify(shot, templates, cfg["thresholds"])
        print(f"{p.name:32s} -> {state:16s} conf={score:.3f} boxes={len(boxes)}")


def serial_from_daily(path: Path) -> str | None:
    """Serial from daily.yaml, or None when the file is absent or unusable."""
    if not Path(path).exists():
        return None
    try:
        return load_daily_config(Path(path)).device.serial
    except Exception as e:
        log.warning("daily.yaml unusable (%s); ignoring it for serial", e)
        return None


def load_config(path: str | None, daily_path: Path = SIDE / "daily.yaml") -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    user: dict = {}
    if path:
        user = yaml.safe_load(Path(path).read_text()) or {}
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    if "serial" not in user:
        cfg["serial"] = serial_from_daily(daily_path) or cfg["serial"]
    return cfg


def setup_logging(run_dir: Path | None) -> None:
    handlers = [logging.StreamHandler()]
    if run_dir:
        handlers.append(logging.FileHandler(run_dir / "run.log"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=handlers)


def capture(minutes: int, cfg: dict) -> int:
    """Explore helper: screenshot the device every ~2s into runs/explore-<date>/.

    Run it in the background while playing the game normally (e.g. canceling
    one commission by hand) and the frames become template material for
    crop_templates.py — see the P1 spec, section 4.
    """
    binary = detect_adb(cfg["adb"])
    adb = Adb(cfg["serial"], binary)
    run_dir = SIDE / "runs" / ("explore-" + time.strftime("%Y%m%d"))
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(run_dir)
    log.info("capture: every 2s for %d min -> %s (Ctrl+C to stop)", minutes, run_dir)
    deadline = time.time() + minutes * 60
    n = 0
    try:
        while time.time() < deadline:
            try:
                shot = adb.screencap()
            except Exception as exc:  # noqa: BLE001 - keep capturing through hiccups
                log.warning("screencap failed: %s", exc)
                time.sleep(2)
                continue
            n += 1
            cv2.imwrite(str(run_dir / f"{n:03d}.png"), shot)
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("capture stopped by user")
    log.info("captured %d frames -> %s", n, run_dir)
    print(f"captured {n} frames -> {run_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="YAML config (default: bot.yaml if present)")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="classify and log, never tap")
    ap.add_argument("--selftest", action="store_true", help="validate templates, exit 1 on failure")
    ap.add_argument("--replay", metavar="DIR", help="classify saved screenshots, no device")
    ap.add_argument("--capture", metavar="MINUTES", type=int,
                    help="explore mode: screenshot every 2s for MINUTES minutes, no bot logic")
    args = ap.parse_args()

    config_path = args.config or (str(SIDE / "bot.yaml") if (SIDE / "bot.yaml").exists() else None)
    cfg = load_config(config_path)
    if args.serial:
        cfg["serial"] = args.serial
    if args.max_steps:
        cfg["max_steps"] = args.max_steps

    if args.selftest:
        return 0 if selftest(cfg) else 1
    if args.replay:
        replay(args.replay, cfg)
        return 0
    if args.capture is not None:
        return capture(args.capture, cfg)

    binary = detect_adb(cfg["adb"])
    adb = Adb(cfg["serial"], binary)
    templates = load_templates(cfg["assets"])
    bot = CommissionBot(adb, templates, cfg, dry=args.dry_run)
    setup_logging(bot.run_dir)
    log.info("run dir: %s | serial: %s | dry-run: %s", bot.run_dir, adb.serial, bot.dry)
    summary = bot.run()
    return 0 if summary["result"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
