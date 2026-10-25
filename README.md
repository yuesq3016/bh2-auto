# simauto —— 模拟器图像识别脚本(崩坏学园2 参考实现)

一个**跑在 Windows 上、面向安卓模拟器**的图像识别自动化小工具。设计照着
**MAA / MaaFramework** 的思路做:任务即数据、识别用模板匹配、循环用轮询而不是状态机。

- **零依赖可跑**:只用 `numpy` + 标准库时,截图走自写的 Win32 GDI,模板匹配走自写的 NCC。
- **装上 `maafw` 更强**:自动改走 MaaFramework 的 Win32 控制器,多出
  `FramePool` / `PrintWindow` 后台截图 —— 这是 GPU 渲染模拟器窗口(`PrintWindow`/`BitBlt` 全黑)**唯一可靠的解法**。
- **不需要 ADB**,不需要 root,不需要注入。

> 崩坏学园2 的模板图**必须你自己抠**。本仓库不附带任何游戏素材,原因见文末「许可证」。

> **免责声明**:本项目与米哈游 /《崩坏学园2》官方无关,不含任何游戏素材;用自动化操作游戏可能违反游戏用户协议并导致封号,风险自负,仅供学习与自用。

---

## 1. 三个后端:窗口读屏 vs ADB

本机在 **MuMu 模拟器 12**(`MuMuNxDevice`,1280×720)上的**实测结果**:

| 后端 | 帧耗时 | 能后台吗 | 实测 |
|---|---|---|---|
| 自带 GDI `printwindow_fullcontent` / `printwindow_plain` | — | — | ❌ 返回 failure |
| 自带 GDI `bitblt_client` | 9 ms | ❌ | ❌ 全黑(mean=0.0) |
| 自带 GDI `screendc` | **28 ms** | ❌ | ✅ mean≈110 |
| maafw `FramePool`(WGC) | — | — | ❌ `CreateForWindow GraphicsCaptureItem` **access denied** |
| maafw `PrintWindow` | — | — | ❌ **error 5** |
| maafw `GDI` / `DXGI_DesktopDup_Window` | — | — | ❌ 失败 |
| maafw `ScreenDC` | **33 ms** | ❌ | ✅ 选中的就是它 |
| **adb `RawByNetcat`** | **454 ms** | ✅ | ✅ 最快 |
| adb `Default`(自动测速) | 560~690 ms | ✅ | ✅ |
| adb `EmulatorExtras`(MuMu 专用) | — | — | ❌ 扩展库加载失败 |

**结论:窗口类后端一个都不能后台。** 而 **ADB 完全不受窗口状态影响**,这一点做了对照实验:

| 窗口状态 | `adb` 抓到的均值 | `screendc` 抓到的均值 |
|---|---|---|
| MuMu 可见 | **151.97** | 193.84(游戏画面) |
| 被 PowerShell 遮住 | **151.97** | 52.32 ← **拍到的是控制台** |
| MuMu 最小化 | **151.97** | 193.85(拍到别的东西) |

ADB 三次**完全一致**,`screendc` 则随遮挡物乱跳。

### 怎么选

| 你的场景 | 用哪个 |
|---|---|
| 想让机器人后台跑(可以盖住模拟器、去用电脑) | `--backend adb` |
| 要快、要跟得上动作节奏 | `--backend maafw`(或 `gdi`)+ **窗口保持可见** |
| 想两全 | 用 ADB 跑流程,关键时刻切窗口后端 |

```jsonc
"capture": { "backend": "maafw" }                            // 33 ms/帧,窗口必须可见
"capture": { "backend": "gdi" }                              // 等价,零依赖
"capture": { "backend": "adb", "address": "127.0.0.1:16384" } // 后台可用,~2 fps
```

**为什么 ADB 也只有 2 fps**:MaaFramework 的 ADB 截图要在设备端编码再经 adb 传输,
1280×720 一帧约 2.7 MB,`RawByNetcat` 已经是里面最快的。这与窗口读屏的 33 ms 差了 14 倍。
崩坏学园2 是快节奏动作游戏,2 fps 基本只能做"无脑长按攻击"这类不需要反应的操作。

> 复核命令:`simauto verify`(strict 逐方法测窗口后端),`simauto adb`(列设备 + 实测帧率)。
> `find`/`probe` 也已改成 strict,不再把"回退链的结果"冒名成某个方法。

`maafw` 的 `screencap` 是**按位或**,MaaFramework 自己探测;各方式特性:

| 名字 | 枚举 | 速度 | 兼容 | 后台 |
|---|---|---|---|---|
| `gdi` | GDI | 快 | 中 | 否 |
| `framepool` | FramePool | 很快 | 中 | **是**(Win10 1903+) |
| `dxgi_desktopdup` | DXGI_DesktopDup | 很快 | 低 | 否 |
| `dxgi_desktopdup_window` | DXGI_DesktopDup_Window | 很快 | 低 | 否 |
| `printwindow` | PrintWindow | 中 | 中 | **是** |
| `screendc` | ScreenDC | 快 | 高 | 否 |
| `foreground` / `background` | 预设组合 | — | — | — |

输入方式:maafw 用 `--mouse`(`postmessage` 默认、不抢鼠标 / `sendmessage` / `seize` /
`anchoredtouch` / `interception`……);ADB 用 `--adb-input`
(`maatouch` 实测 51 ms 最快 / `adb` 177 ms / `default`)。

---

## 2. 安装

### 方式 A:不用装 Python,也不用管理员权限(推荐先试这个)

本机上原本**没有可用的 Python**:`python.exe` 只是 Microsoft Store 的 0 字节占位符,
`py` 启动器不存在,而工具自带的运行时其 `site-packages` 和 `%APPDATA%\Roaming` **都不可写**,
所以 `pip install` 必然失败(会报 `Permission denied`)。

但 wheel 本质就是个 zip,可以直接解包到项目里用:

```powershell
cd honkai2-bot
powershell -ExecutionPolicy Bypass -File tools\setup-maafw.ps1
```

脚本会:

1. 从 PyPI 取 `maafw`(36 MB,含 `MaaFramework.dll`)、`strenum`、`MaaAgentBinary`;
2. **解包到 `honkai2-bot\vendor\`**,并写入一个 `.pth`;
3. `simauto` 启动时自动把 `vendor\` 加进 `sys.path` —— 不用 pip、不用改环境变量、不动系统目录;
4. 最后跑一次 `simauto.maa_available()` 自检并打印结果。

`-SkipAgentBinary` 可跳过 5 MB 的安卓 minitouch 辅助文件(Win32 窗口截图用不到);
`-Force` 重新下载解包。

### 方式 B:标准做法(装完 Python 之后)

```powershell
winget install -e --id Python.Python.3.12
# 重开一个终端
python -m pip install maafw numpy pillow
```

`maafw` 的 import 名是 `maa`,**别装成 `maa` 或 `maa-framework`**(前者是别的包,后者不存在)。

### 怎么确认装好了

```powershell
python -m simauto doctor
```

看到 `[ok] maafw present: FramePool/PrintWindow screencap available` 就成功了。
反之 `[info] maafw not installed (optional)` 表示还在用 GDI 后端 —— 功能可用,
但**后台截图做不到**,必须让模拟器窗口保持前台。

### 如果两个方式都不行

那说明本机确实需要一个正常安装的 Python。这种情况下 `probe` 里 `screendc` 后端仍然可用,
只是要求模拟器窗口在最前、不被遮挡;用 `grab --focus` 可以先把它激活。

## 3. 六步走完第一次运行

> ⚠️ **先 `cd` 进项目目录**,否则会报 `No module named simauto`:
>
> ```powershell
> cd E:\DSH-Workspace\honkai2-bot
> ```
>
> `simauto` 没有被安装进 site-packages(这台机器的 Python 环境不允许),
> 所以它**只能从项目目录里调用**。三种解决办法任选:
>
> 1. 每条命令前加 `cd 项目目录;`
> 2. 用全路径启动器(任何目录都能用,立即生效):
>    ```powershell
>    & "E:\DSH-Workspace\honkai2-bot\bin\simauto.cmd" probe --title MuMu
>    ```
> 3. 把 `bin` 加进用户 PATH(需重开终端),之后可直接写 `simauto probe ...`:
>    ```powershell
>    [Environment]::SetEnvironmentVariable('PATH', [Environment]::GetEnvironmentVariable('PATH','User').TrimEnd(';') + ';E:\DSH-Workspace\honkai2-bot\bin', 'User')
>    ```

```powershell
python -m simauto list                      # 1. 找到模拟器窗口
python -m simauto doctor                    # 2. 环境体检 + 列出疑似模拟器
python -m simauto probe --title MuMu        # 3. 逐个后端实测,告诉你哪个能用
python -m simauto grab  --title MuMu -o frame.png          # 4. 截图
python -m simauto crop  --frame frame.png --gui -o templates/attack.png   # 5. 拖框抠模板
python -m simauto match --frame frame.png --template templates/attack.png --calibrate
python -m simauto run   --config configs/bh2-starter.json --debug --dry-run
```

第 6 步先加 `--dry-run`:识别照跑、**一次都不点**,你从日志确认它认对了位置,再去掉。

### `find` 之外的排查工具

```powershell
python -m simauto tree --title MuMu --depth 3      # 打印子窗口树
python -m simauto probe --hwnd 0x005E15BC          # 直接探测某个子窗口句柄
python -m simauto verify --title MuMuNxDevice      # 后台截图到底行不行(strict 逐方法)
python -m simauto adb                              # 列 ADB 设备 + 实测帧率
python -m simauto validate --config configs/bh2-starter.json   # 检查配置和模板文件是否齐全
```

`simauto adb` 会自动在 PATH 和常见模拟器目录里找 `adb.exe`(MuMu 的就在
`D:\MuMuPlayer\nx_device\<版本>\shell\adb.exe`),并自动尝试 `127.0.0.1:16384`、
`7555`、`62001` 等已知端口。

切换后端:

```powershell
python -m simauto grab --title MuMuNxDevice --backend maafw -o frame.png
python -m simauto grab --backend adb -o frame.png          # adb 不需要 --title
python -m simauto run --config configs\bh2-starter.json --backend adb
```

模拟器常把画面渲染在**子窗口**里。顶层句柄全黑时,用 `tree` 找到真正有画面的那个句柄,再 `probe --hwnd`。

---

## 4. 配置格式

JSON(或 YAML,需 `pip install pyyaml`)。坐标一律按 **`reference_resolution`(默认 1280×720)** 写,运行时自动缩放到实际窗口尺寸。

```jsonc
{
  "name": "崩坏学园2 · 入门",
  "reference_resolution": [1280, 720],

  "window":  { "title": "MuMuNxDevice" },
  "capture": { "backend": "auto", "screencap": "background", "method": "auto" },
  "input":   { "method": "postmessage", "click_delay": 0.03 },
  "defaults": { "check_every": 0.05, "click_jitter": 3 },

  // 识别器:在哪、用什么方法、阈值多少
  "recognizers": {
    "start":   { "method": "template", "template": "templates/start.png",
                 "roi": [1020, 560, 220, 100], "threshold": 0.87 },
    "attack":  { "method": "template", "template": "templates/attack.png",
                 "roi": [1010, 500, 230, 180], "threshold": 0.85 },
    "hp":      { "method": "bar", "bands": [[[0, 120, 90], [25, 255, 255]]],
                 "roi": [56, 30, 300, 10], "threshold": 0.35 }
  },

  // 任务 = 触发器 + 动作,按 priority 顺序轮询,第一个命中的执行
  "tasks": {
    "startStage": {
      "triggers": [{ "recognizer": "start", "cooldown": 1.0 }],
      "actions":  [{ "action": "click" }, { "action": "wait", "seconds": 1.5 }]
    },
    "fight": {
      "triggers": [{ "recognizer": "attack", "min_interval": 0.12 }],
      "actions":  [{ "action": "click", "target": "attack", "jitter": 6 }]
    }
  },
  "priority": ["startStage", "fight"]
}
```

### 识别方法

| `method` | 用途 | 关键字段 |
|---|---|---|
| `template` | 图标/按钮/文字按钮(主力) | `template`(可为数组,取最高分)、`roi`、`threshold`、`scales`(`"0.9:1.1:0.05"` 多尺度) |
| `color` | 纯色区域、配色判定 | `bands`/`color_scales`、`space`(`hsv`/`rgb`)、`min_pixels` |
| `bar` | 血条/进度条,不用 OCR | `bands`、`roi`;返回填充比例作为 `score` |

`template` 与 `color`/`bar` 的分数语义统一为「越大越像」。模板匹配是零均值归一化互相关
(`TM_CCOEFF_NORMED` 等价实现),**不是**简单像素差,所以亮度整体变化不会影响分数
(实测:全图 +45 亮度后同一位置仍得 0.96)。

⚠️ **红色的 HSV 区间要写环绕**,否则永远匹配不到:

```jsonc
"bands": [[[350, 90, 90], [25, 255, 255]]]   // ✅ 跨 0/360,匹配 hue≈357 的红
"bands": [[[0, 90, 90],  [25, 255, 255]]]    // ❌ 匹配不到红色
```

⚠️ **全屏搜索很贵**。实测 1280×720 全屏找一个 40×40 模板约 **600 ms**,
而限定在 200×180 的 ROI 里只要 **~15~40 ms**。所以每个识别器都要给 `roi` —— 这既是
MAA 的经验,也是这套实现能不能跑到 10 fps 的关键。

### 动作

`click`(支持 `point`/`target`/`count`/`hold`/`jitter`)、`key`、`swipe`、`wait`、
`wait_for`(等某识别器出现,带超时)、`wait_for_result`(等到任一触发器命中)、
`repeat`(`times`+`until`)、`if`/`then`/`else`、`screenshot`、`log`、`set`、`stop`、`abort`。

条件支持表达式:`"score >= 0.8"`、`"fill < 0.3"`、`"!${flag}"`、`["${n}", ">", "3"]`。

### 三种运行模式

- `events`(默认):每个 tick 按 `priority` 轮询所有任务,第一个命中就执行 → 最适合游戏主循环。
- `loop`:按 `loop.sequence` 顺序跑一轮算一回合,可 `rounds` 限制。
- `once`:只跑一遍顶层 `steps`,适合调试和固定脚本。

---

## 5. 崩坏学园2 该怎么配

**能确证的**(有公开资料):这是米哈游 2014 年的 **2D 横版动作/闯关**游戏,
战斗界面是**左侧虚拟摇杆移动 + 右侧武器按键开火/切枪**,左上角有"基友"(助战)图标。

**没能确证的**:血条精确位置、武器键数量与坐标、技能/大招按钮、暂停按钮、结算界面、复活对话框
—— 这些**必须从你自己的截图里量**,本仓库不写任何猜测坐标。

建议顺序(每一步都先 `--dry-run`):

1. 抠 `attack.png`(右侧武器键)、`start.png`(关卡入口按钮),先跑通"点开始 → 战斗中一直点攻击"。
2. 抠 `settlement.png`(结算/下一关),把它设成高优先级任务,打通"打完一关再来一关"。
3. 抠 `revive.png`、`pause.png` 做兜底任务,优先级最高、`cooldown` 给长一点。
4. 血条用 `method: "bar"` 读填充比例,低于阈值时触发加血/换人。
5. 所有阈值都用 `match --calibrate` 得出的建议值,不要照抄 0.85。

**移动**:横版动作游戏的左侧摇杆需要**按住并拖动**,用 `swipe`(带 `duration`)或
`key` 的 `hold`。而"一直按住"在 `postmessage` 模式下不总是被游戏认,这类操作
优先用 `--backend maafw --mouse anchoredtouch` 或 `sendinput`。

---

## 6. 已知限制(诚实版)

1. **后台截图取决于窗口本身**。MaaFramework 会按 `FramePool`(WGC)→ `PrintWindow` → 读屏 的顺序
   逐个试,结果因窗口而异。实测:浏览器窗口(Chromium)三种后台方式**全部被拒**
   (`FramePool` 返回 access denied,`PrintWindow` 返回 error 5),只能退回读屏;
   而 `PrintWindow` 恰好是 MAA 对 MuMu/雷电这类模拟器常用的方式,所以**模拟器窗口大概率比浏览器乐观**,
   但需要你自己 `probe` 一次确认。
2. **GDI 会"假成功"**。在 GPU 合成窗口上,`GDI` 方式不报错、却返回**纯黑帧**。所以
   `MaaBackend` 不接受首次探测得到的全黑帧,会继续沿回退链往下试 —— 否则脚本会对着空气匹配模板。
3. **`screendc` / 读屏方式要求窗口在最前且不被遮挡**。`grab --focus` 会先激活窗口。
4. **模板对分辨率敏感**。换模拟器分辨率就得重抠,或者用 `scales` 开多尺度。
5. **`postmessage` 输入能被游戏忽略**。若游戏用 `GetCursorPos` / Raw Input 判定,只能退回
   `sendinput`(抢鼠标)或 `maafw` 的 `anchoredtouch` / `interception`。
6. **无 OCR、无神经网络**。文字按钮建议抠图;实在需要 OCR 就上 MaaFramework 的 `OCR` 识别算法。

---

## 7. 风险声明(请务必读)

- **崩坏学园2 官方立场明确**:米哈游"风纪委员会"2014 年公开声明,使用外挂/第三方工具
  恶意获取奖励者,**核实后永久封禁**;其用户协议也包含冻结、禁止登录等处罚。
  第三方自动化属于该条款的风险区。
- 本工具是**图像识别 + 模拟输入**,不读写游戏内存、不修改客户端、不注入进程。
  但这**不等于**安全,更不等于官方允许。
- **自行评估、自行承担**账号风险。建议只用小号试、不要挂机刷奖励、不要用于任何交易行为。
- 请遵守游戏服务条款与当地法律。

---

## 8. 代码结构与测试

```
honkai2-bot/
├── simauto/
│   ├── win32.py        ctypes 封装:DPI、窗口枚举、GDI、SendInput/WM_* 消息
│   ├── imaging.py      PNG/BMP 编解码(标准库)+ 缩放/裁剪/灰度
│   ├── scanner.py      找窗口 + 4 个 GDI 截图后端 + probe 自检
│   ├── matcher.py      纯 numpy NCC 模板匹配(多尺度/掩码)+ 颜色/血条 + 阈值校准
│   ├── inputs.py       sendinput / post / send 三种点击,含拖动、长按、按键
│   ├── maa_backend.py  可选 MaaFramework Win32Controller 后端(优雅降级)
│   ├── engine.py       任务引擎:Config、FrameSource、trigger/action、三种模式
│   └── cli.py          list/tree/doctor/probe/grab/crop/match/validate/run
├── configs/            示例配置
├── bin/                simauto.cmd —— 任何目录都能调用的启动器
├── tools/              setup-maafw.ps1 —— 解包式安装 maafw(不用 pip)
└── tests/              回归测试(不需要模拟器)
```
跑测试:

```powershell
python tests/test_simauto.py        # 49 项,纯 numpy,无需模拟器
```

测试的重点是**对拍**:模板匹配和互相关的结果必须与「按 NCC 定义逐窗口暴力算」一致
(容差 1e-5),BMP 编解码必须与 Pillow 读写一致。这不是形式主义 —— 开发过程中有 4 个
bug 属于「不报错但结果是错的」,全部是对拍抓出来的:

| bug | 症状 |
|---|---|
| 把 `I·T` 的互相关当成滑动窗口和(积分图) | 分数全是 0 |
| 积分图四角索引少 1 | 分数恒定、位置乱 |
| `match_bar` 复制粘贴遮罩逻辑,漏掉色相环绕 | 血条填充恒为 0 |
| BMP 行填充按「像素」而非「字节」加 | 图像逐行错位(斜切) |
| 低纹理模板/窗口的方差下限量纲写错 | **纯色区域被报成 1.0 满分** |

最后一条最阴险:在真实截图上,一个 340×60 的文字模板,除了正确位置外还有 **5 个位置**
得满分 —— 全是画面里的平坦灰底。原因是归一化分母里两个标准差都接近 0,噪声被放大成
「完美匹配」。现在模板或候选窗口的灰度标准差低于 **4** 就被拒(报错而不是给分),
同一张图上位置 ≥0.99 的候选从 **5 个降到 1 个**,校准出的阈值也从 0.95(无效)变成
0.675(与 runner-up 0.35 有安全间距)。

如果你抠的模板被拒,通常意味着那块区域太平了:换一块有细节的,或者改用
`method: "color"` / `"bar"`。

### 与真实依赖对接时抓到的(这些只有装上 maafw 才能发现)

| bug | 症状 |
|---|---|
| `Win32Controller(hwnd=...)` 参数名写错 | **每次调用都抛 `unexpected keyword argument 'hwnd'`** |
| 只验证"调用成功"不验证"帧有内容" | 回退链停在返回纯黑帧的 `GDI` 上 |
| 直接调 `MaaSetGlobalOption` 降日志等级 | 无效(该 API 已废弃),输出被几十行重复报错淹没 |

第一个尤其值得记:**官方 `sample/python/demo1.py` 写的是 `hwnd=`,但发布版绑定的参数名是
`hWnd`**。文档与代码不一致,只有真跑一次才暴露。这也是为什么现在改为**位置传参**,
两种拼写都能兼容。

**其余踩过的坑**(都在代码注释里):`ctypes` 缺 `argtypes` 会把 64 位句柄当 int 截断;
`wintypes.HDC` 在某些环境是 32 位;`GetDIBits` 32bpp 要按 4 字节/像素分配缓冲区;
`GetWindowTextLength` 返回值不可信;跨进程要用 `GetWindowDC` 而不是 `GetDC`;
`spec.get("template") or ...` 遇到 numpy 数组会抛「truth value is ambiguous」。

---

## 9. 与 MAA / MaaFramework 的关系与许可证

本项目**参考**了以下两个项目的设计,但**没有复制它们的任何代码**:

| 项目 | 许可证 | 本项目如何使用 |
|---|---|---|
| [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)(MAA) | **AGPL-3.0-only** | 仅借鉴设计:**任务即数据**、720p 基准 + ROI 缩放、多算法分层、动作后顺序轮询 + 兜底、随机点击延时 |
| [MaaFramework](https://github.com/MaaXYZ/MaaFramework) | **LGPL-3.0** | **可选依赖**:通过官方 Python 绑定 `maafw` 调用其 Win32 控制器(`Win32Controller`)做截图与输入 |

两点必须说清楚:

1. **不要复制 MAA 的 `resource/template` 图片**。MAA 是 AGPL-3.0-only,且官方用户协议 §4.5
   明确该目录含明日方舟游戏数据资源、版权属鹰角网络,MAA 的授权**无法转授**这部分;
   §2.2 还单独声明 MAA Logo 不适用 AGPL。所以模板必须来自你自己的截图。
2. `maafw` 是 LGPL-3.0,**作为库被调用**是允许的;若你要分发打包后的程序,请遵守 LGPL 的相应义务。

参考文档:[MaaFramework 任务流水线协议](https://github.com/MaaXYZ/MaaFramework/blob/main/docs/zh_cn/3.1-%E4%BB%BB%E5%8A%A1%E6%B5%81%E6%B0%B4%E7%BA%BF%E5%8D%8F%E8%AE%AE.md)、[控制方式说明](https://github.com/MaaXYZ/MaaFramework/blob/main/docs/zh_cn/2.4-%E6%8E%A7%E5%88%B6%E6%96%B9%E5%BC%8F%E8%AF%B4%E6%98%8E.md)、[MAA 用户协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev-v2/terms-of-service.md)。

> 顺带一提:MaaFramework 的 `TemplateMatch` 阈值**默认 0.7**,本项目默认 0.85。
> 两者都是「归一化互相关」语义,阈值可按需调;`match --calibrate` 会给出实测建议值。

---

## 10. 本项目许可证

本项目代码以 **[MIT](LICENSE)** 协议开源。

- 可以自由使用、修改、分发,包括商用;保留版权声明即可。
- `templates/` 里的模板图**不是**本项目的授权范围:它们来自使用者的真实截图,游戏画面版权归权利人所有,
  仓库里不放游戏素材,使用者需自行抠图。
- 可选的 `maafw` 依赖是 **LGPL-3.0**:以库的方式调用没问题;若要分发打包后的程序,请自行履行 LGPL 义务
  (保留源码可得性与可替换性)。
- 本项目**与米哈游 /《崩坏学园2》官方无关**,自动化操作可能违反游戏用户协议并导致封号,使用风险自负。
