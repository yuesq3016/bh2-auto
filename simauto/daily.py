"""日常奖励领取 + 清体力流程(按实测规则编写)。

规则(来自实际操作确认):
  1. BONUS:第一次点「BONUS」进关卡开打;**每次开打前都要重新勾选双倍券**
     (勾选框在关卡页 (1098, 537);勾上后「消耗体力50」变「100」)
  2. 结算界面**优先检查「再次挑战」**:有就点它(而不是点「确定」回地图),
     重新进关卡后再次勾选双倍券,继续打
  3. 日常奖励:点任务界面左上角的「书」按钮 -> 依次领取「每日任务」和「每周任务」
  4. 之后进入「社交」

为什么每次都要重勾双倍券:实测打完一次后返回关卡页,勾选框会恢复未勾选状态
(有一次因此只消耗了 50 体力而不是 100)。
"""
from __future__ import annotations
from ._root import project_root

import random
import time
from pathlib import Path


from .ocr import OcrEngine
from .sweep import (_click, _dismiss, _find, _find_button, _find_safe, _fight,
                    _is_support_list, _select_support, _texts)

PROJECT_ROOT = Path(project_root())

# 实测坐标
BONUS_LABEL_BELOW = 57          # BONUS 文字下方这么多像素处是可点的节点
DOUBLE_CHECKBOX = (1098, 537)   # 关卡页双倍券勾选框
RETRY_BUTTON = (640, 619)       # 结算界面「再次挑战」的大致位置(仍会 OCR 定位)
RETRY_CHECKBOX = (700, 546)     # 结算界面上的双倍勾选框(青色像素簇实测定位;
                                # 注意不是关卡页那个 (1098,537))
BOOK_ICON = (68, 165)           # 任务界面左上角「书」按钮(立绘页里的,不常用)
TASK_BUTTON = (48, 178)         # 主界面左列第一个按钮 = 打开任务界面
HOME_NAV = (655, 81)            # 顶部导航「首页」
SOCIAL_NAV = (1104, 80)         # 顶部导航「社交」
BATTLE_NAV = (770, 80)          # 顶部导航「战斗」
SHOP_NAV = (993, 81)            # 顶部导航「商店」
ATTACK_BUTTON = (688, 531)      # 虚轴之庭的「出击」(蓝色三角,纯图形故用坐标)
BACK_ARROW = (57, 140)          # 左上角返回箭头
PAGE_PREV = (551, 665)          # 关卡地图左翻页箭头
PAGE_NEXT = (737, 665)          # 关卡地图右翻页箭头
# 内容区:排除顶部导航(y<140)和右上角页面标签(x>1040 且 y<200)
CARD_AREA = (140, 150, 1000, 480)
# 右上角那块「返回上一级」按钮(内容随层级变:多元裂缝/虚轴之庭/拼装时光…)
BACK_BUTTON_ZONE = (1040, 125, 240, 70)
# **纯坐标**的返回按钮中心。用户指出:上面的字会变,但位置不变,
# 所以当按文字找不到导航时,直接用这个坐标。
# 实测:关卡地图上是「拼装时光」@ (1168,156),虚轴之庭里是「虚轴之庭」@ (1169,157)
# —— 位置几乎完全一致,文字在变。取 (1168, 157) 作为兜底中心。
BACK_BUTTON = (1168, 157)
# 关卡地图的分页指示「1/2」所在区域(必须限制,否则会命中顶部体力「89/130」)
PAGE_INDICATOR_ZONE = (500, 630, 300, 80)


def _settle_has(hits, needle, conf=0.85) -> tuple | None:
    return _find(hits, needle, conf)


def open_bonus(inp, engine, log, double=True) -> bool:
    """点 BONUS 进关卡,并按需勾选双倍券。"""
    bon = [h for h in engine.recognize(inp.grab(), (150, 200, 400, 140))
           if "BONUS" in h.text.upper() and h.score >= 0.8]
    if not bon:
        log("    !! 地图上找不到 BONUS 标签")
        return False
    b = bon[0]
    log(f"    进入 BONUS ({b.loc[0]}, {b.loc[1] + BONUS_LABEL_BELOW})")
    _click(inp, (b.loc[0], b.loc[1] + BONUS_LABEL_BELOW), "BONUS 节点", log)
    time.sleep(3.5)

    if double:
        time.sleep(1.0)
        before = inp.grab()
        _click(inp, DOUBLE_CHECKBOX, "双倍券勾选框", log)
        time.sleep(2.0)
        frame = inp.grab()
        cost = [t for t, s, _ in _texts(engine, frame, (600, 520, 300, 50)) if s >= 0.7]
        log(f"    消耗显示: {cost}")
        if not any("100" in c for c in cost):
            # 勾选可能没生效,补一次
            _click(inp, DOUBLE_CHECKBOX, "双倍券勾选框(重试)", log)
            time.sleep(2.0)
            cost = [t for t, s, _ in _texts(engine, frame, (600, 520, 300, 50))]
            log(f"    重试后消耗显示: {cost}")
    return True


def battle_once(inp, engine, log, rng, timeout_s=180) -> bool:
    """开战并等结算。

    兼容两种入口:
      * 在关卡详情页 -> 先点「选择助战好友」并在列表里随机选一个
      * 已经在助战界面(「再次挑战」会把我们直接带到这里,且保留上次的助战)
        -> 不需要再选,直接开战。硬找「选择助战好友」会失败,这是实测踩过的坑。
    """
    frame = inp.grab()
    hits = _texts(engine, frame)
    already = _find(hits, "开战", 0.9)
    if already:
        log("    已在助战界面(开战按钮可见),跳过选助战")
    else:
        if not _select_support(inp, engine, log, rng):
            return False
    return _fight(inp, engine, log, timeout_s=timeout_s)


def _switch_loadout_once(inp, engine, log, *, equipped: bool,
                         set_equipment: bool, equipment_file: str | None) -> bool:
    """第一次开打前切到 equipment.json 配置的套装。

    「再次挑战」会保留上次装备,所以**只切第一次**。切完要回到关卡详情页
    (装备页 -> 返回 -> 关卡页),否则后续选助战/开战会找不到按钮。

    返回 (切过装备了吗, 是否成功回到可开打状态)。
    """
    if equipped or not set_equipment:
        return equipped
    from .sweep import _switch_loadout, _goto_stage_detail
    log("    首次开打前切换装备方案")
    if _switch_loadout(inp, engine, log, equipment_file):
        # 从装备页返回关卡详情页(靠「消耗体力」确认)
        if not _goto_stage_detail(inp, engine, log):
            log("    !! 切完装备回不到关卡页")
            return True        # 装备确实切了,返回 True 让调用方继续尝试
        log("    装备已切换,回到关卡页")
    else:
        log("    (装备切换未完成,继续尝试直接开打)")
    return True


def run_bonus_rounds(inp, engine, log, times=1, double=True,
                     rng: random.Random | None = None,
                     diamond_buys: int = 0,
                     set_equipment: bool = True) -> int:
    """打 ``times`` 次 BONUS,中间用「再次挑战」续关。返回完成次数。

    ``diamond_buys``:遇到「消耗钻石补充体力」时允许兑换的次数(0 = 不兑换)。
    ``set_equipment``:第一次开打前是否切装备(默认切)。
    """
    rng = rng or random.Random()
    done = 0
    bought = 0
    equipped = False
    for i in range(1, times + 1):
        log(f"=== BONUS 第 {i}/{times} 次 ===")
        if i == 1:
            if not open_bonus(inp, engine, log, double=double):
                break
        equipped = _switch_loadout_once(
            inp, engine, log, equipped=equipped,
            set_equipment=set_equipment, equipment_file=None)
        if not battle_once(inp, engine, log, rng):
            log("    !! 战斗未完成")
            break
        done += 1
        if i == times:
            break
        # 结算界面优先找「再次挑战」
        time.sleep(2.5)
        frame = inp.grab()
        hits = _texts(engine, frame)
        retry = _settle_has(hits, "再次挑战", 0.85)
        if retry:
            log(f"    结算界面有「再次挑战」-> 点它续关 {retry[2]}")
            _click(inp, retry[2], "再次挑战", log)
            # 等界面切换(助战列表 / 体力弹窗)
            wait_until(inp, engine, log,
                       lambda e, f: (_is_support_list(e, f)
                                     or bool(_find(_texts(e, f), "兑换", 0.9))
                                     or bool(_find(_texts(e, f), "取消", 0.9))),
                       "再次挑战之后的界面", timeout_s=25, save_tag="after_retry")
            # 「再次挑战」会直接跳到助战选择界面,不经过关卡页;但次数/体力不足
            # 时会先弹补充体力弹窗
            allow = bought < diamond_buys
            verdict = handle_refill_dialog(inp, engine, log,
                                           buy_with_diamond=allow)
            if verdict == "diamond":
                bought += 1
                log(f"    💎 已用钻石兑换体力({bought}/{diamond_buys} 次)")
                time.sleep(2.0)
            elif verdict == "cancel":
                log("    🔚 需要钻石补充体力 -> 结束")
                break
            # 注意:「再次挑战」直达**助战选择**界面,并不经过关卡页,所以
            # 双倍券勾选框在这里不存在,不能去点它(实测点了会落到装备特性
            # 说明文字上)。双倍券只在关卡页设置。
        else:
            log("    结算界面没有「再次挑战」,回地图重进")
            _dismiss(inp, engine, log)
            if not open_bonus(inp, engine, log, double=double):
                break
    return done


def _on_bonus_page(engine, frame) -> bool:
    """是否已经在「BONUS 关卡页」。

    判据:
      * **有「BONUS」字样**(页面标题)
      * 且有「消耗体力」(关卡页必有)
      * 且有**任意一个操作入口**:选择助战好友 / 快捷战斗 / 补充体力

    ⚠️ 不能要求必须是「选择助战好友」:
      体力或次数不足时,那个按钮**会被「补充体力」取代**(实测踩过 ——
      于是明明站在 BONUS 页上却判定为"不在",然后一直去导航,
      日志里表现为"找不到活动页签 / 等导航超时")。

    另外要注意识别顺序:「关卡地图」也用「2/2」这种分页文字,所以 is_bonus
    必须在 assemble 之前判断。
    """
    hits = _texts(engine, frame)
    has_bonus = any(s >= 0.8 and "BONUS" in t.upper() for t, s, _ in hits)
    has_cost = any(s >= 0.8 and "消耗体力" in t for t, s, _ in hits)
    has_entry = any(s >= 0.8 and any(k in t for k in
                                     ("选择助战好友", "快捷战斗", "补充体力"))
                    for t, s, _ in hits)
    return has_bonus and has_cost and has_entry


def _check_double_voucher(inp, engine, log) -> bool:
    """在关卡页勾选双倍券,并**读消耗文字验证**。

    实测:勾选后「消耗体力50」会变成「消耗体力100」。所以验证判据是那段文字
    里出现 100 —— 比看勾选框像素可靠得多(勾选框是 toggle,盲点会来回翻转)。
    """
    frame = inp.grab()
    cost = [t for t, s, _ in _texts(engine, frame, (600, 520, 300, 50)) if s >= 0.7]
    if any("100" in c for c in cost):
        log("    双倍券已生效(消耗显示 100)")
        return True
    for attempt in (1, 2):
        _click(inp, DOUBLE_CHECKBOX, f"双倍券勾选框(第{attempt}次)", log)
        # 等消耗文字更新,而不是盲等固定秒数
        wait_until(inp, engine, log,
                   lambda e, f: any("100" in t for t, s, _ in
                                    _texts(e, f, (600, 520, 300, 50)) if s >= 0.7),
                   "双倍券生效(消耗显示 100)", timeout_s=8, poll_s=0.8)
        cost = [t for t, s, _ in _texts(engine, inp.grab(), (600, 520, 300, 50))
                if s >= 0.7]
        if any("100" in c for c in cost):
            log(f"    双倍券已勾选(消耗显示 {cost})")
            return True
    log(f"    ⚠️ 双倍券可能未生效(消耗显示 {cost})")
    return False


def handle_refill_dialog(inp, engine, log, *, buy_with_diamond: bool = False) -> str:
    """处理「补充体力」弹窗(**只在点完「再次挑战」之后出现**)。

    实测的真实样子(截图确认):

        兑换双倍体力
        主人可兑换 89 双倍体力,补充 178 体力
        [兑换]   [取消]

    规则(用户指定):
      * 有「双倍体力」可兑换 -> 点「兑换」直接消耗,继续打
      * 只能花钻石/水晶:
          - ``buy_with_diamond=False``(默认)-> 点「取消」,本次清体力完成
          - ``buy_with_diamond=True``        -> **点「兑换」消耗钻石**,继续打
            (由调用方计数,用完允许的次数后不再兑换)

    注意按钮是「**兑换**」不是「确定」,标题是「**兑换双倍体力**」不是「补充体力」——
    早先按「补充体力 + 确定/取消」的判据会完全漏掉这个弹窗。
    """
    frame = inp.grab()
    hits = _texts(engine, frame)
    joined = " ".join(t for t, s, _ in hits if s >= 0.6)

    ok_btn = _find_button(hits, ("兑换", "确定", "确认"))
    cancel_btn = _find_button(hits, ("取消",))

    # 判据:出现「双倍体力」/「补充体力」/「兑换双倍」等字样,且有取消按钮
    keywords = ("双倍体力", "补充体力", "体力不足", "购买体力", "恢复体力",
                "兑换双倍", "补充178", "消耗钻石")
    mentions = any(k in joined for k in keywords)
    if not (mentions and cancel_btn):
        return "none"
    if ok_btn is None:
        log("    检测到体力弹窗但没有确认按钮")
        return "none"

    log(f"    检测到体力弹窗:{[t for t, s, _ in hits if s >= 0.85][:4]}")
    if "双倍体力" in joined or "兑换双倍" in joined:
        log(f"    可用双倍体力 -> 点「{ok_btn[0]}」直接消耗")
        _click(inp, ok_btn[2], f"'{ok_btn[0]}'(消耗双倍体力)", log)
        # 补充完体力后,结算界面重新出现,上面有「再次挑战」—— 直接点它继续打,
        # **不要点「确定」**(确定会退出结算,结束清体力)。
        _retry_after_refill(inp, engine, log)
        return "double"

    if buy_with_diamond:
        log(f"    需要钻石,但已开启「钻石兑换体力」-> 点「{ok_btn[0]}」兑换")
        _click(inp, ok_btn[2], f"'{ok_btn[0]}'(消耗钻石换体力)", log)
        _retry_after_refill(inp, engine, log)
        return "diamond"

    log("    只能花钻石 -> 点「取消」,本次清体力完成")
    _click(inp, cancel_btn[2], "'取消'", log)
    time.sleep(2.5)
    return "cancel"


def _retry_after_refill(inp, engine, log) -> bool:
    """补充完体力后,结算界面重新出现 -> 点「再次挑战」继续打。

    用户明确要求:"补充完体力应该直接点再次,而不是确定"。
    点「兑换」补体力后,画面回到结算界面,上面是「再次挑战」和「确定」;
    必须点「再次挑战」继续刷,点「确定」会退出结算、结束清体力。
    """
    ok = wait_until(inp, engine, log, wait_text("再次挑战"),
                    "补充体力后出现「再次挑战」", timeout_s=20,
                    save_tag="after_refill")
    if not ok:
        log("    ⚠️ 补充体力后没看到「再次挑战」")
        return False
    retry = _find(_texts(engine, inp.grab()), "再次挑战", 0.85)
    if retry:
        log("    补充完体力 -> 点「再次挑战」继续")
        _click(inp, retry[2], "再次挑战", log)
        time.sleep(3.0)
        return True
    return False


def clear_first_clear(inp, engine, log, *, set_equipment: bool = True) -> tuple[int, bool]:
    """**先清带钻石(水晶图标)的首通节点**,再交给 BONUS 清体力。

    用户明确要求:清体力要先打带水晶图标的未领首通关卡(领首通钻石奖励),
    然后再打 BONUS。

    复用 `sweep.sweep()`(颜色找水晶图标,不依赖 OCR 认关卡名),它打完会停在
    关卡地图。返回 ``(打了几个首通关卡, 是否已切装备)`` —— 后者让 BONUS 阶段
    复用首通阶段切好的装备,避免再切一次。
    """
    from .sweep import sweep
    if not goto_activity_map(inp, engine, log):
        log("    !! 到不了活动关卡地图,跳过首通清理")
        return 0, False
    log("    === 先清带钻石的首通节点(sweep)===")
    stats = sweep(inp, log=log, set_equipment=set_equipment)
    n = len(stats.get("stages", []))
    equipped = bool(stats.get("equipped"))
    log(f"    首通清理完成,打了 {n} 个首通关卡"
        + ("(已切装备)" if equipped else ""))
    return n, equipped


def clear_stamina(inp, engine, log, *, max_rounds=30, double=True,
                  rng: random.Random | None = None,
                  diamond_buys: int = 0,
                  set_equipment: bool = True,
                  equipment_file: str | None = None,
                  clear_first: bool = True) -> dict:
    """一直打 BONUS 直到无法继续。**本轮清体力的终点是「消耗钻石」弹窗。**

    流程(与实测一致):

      1. **首次进入关卡页时切装备**(见 ``set_equipment``,之后「再次挑战」会
         保留上次装备,不用重复切)
      2. 看结算界面有没有「再次挑战」
      3. **有** -> 勾结算界面的双倍勾选框 -> 点「再次挑战」
         - **之后再检查弹窗**:这一步才会弹「补充体力」
           · 双倍体力可用 -> 消耗,继续开打
           · 只能花钻石   -> ``diamond_buys`` 还有额度就兑换(继续打),
                             否则点取消,本次清体力完成
      4. **没有** -> 导航到 BONUS 关卡

    关键实测事实(决定了检查顺序):
      * 「补充体力」弹窗**只在点完「再次挑战」之后**出现,所以要放在点击之后检查,
        放在循环开头检查是错的(会永远看不到)
      * 「再次挑战」直达助战列表,保留上次助战,不需要再点「选择助战好友」
      * 结算界面的双倍勾选框在 `(700, 546)`,不是关卡页那个 `(1098, 537)`
      * 不读体力数值 —— 用户明确要求不做这项验证

    ``diamond_buys``:允许**消耗钻石**兑换体力的次数上限(0 = 不允许,遇到就结束)。
    ``set_equipment``:开打前是否切到 equipment.json 里配置的套装(默认切)。
    """
    rng = rng or random.Random()
    out = {"runs": 0, "stopped": "", "diamond_seen": False,
           "diamond_bought": 0, "diamond_quota": diamond_buys}
    import numpy as _np
    equipped = False            # 是否已经切过装备(只切第一次)

    # ⚠️ 这里**不做** check_can_fight:
    # 如果当前停在别的界面(比如主界面),那上面根本没有「快捷战斗」,提前检查
    # 会把整个任务误判成"次数不足"直接跳过 —— 实测就是这样错过了清体力。
    # 正确的顺序是:**先导航到 BONUS 关卡页,到了之后再检查能不能打**。
    if diamond_buys:
        log(f"    已开启「钻石兑换体力」,允许兑换 {diamond_buys} 次")

    # 先识别当前在哪(用户要求):已在 BONUS 页/助战列表/结算 → 直接开工,
    # 不重复导航;在别处才去导航。
    screen = preflight(inp, engine, log, "清体力")
    log(f"    -> "
        + {"bonus": "已在 BONUS 关卡页,直接开打",
           "support": "已在助战界面,直接开打",
           "settle": "已在结算界面,可续关",
           "assemble": "已在关卡地图,去点 BONUS",
           }.get(screen, "需要导航到 BONUS 关卡"))

    # ⚠️ **先清带钻石的首通节点**(用户要求),再打 BONUS 清体力。
    # 只在"需要导航"(不在 BONUS/助战/结算)时才清首通 —— 续关场景首通早已清完。
    # 首通阶段若已切装备,equipped 直接继承,避免 BONUS 阶段再切一次。
    if clear_first and screen not in ("bonus", "support", "settle"):
        _n, equipped = clear_first_clear(inp, engine, log,
                                         set_equipment=set_equipment)

    for i in range(1, max_rounds + 1):
        frame = inp.grab()
        hits = _texts(engine, frame)
        retry = _find(hits, "再次挑战", 0.85)

        # 已经在 BONUS 关卡页?直接开打,不重复导航
        if not retry and _on_bonus_page(engine, frame):
            log(f"=== 第 {i} 轮:已在 BONUS 关卡页,直接开打 ===")
            check_can_fight(engine, frame, log, "清体力")   # 到了页面才检查
            if double:
                _check_double_voucher(inp, engine, log)
            equipped = _switch_loadout_once(
                inp, engine, log, equipped=equipped,
                set_equipment=set_equipment, equipment_file=equipment_file)
            if not battle_once(inp, engine, log, rng):
                out["stopped"] = "战斗未完成"
                break
            out["runs"] += 1
            log(f"    完成第 {out['runs']} 次")
            continue

        if not retry:
            # 没有「再次挑战」:要么是首次进入(在助战界面),要么次数已用完
            if _is_support_list(engine, frame):
                log(f"=== 第 {i} 轮:已在助战界面,直接开打 ===")
            else:
                log(f"=== 第 {i} 轮:导航到 BONUS 关卡 ===")
                # 不再依赖"恰好在结算界面":从任意界面都能导航过去
                if not goto_bonus_stage(inp, engine, log):
                    out["stopped"] = "无法导航到 BONUS 关卡"
                    break
                # 到了关卡页才检查体力/次数 —— 这时「快捷战斗」是否被替换成
                # 「补充体力」才是可靠判据
                check_can_fight(engine, inp.grab(), log, "清体力")
                if double:
                    _check_double_voucher(inp, engine, log)
        else:
            log(f"=== 第 {i} 轮:勾双倍 -> 再次挑战 ===")
            if double:
                before = inp.grab()
                x, y = RETRY_CHECKBOX
                _click(inp, RETRY_CHECKBOX, "结算界面双倍勾选框", log)
                time.sleep(1.8)
                after = inp.grab()
                d = float(_np.abs(
                    before[y - 28:y + 28, x - 32:x + 32].astype(_np.int16)
                    - after[y - 28:y + 28, x - 32:x + 32].astype(_np.int16)).mean())
                log(f"    勾选框变化 {d:.2f} {'(已勾选)' if d > 5 else '(可能本来已勾选)'}")
            _click(inp, retry[2], "再次挑战", log)
            # 等界面真的变(助战列表 / 关卡页 / 补充体力弹窗 三者之一)
            wait_until(inp, engine, log,
                       lambda e, f: (_is_support_list(e, f)
                                     or bool(_find(_texts(e, f), "消耗体力", 0.8))
                                     or bool(_find(_texts(e, f), "兑换", 0.9))
                                     or bool(_find(_texts(e, f), "取消", 0.9))),
                       "再次挑战之后的界面", timeout_s=25, save_tag="after_retry")

            # 「补充体力」弹窗只在这一步之后出现
            # 还有钻石额度就用钻石兑换(继续打),否则遇到就取消结束
            allow_diamond = out["diamond_bought"] < diamond_buys
            verdict = handle_refill_dialog(inp, engine, log,
                                           buy_with_diamond=allow_diamond)
            if verdict == "cancel":
                log("    🔚 弹出的是「消耗钻石」-> 本次清体力完成")
                out["diamond_seen"] = True
                if diamond_buys and not allow_diamond:
                    out["stopped"] = (f"钻石兑换额度已用完"
                                      f"({out['diamond_bought']}/{diamond_buys})")
                else:
                    out["stopped"] = "需要钻石补充体力(本次清体力完成)"
                break
            if verdict == "diamond":
                out["diamond_bought"] += 1
                log(f"    💎 已用钻石兑换体力 "
                    f"({out['diamond_bought']}/{diamond_buys} 次),继续打")
                time.sleep(2.0)
                continue        # 兑换完直接进入下一轮(体力已补充)
            if verdict == "double":
                # ⚠️ 双倍体力兑换后,**画面可能回到关卡地图**(实测:117/204,
                # 停在「拼装时光」关卡地图,BONUS 节点在 235,264),而不是助战
                # 列表。所以和 diamond 一样 `continue`,让下一轮开头重新识别
                # 界面并导航 —— 它已经能处理"在关卡地图上 -> 点 BONUS 节点"。
                log("    双倍体力已消耗,画面可能回到关卡地图 -> 重新导航")
                time.sleep(1.5)
                continue

        equipped = _switch_loadout_once(
            inp, engine, log, equipped=equipped,
            set_equipment=set_equipment, equipment_file=equipment_file)
        if not battle_once(inp, engine, log, rng):
            out["stopped"] = "战斗未完成"
            break
        out["runs"] += 1
        log(f"    完成第 {out['runs']} 次")

    if not out["stopped"]:
        out["stopped"] = f"达到上限 {max_rounds} 轮"
    return out


def claim_task_rewards(inp, engine, log, weekly: bool = False) -> dict:
    """任务界面领取奖励。

    实测流程:
      * 「存在感」页签 = **每日任务**,顶部有「一键领取」(OCR 会读成「键领取」)
      * 「每周任务」页签同理
      * 点完一键领取后体力会增加(实测 251 -> 2001),所以这一步也是「领体力」

    ``weekly=True`` 时**额外**领一次每周任务(用户要求只在流程最后一步做)。
    """
    out = {"daily": False, "weekly": False}
    # 先识别当前在哪:已在任务界面就跳过导航
    screen = preflight(inp, engine, log, "存在感")
    if screen == "task":
        log("    已在任务界面,跳过导航")
    else:
        open_task_tab(inp, engine, log, "存在感")
    tabs = [("存在感", "daily")]
    if weekly:
        tabs.append(("每周任务", "weekly"))
    for name, key in tabs:
        frame = inp.grab()
        hits = _texts(engine, frame)
        tab = _find_button(hits, (name,)) or _find(hits, name, 0.85)
        if tab:
            _click(inp, tab[2], f"页签 {name}", log)
            time.sleep(2.5)
        for _ in range(6):
            frame = inp.grab()
            hits = _texts(engine, frame)
            # 「一键领取」OCR 常漏「一」字;用 _find_button 精确优先
            claim = _find_button(hits, ("一键领取", "键领取"))
            if not claim:
                break
            _click(inp, claim[2], f"一键领取({name})", log)
            time.sleep(2.5)
            out[key] = True
            _dismiss(inp, engine, log, rounds=2, also=("确定", "确认"))
        log(f"    {name}: {'已领取' if out[key] else '没有可领取的'}")
    # 收尾:离开任务界面(它点「首页」退不出去,要用左上返回箭头),免得
    # 影响下一个任务 —— 实测停在任务界面时,后面的导航会更费劲。
    go_home(inp, engine, log)
    return out


def goto_shop_tab(inp, engine, log, tab: str, tries: int = 4) -> bool:
    """进入商店并切到指定子页签(``共鸣屋`` / ``回收屋`` / ``万神屋`` / ``崩坏屋``)。

    **实测路径是两级的**(容易走错):

        主界面 -> 商店 (993,81)
               -> 商店首页是**卡片式入口**:崩坏屋卡片 (1012,293)
               -> 进去后左侧才出现子页签:共鸣屋 (80,310) / 回收屋 (82,387)
                  / 万神屋 (81,462)

    也就是说「共鸣屋」**不在商店首页**,必须先进「崩坏屋」卡片。

    ⚠️ 之前 `_run_resonance` 完全没有导航,直接在当前界面读格子,把商店的
    文字(「日常补给包」「日限购：1/1」)当成共鸣屋的装备。
    """
    def _tab_hit(hits_):
        return _find_button(hits_, (tab,)) or _find(hits_, tab, 0.85)

    for i in range(tries):
        frame = inp.grab()
        screen = recognize_screen(engine, frame)
        hits = _texts(engine, frame)

        # 已经在带子页签的界面(有崩坏屋/回收屋/万神屋之一)?直接点目标页签
        if screen in ("shop", "shop_tab"):
            t = _tab_hit(hits)
            if t:
                log(f"    点页签「{tab}」{t[2]}")
                click_and_wait(inp, engine, log, t[2], f"页签 {tab}",
                               lambda e, f: _tab_hit(_texts(e, f)) is not None,
                               timeout_s=15, save_tag="shop_tab_" + tab)
                return True
            # 商店首页(卡片式)没有子页签 -> 先点「崩坏屋」卡片进去
            card = _find_button(hits, ("崩坏屋",)) or _find(hits, "崩坏屋", 0.85)
            if card:
                log(f"    商店首页没有「{tab}」页签 -> 先进「崩坏屋」卡片 {card[2]}")
                click_and_wait(inp, engine, log, card[2], "崩坏屋卡片",
                               lambda e, f: _tab_hit(_texts(e, f)) is not None,
                               timeout_s=20, save_tag="beng_room")
                continue
            log(f"    !! 商店里找不到「{tab}」也找不到「崩坏屋」入口")
            return False

        if screen in ("assemble", "bonus", "stage", "fracture", "boss", "activity",
                      "task", "social", "maze", "tower"):
            log(f"    当前在「{SCREEN_LABELS.get(screen, screen)}」,先退回主界面")
            go_home(inp, engine, log)
            continue

        log(f"    点导航「商店」{SHOP_NAV}(第 {i+1}/{tries} 次)")
        click_and_wait(inp, engine, log, SHOP_NAV, "导航 商店",
                       lambda e, f: any(s >= 0.85 and
                                        any(k in t for k in ("礼包商店", "水晶商店",
                                                             "崩坏屋", "共鸣屋"))
                                        for t, s, _ in _texts(e, f)),
                       timeout_s=20, save_tag="shop_page")
    return False


def open_task_tab(inp, engine, log, tab: str, tries: int = 4) -> bool:
    """打开任务界面并切到指定页签(``存在感`` / ``每周任务`` / ``使魔探险`` / ``任务``)。

    **这是「使魔探险」和「存在感」共用的导航**,实测路径:

        主界面 -> 左列第一个按钮「任务」(48,178) -> 点左侧页签

    ⚠️ 两个坑:
      1. 任务界面是**「首页」导航的子页面** —— 在里面点「首页」**不会退出**
         (实测),所以不能"先点首页再点任务按钮",那会原地打转。
      2. 从深层界面(活动页/关卡页/虚轴之庭)过来时,必须先逐级退回主界面,
         这要用右上角那个方框(见 go_home),而不是顶部导航。

    返回是否切到了目标页签。
    """
    for i in range(tries):
        frame = inp.grab()
        screen = recognize_screen(engine, frame)
        hits = _texts(engine, frame)

        if screen == "task":
            # 已在任务界面 -> 直接点页签
            t = _find_button(hits, (tab,)) or _find(hits, tab, 0.85)
            if t:
                log(f"    已在任务界面,点页签「{tab}」{t[2]}")
                click_and_wait(inp, engine, log, t[2], f"页签 {tab}",
                               lambda e, f: bool(_find(_texts(e, f), tab, 0.85)),
                               timeout_s=12, save_tag="tab_" + tab)
                return True
            log(f"    !! 任务界面里找不到页签「{tab}」")
            return False

        if screen in ("bonus", "stage", "settle"):
            # 关卡详情页 / BONUS 关卡页 / 结算页:**左上角返回箭头** (57,140) 返回。
            # ⚠️ 不能用 back_out(点右上角) —— 实测新活动「海妖秘境」的关卡页
            # 右上角(1168,157)是活动标题标签,点它没反应(死循环)。
            log(f"    当前在「{SCREEN_LABELS.get(screen, screen)}」,点左上角返回")
            _click(inp, BACK_ARROW, "左上角返回", log)
            time.sleep(2.0)
            continue
        if screen in ("assemble", "fracture", "boss"):
            # 活动地图 / 多元裂缝 / 虚轴之庭:用右上角方框逐级退出
            log(f"    当前在「{SCREEN_LABELS.get(screen, screen)}」,先逐级退回主界面")
            back_out(inp, engine, log, times=1)
            continue
        if screen in ("activity", "tower", "shop", "shop_tab", "social"):
            # ⚠️ 这些是**顶部导航栏齐全**的界面,点「首页」就能回主界面。
            # 不能用 back_out —— 实测活动页右上角(1168,157)是「崩坏之塔」/
            # 「拼装时光」等**活动入口标签**,点它反而越陷越深(死循环)。
            log(f"    当前在「{SCREEN_LABELS.get(screen, screen)}」,点「首页」回主界面")
            go_home(inp, engine, log)
            continue
        if screen == "maze":
            # 空想迷域:右上角无效,走左上角箭头
            exit_maze(inp, engine, log)
            continue

        # 主界面(或其它浅层):点左列任务按钮
        #
        # ⚠️ 点之前先清弹窗:登录广告/公告盖在主界面上时,任务按钮点不动,
        # 之前会连点 4 次全部超时、任务直接跳过(实测「存在感」卡死)。
        # 画面本来就干净时 close_popups 只跑 2 轮(~1.2s),代价很小。
        if screen in ("unknown", "home"):
            close_popups(inp, engine, log)
        log(f"    点左列任务按钮 {TASK_BUTTON}(第 {i+1}/{tries} 次)")
        click_and_wait(inp, engine, log, TASK_BUTTON, "任务按钮",
                       lambda e, f: _is_task_screen(e, f),
                       timeout_s=15, save_tag="open_task")
        frame = inp.grab()
        if _is_task_screen(engine, frame):
            hits = _texts(engine, frame)
            t = _find_button(hits, (tab,)) or _find(hits, tab, 0.85)
            if t:
                log(f"    点页签「{tab}」{t[2]}")
                click_and_wait(inp, engine, log, t[2], f"页签 {tab}",
                               lambda e, f: bool(_find(_texts(e, f), tab, 0.85)),
                               timeout_s=12, save_tag="tab_" + tab)
                return True
            log(f"    !! 任务界面里找不到页签「{tab}」")
            return False

        # 还没到任务界面 -> 回主界面重来
        if not go_home(inp, engine, log):
            log("    !! 回不到主界面")
    return False


def _open_task_screen(inp, engine, log, tries: int = 4) -> bool:
    """打开任务界面(兼容旧调用):等价于切到「任务」页签。"""
    return open_task_tab(inp, engine, log, "任务", tries=tries)


def close_popups(inp, engine, log, *, rounds: int = 30) -> int:
    """关掉挡路的弹窗:促销广告、系统公告、维护通知、签到、奖励提示、活动预告。

    **弹窗是不定量的**:登录时可能连续弹好几个公告/活动,关掉一个又冒一个。
    所以策略是**循环关闭,直到"连续 N 次确认画面干净"才停**,而不是关一个就完。

    ⚠️ 判定"干净"要**连续确认**:弹窗常有入场动画/延迟加载,单次看"没有"就
    停止会漏掉还没弹出来的下一个(实测因此只关了第一个公告,后面几个漏掉)。

    判据:
      * 促销/公告的关闭键:右上角的「X」,用模板(纯图形,OCR 读不出)。
      * 维护通知/系统公告/活动预告:**面板标题**出现即认为需要关。
      * 签到:交给 claim_signin()。

    返回关掉的数量。
    """
    from .sweep import _find_safe
    closed = 0
    clean_streak = 0            # 连续"画面干净"的次数
    clean_needed = 2            # 连续 2 次才确认真的没有弹窗了(降一档提速)

    for i in range(rounds):
        frame = inp.grab()
        hits = _texts(engine, frame)
        joined = " ".join(t for t, s, _ in hits if s >= 0.6)

        # ⛔ 强制更新弹窗:没有任何可关的东西,直接停(绝不点「更新」)
        if _is_force_update_screen(engine, frame):
            check_force_update(engine, frame, log)
            return closed

        # 0) 空想迷域:很容易误入,而且顶部导航无效 —— 优先退出去
        if _is_maze_screen(engine, frame):
            exit_maze(inp, engine, log)
            closed += 1
            clean_streak = 0
            continue

        # 0.5) 各种信息浮层(玩家信息 / 账号信息 …)—— 会挡住卡片
        if any(k in joined for k in ("玩家信息", "我的账号", "下一等级经验",
                                     "双倍体力回复时间")):
            log("    检测到「玩家信息」浮层 -> 点确定关闭")
            hit = _find_button(hits, ("确定", "确认", "关闭"))
            if hit:
                _click(inp, hit[2], f"'{hit[0]}'(关闭信息浮层)", log)
                closed += 1
                clean_streak = 0
                time.sleep(1.5)
                continue
            # 没有确定按钮就点浮层外的空白处
            _click(inp, (640, 690), "空白处", log)
            closed += 1
            clean_streak = 0
            time.sleep(1.5)
            continue

        # 1) 签到优先(它也是弹窗,而且领了才有奖励)
        if claim_signin(inp, engine, log):
            closed += 1
            clean_streak = 0
            continue

        # 2) 公告/维护/促销/活动预告面板 -> 点右上角的 X
        #
        # ⚠️ 关键词必须**只在弹窗标题里出现**,不能是页面上的状态文字。
        # 实测「活动已结束」是活动页卡片上的**状态标签**(活动页始终显示
        # "活动已结束,展示时间:剩余2天"),把它当弹窗关键词会导致反复误报
        # "检测到公告/活动面板但找不到关闭按钮" —— 因为根本没有 X 可点。
        # 所以这里去掉「活动已结束」,只保留真正的弹窗标题词。
        panel = any(k in joined for k in
                    ("系统公告", "维护通知", "更新公告", "活动公告", "公告",
                     "活动预告", "活动说明", "活动规则",
                     "限时礼包", "每日礼包",
                     # 新版本登录广告链(2026-10 实测):
                     # 超绝心愿/梦想祈愿 -> 看板礼包/限时发售 -> 活动公告,逐个盖住主界面
                     "超绝心愿", "梦想祈愿",
                     "看板礼包", "限时发售"))
        if panel:
            if _click_close_x(inp, engine, log):
                closed += 1
                clean_streak = 0
                time.sleep(1.5)
                continue
            log("    !! 检测到公告/活动面板但找不到关闭按钮")

        # 3) 通用弹窗按钮:确定/确认/知道了/关闭/取消/继续
        #    「今日不再提示」是「看板礼包/限时发售」广告专用的关闭键。
        btn = None
        for lbl in ("确定", "确认", "知道了", "关闭", "我知道了", "继续", "好的",
                    "今日不再提示"):
            b = _find_safe(hits, lbl, 0.9)
            if b:
                btn = b
                break
        if btn:
            log(f"    关闭弹窗:'{btn[0]}'")
            _click(inp, btn[2], f"'{btn[0]}'", log)
            closed += 1
            clean_streak = 0
            time.sleep(1.5)
            continue

        # 这一轮没找到任何可关的 -> 累计"干净"次数
        clean_streak += 1
        if clean_streak >= clean_needed:
            log(f"    画面已连续 {clean_streak} 次确认干净,没有弹窗了")
            break
        time.sleep(0.6)          # 等一下,给可能还在加载的弹窗时间

    if clean_streak < clean_needed and closed > 0:
        log(f"    ⚠️ 达到轮次上限({rounds}),仍有弹窗可能未关完")
    return closed


def _click_close_x(inp, engine, log) -> bool:
    """点弹窗右上角的「X」关闭按钮。

    **顺序很重要**:先用**模板**再用文字。

    实测:这个 X 是**纯图形按钮**,OCR 完全读不出(试过 "X"/"×"/"✕" 都不行),
    而模板 `announce_close_x.png` 在当前公告面板上**匹配度 1.0000 @ (1176,84)**。
    早先把文字判据放在前面,结果一直找不到按钮、公告关不掉。

    (对比:`popup_close_x.png` 只到 0.397 —— 促销弹窗和公告面板的 X 是两个
      不同模板,必须分开。)
    """
    from .matcher import match_template, run_detector

    # 阈值分开设:公告 X 实测 1.0000;而新登录广告「超绝心愿/梦想祈愿」的 X
    # 只匹配到 0.849 —— 统一用 0.85 会差 0.001 卡住,导致广告关不掉(实测踩过)。
    # 真正主界面上这两个模板只有 0.36~0.43,所以 0.80 依然安全。
    TPL_X = [("announcement", "templates/announce_close_x.png", [1050, 30, 220, 110], 0.85),
             ("promoPopup",   "templates/popup_close_x.png",    [950, 120, 180, 140], 0.80)]
    for name, tpl, roi, thr in TPL_X:
        try:
            r = match_template(frame := inp.grab(), load_image_rel(tpl),
                               roi=roi, threshold=thr)
        except Exception as exc:  # noqa: BLE001
            log(f"    (模板 {name} 匹配失败: {type(exc).__name__})")
            continue
        if r.hit and r.loc:
            log(f"    模板命中 {name}({r.score:.3f}) -> 点 {r.loc}")
            _click(inp, r.loc, f"{name} 关闭键", log)
            return True

    # 兜底:试着在右上角区域找单字符 X(有些皮肤会渲染成文字)
    frame = inp.grab()
    for zone in ((1050, 30, 220, 110), (950, 120, 180, 140)):
        for t, s, loc in _texts(engine, frame, zone):
            if s >= 0.6 and t.strip().upper() in ("X", "×", "✕", "✖"):
                log(f"    文字命中 '{t.strip()}' {loc}")
                _click(inp, loc, "X(关闭弹窗)", log)
                return True
    return False


def load_image_rel(rel: str):
    """按项目根目录加载图片(模板路径都相对于项目)。"""
    from .imaging import load_image
    p = PROJECT_ROOT / rel
    return load_image(str(p))


def claim_signin(inp, engine, log) -> bool:
    """如果屏幕上有「每日签到」弹窗,就点「领取」领掉它。

    独立成一个函数,因为签到**随时可能出现**:
      * 刚启动游戏、还在登录流程时
      * 已经在游戏里,但签到弹窗还挂着(实测就是这种情况 —— 之前
        `launch_game` 识别到 `signin` 后直接 return,签到被整个跳过)
      * 任何任务的间隙

    ⚠️ 之前踩过的坑:用旧版模板 `signin_claim.png` 匹配,新版弹窗(带
    「第6天/第7天」格子)对不上,于是漏领。现在用 OCR 认文字。

    返回是否领取了。
    """
    frame = inp.grab()
    hits = _texts(engine, frame)
    joined = " ".join(t for t, s, _ in hits if s >= 0.6)
    # 弹窗判据:出现签到独有文字。用「第N天」而不是「领取」—— 后者太泛。
    looks_like = any(k in joined for k in
                     ("每日签到", "第1天", "第2天", "第3天", "第4天", "第5天",
                      "第6天", "第7天", "签到领取奖励", "累计签到"))
    if not looks_like:
        return False

    log("    检测到「每日签到」弹窗")
    # 「领取」按钮:精确匹配 + 限制在下半部分,避免命中正文或别处
    btn = None
    for t, s, loc in hits:
        if s >= 0.8 and t.strip() == "领取" and loc[1] > 480:
            btn = (t, s, loc)
            break
    if btn is None:
        cands = [(t, s, loc) for t, s, loc in hits
                 if s >= 0.8 and "领取" in t and loc[1] > 480]
        if cands:
            cands.sort(key=lambda h: -h[1])
            btn = cands[0]
    if btn is None:
        log("    !! 认得是签到弹窗,但找不到「领取」按钮")
        return False

    log(f"    点「领取」{btn[2]}")
    _click(inp, btn[2], "领取(签到)", log)
    # 等弹窗消失(领取后通常会有关闭动画/奖励提示)
    wait_until(inp, engine, log,
               lambda e, f: not any(
                   k in " ".join(t for t, s, _ in _texts(e, f) if s >= 0.6)
                   for k in ("每日签到", "签到领取奖励")),
               "签到弹窗关闭", timeout_s=15, save_tag="signin_close")
    # 领完可能还有奖励提示要关
    _dismiss(inp, engine, log, rounds=2, also=("确定", "确认", "好的"))
    log("    签到已领取")
    return True


def launch_game(inp, engine, log, *, wait_s: float = 180,
                stop_event=None) -> bool:
    """启动崩坏学园2 并等到**真正进入游戏**。

    包名 ``com.miHoYo.HSoDv2Original``(官服;混服是另一个包名)。

    流程:
      1. ``ensure_game_running()`` 启动 + 等它到前台
      2. **等标题页出现**(实测必需:游戏启动要十几秒到一分钟,之前直接走进
         游戏流程,引擎在"还没画面"的时候就开始判定,于是 60 tick 全部落空)
      3. 走 `bh2-enter.json` 的进游戏流程:标题页 → 关促销/公告 → 领签到 → 主界面
         (那套配置已实测稳定,所以直接复用)

    ``stop_event``:线程事件,置位后立即返回(供界面的停止按钮使用)。
    """
    from .engine import Engine, FrameSource, load_config

    if not hasattr(inp, "ensure_game_running"):
        raise RuntimeError("当前输入后端不支持启动 App(需要 ADB 后端)")

    def _stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    if not inp.ensure_game_running(log=log, timeout_s=wait_s):
        # 用户要求:游戏已经启动过,**不要**因为启动环节而报错。
        # 只要进程还在跑,就当作"已启动",继续往下识别当前界面。
        try:
            still_running = bool(inp.is_app_running())
        except Exception:  # noqa: BLE001
            still_running = False
        if still_running:
            log("    ⚠️ 等前台超时,但游戏进程仍在运行 —— "
                "不报启动失败,继续识别当前界面")
        else:
            log("    !! 游戏没起来")
            return False
    if _stopped():
        return False

    # ⚠️ **启动后立刻截图 + 保存 + 判断当前页面**(用户要求)。
    # 后续导航怎么走,完全取决于这一帧识别到的页面:
    #   * 已在游戏内(主界面/活动页/关卡…)-> 跳过标题页等待与登录流程
    #   * 标题页/未知       -> 等标题页 -> 走进游戏流程
    # 截图存到 sessions/launch_screen.png,便于事后核对"它到底看到了什么"。
    from .imaging import save_image as _save_img
    frame = inp.grab()
    try:
        _save_img(str(PROJECT / "sessions" / "launch_screen.png"), frame)
    except Exception:  # noqa: BLE001
        pass
    screen = recognize_screen(engine, frame)
    log(f"    [启动游戏] 截图已存,识别当前界面:"
        f"{SCREEN_LABELS.get(screen, screen)}({screen})")

    if screen not in NOT_IN_GAME_SCREENS:
        log(f"    -> 已在游戏内,跳过标题页等待与登录流程")
        # 已经在游戏里,但**弹窗可能还挂着**(签到/公告/维护通知)。交给
        # close_popups 统一处理 —— 它也会顺手领签到。这些弹窗会挡住后面的
        # 识别,不关掉的话后续任务全部导航失败(实测踩过)。
        close_popups(inp, engine, log)
        return True

    # ⚠️ 关键等待:游戏进程到前台 ≠ 画面已加载。实测启动后要十几秒才出标题页,
    # 太早进入进游戏流程会出现"nothing matched for 60 ticks"。
    log("    等标题页出现(游戏启动后需十几秒加载)…")
    ok = wait_until(inp, engine, log,
                    wait_any("账号切换", "屏幕继续", "开始游戏", "点击屏幕",
                             "适龄提示", "CADPA", min_conf=0.6),
                    "标题页出现", timeout_s=90, poll_s=1.5,
                    save_tag="wait_title")
    if _stopped():
        return False
    if ok:
        log("    标题页已出现")
    else:
        # 没等到标题页:只要识别出来已经在游戏内,就**不要**再走登录流程
        # (否则会在游戏里乱点)。只有确实停在标题页或认不出来时才继续。
        now = recognize_screen(engine, inp.grab())
        if now not in NOT_IN_GAME_SCREENS:
            log(f"    没等到标题页,但当前是「{SCREEN_LABELS.get(now, now)}」"
                f" -> 已在游戏内,跳过登录流程")
            return True
        log("    ⚠️ 没等到标题页,仍继续尝试")

    log("    走进游戏流程(关弹窗 / 签到)")
    cfg = load_config(str(PROJECT_ROOT / "configs" / "bh2-enter.json"))
    src = FrameSource(cfg, capture=inp, input_controller=inp, window=None)
    eng = Engine(cfg, frame_source=src, input_controller=inp, log=log)
    eng.run()
    # 引擎流程结束后统一收尾:公告/维护/签到/奖励提示都可能还挂着
    close_popups(inp, engine, log)
    return True


class Insufficient(Exception):
    """体力或次数不足 —— 跳过当前任务,不算失败。

    判据来自实测:体力/次数不足时,关卡页上的「快捷战斗」按钮会**被替换成
    「补充体力」**。所以只要看不到「快捷战斗」而看到「补充体力」,就说明这一项
    做不了,应该跳过而不是报错。
    """


# 出现这些文字说明做不了了(体力/次数不足)
INSUFFICIENT_MARKERS = ("补充体力", "体力不足", "次数不足", "今日剩余次数0", "购买体力")


def check_can_fight(engine, frame, log, what: str = "当前关卡") -> None:
    """检查能否开打;不能则抛 Insufficient 让调用方跳过。

    优先看「快捷战斗」在不在 —— 它在就一定能打;不在再看有没有不足提示。
    这样即使提示文字变了,只要按钮还在就不会误判。
    """
    hits = _texts(engine, frame)
    has_quick = any(s >= 0.8 and "快捷战斗" in t for t, s, _ in hits)
    if has_quick:
        return
    reasons = [t for t, s, _ in hits
               if s >= 0.7 and any(m in t for m in INSUFFICIENT_MARKERS)]
    if reasons:
        raise Insufficient(f"{what}:体力或次数不足({reasons[0]})")


def back_out(inp, engine, log, times: int = 1) -> bool:
    """点**右上角那个按钮**返回上一级。

    这是用户早就点明的规则,也是深层界面的唯一出路:
      * 在「虚轴之庭」「多元裂缝」「拼装时光」这类子界面里,点顶部导航的
        「首页」「战斗」**都不会返回**(实测停在虚轴之庭时两者都无效)
      * 但**右上角那块方框**是"返回上一级",内容随层级变化
        (显示「多元裂缝」「虚轴之庭」「拼装时光」…)

    两级策略:
      1. 先用 OCR 在右上角区域找文字,按文字中心点(最准)
      2. **找不到就直接用纯坐标 `BACK_BUTTON`** —— 用户明确要求:
         "这里的字会变但是位置不变,当其他导航找不到时使用"

    返回是否执行了返回动作。
    """
    for i in range(times):
        frame = inp.grab()
        # ⚠️ **优先点右上角按钮**(多元裂缝/虚轴之庭/拼装时光这些深层界面的
        # 唯一返回按钮)。之前先查左上角颜色箭头,结果这些界面**根本没有左上角
        # 箭头**,而 `find_back_arrow` 对背景噪点误报 (67,3),点空气导致死循环。
        # 所以顺序必须是:右上角文字 -> 右上角纯坐标 -> 左上角颜色箭头兜底。
        #
        # ① 右上角文字(要**排除**「展示时间」「剩余」这类状态标签 —— 实测
        #    旧做法把「活动已结束,展示时间:剩余4天」当成了返回按钮)
        hits = [(t, s, l) for t, s, l in _texts(engine, frame, BACK_BUTTON_ZONE)
                if s >= 0.8
                and not any(bad in t for bad in ("展示时间", "剩余", "时间", "难度"))]
        if hits:
            hits.sort(key=lambda h: -h[1])
            t, s, loc = hits[0]
            log(f"    点右上角「{t}」{loc} 返回上一级")
            _click(inp, loc, f"右上角'{t}'(返回)", log)
            time.sleep(1.8)
            continue
        # ② 右上角没读到文字 -> 纯坐标(用户指定:字会变、位置不变)
        log(f"    右上角读不到文字 -> 用纯坐标 {BACK_BUTTON} 返回")
        _click(inp, BACK_BUTTON, "右上角返回(坐标兜底)", log)
        time.sleep(1.8)
        # ③ 左上角颜色箭头兜底(仅当上面的坐标点击后界面确实变了,才值得检查)
        #    —— 大多数深层界面根本没有它,所以放最后。
        arrow = find_back_arrow(inp.grab())
        if arrow and arrow[1] > 8:     # y<=8 的极可能是背景噪点,过滤掉
            log(f"    左上角颜色箭头 {arrow} 也存在,记录(不重复点击)")
    return True


def _is_task_screen(engine, frame) -> bool:
    """是否是任务界面(「首页」导航下的子页面)。

    这个界面**也带完整导航栏**,而且点「首页」不会退出(实测卡在这里时
    go_home 反复点首页都无效)。特征是左侧四个页签:任务/存在感/每周任务/使魔探险。
    """
    hits = _texts(engine, frame)
    tabs = ("任务", "存在感", "每周任务", "使魔探险")
    found = sum(1 for n in tabs
                if any(s >= 0.85 and t.strip() == n for t, s, _ in hits))
    return found >= 3


def _is_boss_screen(engine, frame) -> bool:
    """是否是「虚轴之庭」世界BOSS界面。

    这个界面**也显示完整顶部导航栏**,所以按导航栏判断会误认为在主界面
    (实测 `_at_home` 返回 True 而实际停在虚轴之庭,导致 go_home 直接放弃)。
    特征:右下有「兑换」+ 中间有 LEVEL,且左侧有「开放」。
    """
    hits = _texts(engine, frame)
    joined = [t for t, s, _ in hits if s >= 0.8]
    has_level = any("LEVEL" in t.upper() for t in joined)
    has_exchange = any(t.strip() == "兑换" for t in joined)
    has_open = any("开放" in t for t in joined)
    return has_level and has_exchange and has_open


def _is_tower_screen(engine, frame) -> bool:
    """是否是「崩坏之塔」界面(爬塔活动)。

    实测(2026-10-16):点「战斗」导航会直接进到这里,而不是战斗页签选择页 ——
    和「拼装时光」「多元裂缝」一样是"上次停留的子页面"坑。之前识别器不认识它,
    返回 unknown,`goto_activity` 反复回主界面重试,死循环。

    特征文字:
        崩坏之塔(右上角标签)
        剩余时间 6天3时54分
        L16层 / L17层 / 16层 / 15层 / 14层 / 首通奖励 / 挑战 / 战况 / 奖励总览
    """
    hits = _texts(engine, frame)
    joined = [t for t, s, _ in hits if s >= 0.8]
    has_tower = any("崩坏之塔" in t for t in joined)
    has_layer = any("层" in t and any(c.isdigit() for c in t) for t in joined)
    has_reward = any("首通奖励" in t or "奖励总览" in t for t in joined)
    return has_tower and has_layer and has_reward


class Stopped(Exception):
    """用户按了「停止」—— 立刻中止当前任务。

    在等待循环里检测到停止事件时抛出,由 GUI 捕获并结束整轮流程
    (不等超时,也不必等当前步骤走完)。
    """


# 当前生效的停止事件(线程事件)。GUI 在启动线程时设置它,
# 让 wait_until 这类轮询等待也能被停止按钮打断。
_STOP_EVENT = None


def set_stop_event(event) -> None:
    """注册全局停止事件(None 表示不响应停止)。"""
    global _STOP_EVENT
    _STOP_EVENT = event


def check_stopped() -> None:
    """如果用户请求了停止,抛 Stopped。"""
    if _STOP_EVENT is not None and _STOP_EVENT.is_set():
        raise Stopped("用户停止了执行")


def preflight(inp, engine, log, what: str) -> str:
    """任务开始前的界面识别 —— 用户要求"每当任务启动时先看当前在哪"。

    截一帧、识别、打印,然后返回界面键。调用方据此决定要不要导航。
    """
    frame = inp.grab()
    key = recognize_screen(engine, frame)
    log(f"    [{what}] 当前界面:{SCREEN_LABELS.get(key, key)}({key})")
    return key


# 「已经在游戏里」的界面集合 —— 这些情况下不需要再走启动/登录流程。
#
# ⚠️ 这里必须是**白名单的反面**:即"除了标题界面和认不出来之外,全都算在游戏内"。
# 之前是正列白名单,结果漏了 `signin` —— 于是出现自相矛盾的日志:
#     当前界面:每日签到        <- 已经认出来在游戏里
#     等标题页出现…            <- 却还去等标题页
# 改成黑名单式判断,以后新增界面不会再漏。
NOT_IN_GAME_SCREENS = {"title", "unknown"}


def _is_force_update_screen(engine, frame) -> bool:
    """是否是**强制更新客户端**弹窗(只有「更新」一个按钮)。

    实测(游戏版本升级期间):
        有新版本可以更新啦
        您的当前游戏版本较低，请点击更新
        进行游戏更新
        更新后将直接获得600水晶！
        [ 更新 ]

    ⚠️ **绝对不能点「更新」** —— 那是几 GB 的客户端下载。这不是脚本能绕过的
    问题,只能由用户手动更新客户端。脚本要做的是**认出它并明确报出来**,
    而不是在 unknown 状态下反复点首页/返回(实测死循环 6 轮)。
    """
    hits = _texts(engine, frame)
    joined = " ".join(t for t, s, _ in hits if s >= 0.7)
    return ("有新版本可以更新" in joined
            or "当前游戏版本较低" in joined
            or "进行游戏更新" in joined)


def check_force_update(engine, frame, log) -> bool:
    """如果是强制更新弹窗,明确报告并返回 True(调用方应停止流程)。"""
    if not _is_force_update_screen(engine, frame):
        return False
    log("")
    log("    " + "=" * 58)
    log("    ⛔ 游戏要求**更新客户端**才能继续")
    log("       「有新版本可以更新啦 / 您的当前游戏版本较低」")
    log("       这是强制的,脚本无法绕过,也**不会**去点「更新」")
    log("       (那是几 GB 的下载)。")
    log("    ➜ 请在模拟器里手动完成客户端更新后重跑。")
    log("    " + "=" * 58)
    log("")
    return True


def recognize_screen(engine, frame) -> str:
    """识别当前在哪个界面 —— 每个任务开始前先问这个,再决定要不要导航。

    用户要求的做法:"每当任务启动时应该进行一次识别,查看当前在哪,再进行任务"。

    返回值(按**由深到浅**的顺序判断,深的先判,否则会被浅的规则抢走):

      ``boss``      虚轴之庭(世界BOSS)
      ``assemble``  拼装时光关卡地图(有分页)
      ``bonus``     BONUS 关卡页(可开打)
      ``support``   助战好友列表
      ``settle``    战斗结算
      ``fracture``  多元裂缝子菜单
      ``activity``  活动页(三页签 + 推荐卡片)
      ``shop``      商店
      ``social``    社交
      ``task``      任务界面(首页的子页面)
      ``signin``    每日签到弹窗
      ``home``      主界面
      ``title``     标题/登录界面
      ``unknown``   认不出来
    """
    hits = _texts(engine, frame)

    def has(*needles, conf: float = 0.8) -> bool:
        return any(s >= conf and any(n in t for n in needles) for t, s, _ in hits)

    # 强制更新弹窗优先:它盖住一切,里面的文字可能被误判成别的界面
    if _is_force_update_screen(engine, frame):
        return "force_update"
    if has("HP", "MAZING", "WAVE", conf=0.85) and not has("战斗结算"):
        return "battle"
    if has("战斗结算", "再次挑战"):
        return "settle"
    if has("账号切换", "屏幕继续", "适龄提示", "CADPA", conf=0.7):
        return "title"
    if _is_support_list(engine, frame):
        return "support"
    # ⚠️ 必须先确认"不在活动页"!
    # 活动页上有一张叫「空想迷域」的**入口卡片**,还带「信仰检证 1/4」字样 ——
    # 所以活动页也会命中迷域的特征词(实测:event-page 命中「空想迷域」+
    # 「信仰检证」两个强特征)。判据必须区分"活动页上的入口卡片"和"迷域内部"。
    if _on_activity_page(engine, frame):
        return "activity"
    if _is_maze_screen(engine, frame):
        return "maze"
    if _is_tower_screen(engine, frame):
        return "tower"
    if _is_boss_screen(engine, frame):
        return "boss"
    # ⚠️ is_bonus 必须在 assemble 之前:关卡地图也用「2/2」分页文字,
    # 但 BONUS 关卡页的「BONUS + 消耗体力」是更强的证据。
    if _on_bonus_page(engine, frame):
        return "bonus"
    # ⚠️ task 必须在 activity 之前:「主线」任务界面左侧是
    # 任务/存在感/每周任务/使魔探险,但它也有「主线」字样,会被 activity 的
    # 三页签判据(主线/活动/挑战)命中 —— 实测就是这样把任务界面认成了活动页。
    # 而「使魔探险」页也属于任务界面,内容完全不同,所以排在 task 之前单独识别。
    if any(s >= 0.8 and any(k in t for k in
                            ("使魔探险将于", "已接取的探险数", "点击选择使魔"))
           for t, s, _ in hits):
        return "familiar"
    if _is_task_screen(engine, frame):
        return "task"
    if has("每日签到", "第6天", "第7天", "签到领取奖励", conf=0.6):
        return "signin"
    # 关卡地图:靠**分页指示**「N/M」判断。两个关键点:
    #   * 必须限制在分页区域(520,630,260,80)内 —— 否则会把顶部的体力
    #     「89/130」也当成分页(实测踩过,导致每个界面都被认成关卡地图)
    #   * OCR 会把「2/2」读成「2 /2」,所以先去空格再匹配
    for t, s, _ in _texts(engine, frame, PAGE_INDICATOR_ZONE):
        if s >= 0.6:
            compact = t.replace(" ", "")
            if "/" in compact and any(c.isdigit() for c in compact) \
                    and len(compact) <= 6:
                return "assemble"
    # 兜底:单页活动地图可能**没有分页指示**,但有「BONUS」节点标签。
    # (BONUS 关卡页已在上面判成 bonus 了,所以这里不会误判。)
    # 加这条是为了活动换代时不至于因为"新地图不分页"就整个认不出来。
    if any(s >= 0.8 and "BONUS" in t.upper() for t, s, _ in hits):
        return "assemble"
    # 关卡详情页(含「使魔的爱」这类):有消耗体力+助战入口。
    # **必须在 activity 之前判** —— 实测这些页面也会带「多元裂缝」等标题字样。
    if has("消耗体力") and has("选择助战好友", "补充体力", "快捷战斗"):
        return "stage"
    # 活动页优先于多元裂缝:活动页上也摆着「多元裂缝」入口卡片名
    if _on_activity_page(engine, frame):
        return "activity"
    if has("多元裂缝", "搏击训练室", "螺旋回廊", "曜日挑战") \
            and has("使魔的爱", "虚轴之庭"):
        return "fracture"
    if has("礼包商店", "水晶商店", "吼美礼包", "心意商店", "常驻礼包", "吼姆的交易"):
        return "shop"
    # 共鸣屋/回收屋/万神屋/崩坏屋 也是商店的子页签
    if has("共鸣屋", "崩坏屋", "回收屋", "万神屋"):
        return "shop_tab"
    if has("我的社团", "我的消息", "官方社区", "找家好友", "俺家好友"):
        return "social"
    if _at_home(engine, frame):
        return "home"
    return "unknown"


# 各任务期望的界面,以及"在哪就跳过导航"
SCREEN_LABELS = {
    "home": "主界面", "title": "标题界面", "task": "任务界面",
    "familiar": "使魔探险页",
    "maze": "空想迷域",
    "tower": "崩坏之塔",
    "force_update": "⛔ 强制更新客户端弹窗",
    "activity": "活动页", "fracture": "多元裂缝", "boss": "虚轴之庭",
    "assemble": "关卡地图", "bonus": "BONUS 关卡页", "support": "助战列表",
    "settle": "战斗结算", "battle": "战斗中", "shop": "商店",
    "shop_tab": "商店子页(共鸣屋/回收屋…)",
    "social": "社交", "signin": "每日签到", "stage": "关卡详情", "unknown": "未知界面",
}

# 出现这个界面时**任何自动化都没有意义** —— 必须停下来让用户处理
FATAL_SCREENS = {"force_update"}


def describe_screen(engine, frame) -> str:
    """给日志用的中文描述。"""
    key = recognize_screen(engine, frame)
    return SCREEN_LABELS.get(key, key)


def wait_until(inp, engine, log, condition, what: str, *, timeout_s: float = 20.0,
               poll_s: float = 0.7, save_tag: str | None = None) -> bool:
    """轮询直到 ``condition(engine, frame)`` 成立。

    这是"点击之后要等界面真的变"的统一实现。

    为什么必需:ADB 截屏约 0.4s/帧,而游戏切界面要 1~3 秒。之前每一步只是
    `click(); sleep(3)`,在**加载慢的时候**会带着旧画面进入下一步 —— 实测
    出现过"游戏还没加载完就已经开始点下一步",于是全部点空。

    这里**以画面为准**:反复截屏判断条件,成立立刻继续(快的时候不白等),
    不成立就一直等到超时(慢的时候也不误判)。超时会存现场截图便于排查。

    轮询期间也会检查停止事件:按了「停止」立刻抛 Stopped,不必等超时。

    返回是否在超时前满足条件。
    """
    deadline = time.time() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        check_stopped()                # 停止按钮可以打断等待
        frame = inp.grab()
        try:
            if condition(engine, frame):
                if attempt > 1:
                    log(f"    ✔ {what}(等了 {attempt} 次轮询)")
                return True
        except Exception:  # noqa: BLE001 - 条件函数出错不该打断流程
            pass
        if time.time() >= deadline:
            log(f"    ⚠️ 等「{what}」超时({timeout_s:.0f}s),继续但可能出错")
            if save_tag:
                try:
                    from .imaging import save_image
                    save_image(str(PROJECT / "sessions" / f"timeout_{save_tag}.png"),
                               frame)
                    log(f"       现场截图 sessions\\timeout_{save_tag}.png")
                except Exception:  # noqa: BLE001
                    pass
            return False
        time.sleep(poll_s)


def wait_text(needle: str, min_conf: float = 0.8):
    """条件:画面上出现某段文字。"""
    def cond(engine, frame) -> bool:
        return any(s >= min_conf and needle in t for t, s, _ in _texts(engine, frame))
    return cond


def wait_any(*needles: str, min_conf: float = 0.8):
    """条件:画面上出现其中任意一段文字。"""
    def cond(engine, frame) -> bool:
        hits = _texts(engine, frame)
        return any(s >= min_conf and any(n in t for n in needles)
                   for t, s, _ in hits)
    return cond


def click_and_wait(inp, engine, log, point, what: str, condition, *,
                   timeout_s: float = 20.0, save_tag: str | None = None) -> bool:
    """点一下,然后**等界面变成期望的样子**才算这一步完成。"""
    _click(inp, point, what, log)
    return wait_until(inp, engine, log, condition, f"{what} 之后",
                      timeout_s=timeout_s, save_tag=save_tag)


# 「空想迷域」(Divine Maze)左上角的返回箭头。
# 实测:这个界面很容易误入,而且**顶部导航(首页/战斗/…)点了没用**,
# 唯一出路是左上角这个箭头。用户明确要求:认出来 + 能出去 + 加入所有导航。
#
# 坐标是**从用户截图实测**出来的:箭头是浅蓝白渐变,在左上角
# x=[51,76] y=[15,47] 的范围内,中心约 (63, 31)。
# (注意:这个位置比任务界面那个「左上返回箭头」(57,140) **高得多** ——
#  迷域有自己的标题栏,箭头在标题栏里。两者不能混用。)
MAZE_BACK_ARROW = (63, 31)


def find_back_arrow(frame) -> tuple[int, int] | None:
    """在**左上角**用颜色找那个蓝色返回箭头,返回它的中心坐标。

    为什么不用 OCR:
      * 箭头是**图形按钮**,OCR 读不出
      * 更糟的是,按"右上角区域取第一个文字"的旧做法会把
        **「活动已结束,展示时间:剩余4天」这种标签**当成返回按钮,点它毫无作用
        —— 实测 `go_home` 因此死循环 6 轮都出不去

    做法:在左上角 ``(0,0)-(110,90)`` 找**蓝色占优**(B 比 R 高 60 以上)的像素团,
    面积够大就认为找到按钮。实测:
      * 活动页/使魔探险页 (57,47) 是**粉色背景图案** (239,190,181) —— 不会被误判
      * 空想迷域 (57,47) 是**蓝色** (41,117,148) —— 正确命中

    返回 ``None`` 表示没找到(调用方回退到固定坐标)。
    """
    import numpy as np
    h, w = frame.shape[:2]
    y2, x2 = min(90, h), min(110, w)
    crop = frame[0:y2, 0:x2].astype(np.int16)
    mask = (crop[:, :, 2] - crop[:, :, 0]) > 60
    ys, xs = np.nonzero(mask)
    if len(xs) < 150:            # 面积太小 -> 不是按钮,是背景噪点
        return None
    cx, cy = int(xs.mean()), int(ys.mean())
    # 过滤"顶部导航栏"的蓝条噪点:实测多元裂缝界面质心在 y=3,而真箭头
    # (空想迷域)质心在 y≈41。所以只看质心 y,太贴近顶部就判定为噪点。
    if cy < 12:
        return None
    return cx, cy


def _is_maze_screen(engine, frame) -> bool:
    """是否是**「空想迷域」内部**(不是活动页上那张入口卡片)。

    判据:命中 ≥2 个迷域特征词:
    ``空想迷域 / DIVINE / 赛季主题 / 信仰检证 / 距离赛季轮换 / 赋格等级 / OS-``

    ⚠️ **不能单独用它判断界面**,必须先排除活动页:
      活动页上摆着一张叫「空想迷域」的入口卡片,还带「信仰检证 1/4」字样,
      所以活动页也会命中这些特征词(实测命中 2 个)。
      这个函数只回答"这些词在不在",**"是不是迷域内部"由
      `recognize_screen()` 在排除活动页之后决定**。
    """
    hits = _texts(engine, frame)
    # ⚠️ 判据要挑**迷域内部**才有的词,不能只看活动页也有的卡名。
    # 实测 mazes 截图命中 16 个弱特征,活动页一个都没有 —— 「赋格等级」和
    # 「OS-xx-XX」编号是关键区分。所以要求:
    #   * 「赋格等级」或 ≥2 个 OS- 编号(迷域内部独有),并且
    #   * 至少再有一个迷域特征词(空想迷域/赛季主题/信仰检证…)
    maze_only = 0
    other = 0
    for t, s, _ in hits:
        if s < 0.7:
            continue
        up = t.upper()
        if "赋格等级" in t or up.startswith("OS-"):
            maze_only += 1
        elif any(k.upper() in up for k in ("空想迷域", "DIVINE", "赛季主题",
                                           "信仰检证", "距离赛季轮换", "行皆宜")):
            other += 1
    return maze_only >= 2 and other >= 1


def exit_maze(inp, engine, log) -> bool:
    """从「空想迷域」退出去 —— 点**左上角箭头** ``(57,140)``。

    实测这个界面点顶部导航(首页/战斗)无效,只有左上角箭头能出去。
    退一次不够时(可能退到子页面)会连点两次。
    """
    log("    检测到「空想迷域」界面 -> 点左上角箭头退出")
    for i in range(2):
        _click(inp, MAZE_BACK_ARROW, f"空想迷域 左上返回(第{i+1}次)", log)
        time.sleep(1.8)
        frame = inp.grab()
        if not _is_maze_screen(engine, frame):
            log("    已退出空想迷域")
            return True
    log("    ⚠️ 两次返回后仍在空想迷域")
    return False


def click_back_button(inp, engine, log, times: int = 1) -> bool:
    """点右上角那个「返回上一级」方框 —— **使用纯坐标**。

    用户说明:「这里的字会变但是位置不变,当其他导航找不到时使用」。

    实测这个按钮:
      * 位置固定在 **(1100, 156)** 附近(按钮中心约 (1100, 157))
      * **文字随所在页面变化** —— 在关卡地图上显示「拼装时光」、在多元裂缝里
        显示「多元裂缝」、在虚轴之庭里显示「虚轴之庭」…所以**不能按文字找**
      * 它才是深层界面的唯一出路(顶部导航「首页/战斗」在这些页面里点不动)

    所以这里**优先直接用坐标点**,不依赖 OCR。仅当日志需要时读一下上面的字
    作为描述。``times`` 为连点次数(逐级返回)。

    返回是否点过(坐标点击几乎总能成功)。
    """
    ok_any = False
    for i in range(times):
        # ⚠️ 直接点右上角坐标(这是深层界面的唯一返回按钮)。左上角颜色箭头
        # 只在极少数界面(空想迷域)存在,而且 `find_back_arrow` 会对背景噪点
        # 误报 (67,3) —— 所以这里**不再先查颜色箭头**,只读一下按钮上的字
        # 作日志描述。
        frame = inp.grab()
        label = "?"
        for t, s, _ in _texts(engine, frame, BACK_BUTTON_ZONE):
            if s >= 0.6:
                label = t
                break
        log(f"    点右上角返回按钮 ({BACK_BUTTON[0]}, {BACK_BUTTON[1]})"
            + (f"  [上面写着「{label}」]" if label != "?" else "  [纯坐标]"))
        _click(inp, BACK_BUTTON, "右上角返回(坐标)", log)
        ok_any = True
        time.sleep(1.8)
    return ok_any


def go_home(inp, engine, log, tries: int = 6) -> bool:
    """把游戏带回**已知状态**:主界面。

    这是所有导航的起点,也是级联失败的教训:一旦游戏停在某个未知子界面,
    后面每一个"找东西"的任务都会失败 —— 多元裂缝、清体力、存在感、社团
    全挂,而日志里只看到"找不到某某"。

    顺序(重要,每一步都实测过):
      1. 已在主界面 -> 返回
      2. **先关浮层** —— 浮层会挡住点击!
         实测:活动页挂着「玩家信息」浮层时,点顶部「首页」无效,
         于是反复点首页 + 反复点右上角,死循环 6 轮都出不去。
      3. 空想迷域 -> 点左上角箭头(用颜色检测,见 `find_back_arrow`)
      4. 虚轴之庭 -> 右上角方框逐级返回
      5. 任务界面 -> 左上返回箭头(它点「首页」退不出去)
      6. 其它深层界面 -> 右上角方框 / 颜色检测到的箭头
      7. 都没有 -> 点「首页」,再不行就用纯坐标兜底
    """
    for attempt in range(1, tries + 1):
        frame = inp.grab()
        # ⛔ 强制更新弹窗:点了也没用,直接放弃(避免死循环刷日志)
        if _is_force_update_screen(engine, frame):
            check_force_update(engine, frame, log)
            log("    !! 无法回到主界面(游戏要求先更新客户端)")
            return False
        if _at_home(engine, frame) and not _is_boss_screen(engine, frame) \
                and not _is_maze_screen(engine, frame):
            log("    已在主界面")
            return True

        # ⚠️ 第 2 步:先关掉挡路的浮层。浮层会吞掉点击,导致后面所有导航无效。
        # 但 go_home 里不需要等"连续 3 次干净"——只要关掉当前挡路的那个就行,
        # 关完立刻尝试导航,别在这里耗太久。所以传较小的轮次。
        if close_popups(inp, engine, log, rounds=4):
            continue

        # 空想迷域:顶部导航无效,只有左上角箭头能出去
        if _is_maze_screen(engine, frame):
            log(f"    检测到空想迷域,点左上角箭头退出(第 {attempt} 次)")
            exit_maze(inp, engine, log)
            continue
        if _is_boss_screen(engine, frame):
            log(f"    检测到虚轴之庭界面,用右上角按钮退出(第 {attempt} 次)")
            back_out(inp, engine, log, times=2)
            continue
        # 任务界面是「首页」下的子页面,点「首页」不会退出 —— 必须点左上返回箭头
        if _is_task_screen(engine, frame):
            log(f"    检测到任务界面,点返回箭头退出(第 {attempt} 次)")
            click_and_wait(inp, engine, log, BACK_ARROW, "返回(离开任务界面)",
                           lambda e, f: _at_home(e, f) and not _is_task_screen(e, f),
                           timeout_s=12, save_tag="leave_task")
            continue

        log(f"    点「首页」导航(第 {attempt}/{tries} 次)")
        # 点击后**等界面真的变**,而不是睡固定秒数 —— 加载慢时固定睡眠会让
        # 下一步带着旧画面执行
        click_and_wait(inp, engine, log, HOME_NAV, "首页",
                       lambda e, f: _at_home(e, f) and not _is_boss_screen(e, f)
                       and not _is_task_screen(e, f),
                       timeout_s=12, save_tag="home")
        frame = inp.grab()
        if _at_home(engine, frame) and not _is_boss_screen(engine, frame) \
                and not _is_task_screen(engine, frame):
            log("    回到主界面")
            return True

        # 还在深层界面:先把界面识别出来,按类型选出路
        screen = recognize_screen(engine, frame)
        log(f"    首页导航无效,当前是「{SCREEN_LABELS.get(screen, screen)}」"
            f" -> 逐级返回")
        # 关卡详情/BONUS/结算页:左上角返回箭头 (57,140) 返回
        # (右上角是活动标题标签,点它没反应 —— 实测新活动「海妖秘境」)
        if screen in ("bonus", "stage", "settle"):
            log(f"    当前在「{SCREEN_LABELS.get(screen, screen)}」,点左上角返回")
            _click(inp, BACK_ARROW, "左上角返回", log)
            time.sleep(2.0)
            continue
        # 活动地图 / 多元裂缝 / 虚轴之庭 / 助战列表:用右上角方框逐级退出。
        # 用户指定:字会变、位置不变,读不到字就用纯坐标。
        if screen in ("assemble", "fracture", "boss", "support"):
            back_out(inp, engine, log, times=1)
            continue
        # ⚠️ 顶部导航栏齐全的界面,点「首页」本应能回主界面;这里走到说明
        # 首页导航没生效(可能是活动页右上角那个活动入口标签干扰了识别)。
        # 但**不能用 back_out 点右上角** —— 实测活动页右上角(1168,157)是
        # 「崩坏之塔」/「拼装时光」等入口,点它越陷越深(死循环)。再点一次首页。
        if screen in ("activity", "tower", "shop", "shop_tab", "social"):
            log(f"    当前在「{SCREEN_LABELS.get(screen, screen)}」,再点「首页」")
            click_and_wait(inp, engine, log, HOME_NAV, "首页",
                           lambda e, f: _at_home(e, f) and not _is_task_screen(e, f),
                           timeout_s=10, save_tag="home2")
            continue
        # 空想迷域是特殊界面:右上角方框无效,只有左上角箭头能出去
        if screen == "maze":
            exit_maze(inp, engine, log)
            continue
        # 认不出来的界面:坐标兜底也试一次(这是用户的明确要求)
        log("    界面未知 -> 用纯坐标返回兜底")
        click_back_button(inp, engine, log, times=1)
        frame = inp.grab()
        if _at_home(engine, frame) and not _is_boss_screen(engine, frame):
            log("    回到主界面")
            return True

        # 关弹窗
        hits = _texts(engine, frame)
        for lbl in ("确定", "确认", "继续", "跳过", "关闭", "返回"):
            hit = _find_button(hits, (lbl,))
            if hit:
                log(f"    关闭浮层:'{lbl}'")
                _click(inp, hit[2], f"'{lbl}'", log)
                time.sleep(2.0)
                break

    frame = inp.grab()
    ok = _at_home(engine, frame) and not _is_boss_screen(engine, frame)
    if not ok:
        log("    !! 无法回到主界面")
    return ok


def _at_home(engine, frame) -> bool:
    """判断是否在主界面(已知状态)。

    误判历史(两次都栽在同一个思路上,所以判据一步步收紧):
      1. 只看「首页」-> 别的界面也有
      2. 看导航六页签齐全 -> **深层界面(虚轴之庭)也有完整导航栏**
      3. 再看"没有大段对话文字" -> **主界面本身就有角色对话**,误伤

    现在的判据:导航六页签齐全 **且左侧有主界面独有的一列按钮**。
    主界面左列有 5 个图标按钮(笔记本/对话/星星/头像/方块),而任务界面、
    活动页、虚轴之庭这些地方左列是页签或没有,靠这个区分。
    """
    hits = _texts(engine, frame)
    need = ("首页", "战斗", "装备", "商店", "社交", "其他")
    found = sum(1 for n in need
                if any(s >= 0.85 and t.strip() == n for t, s, _ in hits))
    if found < 5:
        return False
    # 排除明显不是主界面的界面(它们的特征文字)。
    # 关卡地图有分页指示(在固定区域),主界面没有 —— 用区域判断,避免误伤。
    # ⚠️ 礼包商店内部也要排除:它顶部导航栏齐全,之前 not_home 里没有它的
    # 独有词(吼美礼包/购买/限购),导致 _at_home 误判为主界面,go_home 直接
    # 返回"已在主界面",实际还停在礼包商店里 —— 实测共鸣屋任务因此跳过。
    not_home = ("存在感", "每周任务", "使魔探险", "今天没有任务", "主线", "挑战",
                "开放", "LEVEL", "难度指数", "剩余时间", "消耗体力", "一键领取",
                "BONUS", "礼包商店", "我的社团", "我的消息",
                # 商店商品页(礼包商店/吼美礼包内部)独有词
                "吼美礼包", "心意商店", "常驻礼包", "月卡礼包", "限购", "购买",
                "期间限购", "剩余", "月卡",
                # 登录广告「超绝心愿/梦想祈愿」+「看板礼包/限时发售」:
                # 它们都盖在主界面上(顶部导航栏仍可见),不排除的话 _at_home
                # 会误判成主界面,任务按钮一直点不动(实测卡死)。
                "超绝心愿", "梦想祈愿", "看板礼包", "限时发售", "今日不再提示")
    for t, s, _ in hits:
        if s < 0.8:
            continue
        if any(k in t for k in not_home):
            return False
    for t, s, _ in _texts(engine, frame, PAGE_INDICATOR_ZONE):
        if s >= 0.6:
            compact = t.replace(" ", "")
            if "/" in compact and any(c.isdigit() for c in compact) \
                    and len(compact) <= 6:
                return False
    return True


def goto_activity_map(inp, engine, log, tries: int = 3) -> bool:
    """从任意界面导航到**活动关卡地图**(难度B活动),不找 BONUS 节点。

    这是「清体力」的第一阶段:先到关卡地图,好让首通清理(sweep)和 BONUS
    清体力接着做。抽出来是为了让首通清理复用同一段导航。
    """
    for attempt in range(1, tries + 1):
        frame = inp.grab()
        screen = recognize_screen(engine, frame)
        if screen == "assemble":
            log("    已在活动关卡地图")
            return True
        if screen == "bonus":
            log("    已在 BONUS 关卡页(无需再进地图)")
            return True

        log(f"    导航到活动页(第 {attempt}/{tries} 次)")
        if not go_home(inp, engine, log):
            log("    !! 回不到主界面")
            continue
        if not goto_activity(inp, engine, log):
            log("    !! 到不了活动页")
            continue

        # 找「难度指数：B」的活动卡片(不按名字,名字会随赛季换)
        #
        # ⚠️ 活动页可能**同时有多个难度 B**(实测出现过 2 个),旧的"只挑置信度
        # 最高的"会选错活动。现在拿到全部候选,**逐个尝试点击**,哪个能进关卡
        # 地图就用哪个 —— 这样活动换代、多活动并存都能自动扛住。
        cands = []
        for i in range(4):
            cands = find_difficulty_cards(engine, inp.grab(), "B")
            if cands:
                break
            log(f"    左列下翻找「难度指数：B」({i+1}/4)")
            inp.drag(430, 620, 430, 200, duration=0.8, steps=25)
            time.sleep(2.5)
        if not cands:
            log("    !! 活动页找不到「难度指数：B」的活动卡片"
                "(活动可能已结束、或新活动不是难度B)")
            continue
        log(f"    找到 {len(cands)} 个「难度指数：B」候选:")
        for t, s, pt in cands:
            log(f"       「{t}」 conf={s:.2f} -> 点 {pt}")

        entered = False
        for t, s, click_at in cands:
            click_and_wait(inp, engine, log, click_at, f"难度B卡片「{t}」",
                           lambda e, f: recognize_screen(e, f) == "assemble",
                           timeout_s=20, save_tag="assemble_map")
            if recognize_screen(engine, inp.grab()) == "assemble":
                log(f"    ✓ 通过「{t}」进入关卡地图")
                entered = True
                break
            log(f"    点「{t}」没进关卡地图,试下一个候选")
        if entered:
            return True
        log("    所有难度B候选都点不进关卡地图")
    return False


def goto_bonus_stage(inp, engine, log, tries: int = 3) -> bool:
    """从**任意界面**导航到「拼装时光 BONUS」关卡页。

    这是之前完全缺失的一环 —— `clear_stamina` 只会从结算界面续关,一旦不在
    结算界面就彻底没辙(实测两次清体力都是 0 次、3 秒就"完成")。

    实测路径:
      首页 -> 战斗 -> 活动页签 -> 「拼装时光」卡片
      进入后是关卡地图;BONUS 在**第 2 页**(点右箭头翻页)
      点「BONUS」文字**下方 57px** 的节点
    """
    for attempt in range(1, tries + 1):
        # 目的地有三个层级,任一到达都**不要再导航**:
        #   1) BONUS 关卡页(已可开打)
        #   2) 拼装时光**关卡地图** —— 上面直接有 BONUS 节点,点它就行
        #   3) 活动页 —— 需要点「拼装时光」卡片进去
        #
        # ⚠️ 之前只认 1),于是在关卡地图上一直去回主界面找「活动」页签,
        # 反复超时。用户指出:**已经在关卡地图上时,BONUS 节点就在眼前**,
        # 应该直接点(这正是他早先教的"右上角按钮=返回"的同一层逻辑)。
        frame = inp.grab()
        screen = recognize_screen(engine, frame)
        if screen == "bonus":
            log("    已在 BONUS 关卡页")
            return True
        if screen == "assemble":
            log("    已在拼装时光关卡地图,直接找 BONUS 节点")
            if _click_bonus_node(inp, engine, log):
                return True
            log("    本页没有 BONUS,翻页再找")
            for _ in range(3):
                click_and_wait(inp, engine, log, PAGE_NEXT, "右翻页",
                               lambda e, f: True, timeout_s=2.0)
                if _click_bonus_node(inp, engine, log):
                    return True
            log("    !! 关卡地图上翻遍页面都没找到 BONUS 节点")

        log(f"    导航到活动页(第 {attempt}/{tries} 次)")
        # 先回已知状态,否则可能停在未知子界面里怎么找都找不到
        if not go_home(inp, engine, log):
            log("    !! 回不到主界面")
            continue
        if not goto_activity(inp, engine, log):
            log("    !! 到不了活动页")
            continue

        # 找「拼装时光」卡片(要求完整识别,避开右上角同名标签)
        card = None
        for i in range(4):
            card = find_difficulty_card(engine, inp.grab(), "B")
            if card:
                break
            log(f"    左列下翻找「难度指数：B」({i+1}/4)")
            inp.drag(430, 620, 430, 200, duration=0.8, steps=25)
            time.sleep(2.5)
        if not card:
            log("    !! 活动页找不到「难度指数：B」的活动卡片")
            continue
        t, s, click_at = card
        log(f"    找到「难度指数：B」卡片(文字「{t}」置信 {s:.2f},点 {click_at})")
        # 等关卡地图出现(有 BONUS 标签或分页指示)才继续
        click_and_wait(inp, engine, log, click_at, "难度B活动卡片",
                       lambda e, f: any(s >= 0.8 and ("BONUS" in t.upper()
                                                      or "/" in t and "2" in t)
                                        for t, s, _ in _texts(e, f)),
                       timeout_s=25, save_tag="assemble_map")

        # 翻到第 2 页找 BONUS(BONUS 在第 2 页)
        for flip in range(3):
            hits = _texts(engine, inp.grab())
            bon = _find(hits, "BONUS", 0.8)
            if bon:
                x, y = bon[2][0], bon[2][1] + BONUS_LABEL_BELOW
                log(f"    点 BONUS 节点 ({x}, {y})")
                # 等关卡页真正出现(「消耗体力」+「选择助战好友」)才算进关成功
                ok = click_and_wait(inp, engine, log, (x, y), "BONUS 节点",
                                    lambda e, f: (_find(_texts(e, f), "消耗体力", 0.8)
                                                  and _find(_texts(e, f),
                                                            "选择助战好友", 0.8)),
                                    timeout_s=25, save_tag="bonus_stage")
                if ok:
                    log("    已进入 BONUS 关卡页")
                    return True
                log("    点了 BONUS 但没进关卡页")
            click_and_wait(inp, engine, log, PAGE_NEXT, "右翻页",
                           lambda e, f: True, timeout_s=2.0)
        log("    翻遍页面都没找到 BONUS")
    return False


def _click_bonus_node(inp, engine, log) -> bool:
    """在当前关卡地图上点「BONUS」节点(文字下方 57px 处)。

    关卡地图上 BONUS 是个带皇冠的卡片,文字标签在上方,**标签本身不可点**,
    要点它下方约 57px 的节点(实测)。
    点完等关卡页出现,成功则返回 True。
    """
    hits = _texts(engine, inp.grab())
    bon = _find(hits, "BONUS", 0.8)
    if not bon:
        return False
    x, y = bon[2][0], bon[2][1] + BONUS_LABEL_BELOW
    log(f"    点 BONUS 节点 ({x}, {y})")
    ok = click_and_wait(inp, engine, log, (x, y), "BONUS 节点",
                        lambda e, f: _on_bonus_page(e, f),
                        timeout_s=25, save_tag="bonus_stage")
    if ok:
        log("    已进入 BONUS 关卡页")
    return ok


def goto_activity(inp, engine, log, tries: int = 3) -> bool:
    """导航到**活动页**,并确认真的到了。

    实测教训:全流程跑到这一步时,点「战斗」进的是**上次停留的子页面**,
    所以找不到「活动」页签;而旧代码用 `if act:` 包着,找不到就静默跳过,
    接着在错误的界面上滚动 —— 日志里只看到"找不到多元裂缝卡片",
    完全看不出是导航没到位。

    这里每一步都验证,失败就明确报出来。
    """
    for attempt in range(1, tries + 1):
        if _on_activity_page(engine, inp.grab()):
            log("    已在活动页")
            return True
        if not go_home(inp, engine, log):
            log("    !! 回不到主界面")
            continue

        # 点「战斗」后**快速分流**:点「战斗」可能直接进到上次停留的子页面
        # (实测:直接进了活动页 / 关卡地图,而不是停在"主线/活动/挑战"三页签
        # 那个选择页)。所以不要傻等三页签齐全,而是先识别"现在到哪了":
        #   * 已在活动页     -> 直接用
        #   * 已进关卡地图等深层界面 -> 点右上角返回按钮退回(快速,别等超时)
        #   * 三页签齐全     -> 再点「活动」
        _click(inp, BATTLE_NAV, "导航 战斗", log)
        time.sleep(2.0)
        frame = inp.grab()
        screen = recognize_screen(engine, frame)

        if screen == "activity":
            log("    点战斗后已直接在活动页")
            return True

        # 直接进到了别的深层界面 -> 退回(关卡详情用左上角,其余用右上角)
        if screen in ("bonus", "stage"):
            log(f"    点战斗后进了「{SCREEN_LABELS.get(screen, screen)}」"
                f" -> 点左上角返回按钮退回")
            _click(inp, BACK_ARROW, "左上角返回", log)
            time.sleep(2.0)
            continue
        if screen in ("assemble", "fracture", "boss", "maze", "tower"):
            log(f"    点战斗后进了「{SCREEN_LABELS.get(screen, screen)}」"
                f" -> 点右上角返回按钮退回")
            back_out(inp, engine, log, times=1)
            continue

        # 三页签齐全 -> 点「活动」
        tabs = sum(1 for n in ("主线", "活动", "挑战")
                   if any(s >= 0.85 and t.strip() == n
                          for t, s, _ in _texts(engine, frame)))
        if tabs >= 3:
            hits = _texts(engine, frame)
            act = _find_button(hits, ("活动",)) or _find(hits, "活动", 0.85)
            if act:
                click_and_wait(inp, engine, log, act[2], "页签 活动",
                               lambda e, f: _on_activity_page(e, f),
                               timeout_s=20, save_tag="activity_page")
                if _on_activity_page(engine, inp.grab()):
                    log("    已进入活动页")
                    return True
                log(f"    点了「活动」但界面不对(第 {attempt}/{tries} 次)")
                continue

        log(f"    !! 点战斗后没进入预期界面(第 {attempt}/{tries} 次),"
            f"当前是「{SCREEN_LABELS.get(screen, screen)}」")
    return False


def _on_activity_page(engine, frame) -> bool:
    """确认在活动页。

    **判据(只用页签,不依赖卡片内容)**:
      1. 画面里确实有写着「活动」的页签
      2. 左侧三个页签 主线/活动/挑战 都在

    ⚠️ 曾经还额外要求「RECOMMEND / 难度指数 / 剩余时间」之一,结果卡片滚走
    之后这些文字读不到 → 活动页被判成"未知界面" → `go_home` 反复点首页、
    点右上角返回,**死循环回不到主界面**(实测卡了 6 轮)。页签才是稳定的
    结构特征,卡片内容会随滚动变化,不能当判据。

    ⚠️ 调用前应先排除**任务界面**:「主线」任务界面也有「主线」字样。
    """
    hits = _texts(engine, frame)
    if _is_task_screen(engine, frame):
        return False
    # 「活动」页签必须**确实写着「活动」** —— 任务界面那三个是任务/存在感/
    # 每周任务,不含「活动」。这样即使顺序被改错也不会误判。
    has_activity_tab = any(s >= 0.85 and t.strip() == "活动" for t, s, _ in hits)
    if not has_activity_tab:
        return False
    tabs = sum(1 for n in ("主线", "活动", "挑战")
               if any(s >= 0.85 and t.strip() == n for t, s, _ in hits))
    return tabs >= 3


def _find_card(engine, frame, needle, min_conf: float = 0.85,
               require_full: bool = True):
    """在**内容区**找卡片,避开右上角的页面标签与顶部导航。

    必要性:多元裂缝和拼装时光的页面右上角都有一块**同名标签**,文字与入口
    卡片完全一样。不限制区域时 `_find_button` 会选中那块标签 —— 实测「点击
    多元裂缝 (1167,156)」点的是标签,导致后续全找不到。

    ``require_full``(默认开):要求识别到的文字**等于**目标串(如「多元裂缝」
    四个字必须全中),用于"识别全了才允许点击"的场合。OCR 偶尔只读出一部分
    (例如「多元裂」「元裂缝」),那种情况不允许点,以免点到别的东西。
    """
    hits = []
    for t, s, l in _texts(engine, frame, CARD_AREA):
        if s < min_conf:
            continue
        if require_full:
            if t.strip() == needle:
                hits.append((t, s, l))
        elif needle in t:
            hits.append((t, s, l))
    if not hits:
        return None
    hits.sort(key=lambda h: -h[1])         # 置信度最高优先
    return hits[0]


def find_difficulty_cards(engine, frame, grade: str = "B",
                          min_conf: float = 0.85) -> list:
    """在活动页按**难度标记**找**所有**活动卡片(不只一个),按置信度降序。

    为什么需要复数版本:活动页可能**同时存在多个「难度指数：B」**——
    实测(2026-10-18 的 activity 布局)就有两个:
        (994, 180) conf=0.995   ← 另一个 B 活动
        (472, 462) conf=0.984   ← 目标活动
    旧版 `find_difficulty_card` 只挑置信度最高的,会选中**错的那个**。
    现在调用方拿到全部候选,逐个尝试点击,哪个能进关卡地图就用哪个。

    返回 ``[(text, score, click_xy), ...]``,点击位置是难度标记**下方约 45px**
    的卡片本体(标签本身不可点,和 BONUS 节点同理)。

    ⚠️ **用全屏 OCR 再按坐标过滤,不要用 CARD_AREA 做 ROI 裁剪**:
    实测 ROI 裁剪会让靠近边缘的文字被截断 —— 同一个「难度指数：B」在全屏下
    conf=0.995 完整识别,裁进 ROI 后变成「难度指数」(冒号和 B 都没了),
    于是候选被漏掉。全屏识别 + 坐标过滤更可靠。
    """
    rx, ry, rw, rh = CARD_AREA
    out = []
    for t, s, l in _texts(engine, frame):
        if s < min_conf:
            continue
        x, y = l
        if not (rx <= x <= rx + rw and ry <= y <= ry + rh):
            continue
        # 「难度指数:B」或 OCR 拆成「难度指数」+「B」的情况都覆盖
        if ("难度指数" in t and grade in t) or t.strip() == grade:
            out.append((t, s, (x, y + DIFFICULTY_CARD_BELOW)))
    out.sort(key=lambda x: -x[1])
    return out


def find_difficulty_card(engine, frame, grade: str = "B",
                         min_conf: float = 0.85):
    """兼容旧接口:返回置信度最高的**单个**难度卡片候选(或 None)。

    新代码请用 `find_difficulty_cards` —— 活动页可能同时有多个难度 B。
    """
    cards = find_difficulty_cards(engine, frame, grade, min_conf)
    return cards[0] if cards else None


# 难度标记下方这个像素距离是卡片本体(可点)。实测「难度指数：B」(460,462)
# 到卡片名「拼装时光」(316,507) 中心差 45px,取卡片偏上的点即可命中。
DIFFICULTY_CARD_BELOW = 45


def run_familiar_daily(inp, engine, log, dry: bool = False) -> bool:
    """使魔的爱(每日一次)。

    路径:首页 → 战斗 → 活动页签 → 右列下翻找「多元裂缝」卡片 → 点卡片进入
    → 点「使魔的爱」**图标**(标签下方约 63px,标签本身不可点)→ 快捷战斗。

    ⚠️ 两个实测坑:
      * 活动页**右上角有一块同名标签**「多元裂缝」,直接按文字找会点到它而
        不是入口卡片 —— 所以必须用 `_find_card` 限制在内容区。
      * 进过一次之后会留下详情浮层,必须先返回才能点别的卡片。
    """
    # ① 先识别当前在哪,再决定要不要导航(用户要求)
    screen = preflight(inp, engine, log, "使魔的爱")
    if screen == "stage":
        # 已经在某个关卡详情页 —— 可能是上次留下的,先退出去再走正常路径。
        # 实测:关卡页点**左上箭头**会切到主线任务界面,点**右上角方框**才是
        # 回上一级(活动页),所以这里用右上角那个。
        log("    当前在关卡详情页,先退回")
        click_back_button(inp, engine, log, times=1)
        preflight(inp, engine, log, "使魔的爱")
    # 导航到多元裂缝菜单(复用统一实现,不再重复一份滚动逻辑)
    if not goto_fracture(inp, engine, log):
        log("    !! 到不了多元裂缝")
        return False

    # 使魔的爱:点标签下方 63px 的图标
    hits = _texts(engine, inp.grab())
    lab = _find_button(hits, ("使魔的爱",)) or _find(hits, "使魔的爱", 0.85)
    if not lab:
        log("    !! 多元裂缝里找不到「使魔的爱」")
        return False
    x, y = lab[2][0], lab[2][1] + 63
    log(f"    点「使魔的爱」图标 ({x}, {y})")
    # 点图标后**等关卡页真的加载出来**(「消耗体力」出现)才继续
    if not click_and_wait(inp, engine, log, (x, y), "使魔的爱图标",
                          wait_text("消耗体力"), timeout_s=25,
                          save_tag="familiar_stage"):
        log("    !! 没进入「使魔的爱」关卡页")
        return False

    frame = inp.grab()
    hits = _texts(engine, frame)
    quick = _find_button(hits, ("快捷战斗",)) or _find(hits, "快捷战斗", 0.8)
    if quick:
        # 点快捷战斗后等结算出现
        click_and_wait(inp, engine, log, quick[2], "快捷战斗",
                       wait_any("战斗结算", "获得奖励", "再次挑战", "确定"),
                       timeout_s=120, save_tag="familiar_result")
        _dismiss(inp, engine, log, rounds=3, also=("确定", "确认"))
        log("    使魔的爱:完成")
    else:
        # 体力/次数不足时按钮会变成「补充体力」—— 跳过而不是失败
        check_can_fight(engine, frame, log, "使魔的爱")
        log("    !! 没有「快捷战斗」按钮,原因未知")
    return True


def goto_social(inp, engine, log, tries: int = 3) -> bool:
    """导航到社交界面(我的消息 / 我的社团 的入口页)。

    实测路径:主界面 -> 社交 (1104,80)。

    ⚠️ 和收菜/共鸣屋一样的坑:之前 `claim_social` 里只是"回主界面然后点社交"
    一次,没有验证真的到了;失败时后续步骤会在错误界面上找按钮。
    """
    for i in range(tries):
        frame = inp.grab()
        if recognize_screen(engine, frame) == "social":
            log("    已在社交界面")
            return True
        if not go_home(inp, engine, log):
            log("    !! 回不到主界面")
            continue
        ok = click_and_wait(inp, engine, log, SOCIAL_NAV, "导航 社交",
                            lambda e, f: any(s >= 0.85 and
                                             any(k in t for k in ("我的社团",
                                                                  "我的消息"))
                                             for t, s, _ in _texts(e, f)),
                            timeout_s=20, save_tag="social_page")
        if ok and recognize_screen(engine, inp.grab()) == "social":
            return True
        log(f"    社交导航没成功(第 {i+1}/{tries} 次)")
    return False


def goto_fracture(inp, engine, log, tries: int = 3) -> bool:
    """导航到**多元裂缝子菜单**(使魔的爱 / 虚轴之庭 的入口页)。

    实测路径:
        主界面 -> 战斗 (770,80) -> 活动页签 -> **右列**下翻找「多元裂缝」卡片
        -> 点卡片进入

    实测要点:
      * 活动页左右列**各有独立滚动条**,必须用 drag 在右列内滚(x≈950)
      * 用 `_find_card` 而不是 `_find` —— 活动页右上角有**同名标签**,
        按文字找会点到那里而不是入口卡片

    用户要求:「已经在 BONUS 界面时不要重复导航」同理 —— 已经在多元裂缝
    或活动页时只做必要的部分。
    """
    for attempt in range(1, tries + 1):
        frame = inp.grab()
        screen = recognize_screen(engine, frame)
        if screen == "fracture":
            log("    已在多元裂缝菜单")
            return True

        if screen != "activity":
            if not go_home(inp, engine, log):
                log("    !! 回不到主界面")
                continue
            if not goto_activity(inp, engine, log):
                log("    !! 到不了活动页")
                continue
        else:
            log("    已在活动页")

        # 找「多元裂缝」卡片:**先向上滚几次回顶部(重置),再向下拉找**。
        #
        # 这是用户给的方案,比"先向下再回翻"稳健得多:
        #   * 活动页右列会**残留上一次的滚动位置**(从关卡地图/其它页面返回时)
        #   * 直接向下翻,可能从半山腰开始,越翻越远,永远错过卡片
        #   * 先把右列拉回顶部(重置),再从顶部往下找,卡片一定在
        #
        # 方向:手指下滑(drag 200->620)=回顶;手指上滑(drag 620->200)=向下找。
        frac = None
        # 第一步:向上滚回顶部(重置右列)
        for i in range(3):
            frac = _find_card(engine, inp.grab(), "多元裂缝")
            if frac:
                break
            inp.drag(950, 200, 950, 620, duration=0.6, steps=20)
            time.sleep(1.5)
        # 第二步:从顶部向下拉,逐次找
        if not frac:
            for i in range(8):
                frac = _find_card(engine, inp.grab(), "多元裂缝")
                if frac:
                    log(f"    向下拉时找到「多元裂缝」卡片(第 {i+1} 步)")
                    break
                log(f"    右列下翻找「多元裂缝」({i+1}/8)")
                inp.drag(950, 620, 950, 200, duration=0.6, steps=20)
                time.sleep(1.6)
        if not frac:
            log(f"    !! 重置+下翻都找不到「多元裂缝」入口卡片(第 {attempt}/{tries} 次)")
            continue
        log(f"    找到「多元裂缝」卡片 {frac[2]}(完整识别四字)")

        click_and_wait(inp, engine, log, frac[2], "多元裂缝",
                       lambda e, f: recognize_screen(e, f) == "fracture",
                       timeout_s=25, save_tag="fracture_menu")
        if recognize_screen(engine, inp.grab()) == "fracture":
            return True
        log("    点了卡片但没进多元裂缝")
    return False


def run_world_boss(inp, engine, log, times: int = 3) -> int:
    """虚轴之庭(世界BOSS),打 ``times`` 次(受今日免费次数限制)。

    用户要求:**打完一次后不返回活动界面,原地继续**;打够次数就停。

    实测(2026-10-20 验证):
      * 免费次数还在时,详情页底部有「快捷战斗」(996,650),点它 -> 数量对话框
        -> 确定 -> 战斗。
      * 打完点结算的「确定」(787,616)后,界面**回到详情页原地**,下一次直接
        继续打,不需要返回多元裂缝找卡片(之前每轮都点 BACK_ARROW 返回,
        第 2 次就"找不到虚轴之庭卡片")。
      * 免费次数用完后,详情页变成「消耗体力N」+「选择助战好友」(按钮点不动)。
        **用户明确要求:打够次数就停,不消耗体力继续** —— 所以直接结束。
      * 只有**第一次**需要从多元裂缝点「虚轴之庭」卡片进 BOSS 主界面,
        再点「出击」进详情页。
    """
    done = 0

    # 先确保在多元裂缝菜单里(第一次找卡片需要)
    screen = preflight(inp, engine, log, "虚轴之庭")
    if screen != "fracture" and not _is_boss_screen(engine, inp.grab()):
        if not goto_fracture(inp, engine, log):
            log("    !! 到不了多元裂缝,跳过虚轴之庭")
            return 0

    def _has_quick(frame_) -> bool:
        """详情页才有底部「快捷战斗」(y>600);BOSS 主界面只有「出击」。"""
        return any(s >= 0.85 and t.strip() == "快捷战斗" and loc[1] > 600
                   for t, s, loc in _texts(engine, frame_))

    # 第一次:多元裂缝点「虚轴之庭」卡片 -> BOSS 主界面 -> 点「出击」-> 详情页
    if not _has_quick(inp.grab()):
        if not _is_boss_screen(engine, inp.grab()):
            card = _find_card(engine, inp.grab(), "虚轴之庭")
            if not card:
                log("    !! 找不到「虚轴之庭」卡片")
                return 0
            click_and_wait(inp, engine, log, card[2], "虚轴之庭",
                           lambda e, f: _is_boss_screen(e, f),
                           timeout_s=25, save_tag="boss_screen")
        # BOSS 主界面 -> 点「出击」(纯图形按钮,OCR 读不到,用固定坐标)进详情页
        _click(inp, ATTACK_BUTTON, "出击", log)
        wait_until(inp, engine, log, wait_any("快捷战斗", "选择助战好友", "开战"),
                   "出击之后的详情页", timeout_s=25, save_tag="boss_detail")

    # 之后每轮都在当前 BOSS 界面原地打:快捷战斗 -> 数量确定 -> 战斗 -> 结算
    for i in range(1, times + 1):
        frame = inp.grab()
        hits = _texts(engine, frame)

        # 数量对话框的「确定」已经在?不在就先点「快捷战斗」把它叫出来
        ok = _find_button(hits, ("确定",)) or _find(hits, "确定", 0.9)
        if not ok:
            # BOSS 界面的「快捷战斗」在底部(y>600);对话框标题也含这四个字,
            # 用 y 过滤避免点错位置。
            quick = None
            for t, s, loc in hits:
                if s >= 0.85 and t.strip() == "快捷战斗" and loc[1] > 600:
                    quick = (t, s, loc)
                    break
            if not quick:
                quick = _find_button(hits, ("快捷战斗",)) or _find(hits, "快捷战斗", 0.85)
            if not quick:
                # 没有「快捷战斗」= 今日免费次数用完(已参与次数 N/N)。
                # 用户明确要求:**打够次数就停,不需要消耗体力继续**。
                log("    没有「快捷战斗」了(今日次数已用完),虚轴之庭完成")
                break
            _click(inp, quick[2], "快捷战斗", log)
            # 数量对话框:默认已是上限,直接确定
            wait_until(inp, engine, log, wait_any("确定", "最大", "Max"),
                       "数量对话框", timeout_s=15, save_tag="quantity_dialog")
            hits = _texts(engine, inp.grab())
            ok = _find_button(hits, ("确定",)) or _find(hits, "确定", 0.9)
            if not ok:
                log("    !! 数量对话框里没有「确定」")
                break

        # 点确定开始战斗,等结算
        click_and_wait(inp, engine, log, ok[2], "确定(开始战斗)",
                       wait_any("战斗结算", "再次挑战", "获得奖励"),
                       timeout_s=180, save_tag="boss_result")
        # 关结算:点「确定」回到 BOSS 界面原地(不返回多元裂缝)
        _dismiss(inp, engine, log, rounds=3, also=("确定", "确认"))
        done += 1
        log(f"    虚轴之庭:第 {done} 次完成(原地继续)")
    return done


def run_shop(inp, engine, log, navigate_only: bool = False) -> dict:
    """商店 → 礼包商店 → 吼美礼包 → 滚到底 → 买「零时馈礼」(×0 免费)。

    ``navigate_only=True`` 时只负责**导航到商店**,不买东西(供任务前置检查用)。

    实测路径与坑:
      * 商店导航 (993, 81) → 礼包商店卡片 (516, 292)
      * 左侧分类「吼美礼包」(72, 299)
      * **零时馈礼在列表最底部**,要向下滚好几次才看得到
      * 价格 ×0 = 免费;旁边的「日常补给包」是 ×6,别买错
      * 买完弹「是否购买零时馈礼」,要点弹窗里的**购买**(在取消左侧),不是列表里的
      * 买完还要关掉「购买成功」提示
      * 注意:**共鸣屋/回收屋/万神屋在「崩坏屋」卡片里面**,不在商店首页
    """
    out = {"bought": False, "name": "", "reason": ""}
    # 先识别当前在哪,再决定要不要导航
    screen = preflight(inp, engine, log, "商店")
    if screen not in ("shop", "shop_tab"):
        if not go_home(inp, engine, log):
            log("    !! 回不到主界面")
        click_and_wait(inp, engine, log, SHOP_NAV, "导航 商店",
                       lambda e, f: any(s >= 0.85 and
                                        any(k in t for k in ("礼包商店", "水晶商店",
                                                             "崩坏屋", "共鸣屋"))
                                        for t, s, _ in _texts(e, f)),
                       timeout_s=20, save_tag="shop_page")
    else:
        log("    已在商店,跳过导航")

    if navigate_only:
        out["reason"] = "navigate_only"
        return out

    hits = _texts(engine, inp.grab())
    pack = _find_card(engine, inp.grab(), "礼包商店") or _find(hits, "礼包商店", 0.85)
    if not pack:
        log("    !! 商店里找不到「礼包商店」")
        return out
    _click(inp, pack[2], "礼包商店", log)
    time.sleep(3.0)

    # 左侧分类 → 吼美礼包
    hits = _texts(engine, inp.grab())
    hm = _find_button(hits, ("吼美礼包",)) or _find(hits, "吼美礼包", 0.85)
    if hm:
        _click(inp, hm[2], "分类 吼美礼包", log)
        time.sleep(3.0)

    # ⚠️ 用户要求:**导航到购买页面后默认向下翻**。
    # 零时馈礼固定在「吼美礼包」列表的**最底部** —— 页面默认停在顶部时它不可见,
    # 而且上一次任务可能把列表留在半山腰。先无条件下翻几次滚到底再找,
    # 比"先在本屏碰运气"稳定得多。
    log("    进入分类后默认向下翻(零时馈礼在列表底部)")
    for _ in range(3):
        inp.drag(700, 600, 700, 350, duration=0.5, steps=18)
        time.sleep(1.8)

    # 向下滚找「零时馈礼」
    # 实测:它是**每日限购 1/1**,买完会从列表里**整个消失**(卡片不再渲染),
    # 所以"找不到"通常意味着今天已经买过,而不是失败。
    target = None
    seen_names: list[str] = []
    for i in range(8):
        hits = _texts(engine, inp.grab())
        for t, s, _ in hits:
            if s >= 0.85 and t.strip().endswith("包") and len(t.strip()) <= 8:
                if t.strip() not in seen_names:
                    seen_names.append(t.strip())
        target = _find_button(hits, ("零时馈礼",)) or _find(hits, "零时馈礼", 0.85)
        if target:
            log(f"    找到「零时馈礼」{target[2]}")
            break
        log(f"    向下滚动 ({i+1}/8)")
        inp.drag(700, 600, 700, 350, duration=0.5, steps=18)
        time.sleep(2.2)
    if not target:
        log("    ℹ️ 列表里没有「零时馈礼」——它是每日限购 1/1,买完就消失,"
            "通常说明今天已经买过")
        log(f"    本次看到的礼包:{seen_names[-6:]}")
        out["reason"] = "not_present(可能今天已买)"
        return out
    out["name"] = "零时馈礼"

    # 找**同 x 列、且在名字下方**的「购买」按钮。
    # ⚠️ 实测卡片结构:名字(如「零时馈礼」)在上方,下方依次是价格(×0)、
    # 「购买」按钮、限购标记 —— 名字和按钮**垂直距离约 296px**。
    # 关键:**上一行卡片的购买按钮 x 也对齐**,不加 y 约束会点到上一行,
    # 结果"日志显示 bought=True 但实际没买到"(实测踩过)。
    tx, ty = target[2][0], target[2][1]
    buy = None
    for extra in range(4):
        frame = inp.grab()
        hits = _texts(engine, frame)
        cands = [(t, s, l) for t, s, l in hits
                 if s >= 0.9 and t.strip() == "购买"
                 and abs(l[0] - tx) < 60
                 and l[1] > ty]
        if cands:
            cands.sort(key=lambda h: h[2][1])
            buy = cands[0]
            break
        log(f"    零时馈礼的购买按钮还没露出来,继续下滚({extra+1}/4)")
        inp.drag(700, 600, 700, 350, duration=0.5, steps=18)
        time.sleep(2.0)
    if buy is None:
        log("    !! 找不到「零时馈礼」同列的购买按钮")
        return out
    log(f"    点「购买」{buy[2]}")
    _click(inp, buy[2], "购买", log)
    time.sleep(3.0)

    # 确认弹窗:必须点弹窗里的购买(与「取消」同行、在其左侧)
    hits = _texts(engine, inp.grab())
    if _find(hits, "是否购买", 0.8) or _find(hits, "零时馈礼", 0.8):
        cancels = [h for h in hits if h[0].strip() == "取消" and h[1] >= 0.9]
        confirms = [h for h in hits
                    if h[0].strip() == "购买" and h[1] >= 0.9]
        if cancels:
            cy = cancels[0][2][1]
            same_row = [h for h in confirms if abs(h[2][1] - cy) < 30
                        and h[2][0] < cancels[0][2][0]]
            if same_row:
                log(f"    弹窗确认购买 {same_row[0][2]}")
                _click(inp, same_row[0][2], "弹窗购买", log)
                time.sleep(3.5)
                out["bought"] = True
                _dismiss(inp, engine, log, rounds=3, also=("确定", "确认"))
            else:
                log("    !! 弹窗里找不到「购买」,取消以免误买")
                _click(inp, cancels[0][2], "取消", log)
        else:
            log("    !! 没有确认弹窗也没看到取消按钮")
    else:
        log("    (没有确认弹窗)")
    log(f"    商店结果:{out}")
    return out


def claim_social(inp, engine, log) -> dict:
    """社交:我的消息「一键领取」 + 我的社团「可领」。

    ⚠️ 硬性规则:「我的消息」界面**只允许点「一键领取」**,禁止点「体力领取」
    —— 用 `_find_safe` 过滤,宁可不动手也不误点。
    """
    out = {"messages": False, "club": False}
    # 用统一的社交导航(带验证 + 重试),而不是"点一下就当到了"
    if not goto_social(inp, engine, log):
        log("    !! 到不了社交界面")
        return out

    # --- 我的消息 ---
    frame = inp.grab()
    hits = _texts(engine, frame)
    entry = _find_button(hits, ("我的消息",)) or _find(hits, "我的消息", 0.85)
    if entry:
        _click(inp, entry[2], "我的消息", log)
        time.sleep(3.5)
        for _ in range(6):
            frame = inp.grab()
            hits = _texts(engine, frame)
            # 禁止按钮保护:绝不允许命中「体力领取」
            claim = _find_safe(hits, "一键领取", 0.85) or _find_safe(hits, "键领取", 0.85)
            if not claim:
                log("    没有「一键领取」了")
                break
            _click(inp, claim[2], "一键领取(消息)", log)
            time.sleep(2.5)
            out["messages"] = True
            _dismiss(inp, engine, log, rounds=2, also=("确定", "确认"))
        # 返回社交
        _click(inp, SOCIAL_NAV, "导航 社交(返回)", log)
        time.sleep(3.0)
    else:
        log("    !! 没找到「我的消息」")

    # --- 我的社团 ---
    frame = inp.grab()
    hits = _texts(engine, frame)
    club = _find_button(hits, ("我的社团",)) or _find(hits, "我的社团", 0.85)
    if club:
        _click(inp, club[2], "我的社团", log)
        time.sleep(3.5)
        # 「可领」点完会变「不可领」——只差一个字,OCR 常把「不可领」也读成
        # 「可领」,所以**必须自己验证状态变化**,否则会对着同一个无效按钮
        # 连点(实测连点 5 次)。
        for attempt in range(5):
            frame = inp.grab()
            before_state = _club_claim_state(engine, frame)
            if before_state != "available":
                log(f"    社团没有「可领」了(当前 {before_state})")
                break
            claim = _find_button(_texts(engine, frame), ("可领",))
            if not claim:
                log("    找不到可点的「可领」按钮")
                break
            _click(inp, claim[2], "可领(社团)", log)
            time.sleep(3.0)
            out["club"] = True
            _dismiss(inp, engine, log, rounds=2, also=("确定", "确认", "领取"))
            # 验证:状态必须改变
            after_state = _club_claim_state(engine, inp.grab())
            if after_state == before_state:
                log(f"    ⚠️ 点了「可领」但状态没变(仍 {after_state}),停止以免重复点")
                break
            log(f"    社团领取成功({before_state} -> {after_state})")
        _click(inp, SOCIAL_NAV, "导航 社交(返回)", log)
        time.sleep(2.5)
    else:
        log("    !! 没找到「我的社团」")
    return out


def _club_claim_state(engine, frame) -> str:
    """社团「可领 / 不可领」的真实状态。

    判据必须**先查否定形式**:「不可领」里也含「可领」,先查「可领」会永远
    命中 —— 这正是之前连点 5 次的根因。
    """
    for t, s, _ in _texts(engine, frame):
        if s < 0.8:
            continue
        if "不可领" in t:
            return "unavailable"
    for t, s, _ in _texts(engine, frame):
        if s >= 0.85 and t.strip() == "可领":
            return "available"
    return "unknown"
    """任务界面:点左上角「书」按钮 -> 领「每日任务」-> 领「每周任务」。

    这是「快速领取」按钮的流程:两个页签各有自己的领取入口。
    """
    out = {"daily": False, "weekly": False}
    _click(inp, BOOK_ICON, "书按钮(任务界面)", log)
    time.sleep(3.0)
    for name, key in (("每日任务", "daily"), ("每周任务", "weekly")):
        frame = inp.grab()
        hits = _texts(engine, frame)
        tab = _settle_has(hits, name, 0.85)
        if tab:
            _click(inp, tab[2], f"页签 {name}", log)
            time.sleep(2.5)
        # 找领取按钮
        frame = inp.grab()
        hits = _texts(engine, frame)
        claim = _settle_has(hits, "一键领取", 0.85) or _settle_has(hits, "领取", 0.85)
        if claim:
            _click(inp, claim[2], f"领取({name})", log)
            time.sleep(2.5)
            _dismiss(inp, engine, log, rounds=2)
            out[key] = True
            log(f"    {name} 已领取")
        else:
            log(f"    {name} 没有可领取的按钮")
    return out
