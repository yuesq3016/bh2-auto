"""首通清理:sweep 所有「首通奖励未领取」的关卡,直到全部通关。

流程(每一步都实测验证过):
  1. 回地图,翻页扫描 -> `stagemap.find_first_clear_markers` 用颜色找水晶图标
  2. 点该节点 -> 进入关卡详情页(靠「消耗体力」文字确认)
  3. 开装备页 -> `equip_select` 切到配置的套装,返回
  4. 点「选择助战好友」-> 用文字锚点找卡片 -> **随机选一个**
  5. 点「开战」-> 等结算(出现「确定」或「战斗结算」)
  6. 关掉结算和可能弹出的加好友框 -> 回到 1

设计要点:
  * 关卡是**依次出现**的(打完 2-1 才出 2-2),所以每轮都要重新扫描,不能一次性列清单
  * 水晶图标用**颜色聚类**识别,不依赖 OCR 认关卡名(小号描边字 OCR 读不出来)
  * 助战卡片用文字锚点(名字/友情点),因为卡片底色检测不可靠
"""
from __future__ import annotations

import random
import time

from .ocr import OcrEngine
from .resonance import load_equipment_settings
from .stagemap import find_first_clear_markers

# 点击目标(全部实测取得)
PAGE_PREV = (551, 665)
PAGE_NEXT = (737, 665)
BACK_ARROW = (57, 140)
EQUIP_ICON = (1146, 202)
SUPPORT_BUTTON_Y = 650
FIGHT_BUTTON = (826, 664)
SUPPORT_LIST_ROI = (370, 200, 900, 470)


def _texts(engine, frame, roi=None):
    hits = engine.recognize(frame, roi)
    return [(h.text, h.score, h.loc) for h in hits]


def _find(hits, needle, min_conf=0.85):
    best = None
    for text, score, loc in hits:
        if score >= min_conf and needle in text:
            if best is None or len(text) > len(best[0]):
                best = (text, score, loc)
    return best


# 「我的消息」界面里禁止点击的按钮。
# 用户明确要求:只许点「一键领取」,不许点「体力领取」。
FORBIDDEN_BUTTONS = ("体力领取",)


def _find_button(hits, labels, min_conf=0.9, exact_first=True):
    """找一个**按钮**:标签文字必须等于某候选(而不是包含)。

    为什么需要这个:_find 偏好最长匹配,用来找标签很合适,但找按钮会出错 ——
    实测「兑换」按钮旁边有正文「主人可兑换89双倍体力,补充178体力」,两者都
    含「兑换」,而正文更长,于是 _find 返回了正文,点击落在文字上毫无效果。

    按钮的判据是**整段文字就是按钮名**,所以这里默认要求精确相等;找不到才
    退回子串匹配(应对 OCR 漏字,比如「一键领取」被读成「键领取」)。
    """
    if isinstance(labels, str):
        labels = (labels,)
    hits = [(t, s, l) for t, s, l in hits if s >= min_conf]
    if exact_first:
        for lbl in labels:
            for text, score, loc in hits:
                if text.strip() == lbl:
                    return (text, score, loc)
    # 回退:子串,但取**最短**的匹配(按钮名通常比正文短)
    best = None
    for lbl in labels:
        for text, score, loc in hits:
            if lbl in text:
                if best is None or len(text) < len(best[0]):
                    best = (text, score, loc)
    return best


def _find_safe(hits, needle, min_conf=0.85, forbidden=FORBIDDEN_BUTTONS):
    """和 _find 一样,但会排除掉禁止按钮。

    必要性:OCR 有噪声,而且「一键领取」和「体力领取」在屏幕上相邻。任何
    模糊匹配都可能误命中禁止按钮 —— 实测「体力领取」在 (941,145),只差
    200 多像素。所以命中后必须再核对一次文字里没有禁止词。
    """
    best = None
    for text, score, loc in hits:
        if score < min_conf or needle not in text:
            continue
        if any(bad in text for bad in forbidden):
            continue                     # 命中禁止按钮,直接丢弃
        if best is None or len(text) > len(best[0]):
            best = (text, score, loc)
    return best


def _click(inp, pt, what, log):
    log(f"    点击 {what} ({pt[0]}, {pt[1]})")
    res = inp.click(int(pt[0]), int(pt[1]))
    if not res.ok:
        raise RuntimeError(f"input failed: {res.detail}")
    return res


def _dismiss(inp, engine, log, settle=2.0, rounds=4, also=("返回",)):
    """关掉结算/加好友等弹窗。"""
    for _ in range(rounds):
        time.sleep(settle)
        frame = inp.grab()
        hits = _texts(engine, frame)
        # 加好友邀请优先点「返回」(不加好友)
        for lbl in also:
            hit = _find(hits, lbl, 0.9)
            if hit:
                _click(inp, hit[2], f"'{lbl}'(关闭弹窗)", log)
                break
        else:
            for lbl in ("确定", "确认"):
                hit = _find(hits, lbl, 0.9)
                if hit:
                    _click(inp, hit[2], f"'{lbl}'(关闭结算)", log)
                    break
            else:
                return 0
        continue
    return 0


def _on_map(engine, frame) -> bool:
    """判断是否在关卡地图(有分页指示或 BONUS 标签)。"""
    hits = _texts(engine, frame, (150, 180, 1130, 100))
    if any("BONUS" in t for t, s, _ in hits if s >= 0.8):
        return True
    page = _texts(engine, frame, (560, 645, 150, 50))
    return any("/" in t and s >= 0.6 for t, s, _ in page)


def _goto_map(inp, engine, log, tries=8) -> bool:
    """回到关卡地图。

    这里必须自己处理弹窗:结算和加好友框会盖住界面,此时点左上返回箭头
    没有任何效果(实测连续点 4 次仍在结算页)。所以每轮先尝试关掉
    「确定 / 返回」这类弹窗按钮,没有再点返回箭头。
    """
    for i in range(tries):
        frame = inp.grab()
        if _on_map(engine, frame):
            return True
        hits = _texts(engine, frame)
        # 弹窗按钮优先。加好友框用「返回」,结算用「确定」。
        handled = False
        for lbl in ("返回", "确定", "确认"):
            hit = _find(hits, lbl, 0.9)
            if hit:
                _click(inp, hit[2], f"'{lbl}'(关弹窗)", log)
                handled = True
                break
        if not handled:
            _click(inp, BACK_ARROW, "返回箭头(回地图)", log)
        time.sleep(2.5)
    return _on_map(engine, inp.grab())


def _is_bonus_marker(frame, engine, marker) -> bool:
    """水晶图标是否属于 BONUS 节点(BONUS 节点留给清体力阶段,不算首通)。

    实测:关卡地图上 BONUS 节点**也有水晶图标**(BONUS 首通未领时),它的
    「BONUS」文字标签在水晶上方约 118px 处(文字 235,262 vs 水晶 180,380)。
    普通首通(如「2-1」)没有「BONUS」标签。所以判据:水晶上方 100~180px 内、
    x 相差 80px 内,出现「BONUS」文字。
    """
    hits = _texts(engine, frame)
    for t, s, loc in hits:
        if s < 0.7 or "BONUS" not in t.upper():
            continue
        bx, by = loc
        if abs(bx - marker.x) <= 80 and 100 <= (marker.y - by) <= 180:
            return True
    return False


def _scan_map(inp, engine, log):
    """翻遍两页找首通节点(排除 BONUS)。返回 (node_point 或 None, 当前帧)。"""
    for flip in range(2):
        frame = inp.grab()
        ms = find_first_clear_markers(frame, require_map=True, engine=engine)
        for m in ms:
            if _is_bonus_marker(frame, engine, m):
                log(f"    跳过 BONUS 节点 {m.node_point}(留给清体力阶段)")
                continue
            return m.node_point, frame
        _click(inp, PAGE_NEXT if flip == 0 else PAGE_PREV, "翻页", log)
        time.sleep(2.5)
    return None, inp.grab()


def _enter_stage(inp, engine, log, pt) -> bool:
    _click(inp, pt, "首通关卡节点", log)
    time.sleep(3.5)
    frame = inp.grab()
    hits = _texts(engine, frame)
    return _find(hits, "消耗体力", 0.8) is not None


def _is_support_list(engine, frame) -> bool:
    """判断当前是否是「助战好友列表」界面。

    必须区分,否则会把关卡详情页的文字(「可能获得」「使用双倍券」)当成
    助战卡片名 —— 实测因此点错位置,还误把双倍券勾选框给取消了。

    助战列表的特征:同一屏出现 **两个及以上**「友情点」标签(每张卡片右下
    各一个),且能看到等级标签「LV.」。
    """
    hits = _texts(engine, frame, SUPPORT_LIST_ROI)
    pts = sum(1 for t, s, _ in hits if s >= 0.8 and "友情点" in t)
    lv = sum(1 for t, s, _ in hits if s >= 0.8 and "LV" in t.upper())
    return pts >= 2 and lv >= 1


def _select_support(inp, engine, log, rng: random.Random, is_double: bool = False):
    """选一个助战好友。

    两种入口都要支持(实测两种情况都会遇到):
      * 关卡详情页 -> 先点「选择助战好友」才会出现列表
      * 「再次挑战」续关 -> **直接就是助战列表**,没有那个按钮

    所以先看列表在不在;不在才去点按钮。
    """
    frame = inp.grab()
    if not _is_support_list(engine, frame):
        hits = _texts(engine, frame)
        btn = _find(hits, "选择助战好友", 0.85)
        if btn:
            _click(inp, btn[2], "选择助战好友", log)
            time.sleep(4.0)
            frame = inp.grab()
        else:
            log("    !! 既没有助战列表也没有「选择助战好友」按钮")
            return False

    if not _is_support_list(engine, frame):
        log("    !! 点了按钮但助战列表没出现")
        return False

    cards = _support_cards(engine, frame)
    if not cards:
        log("    !! 助战列表里没识别到卡片名字")
        return False
    pick = rng.choice(cards)
    log(f"    助战候选 {len(cards)} 个: {[c[2] for c in cards]}")
    _click(inp, (pick[0] + 20, pick[1] + 45), f"助战卡片 {pick[2]!r}", log)
    time.sleep(3.5)
    return True


def _support_cards(engine, frame) -> list[tuple[int, int, str]]:
    """助战列表里的卡片。

    锚点必须用**名字**(卡片左上),不能用「友情点+N」—— 后者在卡片右下,
    加偏移后会落到隔壁列的空隙上(实测因此点空、开战按钮不出现)。
    """
    hits = _texts(engine, frame, SUPPORT_LIST_ROI)

    def is_name(t: str) -> bool:
        t = t.strip()
        if not (3 <= len(t) <= 8):
            return False
        if "友情点" in t or "LV" in t.upper() or "好友" in t or "兑换" in t:
            return False
        return all("\u4e00" <= c <= "\u9fff" or c in "·~～" for c in t)

    cards: list[tuple[int, int, str]] = []
    for text, score, loc in sorted(hits, key=lambda h: h[2][1]):
        if score < 0.9 or not is_name(text):
            continue
        if any(abs(c[1] - loc[1]) < 60 for c in cards):   # 同一行 = 同一张卡
            continue
        cards.append((loc[0], loc[1], text))
    return cards


def _wait_for_text(inp, engine, needles, timeout_s: float, log, what: str,
                   poll_s: float = 1.5) -> bool:
    """轮询直到画面上出现任意一段文字。

    开战之后必须**等结算真的出来**,不能睡固定秒数:战斗时长取决于关卡和角色,
    而 ADB 一帧要 0.4s。实测固定睡眠会造成"游戏还没加载完就开始下一步",
    后续点击全部落空。
    """
    deadline = time.time() + timeout_s
    while True:
        frame = inp.grab()
        hits = _texts(engine, frame)
        if any(s >= 0.8 and any(k in t for k in needles) for t, s, _ in hits):
            return True
        if time.time() >= deadline:
            log(f"    ⚠️ 等「{what}」超时({timeout_s:.0f}s)")
            return False
        time.sleep(poll_s)


def _fight(inp, engine, log, timeout_s=150):
    frame = inp.grab()
    hits = _texts(engine, frame)
    go = _find(hits, "开战", 0.9)
    if not go:
        log("    !! 没找到「开战」")
        return False
    _click(inp, go[2], "开战", log)
    # 先等战斗界面(HP),再等结算 —— 这样能区分"还在加载"和"已经打完"
    _wait_for_text(inp, engine, ("HP", "战斗结算", "再次挑战", "获得奖励"),
                   45, log, "战斗界面出现")
    ok = _wait_for_text(inp, engine, ("战斗结算", "再次挑战", "获得奖励", "确定"),
                        timeout_s, log, "战斗结算")
    if ok:
        log("    战斗结束,出现结算")
    return ok


def _switch_loadout(inp, engine, log, equipment_file=None) -> bool:
    """在装备页切到配置的套装。

    这里是 equip_select 直接调用的轻量版:打开装备页 -> 点角色「切换」->
    必要时展开组 -> 点套装 -> 选择方案。参数全部来自 equipment.json。
    """
    st = load_equipment_settings(equipment_file)
    open_pt = st.get("open_point") or list(EQUIP_ICON)
    switch_pt = st.get("switch_point") or [459, 204]
    group = str(st.get("group") or "")
    suit = str(st.get("suit") or "")
    apply_text = str(st.get("apply_text") or "选择方案")
    settle = float(st.get("settle", 2.5))
    list_marker = str(st.get("list_marker") or "自定义套装")

    _click(inp, open_pt, "手枪图标(开装备页)", log)
    time.sleep(settle)
    _click(inp, switch_pt, "带手枪图标的「切换」(展开方案列表)", log)
    time.sleep(settle)

    # 关键:点「切换」后列表**已经是展开的**。此时再点组名会把列表收起
    # (组名是展开/收回开关),所以只在套装条目不可见时才去点组。
    min_y = int(st.get("list_min_y", 210))

    def suit_candidates():
        hits = _texts(engine, inp.grab())
        return [(t, s, loc) for t, s, loc in hits
                if suit and suit in t and s >= 0.85 and loc[1] > min_y]

    if suit and not suit_candidates() and group:
        for _ in range(2):
            hits = _texts(engine, inp.grab())
            g = _find(hits, group, 0.85)
            if not g:
                log(f"    !! 找不到装备组 {group!r}")
                return False
            _click(inp, g[2], f"装备组 {group!r}(展开)", log)
            time.sleep(settle)
            if suit_candidates():
                break

    if suit:
        cands = suit_candidates()
        if not cands:
            log(f"    !! 列表里找不到套装 {suit!r}")
            return False
        _click(inp, cands[0][2], f"套装 {suit!r}", log)
        time.sleep(settle)

    hits = _texts(engine, inp.grab())
    ap = _find(hits, apply_text, 0.85)
    if not ap:
        log(f"    !! 找不到 {apply_text!r}")
        return False
    _click(inp, ap[2], f"{apply_text!r}", log)
    time.sleep(settle)
    log(f"    装备方案已应用: {suit or group}")
    return True


def sweep(capture, *, dry_run=False, max_stages=12, log=print,
          equipment_file=None, seed=None, set_equipment=True) -> dict:
    """打光所有首通关卡。返回统计。

    装备切换**只在第一次进入战斗之前**做一次(用户要求),之后的首通关卡和
    BONUS 都复用这次切换的装备 —— 不每个关卡都切。
    """
    engine = OcrEngine.shared()
    rng = random.Random(seed)
    stats = {"stages": [], "scans": 0, "done": False, "equipped": False}
    equipped = False            # 只切第一次

    for n in range(1, max_stages + 1):
        log(f"=== 第 {n} 轮:找首通关卡 ===")
        if not _goto_map(capture, engine, log):
            log("  !! 无法回到地图,停止")
            break
        pt, frame = _scan_map(capture, engine, log)
        stats["scans"] += 1
        if pt is None:
            log("  ✅ 没有未领首通奖励的关卡了")
            stats["done"] = True
            break
        log(f"  首通节点 -> 点击 {pt}")
        if dry_run:
            log("  [dry-run] 停止")
            break

        if not _enter_stage(capture, engine, log, pt):
            log("  !! 没能进入关卡详情页,停止")
            break

        # 装备切换只在**第一次开战前**做一次
        if set_equipment and not equipped:
            log("  切换装备方案(首次开战前)")
            if _switch_loadout(capture, engine, log, equipment_file):
                _goto_stage_detail(capture, engine, log)
                equipped = True
            else:
                log("    (装备切换未完成,继续尝试直接开打)")

        if not _select_support(capture, engine, log, rng):
            log("  !! 选助战失败,停止")
            break
        if not _fight(capture, engine, log):
            log("  !! 开战失败,停止")
            break
        _dismiss(capture, engine, log)
        stats["stages"].append(pt)

    stats["equipped"] = equipped
    return stats


def _goto_stage_detail(inp, engine, log, tries=4) -> bool:
    """从装备页返回关卡详情页(靠「消耗体力」确认)。"""
    for _ in range(tries):
        frame = inp.grab()
        hits = _texts(engine, frame)
        if _find(hits, "消耗体力", 0.8):
            return True
        _click(inp, BACK_ARROW, "返回(离开装备页)", log)
        time.sleep(2.5)
    return False
