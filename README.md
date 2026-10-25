<div align="center">

# Simauto · 崩坏学园2 自动化脚本

**崩坏学园2 自动化脚本 · Python 版**

基于 [MAAHG2](https://github.com/Choi-Peng/MAAHG2) 的思路重写：任务即数据、模板匹配、轮询兜底。
Windows + 安卓模拟器，不需要 ADB，不需要 root，不需要注入。

</div>

## 如何使用

### 直接下载

到 [Releases](https://github.com/yuesq3016/simauto/releases) 下载压缩包，解压后**双击 `Simauto.exe`**，不需要装 Python。
第一次用先跑 `Simauto.exe doctor` 和 `Simauto.exe find`，确认能抓到模拟器画面。

### 从源码跑

1. 装 Python 3.11/3.12，执行 `pip install -r requirements.txt`（只有 numpy；想要更强截图后端再 `pip install maafw`）。
2. **模板自己抠**。仓库不含游戏截图，用 `python -m simauto crop` 从你的截图里切。
3. 校准识别：`python -m simauto match --calibrate`，把建议阈值填回配置。
4. 先干跑：`python -m simauto run --config configs/bh2-enter.json --dry-run`。
5. 没问题再真跑，或者双击 `启动.cmd` 用界面。

详细说明（后端对比、踩坑记录、协议解析）在 [docs/DETAILS.md](docs/DETAILS.md)。

### 任务列表

| 任务 | 配置 |
| --- | --- |
| 标题界面 → 进入游戏 | `configs/bh2-enter.json` |
| 每日签到领取 | `configs/bh2-enter.json` |
| 使魔探险：收菜 + 再次派出 | `configs/bh2-summon.json` / `configs/bh2-recycle.json` |
| 共鸣屋：刷新 + 目标装备自动购买 | `configs/bh2-resonance.json` |
| 关卡首通奖励扫描 | `python -m simauto stagemap` |
| 入门骨架（先校准再用） | `configs/bh2-starter.json` |

### 常用命令

```
python -m simauto doctor            # 检查环境、找模拟器
python -m simauto find              # 自动定位窗口并试所有截图后端
python -m simauto grab              # 存一张截图
python -m simauto ocr               # 读屏上的文字
python -m simauto match --calibrate # 模板匹配 + 阈值建议
python -m simauto validate -c ...   # 校验配置和模板文件
python -m simauto run -c ... --dry-run
python -m simauto gui               # 图形界面
```

## 测试平台

- Windows 11（26200）
- MuMu 模拟器 12，`MuMuNxDevice`，1280×720

## 注意

- 仓库不含游戏素材，`templates/` 需自己准备。
- 自动化操作可能违反游戏用户协议并导致封号，风险自负，仅供学习与自用。
- 本项目与米哈游 /《崩坏学园2》官方无关。

## 鸣谢

- **[DeepSeek](https://www.deepseek.com/)** —— 本项目的代码与文档主要由 DeepSeek Harness 驱动完成。
- **[MAAHG2](https://github.com/Choi-Peng/MAAHG2)**（Choi-Peng）—— 底子来源，崩 2 的流程与界面理解都出自它。
- **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** —— 可选的截图与输入后端。
- **[MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights)** —— 设计借鉴。
- **MaaCommonAssets** —— OCR 模型来源。

## 许可证

[MIT](LICENSE)，第三方署名见 [NOTICE](NOTICE)。
