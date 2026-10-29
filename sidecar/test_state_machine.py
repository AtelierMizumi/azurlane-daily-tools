"""State-machine regression test for commission_bot — no device needed.

Feeds a realistic classify() state sequence into CommissionBot._step with a
recording fake adb and asserts the resulting taps: navigation, claiming,
URGENT grace step, the phase="out" return (must NOT re-enter the commission
screen), and a clean success.

Run:  uv run python test_state_machine.py
"""
import logging
import tempfile
from pathlib import Path

import numpy as np

import commission_bot as cb

logging.disable(logging.CRITICAL)


class FakeAdb:
    """Records every adb command instead of sending it."""

    def __init__(self):
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple] = []

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, *a):
        self.swipes.append(a)

    def screencap(self):
        return np.zeros((720, 1280, 3), np.uint8)


B = [(1130, 445, 120, 120)]    # battle_btn box (home)
H = [(812, 585, 136, 55)]      # commissions_btn box (attack hub)
C2 = [(365, 155, 87, 28), (365, 300, 87, 28)]  # two Complete buttons
C1 = [(365, 155, 87, 28)]      # one left
BLACK = np.zeros((720, 1280, 3), np.uint8)


def main() -> None:
    cb.SIDE = Path(tempfile.mkdtemp())  # run dirs go to a temp dir, not runs/
    cfg = cb.load_config(None)
    templates = cb.load_templates(cfg["assets"])
    adb = FakeAdb()
    bot = cb.CommissionBot(adb, templates, cfg, dry=False)  # dry=False only so FakeAdb records

    sequence = [
        ("battle_btn", B),        # start at home -> tap BATTLE
        ("commissions_btn", H),   # attack hub -> tap COMMISSIONS
        ("complete_btn", C2),     # claim #1 (topmost)
        ("well_done", []),        # reward animation
        ("item_found", []),       # bonus popup -> continue
        ("complete_btn", C1),     # claim #2
        ("daily_icon", []),       # nothing visible -> scroll
        ("daily_icon", []),       # still nothing -> URGENT tab, grace armed
        ("unknown", []),          # URGENT list unrecognized -> grace back
        ("daily_icon", []),       # DAILY exhausted -> back out, phase="out"
        ("commissions_btn", H),   # hub on the way home -> back (must NOT re-enter)
        ("battle_btn", B),        # home -> success
    ]
    for state, boxes in sequence:
        if not bot._step(state, boxes, 0.99, BLACK):
            break

    taps = adb.taps
    assert bot.stop_reason == "success", f"stop_reason={bot.stop_reason}"
    assert bot.claimed == 2, f"claimed={bot.claimed}"
    assert taps[0] == (1190, 505) and taps[1] == (880, 612), taps
    assert taps[2] == (408, 169) and taps[3] == (640, 480), taps
    assert len([t for t in taps if t == (880, 612)]) == 1, f"COMMISSIONS re-entered: {taps}"
    assert len([t for t in taps if t == (55, 55)]) == 3, f"back arrows: {taps}"
    assert len(adb.swipes) == 1, f"scroll count: {len(adb.swipes)}"
    print("taps:", taps)
    network_dialog_case(templates, cfg)
    unknown_loading_case(templates, cfg)
    title_screen_case(templates, cfg)
    print("STATE MACHINE TEST PASS")


def title_screen_case(templates, cfg) -> None:
    """Title screen ('LOG IN' / 'PRESS TO START'): tap to start, never a Log Out/Recovery button."""
    adb = FakeAdb()
    bot = cb.CommissionBot(adb, templates, cfg, dry=False)
    assert bot._step("title_screen", [(548, 536, 184, 32)], 0.9, BLACK) is True
    assert adb.taps == [cb.CENTER], f"title tap: {adb.taps}"
    print("title screen taps:", adb.taps)


def unknown_loading_case(templates, cfg) -> None:
    """A loading screen is 'unknown' for a while: wait it out, but abort if it never resolves."""
    fast = cb.load_config(None)
    fast["timings"].update(unknown_poll=0.01, loading=0.025)  # 3rd unknown exceeds 0.025s
    adb = FakeAdb()
    bot = cb.CommissionBot(adb, templates, fast, dry=False)
    assert bot._step("unknown", [], 0.6, BLACK) is True, "loading screen aborted the run"
    assert bot._step("unknown", [], 0.6, BLACK) is True, "loading screen aborted the run"
    assert bot._step("battle_btn", B, 0.99, BLACK) is True   # game came up -> BATTLE
    assert adb.taps == [(1190, 505)], f"taps while loading: {adb.taps}"

    adb = FakeAdb()
    bot = cb.CommissionBot(adb, templates, fast, dry=False)
    results = [bot._step("unknown", [], 0.6, BLACK) for _ in range(5)]
    assert False in results, "persistent unknown screen never aborted"
    assert bot.stop_reason == "unknown_screen", bot.stop_reason
    print("loading wait: ok, persistent unknown aborts with", bot.stop_reason)


def network_dialog_case(templates, cfg) -> None:
    """Network drop -> tap Confirm once and arm the reload wait (must not raise)."""
    adb = FakeAdb()
    bot = cb.CommissionBot(adb, templates, cfg, dry=False)
    box = [(100, 100, 50, 30)]    # confirm button box
    assert bot._step("network_dialog", box, 0.99, BLACK) is True
    assert adb.taps == [(125, 115)], f"network Confirm tap: {adb.taps}"
    assert bot.reload_deadline > 0, "reload wait not armed"
    print("network dialog taps:", adb.taps)


if __name__ == "__main__":
    main()
