"""Finding stages whose first-clear reward is still unclaimed.

The stage map labels (``1-1``, ``2-1``, ``BONUS``) are small white text with an
outline, and OCR reads them unreliably - often missing them entirely.  The
crystal icon in the bottom-left of an unclaimed node, however, is a solid cyan
gem that is trivially separable by colour: on the real screen it measures ~350
cyan pixels where a claimed node measures 0.

So detection keys on the icon, not the text.  Nothing here needs OCR, which also
makes it immune to the game being in any language.

The caller gets click points in *node* space: the icon sits at the bottom-left of
the node's card, and the game opens the stage when the card itself is tapped, so
:func:`node_center_of` converts an icon position into a tap position.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["FirstClearMarker", "find_first_clear_markers", "node_center_of",
           "looks_like_map", "CRYSTAL_OFFSET", "SCAN_REGION"]

# How to turn an icon position into a tap point on the node card.
#
# Measured on two real nodes with different card heights:
#   node 2-1 (tall card, S badge): icon (453, 520), card centre (505, 455)
#   node 2-2 (fresh node):         icon (724, 290), card centre (769, 263)
# The icon is at the card's bottom-left in both cases, but the vertical gap
# differs because 2-1's card is taller.  Tapping anywhere inside the card opens
# the stage, so a small fixed nudge up-and-right is both sufficient and safe -
# a bigger offset overshoots above a shorter card and the tap lands on nothing
# (which is exactly what happened with an earlier (55, 61) offset).
CRYSTAL_OFFSET = (45, 20)

# Default search band.  Cyan pixel distribution measured on real map frames:
#   y   0-199  ~15k px  - top nav bar icons (false positives)
#   y 200-279      56 px - art noise, below MIN_CYAN_PIXELS
#   y 280-319     360 px - a crystal icon on a high node (2-2)
#   y 500-549     350 px - a crystal icon on a low node (2-1)
#   y 600-719  ~14k px  - bottom buttons (false positives)
# This band keeps both node rows and drops all the UI chrome.  It must not be
# narrowed to the lower row: stages appear sequentially (2-2 only shows after
# 2-1 is cleared) and land at different heights.
SCAN_REGION = (150, 200, 1130, 400)

# A real icon contributes hundreds of bright-cyan pixels; scattered UI art and
# ocean-background highlights contribute fewer per 40x40 window.  Measured on
# the new "海妖秘境" map: real crystal 195 px/window, background noise 124~130
# px/window.  150 cleanly separates them.
MIN_CYAN_PIXELS = 150
# Cluster radius: cyan pixels belonging to one icon are contiguous, so a coarse
# grid is enough to group them without pulling in a neighbouring node.
CLUSTER_RADIUS = 60


@dataclass
class FirstClearMarker:
    """One unclaimed-first-clear node, in reference-frame coordinates."""

    x: int                      # icon centroid x
    y: int                      # icon centroid y
    pixels: int                 # cyan pixels in the cluster

    @property
    def node_point(self) -> tuple[int, int]:
        """Where to tap to open the stage."""
        return node_center_of(self)

    def describe(self) -> str:
        return (f"icon at ({self.x}, {self.y}) [{self.pixels} px] "
                f"-> tap node at {self.node_point}")


def _cyan_mask(frame: np.ndarray) -> np.ndarray:
    s = frame.astype(np.int16)
    r, g, b = s[:, :, 0], s[:, :, 1], s[:, :, 2]
    # 首通水晶图标 = 亮青蓝宝石。阈值针对**海洋主题活动**重新收紧:
    # 旧阈值 (b>170 & g>150) 在「海妖秘境」这类海洋背景上会误判 —— 海洋本身
    # 是大片蓝青色,检出 6 个假水晶。真水晶的绿色分量集中在 185~225、蓝 >215,
    # 且是紧凑团块;海洋深蓝 g 更低,顶部/底部 UI 亮蓝不在扫描带内。
    return (g > 185) & (g < 225) & (b > 215)


def _cluster_centers(mask: np.ndarray, cluster_radius: int = 20) -> list[tuple[int, int, int]]:
    """把掩码聚成团,返回 [(像素数, 质心x, 质心y)],按像素数降序。

    半径用 20(而不是旧的 60):海洋背景的连续蓝色在 60px 半径下会聚成一个
    上千像素的大块,把真正的紧凑水晶淹掉。20px 只把**相邻**像素聚起来,
    海洋背景的分散点各自成团(像素数很少,后续按 MIN_CYAN_PIXELS 过滤)。
    """
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return []
    order = np.argsort(ys)
    clusters: list[list[tuple[int, int]]] = []
    for i in order:
        px, py = int(xs[i]), int(ys[i])
        placed = False
        for cluster in clusters:
            cx = sum(p[0] for p in cluster) / len(cluster)
            cy = sum(p[1] for p in cluster) / len(cluster)
            if abs(px - cx) <= cluster_radius and abs(py - cy) <= cluster_radius:
                cluster.append((px, py))
                placed = True
                break
        if not placed:
            clusters.append([(px, py)])
    out = []
    for cluster in clusters:
        n = len(cluster)
        cx = int(sum(p[0] for p in cluster) / n)
        cy = int(sum(p[1] for p in cluster) / n)
        out.append((n, cx, cy))
    out.sort(key=lambda t: -t[0])
    return out


def looks_like_map(frame, engine=None) -> bool:
    """Whether ``frame`` is the stage map rather than some other screen.

    Needed because the crystal detector is purely colour-based and other screens
    contain cyan art too: the battle-settlement screen's reward icons produced
    fifteen false markers in testing.  The map is identified by its page
    indicator ("1/2") or a BONUS node label, both absent from other screens.
    """
    if engine is None:
        from .ocr import OcrEngine
        engine = OcrEngine.shared()
    try:
        for hit in engine.recognize(frame, (520, 630, 260, 80)):
            t = hit.text.replace(" ", "")
            if hit.score >= 0.6 and "/" in t and any(c.isdigit() for c in t):
                return True
        for hit in engine.recognize(frame, (150, 180, 400, 140)):
            if hit.score >= 0.8 and "BONUS" in hit.text.upper():
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def find_first_clear_markers(frame: np.ndarray,
                             region: tuple[int, int, int, int] | None = None,
                             min_pixels: int = MIN_CYAN_PIXELS,
                             require_map: bool = False,
                             engine=None) -> list[FirstClearMarker]:
    """All unclaimed-first-clear nodes visible in ``frame``.

    ``region`` defaults to :data:`SCAN_REGION`, which excludes the nav bar and
    the bottom buttons - both of which contain bright blue art that would
    otherwise be reported as icons.

    ``require_map`` guards against running on the wrong screen: pass ``True``
    when the frame comes straight from the device rather than from a known map
    page.  Without it, the settlement screen yields many false markers.
    """
    if require_map and not looks_like_map(frame, engine):
        return []
    if region is None:
        region = SCAN_REGION
    if region:
        rx, ry, rw, rh = (int(v) for v in region)
        # Clip to the frame so callers can pass a generous band.
        rx = max(0, rx)
        ry = max(0, ry)
        rw = min(rw, frame.shape[1] - rx)
        rh = min(rh, frame.shape[0] - ry)
        sub = frame[ry:ry + rh, rx:rx + rw]
    else:
        rx = ry = 0
        sub = frame
    if sub.size == 0:
        return []
    mask = _cyan_mask(sub)

    # 滑动窗口密度检测:真水晶是**紧凑的亮青块**,海洋背景的假阳性是零散
    # 亮点。40x40 窗口内亮青像素密度高(>=150)才认定是水晶;零散亮点凑不满。
    # 之后做非极大值抑制,相邻窗口合并成一个。
    h, w = mask.shape
    win = 40
    step = 10
    hits: list[tuple[int, int, int]] = []   # (密度, cx, cy)
    for y in range(0, max(1, h - win + 1), step):
        for x in range(0, max(1, w - win + 1), step):
            cnt = int(mask[y:y + win, x:x + win].sum())
            if cnt >= min_pixels:
                hits.append((cnt, x + win // 2, y + win // 2))
    if not hits:
        return []
    hits.sort(key=lambda t: -t[0])

    markers = []
    used: list[tuple[int, int]] = []
    for cnt, cx, cy in hits:
        # 非极大值抑制:和已接受的窗口中心距离 < win 就跳过
        if any(abs(cx - ux) < win and abs(cy - uy) < win for ux, uy in used):
            continue
        used.append((cx, cy))
        markers.append(FirstClearMarker(x=cx + rx, y=cy + ry, pixels=cnt))
    markers.sort(key=lambda m: (m.y, m.x))
    return markers


def node_center_of(marker: FirstClearMarker) -> tuple[int, int]:
    """Tap point for the stage whose crystal icon is at ``marker``.

    The icon sits at the card's bottom-left, so the card interior is down-right
    of it in screen terms... except the "down" part is unreliable because a
    cleared card is taller than a fresh one.  Moving right and a *small* amount
    up lands inside the card in both cases (see CRYSTAL_OFFSET).
    """
    dx, dy = CRYSTAL_OFFSET
    return (marker.x + dx, marker.y - dy)
