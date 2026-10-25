"""共鸣屋 (resonance exchange) screen reader and refresh loop.

The resonance shop shows six fixed slots in a 2x3 grid.  Each slot has an item
name and either an ``已拥有`` (already owned) badge or not - which is the signal
that matters, because the whole point of refreshing is hunting for something you
do *not* have yet.

Rather than cropping each slot (fragile: content shifts, and a wrong ROI silently
returns nonsense), the whole panel is OCR'd once and the strings are grouped into
slots by geometry.  That was verified against the real screen: names land around
y = 245 / 399 / 551 and the price/owned row sits ~85px below, so a slot is
"everything within its cell".
"""

from __future__ import annotations
from ._root import project_root

import os
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ResonanceSlot", "read_slots", "find_slot", "read_refresh_state",
           "watchlist_hits", "load_watchlist", "watchlist_path", "read_currency"]

# Panel geometry in reference (1280x720) coordinates, measured on the real screen.
PANEL_ROI = (230, 200, 900, 470)
COLUMN_X = (240, 690)          # left / right column left edge
COLUMN_W = 430
ROW_Y = (215, 370, 525)        # top of each row
ROW_H = 150

OWNED_TEXT = "已拥有"

# Default watchlist file: project_root/watchlist.txt, editable in Notepad.
WATCHLIST_FILENAME = "watchlist.txt"


def watchlist_path(path: str | None = None) -> str:
    """Absolute path of the watchlist text file."""
    if path:
        return path if os.path.isabs(path) else os.path.abspath(path)
    root = project_root()
    return os.path.join(root, WATCHLIST_FILENAME)


def load_equipment_settings(path: str | None = None) -> dict:
    """Read the editable equipment-loadout settings (equipment.json).

    Kept in its own JSON file rather than in the config so the loadout can be
    changed without touching any automation logic - the group and suit names are
    the player's own naming, and they change whenever a new set is saved.
    """
    import json
    if path:
        resolved = path if os.path.isabs(path) else os.path.abspath(path)
    else:
        root = project_root()
        resolved = os.path.join(root, "equipment.json")
    if not os.path.exists(resolved):
        return {}
    # utf-8-sig: Notepad adds a BOM and json.loads rejects it outright.
    with open(resolved, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    # Drop the "_comment" style documentation keys so callers can splat it.
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_watchlist(path: str | None = None) -> list[str]:
    """Read the watchlist text file into a list of names.

    Format (deliberately forgiving, because this file is meant to be edited in
    Notepad between runs):

    * one name per line;
    * blank lines ignored;
    * ``#`` at the start of a line comments it out - so a name can be parked
      without deleting it;
    * inline comments after a name are stripped;
    * a leading ``-`` or ``!`` also disables a line, as does a trailing ``off``
      / ``禁用`` in any case, because people reach for those too.

    A missing file is not an error: it just means "no targets", and the caller
    decides whether that is worth warning about.
    """
    resolved = watchlist_path(path)
    if not os.path.exists(resolved):
        return []
    names: list[str] = []
    seen: set[str] = set()
    with open(resolved, "r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line[0] in "-!":
                continue
            # Strip inline comments ("名구 # note" / "名字 // note").
            for marker in ("#", "//"):
                idx = line.find(marker)
                if idx > 0:
                    line = line[:idx].strip()
            # Trailing toggle words.
            low = line.lower()
            for tail in (" off", " no", " 禁用", " 停用"):
                if low.endswith(tail):
                    line = line[: -len(tail)].strip()
                    break
            if not line or line in seen:
                continue
            seen.add(line)
            names.append(line)
    return names


def read_currency(frame, roi=(330, 120, 230, 60), engine=None) -> int | None:
    """The resonance currency (共鸣币) shown at the top of the shop."""
    if engine is None:
        from .ocr import OcrEngine
        engine = OcrEngine.shared()
    best: int | None = None
    # engine.recognize() yields TextHit objects; the (text, score, box) tuple
    # form is matcher.ocr_strings().
    for hit in engine.recognize(frame, roi):
        if hit.score < 0.6:
            continue
        digits = "".join(ch for ch in hit.text if ch.isdigit())
        if digits:
            value = int(digits)
            # Take the largest number in the region: the wallet is far bigger
            # than any stray digit OCR might pick up beside it.
            best = value if best is None else max(best, value)
    return best


@dataclass
class ResonanceSlot:
    """One of the six exchange slots."""

    index: int                      # 0..5, row-major
    name: str = ""
    owned: bool = False
    price: str = ""
    name_box: tuple[int, int, int, int] | None = None
    exchange_point: tuple[int, int] | None = None
    raw: list[str] = field(default_factory=list)

    @property
    def row(self) -> int:
        return self.index // 2

    @property
    def col(self) -> int:
        return self.index % 2

    def describe(self) -> str:
        state = "已拥有" if self.owned else "未拥有"
        return f"[{self.index}] {self.name or '?'}  {state}  {self.price}"


def _slot_of(box: tuple[int, int, int, int], panel_origin: tuple[int, int]) -> int | None:
    """Which slot a box falls into, or None if outside the grid."""
    x, y = box[0] - panel_origin[0], box[1] - panel_origin[1]
    col = 0 if x < (COLUMN_X[1] - PANEL_ROI[0]) else 1
    row = None
    for i, ry in enumerate(ROW_Y):
        top = ry - PANEL_ROI[1]
        if top - 20 <= y < top + ROW_H:
            row = i
            break
    if row is None:
        return None
    return row * 2 + col


def read_slots(frame, engine=None) -> list[ResonanceSlot]:
    """Read all six slots from a resonance-house frame."""
    if engine is None:
        from .ocr import OcrEngine
        engine = OcrEngine.shared()
    hits = engine.recognize(frame, PANEL_ROI)
    slots = [ResonanceSlot(index=i) for i in range(6)]
    for h in hits:
        if h.score < 0.6:
            continue
        idx = _slot_of(h.box, (PANEL_ROI[0], PANEL_ROI[1]))
        if idx is None:
            continue
        slot = slots[idx]
        slot.raw.append(h.text)
        text = h.text.strip()
        if OWNED_TEXT in text:
            slot.owned = True
        elif text.startswith("兑换"):
            # The exchange button marks the slot's clickable target.
            slot.exchange_point = (h.box[0] + h.box[2] // 2, h.box[1] + h.box[3] // 2)
        elif text and any(ch.isdigit() for ch in text) and (
                text[0] in "xX×" or text.isdigit()):
            slot.price = text
        elif len(text) >= 2 and not slot.name:
            slot.name = text
    for slot in slots:
        if slot.exchange_point is None and slot.name_box:
            slot.exchange_point = (slot.name_box[0], slot.name_box[1])
    return slots


def find_slot(slots: list[ResonanceSlot], wanted: list[str],
              *, require_unowned: bool = True) -> ResonanceSlot | None:
    """First slot matching any wanted name (substring, longest match wins)."""
    best: tuple[int, float, ResonanceSlot] | None = None
    for slot in slots:
        if not slot.name:
            continue
        if require_unowned and slot.owned:
            continue
        for needle in wanted:
            needle = str(needle).strip()
            if needle and needle in slot.name:
                rank = (len(needle), 0.0, slot)
                if best is None or rank[0] > best[0]:
                    best = rank
                break
    return best[2] if best else None


def watchlist_hits(slots: list[ResonanceSlot], wanted: list[str]) -> list[ResonanceSlot]:
    """Every slot whose name matches the watchlist, owned or not.

    Used for reporting: telling the user "your target is here but you already own
    it" is very different from "your target is not here".
    """
    hits = []
    for slot in slots:
        if not slot.name:
            continue
        if any(str(n).strip() and str(n).strip() in slot.name for n in wanted):
            hits.append(slot)
    return hits


def read_refresh_state(frame, engine=None) -> dict[str, Any]:
    """Everything the refresh button's header tells us.

    The header is stateful, which matters for verification:

    * while free manual refreshes remain it reads ``剩余次数 N 次``;
    * once they run out it switches to ``消耗1零时之种`` - i.e. further refreshes
      cost a 零时之种 (the item this bot buys in the gift shop).

    Both are reported, plus the auto-refresh countdown, so the caller can verify
    a refresh by *either* signal depending on which is on screen.
    """
    if engine is None:
        from .ocr import OcrEngine
        engine = OcrEngine.shared()
    out: dict[str, Any] = {"remaining": None, "cost": None, "countdown": None,
                           "raw": []}
    for hit in engine.recognize(frame, (760, 110, 340, 90)):
        if hit.score < 0.5:
            continue
        text = hit.text.replace(" ", "")
        out["raw"].append(hit.text)
        if "剩余" in text and "次" in text:
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                out["remaining"] = int(digits)
        elif "消耗" in text:
            out["cost"] = hit.text
        elif "刷新" in text:
            out["countdown"] = hit.text
    return out
