"""图形界面(仿 MAA 前端)。

布局参照明日方舟 MAA 的「一键长草」页:

    ┌─────────────────────────────────────────────────────┐
    │  一键长草        设置                                │  标签页
    ├──────────────────┬──────────────────────────────────┤
    │ ☑ 开始唤醒      ⚙ │  □ 使用双倍券   [ 97 ]           │
    │ ☑ 使魔探险      ⚙ │  □ 指定次数     [ 999 ]          │
    │ ☑ 收菜+派出       │  清体力关卡     [ 拼装时光BONUS ▾]│
    │ ☑ 商店领零时之种  │                                  │
    │ ☑ 共鸣屋刷新      │  [ 常规设置 ] [ 高级设置 ]        │
    │ ☑ 存在感(每日)   │  ┌────────────────────────────┐  │
    │ ☑ 社交领取        │  │ 运行日志                   │  │
    │ ☑ 首通清理        │  │ ...                        │  │
    │ ☑ 清体力          │  └────────────────────────────┘  │
    │ ☑ 虚轴之庭        │                                  │
    │ ──────────────── │                                  │
    │ [全选] [清空]     │                                  │
    │                   │                                  │
    │  完成后           │                                  │
    │  ○ 无动作         │                                  │
    │ [  开始!  ]       │                                  │
    └──────────────────┴──────────────────────────────────┘

「清体力」是左侧任务列表里的一个勾选项(不是单独的标签页),勾上就参与执行。

所有任务在后台线程运行,日志通过队列实时回传到界面,界面不会卡死。
"""

from __future__ import annotations
from ._root import project_root

import queue
import threading
import tkinter as tk
from pathlib import Path

from tkinter import messagebox, ttk

PROJECT = Path(project_root())
CONFIG_PATH = PROJECT / "configs" / "bh2-summon.json"
WATCHLIST = PROJECT / "watchlist.txt"
EQUIPMENT = PROJECT / "equipment.json"

# 任务列表:(键, 显示名, 分组)。**按用户指定的执行顺序排列**,
# 执行时就是这个顺序(不按勾选顺序)。
TASKS: list[tuple[str, str, str]] = [
    ("signin",       "① 启动游戏 + 进游戏(关弹窗)",        "启动"),
    ("popups",       "② 关闭弹窗(公告/维护/签到/促销)",     "启动"),
    ("signin2",      "③ 领取每日签到(可独立勾选)",         "启动"),
    ("harvest",      "④ 收菜(使魔探险:收菜 + 派出)",       "日常"),
    ("shop",         "⑤ 商店:领免费零时之种",               "日常"),
    ("resonance",    "⑥ 共鸣屋:刷新 + 按名单买",            "日常"),
    ("fracture",     "⑦ 多元裂缝:使魔的爱 + 虚轴之庭",      "战斗"),
    ("stamina1",     "⑧ 清体力(BONUS)",                     "战斗"),
    ("presence1",    "⑨ 存在感(每日任务)",                  "日常"),
    ("social",       "⑩ 社团领取(消息 + 社团)",             "日常"),
    ("stamina2",     "⑪ 再次清体力(BONUS)",                 "战斗"),
    ("presence2",    "⑫ 再次存在感(含每周任务)",            "日常"),
]

# 默认勾选项:整套日常
DEFAULT_CHECKED = {k for k, _, _ in TASKS}

# 键 -> 显示名,供提示与错误信息使用
TASK_LABELS: dict[str, str] = {k: label for k, label, _ in TASKS}

# ---------------------------------------------------------------------------
#  每个任务的「前置界面」约定 + 收尾策略
#
#  这是本轮审查最重要的结论:**之前每个任务都假设游戏恰好在某个界面**,
#  但没有任何地方保证这一点。实测因此出过:
#    * 收菜在别的页面上找「探索完成」-> 报 "looks clean",0 次收菜
#    * 共鸣屋在商店页上读格子 -> 把「日常补给包」当成装备名
#    * 虚轴之庭在错误界面上找卡片 -> 直接 0 次
#
#  (前置界面, 任务结束后是否回主界面)
#    "any"  = 不挑界面(靠自带预检/导航)
TASK_PLAN: dict[str, tuple[str, bool]] = {
    "signin":     ("any",      False),   # 启动+进游戏,自带流程
    "popups":     ("any",      False),   # 关弹窗,自带处理
    "signin2":    ("any",      False),   # 签到,自带处理
    "harvest":    ("familiar", False),   # 必须到使魔探险页
    "shop":       ("shop",     True),    # 必须到商店
    "resonance":  ("shop_tab", True),    # 必须到共鸣屋子页
    "fracture":   ("any",      True),    # 多元裂缝,自带导航
    "stamina1":   ("any",      True),    # 清体力,自带导航;结束回主界面
    "presence1":  ("task",     True),    # 必须到任务界面
    "social":     ("social",   True),    # 必须到社交
    "stamina2":   ("any",      True),    # 再次清体力;结束回主界面
    "presence2":  ("task",     True),
}


def ensure_task_start(key: str, cap, engine, log) -> bool:
    """任务开始前把界面带到它需要的位置。

    用户要求的精神:「每当任务启动时应该进行一次识别,查看当前在哪,再进行任务」。
    这里把它固化成**每个任务的前置界面约定**,而不是靠"上一个任务留下的状态"。

    返回 False 表示到不了前置界面(调用方应跳过这一项)。
    """
    from .daily import (SCREEN_LABELS, goto_shop_tab, goto_social, open_task_tab,
                        preflight, run_shop)
    want, _ = TASK_PLAN.get(key, ("any", False))
    if want == "any":
        preflight(cap, engine, log, "前置检查")
        return True

    screen = preflight(cap, engine, log, "前置检查")
    if screen == want:
        log(f"    已在目标界面({SCREEN_LABELS.get(want, want)}),跳过导航")
        return True

    if want == "familiar":
        return open_task_tab(cap, engine, log, "使魔探险")
    if want == "task":
        return open_task_tab(cap, engine, log, "存在感")
    if want == "shop":
        # 只导航不买(买由任务本身做)。判据:真的到了商店类页面
        from .daily import recognize_screen as _rs
        run_shop(cap, engine, log, navigate_only=True)
        return _rs(engine, cap.grab()) in ("shop", "shop_tab")
    if want == "shop_tab":
        return goto_shop_tab(cap, engine, log, "共鸣屋")
    if want == "social":
        return goto_social(cap, engine, log)
    return True


def finish_task(key: str, cap, engine, log) -> None:
    """任务结束后按约定收尾,保证下一个任务从干净状态开始。

    「收尾」= 关掉残留弹窗 + 按需要回主界面。收尾失败不影响任务本身的结果
    (只记日志),因为下一个任务的前置检查会再纠正一次。
    """
    from .daily import close_popups, go_home
    _, need_home = TASK_PLAN.get(key, ("any", False))
    try:
        if need_home:
            # go_home 内部已经会先 close_popups,所以这里不重复关
            go_home(cap, engine, log)
        else:
            close_popups(cap, engine, log)
    except Exception as exc:  # noqa: BLE001
        log(f"    (收尾时出错,忽略:{type(exc).__name__}: {exc})")


class _Skip(Exception):
    """该任务尚未接入自动化 —— 只提示,不算失败。"""


class _QueueWriter:
    """把 print/日志调用转到队列,供界面线程消费。"""

    def __init__(self, q: queue.Queue, prefix: str = ""):
        self.q = q
        self.prefix = prefix

    def write(self, text: str) -> None:
        if text:
            for line in text.splitlines():
                if line.strip():
                    self.q.put(("log", f"{self.prefix}{line}"))

    def flush(self) -> None:
        pass

    def __call__(self, text: str) -> None:
        self.write(str(text))
        self.q.put(("log", ""))


class BotGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("崩坏学园2 自动化 — 仿 MAA 前端")
        self.geometry("1080x720")
        self.minsize(1000, 660)

        self.log_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False
        # 停止请求:后台线程在每个步骤边界检查它,置位后安全退出
        self.stop_event = threading.Event()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.gear_buttons: dict[str, tk.Widget] = {}

        self._build_style()
        self._build_layout()
        self.after(120, self._pump)

    # ---------------------------------------------------------------- 样式

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Start.TButton", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Task.TCheckbutton", font=("Microsoft YaHei UI", 9))
        # 标签页默认内边距太窄,中文标签会被截断(「一键长草」显示成「长草」)。
        # 加大内边距并指定字体,四个字才放得下。
        style.configure("TNotebook.Tab", padding=[22, 8],
                        font=("Microsoft YaHei UI", 10))

    def _build_layout(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_main_tab(nb)
        self._build_settings_tab(nb)

    # ------------------------------------------------------- 「一键长草」

    def _build_main_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="一键长草")

        left = ttk.Frame(tab, width=330)
        left.pack(side="left", fill="y", padx=(6, 10), pady=8)
        left.pack_propagate(False)

        # 任务勾选列表
        box = ttk.LabelFrame(left, text="任务")
        box.pack(fill="x")
        last_group = None
        for key, label, group in TASKS:
            if last_group is not None and group != last_group:
                ttk.Separator(box).pack(fill="x", padx=6, pady=3)
            last_group = group
            row = ttk.Frame(box)
            row.pack(fill="x", padx=6, pady=1)
            var = tk.BooleanVar(value=key in DEFAULT_CHECKED)
            self.vars[key] = var
            cb = ttk.Checkbutton(row, text=label, variable=var, style="Task.TCheckbutton")
            cb.pack(side="left", fill="x", expand=True)
            gear = ttk.Button(row, text="⚙", width=3,
                              command=lambda k=key: self._show_task_options(k))
            gear.pack(side="right")
            self.gear_buttons[key] = gear

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="全选", command=lambda: self._set_all(True)).pack(
            side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(btns, text="清空", command=lambda: self._set_all(False)).pack(
            side="left", expand=True, fill="x", padx=(3, 0))

        ttk.Separator(left).pack(fill="x", pady=10)

        after = ttk.LabelFrame(left, text="完成后")
        after.pack(fill="x")
        self.after_action = tk.StringVar(value="none")
        for val, text in (("none", "无动作"), ("close", "关闭游戏"),
                          ("shutdown", "关闭模拟器"), ("exit", "退出程序")):
            ttk.Radiobutton(after, text=text, value=val,
                            variable=self.after_action).pack(anchor="w", padx=6)

        btns_row = ttk.Frame(left)
        btns_row.pack(fill="x", pady=(14, 0))
        self.start_btn = ttk.Button(btns_row, text="开 始 !", style="Start.TButton",
                                    command=self._on_start)
        self.start_btn.pack(side="left", fill="x", expand=True, ipady=6)
        # 停止按钮:运行中才可用;点击只是**请求**停止,由后台线程在步骤边界退出
        self.stop_btn = ttk.Button(btns_row, text="停 止", width=8,
                                   command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0), ipady=6)

        # 右侧
        right = ttk.Frame(tab)
        right.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)

        opts = ttk.LabelFrame(right, text="运行选项")
        opts.pack(fill="x")
        grid = ttk.Frame(opts)
        grid.pack(fill="x", padx=8, pady=6)

        self.opt_double = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="BONUS 使用双倍券(勾选后消耗 50→100)",
                        variable=self.opt_double).grid(row=0, column=0, columnspan=2,
                                                       sticky="w", pady=2)

        self.opt_times = tk.BooleanVar(value=False)
        ttk.Checkbutton(grid, text="清体力指定次数(不勾 = 清到需要钻石为止)",
                        variable=self.opt_times).grid(row=1, column=0, sticky="w", pady=2)
        self.entry_times = ttk.Entry(grid, width=8)
        self.entry_times.insert(0, "2")
        self.entry_times.grid(row=1, column=2, sticky="w", padx=(8, 0))

        self.opt_refill = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="补充体力弹窗:有双倍体力则兑换",
                        variable=self.opt_refill).grid(row=2, column=0, sticky="w",
                                                       pady=2)

        # 钻石换体力:勾选后填次数,就会消耗钻石兑换那么多次(不勾 = 遇到就取消结束)
        self.opt_diamond = tk.BooleanVar(value=False)
        ttk.Checkbutton(grid, text="钻石兑换体力(遇到「需钻石」时消耗,允许",
                        variable=self.opt_diamond).grid(row=3, column=0, sticky="w",
                                                        pady=2)
        self.entry_diamond = ttk.Entry(grid, width=6)
        self.entry_diamond.insert(0, "1")
        self.entry_diamond.grid(row=3, column=2, sticky="w", padx=(6, 0))
        ttk.Label(grid, text="次)", style="Hint.TLabel").grid(row=3, column=3,
                                                             sticky="w", padx=(2, 0))

        self.opt_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="体力/次数不足时跳过该项(不算失败)",
                        variable=self.opt_skip).grid(row=4, column=0, columnspan=3,
                                                     sticky="w", pady=2)

        # 「有没有自动套」决定进关卡后怎么打:
        #   勾上(默认)= 当前策略,自动套自己会打,脚本只等结算;
        #   取消     = 按住「向右 + 第一个装备键」一直挂到战斗结束。
        self.opt_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="有自动套(取消勾选:进关卡后按住「向右 + 第一个装备键」)",
                        variable=self.opt_auto).grid(row=5, column=0, columnspan=3,
                                                     sticky="w", pady=2)

        tip = ttk.Label(right, style="Hint.TLabel", wraplength=620, justify="left",
                        text=("任务按上面列出的顺序执行(与勾选顺序无关)。"
                              "每项开始前都会先回到主界面,避免停在深层界面里找不到目标。"))
        tip.pack(fill="x", padx=4, pady=(4, 0))

        ttk.Separator(right).pack(fill="x", pady=8)

        logbox = ttk.LabelFrame(right, text="运行日志")
        logbox.pack(fill="both", expand=True)
        self.log_text = tk.Text(logbox, wrap="word", height=14,
                                font=("Consolas", 9), background="#fbfbfb")
        scroll = ttk.Scrollbar(logbox, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.configure(state="disabled")

        self.status = ttk.Label(right, text="就绪", style="Hint.TLabel")
        self.status.pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------------ 「设置」

    def _build_settings_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="设置")

        info = ttk.LabelFrame(tab, text="文件位置(可直接编辑)")
        info.pack(fill="x", padx=8, pady=8)
        rows = [
            ("共鸣屋购买名单", WATCHLIST, "每行一个装备名,# 开头为注释"),
            ("装备方案", EQUIPMENT, "组名 / 套装名 / 坐标"),
            ("使魔探险配置", CONFIG_PATH, "收菜 + 派出的参数"),
        ]
        for i, (name, path, hint) in enumerate(rows):
            ttk.Label(info, text=name, style="Title.TLabel").grid(
                row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Label(info, text=str(path.name), style="Hint.TLabel").grid(
                row=i, column=1, sticky="w")
            ttk.Label(info, text=hint, style="Hint.TLabel").grid(
                row=i, column=2, sticky="w", padx=8)
            ttk.Button(info, text="打开",
                       command=lambda p=path: self._open_path(p)).grid(
                row=i, column=3, padx=8)

        dev = ttk.LabelFrame(tab, text="设备")
        dev.pack(fill="x", padx=8, pady=8)
        ttk.Label(dev, text="ADB 地址").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.entry_addr = ttk.Entry(dev, width=24)
        self.entry_addr.insert(0, "127.0.0.1:16384")
        self.entry_addr.grid(row=0, column=1, sticky="w")
        ttk.Label(dev, text="(MuMu 12 模拟器)", style="Hint.TLabel").grid(
            row=0, column=2, sticky="w", padx=8)
        ttk.Label(dev, text="输入方式").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.combo_input = ttk.Combobox(dev, width=22, state="readonly",
                                        values=["maatouch(最快)", "minitouch", "adbshell(兼容好)"])
        self.combo_input.current(0)
        self.combo_input.grid(row=1, column=1, sticky="w")

        dev = ttk.LabelFrame(tab, text="装备方案(清体力前自动切换)")
        dev.pack(fill="x", padx=8, pady=8)
        ttk.Label(dev, text="装备组").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.combo_equip_group = ttk.Combobox(dev, width=18, state="readonly",
                                              values=[f"{c}自定义组" for c in "ABC"])
        self.combo_equip_group.grid(row=0, column=1, sticky="w")
        ttk.Label(dev, text="A / B / C 组", style="Hint.TLabel").grid(
            row=0, column=2, sticky="w", padx=8)
        ttk.Label(dev, text="组内装备").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.combo_equip_suit = ttk.Combobox(dev, width=18, state="readonly",
                                             values=[f"{n:02d}" for n in range(1, 11)])
        self.combo_equip_suit.grid(row=1, column=1, sticky="w")
        ttk.Label(dev, text="01 ~ 10 号", style="Hint.TLabel").grid(
            row=1, column=2, sticky="w", padx=8)
        ttk.Button(dev, text="保存到 equipment.json",
                   command=self._save_equipment).grid(row=0, column=3, rowspan=2,
                                                     padx=8, sticky="nsew")
        # 从 equipment.json 读当前值回填
        self._load_equipment_fields()

        dry = ttk.LabelFrame(tab, text="安全")
        dry.pack(fill="x", padx=8, pady=8)
        self.opt_dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(dry, text="演练模式(只识别不点击,用于检查坐标)",
                        variable=self.opt_dry).pack(anchor="w", padx=8, pady=6)

        note = ttk.LabelFrame(tab, text="说明")
        note.pack(fill="both", expand=True, padx=8, pady=8)
        text = tk.Text(note, wrap="word", height=10, font=("Microsoft YaHei UI", 9))
        text.pack(fill="both", expand=True, padx=8, pady=6)
        text.insert("1.0",
            "· 「清体力」是「一键长草」任务列表里的一个勾选项,勾上才会执行。\n"
            "· 任务的先后顺序是固定的(按列表从上到下),不按勾选顺序。\n"
            "· 所有点击前都会先识别界面确认,失败会停下并保存现场截图。\n"
            "· 录制/日志保存在 sessions\\ 目录。\n"
            "· 已知问题:清体力的地图翻页还不够稳,可能偶尔需要人工接管(见 NOTES.md)。\n"
            "· 本工具会真实消耗体力/货币,请自行评估账号风险。")
        text.configure(state="disabled")

    # -------------------------------------------------------------- 行为

    def _set_all(self, value: bool) -> None:
        for var in self.vars.values():
            var.set(value)

    def _load_equipment_fields(self) -> None:
        """从 equipment.json 读 group / suit 回填到两个下拉框。

        组名形如 ``C自定义组`` -> 回填组下拉(取首字母 A/B/C);
        套装名形如 ``C01-自定义套装`` -> 回填编号下拉(取 ``01``)。
        """
        from .resonance import load_equipment_settings
        st = load_equipment_settings()
        group = str(st.get("group") or "")
        suit = str(st.get("suit") or "")
        # 组下拉:匹配 "X自定义组" 的首字母
        if group:
            prefix = group[0].upper() if group[0].isalpha() else ""
            if prefix in "ABC":
                self.combo_equip_group.set(f"{prefix}自定义组")
            else:
                self.combo_equip_group.set(group)
        # 编号下拉:从 "C01-自定义套装" 里提取 "01"
        if suit:
            import re
            m = re.search(r"([A-Za-z])?(\d{2})", suit)
            if m and m.group(2):
                self.combo_equip_suit.set(m.group(2))
            else:
                self.combo_equip_suit.set(suit)

    def _save_equipment(self) -> None:
        """把两个下拉框的组 / 编号写回 equipment.json。

        组下拉 -> ``X自定义组``;编号下拉 -> ``XNN-自定义套装``(NN 取编号,
        X 取组的首字母,和实际命名 ``C01-自定义套装`` 一致)。
        """
        import json
        group = self.combo_equip_group.get().strip()
        num = self.combo_equip_suit.get().strip()
        prefix = group[0].upper() if group else "C"
        suit = f"{prefix}{num}-自定义套装"
        st = {}
        try:
            from .resonance import load_equipment_settings
            st = load_equipment_settings()
        except Exception:  # noqa: BLE001
            st = {}
        st["group"] = group
        st["suit"] = suit
        try:
            with open(EQUIPMENT, "w", encoding="utf-8") as fh:
                json.dump(st, fh, ensure_ascii=False, indent=2)
            messagebox.showinfo("已保存",
                                f"装备组:{group}\n套装:{suit}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def _open_path(self, path: Path) -> None:
        """用系统默认程序打开文件。

        ⚠️ 不能用 os.startfile:实测本机 .txt 的关联是 **Store 版记事本**
        (`AppX4ztfk9wxr86nxmzzq47px0nh0e58b8fw`),启动它会走 COM 激活路径
        并抛 `OSError [WinError 5] 拒绝访问`。所以这里按顺序尝试:

          1. 找一个真正的文本编辑器进程(notepad.exe / notepad++ / code)
          2. 再退回 os.startfile(关联成传统程序时有效)
          3. 最后用资源管理器定位到文件(保证总能给出反馈)
        """
        import os
        import shutil
        import subprocess

        if not path.exists():
            messagebox.showwarning("文件不存在", f"{path}\n\n尚未创建。")
            return

        editors = [
            shutil.which("notepad.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "Notepad++", "notepad++.exe"),
            shutil.which("notepad++"),
            shutil.which("code"),
        ]
        for exe in editors:
            if not exe or not os.path.exists(exe):
                continue
            try:
                # 不用 shell=True:绕过文件关联,直接起编辑器
                subprocess.Popen([exe, str(path)])
                return
            except OSError:
                continue

        try:
            os.startfile(str(path))       # noqa: S606 - Windows 专用
            return
        except OSError:
            pass

        # 兜底:打开所在文件夹并选中该文件
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
            messagebox.showinfo(
                "已在资源管理器中定位",
                f"没能直接打开编辑器,已在资源管理器中选中:\n{path}\n\n"
                f"双击即可编辑。")
        except OSError as exc:
            messagebox.showerror("打开失败", f"{path}\n\n{exc}")

    def _show_task_options(self, key: str) -> None:
        label = TASK_LABELS.get(key, key)
        hints = {
            "signin": "在模拟器里启动崩坏学园2(包名 com.miHoYo.HSoDv2Original),\n"
                      "等它到前台。\n"
                      "**先识别当前界面**:已经在游戏里就跳过标题页等待和登录流程;\n"
                      "否则等标题页出现再走进游戏流程(关促销/公告弹窗)。\n"
                      "⚠️ 签到不在这里做,它有自己的独立任务(下一项)。",
            "signin2": "**独立签到任务**:识别当前界面,若有「每日签到」弹窗就点「领取」。\n"
                       "随时可单独勾选运行 —— 不依赖启动流程跑到哪一步。\n"
                       "判据用「第N天 / 每日签到」这类弹窗独有文字(旧模板已对不上新版弹窗)。",
            "popups": "**独立关弹窗任务**:关掉公告 / 维护通知 / 促销 / 签到 / 奖励提示。\n"
                      "⚠️ 很重要:这些弹窗会**挡住后续所有识别** —— 实测公告面板挂着时\n"
                      "界面识别成 unknown,后面每个任务都导航失败。\n"
                      "所以它单独成项,随时可跑;签到也会被它顺手领掉。",
            "harvest": "使魔探险收菜 + 派出。\n"
                       "收菜判据是「探索完成」印章;派出走 一键派遣 → 出发。\n"
                       "参数在 configs\\bh2-summon.json。",
            "shop": "礼包商店 → 吼美礼包 → 滚到底 →「零时馈礼」(×0 免费)。\n"
                    "⚠️ 此项尚未稳定,见 NOTES.md 0.5。",
            "resonance": "按 watchlist.txt 的名单购买未拥有的装备。\n"
                         "免费次数用完后刷新会消耗零时之种;余额不足 6000 不买。",
            "fracture": "多元裂缝 → 使魔的爱(每日 1 次,快捷战斗)\n"
                        "→ 虚轴之庭(世界BOSS,出击 → 快捷战斗 ×3)。\n"
                        "体力/次数不足会自动跳过。",
            "stamina1": "清体力:BONUS 关卡。\n"
                        "先用双倍券,结算界面点「再次挑战」连打;\n"
                        "弹「兑换双倍体力」则兑换,弹消耗钻石则取消并结束本次。",
            "stamina2": "同「清体力」,这是流程后半段的第二次清体力。",
            "presence1": "存在感页签(每日任务)的「一键领取」。",
            "presence2": "再次领取存在感,**并额外领每周任务**。",
            "social": "我的消息「一键领取」+ 我的社团「可领」。\n"
                      "⚠️ 硬性规则:消息界面**只允许点「一键领取」,禁止点「体力领取」**。",
        }
        messagebox.showinfo(f"{label} — 说明", hints.get(key, "暂无额外参数。"))

    # ---------------------------------------------------------- 执行流程

    def _log(self, text: str) -> None:
        self.log_queue.put(("log", text))

    def _pump(self) -> None:
        """定期消费日志队列,保证界面线程安全。"""
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", str(payload) + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status.configure(text=str(payload))
                elif kind == "stopped":
                    self.running = False
                    self.stop_event.clear()
                    self.start_btn.configure(text="开 始 !", state="normal",
                                             style="Start.TButton")
                    self.stop_btn.configure(state="disabled")
                    self._log("⏹ 已被用户停止")
                elif kind == "done":
                    self.running = False
                    self.stop_event.clear()
                    self.start_btn.configure(text="开 始 !", state="normal",
                                             style="Start.TButton")
                    self.stop_btn.configure(state="disabled")
                    if payload:
                        messagebox.showwarning("执行结束", str(payload))
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _on_stop(self) -> None:
        """请求停止:置位后由后台线程在每个步骤边界退出。

        不强制杀线程 —— 强行打断可能正好卡在"点了购买、还没确认"的中间状态,
        所以这里只是**请求**停止,让线程在安全点结束。
        """
        if not self.running:
            return
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.start_btn.configure(text="停止中…", state="disabled")
        self._log("⏹ 收到停止请求,将在当前步骤结束后停止…")

    def _on_start(self) -> None:
        if self.running:
            self._log("!! 正在运行中")
            return
        selected = [k for k, v in self.vars.items() if v.get()]
        if not selected:
            messagebox.showinfo("未选择任务", "请至少勾选一个任务。")
            return
        self.running = True
        self.stop_event.clear()
        # 让 daily 里的等待循环也能被停止按钮打断
        from .daily import set_stop_event
        set_stop_event(self.stop_event)
        self.start_btn.configure(text="运行中…", state="disabled")
        self.stop_btn.configure(state="normal")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.worker = threading.Thread(target=self._run_selected,
                                       args=(selected,), daemon=True)
        self.worker.start()

    def _run_selected(self, selected: list[str]) -> None:
        """后台线程:按固定顺序执行勾选的任务。"""
        log = _QueueWriter(self.log_queue)
        order = [k for k, _, _ in TASKS]          # 固定顺序
        todo = [k for k in order if k in selected]
        names = dict((k, l) for k, l, _ in TASKS)
        # ⚠️ 这两个必须在循环**之前**创建:停止/跳过分支会往 results 里追加,
        # 之前漏了初始化,一按停止就抛 NameError(实测踩过)。
        results: list[tuple[str, str, str]] = []
        failed: list[str] = []

        self.log_queue.put(("status", "连接设备…"))
        log(f"计划执行 {len(todo)} 个任务(共勾选 {len(selected)}):")
        for k in todo:
            log(f"   · {names[k]}")
        log("")

        try:
            from .adb_backend import AdbBackend, AdbBackendUnavailable
            from .ocr import OcrEngine
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(("done", f"模块导入失败:{exc}"))
            return

        cap = None
        try:
            cap = AdbBackend(address=self.entry_addr.get().strip() or None)
            log(f"设备已连接:{cap.device.describe()}")
            engine = OcrEngine.shared()
            log("OCR 引擎已就绪")
            log("")
        except Exception as exc:  # noqa: BLE001
            self.log_queue.put(("done", f"连接设备失败:{exc}"))
            return

        dry = bool(self.opt_dry.get())
        if dry:
            log("!! 演练模式:只识别,不点击")

        # 有没有自动套决定进关卡后怎么打(见 sweep.battle_wait)
        from .sweep import set_auto_loadout
        auto_loadout = bool(self.opt_auto.get())
        set_auto_loadout(auto_loadout)
        log("自动套:" + ("有(开战后只等结算)"
                         if auto_loadout else "没有(按住「向右 + 第一个装备键」挂机)"))

        try:
            for key in todo:
                # 每个任务开始前检查停止请求(步骤边界,安全)
                if self.stop_event.is_set():
                    log("⏹ 已停止,不再执行剩余任务")
                    break
                self.log_queue.put(("status", f"正在执行:{names[key]}"))
                log(f"===== {names[key]} =====")
                try:
                    # ① 前置界面检查:把游戏带到这个任务需要的页面
                    if not ensure_task_start(key, cap, engine, log):
                        log(f"----- {names[key]}:跳过(到不了所需界面)-----")
                        results.append((names[key], "跳过", "前置界面不可达"))
                        continue
                    # ② 执行任务
                    self._run_one(key, cap, engine, log, dry)
                    log(f"----- {names[key]} 完成 -----")
                except _Skip as exc:
                    # 尚未接入的任务只提示,不计入失败,也不弹窗打扰
                    log(f"----- {names[key]}:跳过({exc})-----")
                except Exception as exc:  # noqa: BLE001
                    if type(exc).__name__ == "Stopped":
                        log("⏹ 已停止当前任务")
                        results.append((names[key], "停止", ""))
                        break
                    if type(exc).__name__ == "Insufficient":
                        # 体力/次数不足不是错误 —— 按用户要求跳过当前项
                        log(f"----- {names[key]}:跳过({exc})-----")
                        results.append((names[key], "跳过", str(exc)[:40]))
                        continue
                    log(f"!! {names[key]} 失败:{type(exc).__name__}: {exc}")
                    failed.append(names[key])
                finally:
                    # ③ 收尾:关掉残留弹窗 + 按约定回主界面,保证下一个任务
                    #    从干净状态开始(收尾失败只记日志,不影响结果)
                    finish_task(key, cap, engine, log)
                log("")
        finally:
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass

        if self.stop_event.is_set():
            log("===== 已按用户要求停止 =====")
            self.log_queue.put(("stopped", True))
            return

        if failed:
            self.log_queue.put(("done", "以下任务失败:\n" + "\n".join(failed)))
        else:
            self.log_queue.put(("done", ""))
        self.log_queue.put(("status", "全部完成" if not failed else "有任务失败"))
        self.log_queue.put(("log", "===== 全部结束 ====="))

    def _run_one(self, key: str, cap, engine, log, dry: bool) -> None:
        """单个任务。延迟导入,避免界面启动就加载重型模块。"""
        if key == "signin":
            # 启动崩坏学园2(模拟器内)+ 走进游戏流程(关弹窗)
            # 注意:签到不在这一步做,它有独立任务,见 signin2
            from .daily import launch_game
            if not launch_game(cap, engine, log, stop_event=self.stop_event):
                raise _Skip("游戏启动/进游戏流程未完成")
            return

        if key == "signin2":
            # 独立签到:识别当前界面,有签到弹窗就领
            from .daily import claim_signin, preflight
            preflight(cap, engine, log, "签到")
            if not claim_signin(cap, engine, log):
                log("    当前没有签到弹窗(今天可能已领过)")
            return

        if key == "popups":
            # 独立关弹窗:公告/维护/促销/签到/奖励提示。
            # 这些弹窗会挡住后续所有识别(实测挂着公告时界面识别成 unknown),
            # 所以必须能单独跑,而不是绑在某个任务里。
            from .daily import close_popups, preflight, recognize_screen
            preflight(cap, engine, log, "关弹窗")
            n = close_popups(cap, engine, log)
            log(f"    共关闭 {n} 个弹窗")
            after = recognize_screen(engine, cap.grab())
            log(f"    处理后界面:{after}")
            return

        if key == "harvest":
            # 使魔探险:收菜 + 派出
            #
            # ⚠️ **必须先导航到「使魔探险」页**。bh2-summon.json 是 mode=once 的
            # 纯操作配置,它假设你已经在那一页上了 —— 里面没有任何导航步骤。
            # 之前漏了这一步,于是引擎在当前界面(主界面/别的页)上找「探索完成」,
            # 什么都找不到,日志报 "looks clean / done: 0 claim(s)",
            # 看起来像"今天没有可收的",实际是根本没到那一页。
            from .daily import open_task_tab, preflight
            preflight(cap, engine, log, "收菜")
            # 无论当前在哪(哪怕已经在任务界面),都要确保切到「使魔探险」页签
            if not open_task_tab(cap, engine, log, "使魔探险"):
                log("    !! 无法进入「使魔探险」页,跳过收菜")
                raise _Skip("到不了使魔探险页")

            from .engine import Engine, FrameSource, load_config
            cfg = load_config(str(CONFIG_PATH))
            src = FrameSource(cfg, capture=cap, input_controller=cap, window=None)
            eng = Engine(cfg, frame_source=src, input_controller=cap,
                         dry_run=dry, log=log)
            eng.run()
            # 处理完退出任务界面,回到已知状态,免得影响后面的任务
            from .daily import go_home
            go_home(cap, engine, log)
            return

        if key == "shop":
            # 商店 → 礼包商店 → 吼美礼包 → 零时馈礼(×0 免费)
            from .daily import run_shop
            log(f"商店结果:{run_shop(cap, engine, log)}")
            return

        if key == "resonance":
            self._run_resonance(cap, engine, log, dry)
            return

        if key == "fracture":
            # 多元裂缝:使魔的爱(每日) + 虚轴之庭(世界BOSS)
            from .daily import run_familiar_daily, run_world_boss
            run_familiar_daily(cap, engine, log, dry=dry)
            run_world_boss(cap, engine, log, times=3)
            return

        if key in ("stamina1", "stamina2"):
            from .daily import clear_stamina, run_bonus_rounds
            times = None
            if self.opt_times.get():
                try:
                    times = max(1, int(self.entry_times.get()))
                except ValueError:
                    times = None
            # 钻石换体力的允许次数(0 = 不兑换,遇到就结束本次清体力)
            diamond = 0
            if self.opt_diamond.get():
                try:
                    diamond = max(0, int(self.entry_diamond.get()))
                except ValueError:
                    diamond = 0
            tag = "首次" if key == "stamina1" else "再次"
            if diamond:
                log(f"{tag}清体力:已开启钻石兑换体力,允许 {diamond} 次")
            if times:
                n = run_bonus_rounds(cap, engine, log, times=times,
                                     double=self.opt_double.get(),
                                     diamond_buys=diamond,
                                     set_equipment=True)
                log(f"{tag}清体力:{n} 次(指定次数)")
            else:
                out = clear_stamina(cap, engine, log, max_rounds=30,
                                    double=self.opt_double.get(),
                                    diamond_buys=diamond,
                                    set_equipment=True)
                log(f"{tag}清体力:{out['runs']} 次,停止原因:{out['stopped']}")
                if out.get("diamond_bought"):
                    log(f"  其中钻石兑换体力 {out['diamond_bought']} 次")
            return

        if key in ("presence1", "presence2"):
            from .daily import claim_task_rewards
            weekly = (key == "presence2")      # 最后一步才领每周任务
            res = claim_task_rewards(cap, engine, log, weekly=weekly)
            log(f"领取结果:{res}")
            return

        if key == "social":
            from .daily import claim_social
            res = claim_social(cap, engine, log)
            log(f"社交领取:{res}")
            return

        raise _Skip(f"「{TASK_LABELS.get(key, key)}」尚未接入自动化(见 NOTES.md)")

    def _run_resonance(self, cap, engine, log, dry: bool) -> None:
        """共鸣屋刷新 + 按名单购买。

        复用引擎里的 `resonance_refresh` 动作(而不是另写一份),这样命令行
        和界面的行为完全一致。动作需要 Engine 提供的 frame/input/recorder
        等,所以这里构造一个最小 Engine 并直接调用动作。
        """
        import time as _t

        from .engine import Config, Engine, FrameSource, Tick
        from .resonance import load_watchlist, watchlist_path

        names = load_watchlist()
        log(f"名单文件 {watchlist_path()}")
        log(f"名单 {len(names)} 项")
        if not names:
            log("!! 名单为空,只会刷新不会购买")

        # ⚠️ **必须先导航到「共鸣屋」页**。resonance_refresh 只是读格子+刷新,
        # 不含任何导航 —— 之前没导航,结果在别的页面上读格子,把商店的文字
        # (「日常补给包」「日限购：1/1」)当成共鸣屋的装备。
        from .daily import goto_shop_tab, preflight
        preflight(cap, engine, log, "共鸣屋")
        if not goto_shop_tab(cap, engine, log, "共鸣屋"):
            log("    !! 无法进入「共鸣屋」页,跳过共鸣屋任务")
            raise _Skip("到不了共鸣屋页")

        try:
            rounds = max(1, int(self.entry_times.get()))
        except ValueError:
            rounds = 1
        if not self.opt_times.get():
            rounds = 1        # 不指定次数时只刷新免费次数那几次由动作自行处理

        cfg = Config({
            "name": "gui-resonance", "mode": "once",
            "capture": {"backend": "adb"},
            "recognizers": {}, "tasks": {"noop": {"actions": [
                {"action": "log", "message": "x"}]}},
            "steps": [], "session_dir": "sessions",
        }, None)
        src = FrameSource(cfg, capture=cap, input_controller=cap, window=None)
        eng = Engine(cfg, frame_source=src, input_controller=cap,
                     dry_run=dry, log=log)
        action = {
            "action": "resonance_refresh",
            "watchlist": names,
            "refresh_point": [1088, 153],
            "max_refresh": rounds,
            "buy": True,
            "currency_roi": [330, 120, 230, 60],
            "currency_min": 6000,
            "settle": 2.5,
        }
        tick = Tick(frame=cap.grab(), now=_t.time(), results={}, last=None,
                    task="gui-resonance")
        eng.run_actions([action], tick)


def main() -> int:
    app = BotGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
