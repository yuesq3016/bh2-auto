# 崩坏学园2 自动化 — 项目交接文件

> 生成时间：2026-10-23 ｜ 交接完成：2026-10-23
> 用途：工作区迁移后的接手说明。原工作区（`C:\Users\14520\Documents\deepseek-harness\default-workspace\honkai2-bot`）
> 已清空，代码与发布包已迁至下列新位置（迁移验证见 §14）。

| 项 | 路径 |
|---|---|
| 项目根目录（开发用） | `E:\DSH-Workspace\honkai2-bot` |
| 发布包目录 | `E:\DSH-Workspace\honkai2-bot\releases\v1.0.0-20261023` |
| 发布包 zip | `E:\DSH-Workspace\honkai2-bot\releases\honkai2-bot-release-v1.0.0-20261023.zip`（14 MB） |

---

## 1. 这是什么

《崩坏学园2》日常自动化工具。仿 MAA 的设计思路：**任务即数据、识别用模板/颜色、循环用轮询**。

- 纯 Python（无框架依赖），tkinter 图形界面
- 通过 **ADB** 控制 MuMu 12 模拟器（`127.0.0.1:16384`，游戏包名 `com.miHoYo.HSoDv2Original`）
- OCR 用 **MaaFramework PaddleOCR**（PP-OCRv4，模型在 `vendor/ocr-model/`）
- 坐标系基准：**1280×720 参考帧**

### 已验证运行环境
```
Python 3.12.10
numpy 2.5.4
pillow 12.3.0
maafw 5.14.3
pyyaml（必需）
scipy（可选，缺失时自动降级到 numpy FFT）
```

---

## 2. 目录结构

```
honkai2-bot/
├─ simauto/                 # 核心代码（18 个模块，约 1.2 MB）
│  ├─ daily.py              # ★ 主流程 ~2500 行：界面识别、导航、12 个任务的具体实现
│  ├─ gui.py                # ★ tkinter 前端：任务勾选、设置页、日志队列
│  ├─ fullrun.py            # 命令行全流程（从 gui.TASKS 派生，与前端行为一致）
│  ├─ engine.py             # MAA 风格的任务引擎（recognizer/action 流水线）
│  ├─ ocr.py                # PaddleOCR 封装（经 maafw 的 Tasker）
│  ├─ matcher.py            # 模板匹配 NCC + 颜色/条形匹配（纯 numpy，无 cv2）
│  ├─ stagemap.py           # ★ 首通水晶图标检测（颜色 + 滑动窗口密度）
│  ├─ sweep.py              # ★ 首通清理流程（sweep）与通用点击/等待工具
│  ├─ adb_backend.py        # ADB 截图与输入
│  ├─ maa_backend.py        # maafw Win32 控制器（备用截图后端）
│  ├─ resonance.py          # 共鸣屋格子识别 + 装备配置读写
│  └─ cli.py / imaging.py / window.py / ...
├─ templates/               # 16 张模板图（弹窗关闭键、导航按钮、签到图）
├─ vendor/ocr-model/        # det.onnx (4.5MB) / rec.onnx (10.3MB) / keys.txt
├─ configs/                 # 5 个流水线配置 json（启动/收菜/共鸣屋/回收/召唤）
├─ tests/test_simauto.py    # 64 项单元测试
├─ equipment.json           # 装备切换坐标与目标套装
├─ watchlist.txt            # 共鸣屋购买名单
├─ social-rules.json        # 社交页禁用点击规则
├─ 启动.cmd           # 启动器（自动查找 Python）
├─ README.md / NOTES.md     # 使用说明 / 开发笔记
└─ releases/                # 发布包（v1.0.0-20261023 目录 + 同名 zip）
```

**运行期生成目录**（不必备份）：`sessions/`、`configs/sessions/`（调试截图）、`.maa/`（maafw 日志）、`__pycache__/`

---

## 3. 12 个任务与执行顺序

顺序**固定**（不按勾选顺序），在 `gui.py` 的 `TASKS` 列表里定义：

| # | key | 名称 | 前置界面 | 结束回主界面 |
|---|---|---|---|---|
| ① | `signin` | 启动游戏 + 进游戏(关弹窗) | any | ✗ |
| ② | `popups` | 关闭弹窗(公告/维护/签到/促销) | any | ✗ |
| ③ | `signin2` | 领取每日签到 | any | ✗ |
| ④ | `harvest` | 收菜(使魔探险:收菜 + 派出) | familiar | ✗ |
| ⑤ | `shop` | 商店:领免费零时之种 | shop | ✓ |
| ⑥ | `resonance` | 共鸣屋:刷新 + 按名单买 | shop_tab | ✓ |
| ⑦ | `fracture` | 多元裂缝:使魔的爱 + 虚轴之庭 | any | ✓ |
| ⑧ | `stamina1` | 清体力(BONUS) | any | ✓ |
| ⑨ | `presence1` | 存在感(每日任务) | task | ✓ |
| ⑩ | `social` | 社团领取(消息 + 社团) | social | ✓ |
| ⑪ | `stamina2` | 再次清体力(BONUS) | any | ✓ |
| ⑫ | `presence2` | 再次存在感(含每周任务) | task | ✓ |

「前置界面 + 收尾策略」定义在 `gui.py` 的 `TASK_PLAN` 字典：`(want, need_home)`。

执行流程（`gui.py::_run_selected`，`fullrun.py` 同构）：
```
for key in 勾选的任务（按固定顺序）:
    ① ensure_task_start(key)   # 前置检查：把游戏带到需要的页面
    ② _run_one(key)            # 执行任务
    ③ finish_task(key)         # 收尾：关弹窗 + 按需回主界面
```

---

## 4. 关键常量（1280×720 坐标）

### 顶部导航
| 目标 | 坐标 |
|---|---|
| 首页 | (655, 81) |
| 战斗 | (770, 80) |
| 装备 | (879, 80) |
| 商店 | (993, 81) |
| 社交 | (1104, 80) |
| 其他 | (1220, 82) |

### 返回按钮（⚠️ 区分场景，这是踩坑重灾区）
| 场景 | 坐标 | 说明 |
|---|---|---|
| 关卡详情页 / BONUS 页 / 结算页 | **(57, 140)** 左上角 | 右上角是活动标题，点它没反应 |
| 活动地图 / 多元裂缝 / 虚轴之庭 | **(1168, 157)** 右上角 | 文字随层级变化 |
| 空想迷域 | **(63, 31)** 左上角 | 右上角无效 |
| 任务界面 | (57, 140) 左上角 | 点「首页」退不出去 |

`BACK_BUTTON = (1168,157)`，`BACK_BUTTON_ZONE = (1040,125,240,70)`，`BACK_ARROW = (57,140)`

### 关卡地图
| 目标 | 坐标 |
|---|---|
| 左翻页 | (551, 665) |
| 右翻页 | (737, 665) |
| 分页指示区 | `PAGE_INDICATOR_ZONE = (520,630,260,80)` |
| 「难度指数：B」标记 | OCR 定位，点击 = 标记**(x, y+45)** |
| BONUS 节点 | 「BONUS」文字**(x, y+57)** |
| 首通水晶图标 → 节点 | 图标 **(x+45, y-20)**（`CRYSTAL_OFFSET`） |

### 战斗相关
| 目标 | 坐标 |
|---|---|
| 出击（蓝色三角，纯图形） | (688, 531) |
| 快捷战斗 | (996, 650) |
| 选择助战好友 | (731, 650) |
| 数量对话框「确定」 | (559, 472) |
| 开战 | (826, 664) |
| 结算「确定」 | (787, 616) |
| 再次挑战 | (640, 617) |
| 双倍券勾选（关卡页） | (1098, 537) |
| 双倍券勾选（结算页） | (700, 546) |
| 补充体力弹窗：兑换 / 取消 | (533, 459) / (746, 458) |

### 商店 / 共鸣屋
| 目标 | 坐标 |
|---|---|
| 礼包商店卡片 | (514, 291) |
| 左侧分类「吼美礼包」 | (72, 299) |
| 崩坏屋卡片（商店首页） | (1012, 293) |
| 共鸣屋子页签 | (80, 310) |
| 共鸣屋刷新 | (1088, 153) |

### 装备切换
| 步骤 | 坐标 |
|---|---|
| 手枪图标（开装备页） | (1146, 202) |
| 「切换」（展开方案） | (1206, 157) |
| 套装条目（示例 C01） | (280, 391) |
| 「选择方案」 | (1071, 647) |
| 返回关卡页 | (57, 140) |

### 任务界面
| 目标 | 坐标 |
|---|---|
| 任务按钮（主界面左列第一个） | (48, 178) |
| 页签「存在感」 | (75, 308) |
| 页签「每周任务」 | (75, 381) |
| 页签「使魔探险」 | (73, 455) |

### 弹窗关闭键（模板匹配）
| 弹窗 | 模板 | ROI | 阈值 |
|---|---|---|---|
| 促销/活动广告 | `popup_close_x.png` | (950,120,180,140) | **0.80** |
| 公告面板 | `announce_close_x.png` | (1050,30,220,110) | 0.85 |
| 看板礼包/限时发售 | 文字「今日不再提示」 | — | 0.9 |

---

## 5. 界面识别（`daily.py::recognize_screen`）

**判断顺序至关重要**（深的先判，否则被浅规则抢走）：

```
force_update  ⛔ 强制更新（最优先，绝不点更新）
battle        「HP」+「MAZING」/「WAVE」
settle        「战斗结算」/「再次挑战」
title         「账号切换」/「屏幕继续」/「适龄提示」/「CADPA」
support       助战列表（≥2 个「友情点」+「LV.」）
activity      活动页（三页签 主线/活动/挑战 齐全）
maze          空想迷域
tower         崩坏之塔（「崩坏之塔」+「层」+「首通奖励/奖励总览」）
boss          虚轴之庭（LEVEL + 兑换 + 开放）
bonus         BONUS 关卡页（BONUS + 消耗体力 + 操作入口）
familiar      使魔探险页（「使魔探险将于」/「已接取的探险数」/「点击选择使魔」）
task          任务界面（三页签齐全 + 侧栏特征）
signin        每日签到弹窗
assemble      关卡地图（分页 N/M，或兜底：有「BONUS」文字）
stage         关卡详情（消耗体力 + 选择助战好友/补充体力/快捷战斗）
activity      （二次判）
fracture      多元裂缝（多元裂缝 + 使魔的爱/虚轴之庭）
shop          商店（礼包商店/水晶商店/吼美礼包…）
shop_tab      商店子页（共鸣屋/崩坏屋/回收屋/万神屋）
social        社交（我的社团/我的消息）
home          主界面
unknown
```

`SCREEN_LABELS` 提供中文名用于日志。

### `_at_home` 判据（易误判，务必小心）
要求：导航六页签齐全 **且** 不含 `not_home` 关键词。

`not_home` 目前包含（每次遇到新的"伪装成主界面的弹窗"都要往这里加）：
```
存在感 每周任务 使魔探险 今天没有任务 主线 挑战 开放 LEVEL 难度指数
剩余时间 消耗体力 一键领取 BONUS 礼包商店 我的社团 我的消息
吼美礼包 心意商店 常驻礼包 月卡礼包 限购 购买 期间限购 剩余 月卡
超绝心愿 梦想祈愿 看板礼包 限时发售 今日不再提示
```

---

## 6. 核心设计决策（为什么这么做）

### 6.1 按「难度等级」而非活动名找活动
活动名每期都换（实测：拼装时光 → 海妖月光秘境 → 海妖秘境），但目标活动**永远是「难度指数：B」**。
所以锚点用难度标记，不用名字。

### 6.2 多活动并存要逐个尝试
活动页可能**同时有多个「难度指数：B」**（实测 conf 0.995 和 0.984 两个）。
`find_difficulty_card` 只挑最高分会选错 → 改用 `find_difficulty_cards` 返回全部，
`goto_activity_map` **逐个点击尝试**，哪个能进关卡地图就用哪个。

### 6.3 不要用 ROI 裁剪做 OCR 过滤
实测：同一个「难度指数：B」全屏识别 conf=0.995 完整，
但 `engine.recognize(frame, CARD_AREA)` 裁剪后变成「难度指数」（冒号和 B 被截断）。
**结论：全屏 OCR + 坐标过滤，不要裁图。**

### 6.4 首通靠颜色而非 OCR
关卡地图的关卡标签是小号描边字，OCR 极不可靠。
但「首通奖励未领」的**水晶图标**是实心青色宝石，颜色稳定：
- 真图标：亮青蓝，`g∈(185,225) & b>215`，40×40 窗口内密度 ≥150 px
- 海洋主题背景会产生假阳性（分散亮点，密度 124~130）→ 用**滑动窗口密度**区分

`SCAN_REGION = (150, 200, 1130, 400)`（排除顶部导航和底部按钮）
`MIN_CYAN_PIXELS = 150`（40×40 窗口密度阈值）

### 6.5 BONUS 节点也有水晶，要排除
BONUS 节点首通未领时**同样有水晶图标**，但它属于清体力阶段。
判据：水晶**上方 100~180px、x 相差 ≤80px** 处有「BONUS」文字 → 判定为 BONUS，首通清理时跳过。

### 6.6 清体力 = 先清首通，再打 BONUS
`clear_stamina(clear_first=True)`：
1. `goto_activity_map()` 进活动关卡地图
2. `sweep()` 打掉所有带水晶的首通（**装备只在第一次开战前切一次**）
3. `goto_bonus_stage()` 找 BONUS 节点打 BONUS（双倍券 → 再次挑战循环）

### 6.7 装备切换只在第一次开战前
`_switch_loadout_once` / `sweep` 的 `equipped` 标志 —— 后续「再次挑战」会保留装备，不必重复切。
首通阶段切过之后，`clear_first_clear` 返回 `equipped=True`，BONUS 阶段直接复用。

### 6.8 虚轴之庭原地继续，不返回多元裂缝
- 免费次数可用：详情页底部有「快捷战斗」(996,650) → 点它 → 数量确定 → 战斗
- 打完点结算「确定」(787,616) → **界面原地回到详情页**（不返回多元裂缝）
- 免费次数耗尽：界面变成「消耗体力N」+「选择助战好友」，**按钮点不动**
- **用户明确要求：打够次数就停，不消耗体力继续**

### 6.9 补充体力后点「再次挑战」而不是「确定」
`_retry_after_refill` —— 用户明确要求。

### 6.10 体力/次数不足 = 跳过当前任务，不是失败
抛 `Insufficient` 异常，上层记为「跳过」。

---

## 7. 已修复的坑（按症状索引）

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | 点「战斗」后识别成「未知界面」，反复回主界面死循环 | 点战斗会进入**上次停留的子页面**（关卡地图/多元裂缝/崩坏之塔），不是三页签选择页 | `goto_activity` 点击后**快速分流**：已在活动页直接用；进了深层界面点返回；三页签齐全才点「活动」 |
| 2 | 活动页点右上角(1168,157)越陷越深 | 活动页右上角是**活动入口标签**（崩坏之塔/拼装时光），不是返回按钮 | 活动页/塔/商店/社交 → 走 `go_home`（点首页）；只有真正深层界面才 `back_out` |
| 3 | 存在感任务从关卡页退出卡死 | 关卡详情页返回按钮在**左上角(57,140)**，右上角是活动标题 | `open_task_tab`/`go_home` 里 bonus/stage/settle → 点左上角 |
| 4 | 商店找不到「零时馈礼」的购买按钮 | 卡片名与购买按钮**垂直距离约 296px**（中间隔着价格/限购），名字在屏底时按钮还在屏外 | 找到名字后继续下滚；**购买按钮必须满足 `y > 名字y`**（否则点到上一行的，日志"买成功"其实没买） |
| 5 | 共鸣屋找不到「崩坏屋」入口 | 礼包商店内部被 `_at_home` 误判为主界面，`go_home` 假返回 | `not_home` 加：吼美礼包/心意商店/常驻礼包/月卡/限购/购买 |
| 6 | 使魔的爱找不到「多元裂缝」卡片 | 活动页右列**残留滚动位置**，直接下翻从半山腰开始越滚越远 | **先上滚 3 次回顶部（重置），再往下找**（用户给的方案） |
| 7 | 启动卡在「超绝心愿/梦想祈愿」广告 | 新广告 X 匹配 popup_close_x 只有 **0.849**，阈值 0.85 差 0.001 | 该模板阈值降到 **0.80**（主界面误匹配仅 0.36~0.43，安全） |
| 8 | 任务按钮点 4 次全超时、任务被跳过 | 三层登录弹窗（超绝心愿 → 看板礼包 → 活动公告）盖住主界面 + `open_task_tab` 不清理弹窗 | `close_popups` 关键词加这三个广告 +「今日不再提示」；`open_task_tab` **点任务按钮前先 `close_popups`** |
| 9 | 虚轴之庭第 2 次「找不到卡片」 | 每轮打完点 `BACK_ARROW` 返回多元裂缝，再找卡片 | 去掉返回；结算点确定后**原地继续** |
| 10 | 崩坏之塔识别成 Unknown | 识别器没有这个界面 | 新增 `tower` 类型 + 各处分流 |
| 11 | 强制更新弹窗死循环 | — | `force_update` 加入 `FATAL_SCREENS`，**绝不点「更新」** |
| 12 | 签到漏领 | 旧模板对不上新版弹窗 | 改用 OCR（「每日签到」「第N天」识别） |
| 13 | 切换装备前端交互差 | — | 改成两个下拉框：①A/B/C自定义组 ②01-10 组内编号 |
| 14 | 使魔派遣从下往上选 | 引擎 `_pick`/`_best` 按置信度/长度排序 | 改为**按 y 升序**（从上往下） |
| 15 | 自己先开好游戏再运行，「启动游戏」仍报启动失败/等前台超时 | `ensure_game_running` 只看 `pidof`，且等前台超时就直接判失败；循环里还反复 `start_app` | 已在运行 = 成功：不重复启动、只 `monkey` 切前台；超时但进程还在也不报错；`start_app` 失败但进程在跑同样算成功 |

---

## 8. 活动换代适配指南 ⭐

**当新活动上线时，按这个顺序检查：**

### 步骤 1：确认能否找到活动卡片
```python
from simauto.daily import find_difficulty_cards
cards = find_difficulty_cards(engine, frame, "B")
```
- 返回空 → 活动不是难度B，或难度标记格式变了（检查 `"难度指数"` 关键词）
- 返回多个 → 正常，`goto_activity_map` 会逐个尝试

### 步骤 2：确认能否进关卡地图
跑清体力，看日志是否出现 `✓ 通过「难度指数：B」进入关卡地图`。

### 步骤 3：确认首通检测是否正常
```python
from simauto.stagemap import find_first_clear_markers
ms = find_first_clear_markers(frame, require_map=True, engine=engine)
```
关卡地图上有几个未领首通的关卡，就应返回几个。
- **返回 0 但实际有首通** → 新活动背景色干扰，调 `stagemap.py::_cyan_mask` 的阈值
  （当前：`g∈(185,225) & b>215`；海洋背景就是这样调出来的）
- **返回多个假的** → 提高 `MIN_CYAN_PIXELS`（当前 150）

### 步骤 4：确认 BONUS 节点存在
在关卡地图上找「BONUS」文字。找不到 → 新活动可能没有 BONUS 机制，
清体力会停在 `无法导航到 BONUS 关卡`，需要重新设计清体力策略。

### 步骤 5：确认关卡地图结构
- 是否分页（`1/2` `2/2`）？分页位置是否还是 `(520,630,260,80)`？
- 首通节点的水晶图标到可点节点的偏移 `(45,-20)` 是否还准？
  （验证方法：点一下看能否进关卡详情页）

### 步骤 6：确认虚轴之庭/多元裂缝
- 活动页能否找到「多元裂缝」卡片
- 虚轴之庭详情页的「快捷战斗」是否还在 (996,650)

---

## 9. 已知遗留问题

| 项 | 状态 | 说明 |
|---|---|---|
| 活动换代的真实测试 | ⚠️ 未验证 | 逻辑上覆盖了换名/多活动/单页地图/活动结束残留，但需等下个活动上线实测 |
| 虚轴之庭连续打多次 | ⚠️ 部分验证 | 「打够就停」已验证；「有免费次数时连续原地打」需次数充足时验证 |
| 首通检测阈值 | ⚠️ 硬编码 | 换主题背景（非海洋）可能需要重新标定 `_cyan_mask` 阈值 |
| MAAHG2 的 Equip 功能（校园/实验室） | ❌ 未实现 | 用户任务列表里没有此项需求 |

---

## 10. 调试方法论（遇到问题怎么查）

### 三件套
```python
from simauto.adb_backend import AdbBackend
from simauto.ocr import OcrEngine
from simauto.daily import recognize_screen, SCREEN_LABELS, _texts, _at_home
from simauto.imaging import save_image

e = OcrEngine.shared()
b = AdbBackend()
f = b.grab()
save_image('sessions/debug.png', f)          # 存图，人工看
print(recognize_screen(e, f))                # 看识别成什么
print('_at_home =', _at_home(e, f))          # 看是否被误判成主界面
for t, s, loc in sorted(_texts(e, f), key=lambda h:(h[2][1], h[2][0])):
    print(f'{t!r:26} {loc} conf={s:.2f}')    # 全屏文字与位置
```

### 关键心法
1. **永远先识别，再动作** —— 不要假设当前在哪个界面
2. **每个动作都要验证** —— `click_and_wait` 带等待条件，不睡固定秒数
3. **找不到按钮时先怀疑"不是那个界面"** —— 而不是"按钮改了"
4. **日志要能看出"点在哪、期望什么、实际是什么"**
5. **新增任何"伪装成主界面的弹窗"，必须加进 `_at_home` 的 `not_home` 列表**

### 常用命令
```powershell
# 编译 + 测试
python -c "import py_compile,glob; [py_compile.compile(x,doraise=True) for x in glob.glob('simauto/*.py')]"
python tests\test_simauto.py          # 应输出 all 64 checks passed

# 跑单个任务
python -m simauto.fullrun --only 商店 --log sessions\run-shop.log

# 启动前端
.\启动.cmd
```

---

## 11. 参考项目 MAAHG2 的评估结论

**位置**：`C:\Users\14520\Downloads\MAAHG2-win-x86_64-v1.1.0`（v1.1.0 Release，非 git 仓库）
MaaFramework ProjectInterface 变体，11 个 pipeline JSON / 81 个节点。

### 结论：没有值得吸收的缺失功能，且它的做法普遍更脆弱

| 维度 | MAAHG2 | 本项目 |
|---|---|---|
| 活动识别 | **硬编码活动名**「海湾障碍赛」 | 按「难度指数：B」✅ |
| 多活动并存 | 无处理 | 全候选逐个尝试 ✅ |
| 任务数 | 7 项 | 12 项 ✅ |
| BONUS 偏移 | `[0, 50]` | `57`（实测） |
| 「出击」OCR 拆分 | 双候选 `["出击","击"]` | 固定坐标 (688,531) ✅ |
| 点击后等待 | `post_wait_freezes`（画面冻结） | `click_and_wait`（等具体条件）✅ |
| 打完续战 | 每轮重新导航 | 原地继续 ✅ |

**可借鉴**：`target_offset` 思路（文字锚点 + 固定偏移），我们已在用（BONUS+57 / 难度+45 / 水晶+45,-20）。

**不可借鉴**：硬编码活动名（换期即失效）。

**它的独有功能**（我们未做）：`Equip` 任务 —— 校园 `EnterCampus` / 实验室 `EnterLabBuilding` / `AddServant` / `ReceiveIncome` / `FightInCampus`。用户需求里没有，暂不实现。

**它的打包缺陷**：`interface.json` 声明「官服」指向 `assets/resource/base`，但该目录不存在（只有 `mix`）。

---

## 12. 新工作区上手指南

1. 项目已在 `E:\DSH-Workspace\honkai2-bot`（迁移与验证已完成，见 §14）
2. 验证环境：
   ```powershell
   cd E:\DSH-Workspace\honkai2-bot
   python -c "import py_compile,glob; [py_compile.compile(x,doraise=True) for x in glob.glob('simauto/*.py')]; print('OK')"
   python tests\test_simauto.py
   ```
   预期：18 模块编译 OK + `all 64 checks passed`
3. 连接模拟器：
   ```powershell
   & "D:\MuMuPlayer\nx_device\12.0\shell\adb.exe" connect 127.0.0.1:16384
   & "D:\MuMuPlayer\nx_device\12.0\shell\adb.exe" devices
   ```
4. 启动前端：`.\启动.cmd`（或双击桌面快捷方式）
5. 在「设置」页确认：设备地址 `127.0.0.1:16384`、装备组/套装
6. 建议第一次先**只勾 ①启动游戏**，确认广告能关掉，再全选

### 桌面快捷方式
`C:\Users\14520\Desktop\崩坏学园2 自动化.lnk` → 指向 `E:\DSH-Workspace\honkai2-bot\启动.cmd`
（交接时已同步更新「目标 / 起始位置 / 图标」；之后再移动项目，需右键 → 属性 → 同步改这三项。）

---

## 13. 给接手者的三条建议

1. **所有常量都来自实测**，不是拍脑袋。改任何一个坐标前，先截图验证。
2. **日志是唯一真相来源**。这个项目 90% 的 bug 都是"在错误的界面上找东西"，而日志里的"当前界面是 XX"通常已经说明了问题。
3. **活动换代是常态**。凡是依赖"活动名""关卡名"的逻辑都会过期 —— 尽量用难度等级、颜色、相对偏移这些稳定特征。

---

## 14. 交接记录（2026-10-23）

| 项 | 结果 |
|---|---|
| 新位置 | `E:\DSH-Workspace\honkai2-bot`（代码 / 发布包 / 文档齐全） |
| 模块编译 | 18 个模块全部 OK |
| 单元测试 | `all 64 checks passed` |
| 启动冒烟 | 从新目录启动 GUI 正常（窗口正常出现，测试后已关闭） |
| 发布包 | `releases\v1.0.0-20261023\` 62 个文件；同名 zip 校验通过（可完整解压） |
| 启动器 | `启动.cmd` 改为自动查找 Python（PATH → 常见安装位置） |
| 文档路径 | `README.md` / `NOTES.md` 里的旧绝对路径已替换 |
| 桌面快捷方式 | 已指向新路径（目标 / 起始位置 / 图标） |
| 原路径 | `C:\Users\14520\Documents\deepseek-harness\default-workspace\honkai2-bot` 已清空并删除（其中仅剩 `.maa\debug\maafw.log` 日志，一并清除） |

> 清空旧路径时遇到一个残留：旧目录里还开着一个**从旧路径启动的 GUI 实例**，
> 占着 `.maa\debug\maafw.log` 导致文件夹删不掉。已用正常关闭消息让它退出，随后删除。
> 以后再迁移项目，先关掉从旧目录启动的 GUI / 命令行任务，否则旧目录清不干净。

### 迁移时必须做的一步：文件夹权限

整包从旧工作区搬过来的目录会**沿用旧目录的权限设置**（禁用继承、缺「低完整性」标记）。
症状：**能读不能写** —— Python 写 `__pycache__`、测试写 `tests\_tmp`、运行时写 `sessions/`
全部报 `PermissionError`，而在工作区里新建的文件夹却一切正常。

修复（文件夹所有者即可，不需要管理员）：
```powershell
icacls '<项目路径>' /inheritance:e /T /C
icacls '<项目路径>' /grant:r "${env:USERNAME}:(OI)(CI)F" /T /C
icacls '<项目路径>' /setintegritylevel '(OI)(CI)Low' /T /C
```
权限修改前的备份与回滚命令在 `C:\Users\14520\.dsh\acl-reports\`。
以后再把外部目录搬进这个工作区，先做这三步，否则沙箱里的工具一律写不进去。

---

## 15. GitHub 仓库与发布（2026-10-28 迁回）

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/yuesq3016/bh2-auto（PUBLIC） |
| 描述 | 崩坏学园2自动化挂机脚本 \| Assistant for Houkai Gakuen 2 / Guns Girl Z · Python 实现，基于 MAAHG2 |
| 最新发布 | `v0.1.1`（此前 `v0.1.0`） |
| 发布产物 | `simauto-v0.1.0/v0.1.1-win64.zip`、`Simauto-win-x86_64-v0.1.1.zip`、免安装 exe |
| 本地产物 | `dist/`（已 gitignore，不入库） |

**仓库回复监控**：`bash tools/check-replies.sh`（默认查 `yuesq3016/bh2-auto`），
只报上次检查之后的新 issue / PR / 评论，时间戳存在 `.state/replies.json`（不入库）。
会话里挂了每天 **10:00 / 21:00** 两条提醒跑这个脚本。

发布时本机走 `HTTPS_PROXY=http://127.0.0.1:7897`，直连不稳；只读查询直连可以。

