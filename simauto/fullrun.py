"""按顺序跑完整日常流程(与界面「开始!」等价),并把日志写到文件。

界面里的按钮走的是同一条路径,所以这个脚本既方便无人值守,也方便我调试时
直接看完整日志。用法:

    python -m simauto.fullrun [--dry-run] [--only 收菜 共鸣屋 ...]
"""
from __future__ import annotations
from ._root import project_root

import argparse
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path


from .adb_backend import AdbBackend
from .ocr import OcrEngine

PROJECT = Path(project_root())
# ⚠️ 这里**必须和 GUI 的任务列表保持一致**(顺序 + 键名)。
# 之前两边各写一份,导致 GUI 加了前置导航、fullrun 却漏了 ——
# 实测 fullrun 跑收菜时在别的页面上找「探索完成」,报 "looks clean / 0 claim(s)"。
# 现在直接从 gui.TASKS 取,只有一处定义。
def _tasks():
    try:
        from .gui import TASKS as _T
        return [(k, l) for k, l, _ in _T]
    except Exception:  # noqa: BLE001 - 没有 tkinter 时退回内置列表
        return [
            ("signin", "启动游戏 + 进游戏(关弹窗)"),
            ("popups", "关闭弹窗(公告/维护/签到/促销)"),
            ("signin2", "领取每日签到(可独立勾选)"),
            ("harvest", "收菜(使魔探险:收菜 + 派出)"),
            ("shop", "商店:领免费零时之种"),
            ("resonance", "共鸣屋:刷新 + 按名单买"),
            ("fracture", "多元裂缝:使魔的爱 + 虚轴之庭"),
            ("stamina1", "清体力(BONUS)"),
            ("presence1", "存在感(每日任务)"),
            ("social", "社团领取(消息 + 社团)"),
            ("stamina2", "再次清体力(BONUS)"),
            ("presence2", "再次存在感(含每周任务)"),
        ]


TASKS = _tasks()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simauto.fullrun")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*", help="只跑名字里含这些关键字的任务")
    ap.add_argument("--max-stamina-rounds", type=int, default=30)
    ap.add_argument("--diamond-buys", type=int, default=0,
                    help="清体力时允许「消耗钻石兑换体力」的次数(0 = 不兑换)")
    ap.add_argument("--log", default=None)
    args = ap.parse_args(argv)

    log_path = Path(args.log) if args.log else (
        PROJECT / "sessions" / f"fullrun-{datetime.now():%Y%m%d-%H%M%S}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "w", encoding="utf-8")

    def log(msg: str = "") -> None:
        line = str(msg)
        print(line, flush=True)
        fh.write(line + "\n")
        fh.flush()

    log(f"===== 全流程开始 {datetime.now():%Y-%m-%d %H:%M:%S} =====")
    log(f"日志文件: {log_path}")

    cap = AdbBackend()
    engine = OcrEngine.shared()
    results: list[tuple[str, str, str]] = []
    try:
        for i, (key, label) in enumerate(TASKS, 1):
            if args.only and not any(k in label for k in args.only):
                log(f"\n########## [{i}/{len(TASKS)}] {label} —— 跳过(不在 --only 里)")
                continue
            log(f"\n########## [{i}/{len(TASKS)}] {label} ##########")
            t0 = time.time()
            try:
                # ① 前置界检查(和 GUI 用同一套约定)
                from .gui import ensure_task_start
                if not ensure_task_start(key, cap, engine, log):
                    log(f"  ⏭ 跳过:到不了所需界面")
                    results.append((label, "跳过", "前置界面不可达"))
                    continue
                # ② 执行
                _run(key, cap, engine, log, args)
                dt = time.time() - t0
                log(f"  ✅ 完成 ({dt:.0f}s)")
                results.append((label, "OK", f"{dt:.0f}s"))
            except Exception as exc:  # noqa: BLE001
                dt = time.time() - t0
                # 体力/次数不足属于正常情况 —— 跳过当前项,不算失败
                if type(exc).__name__ == "Insufficient":
                    log(f"  ⏭ 跳过 ({dt:.0f}s):{exc}")
                    results.append((label, "跳过", str(exc)[:50]))
                    continue
                # 强制更新弹窗:整个流程都没意义了,直接中止
                if type(exc).__name__ == "ForceUpdateNeeded":
                    log(f"  ⛔ 中止:{exc}")
                    results.append((label, "中止", str(exc)[:50]))
                    break
                log(f"  ❌ 失败 ({dt:.0f}s): {type(exc).__name__}: {exc}")
                log(traceback.format_exc()[-900:])
                results.append((label, "FAIL", f"{type(exc).__name__}: {str(exc)[:60]}"))
            finally:
                # ③ 收尾(和 GUI 一致)
                try:
                    from .gui import finish_task
                    finish_task(key, cap, engine, log)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        cap.close()
        log("\n===== 结果汇总 =====")
        for label, status, note in results:
            log(f"  [{status:4}] {label}  {note}")
        ok = sum(1 for _, s, _ in results if s == "OK")
        log(f"  成功 {ok}/{len(results)}")
        fh.close()
    return 0


def _run(key: str, cap, engine, log, args) -> None:
    dry = args.dry_run
    if key == "signin":
        from .daily import launch_game
        if not launch_game(cap, engine, log):
            raise RuntimeError("启动游戏 / 进游戏流程未完成")

    elif key == "popups":
        from .daily import close_popups
        n = close_popups(cap, engine, log)
        log(f"  关闭了 {n} 个弹窗")

    elif key == "signin2":
        from .daily import claim_signin
        if not claim_signin(cap, engine, log):
            log("  当前没有签到弹窗(今天可能已领过)")

    elif key == "harvest":
        from .engine import Engine, FrameSource, load_config
        cfg = load_config(str(PROJECT / "configs" / "bh2-summon.json"))
        src = FrameSource(cfg, capture=cap, input_controller=cap, window=None)
        Engine(cfg, frame_source=src, input_controller=cap,
               dry_run=dry, log=log).run()

    elif key == "shop":
        from .daily import run_shop
        log(f"  商店结果:{run_shop(cap, engine, log)}")

    elif key == "resonance":
        from .engine import Config, Engine, FrameSource, Tick
        from .resonance import load_watchlist
        names = load_watchlist()
        log(f"  名单 {len(names)} 项")
        cfg = Config({
            "name": "fullrun-resonance", "mode": "once",
            "capture": {"backend": "adb"}, "recognizers": {},
            "tasks": {"n": {"actions": [{"action": "log", "message": "x"}]}},
            "steps": [], "session_dir": "sessions",
        }, None)
        src = FrameSource(cfg, capture=cap, input_controller=cap, window=None)
        eng = Engine(cfg, frame_source=src, input_controller=cap,
                     dry_run=dry, log=log)
        eng.run_actions([{
            "action": "resonance_refresh", "watchlist": names,
            "refresh_point": [1088, 153], "max_refresh": 3, "buy": True,
            "currency_roi": [330, 120, 230, 60], "currency_min": 6000,
            "settle": 2.5,
        }], Tick(frame=cap.grab(), now=time.time(), results={}, last=None,
                 task="fullrun-resonance"))

    elif key == "fracture":
        from .daily import run_familiar_daily, run_world_boss
        run_familiar_daily(cap, engine, log, dry=dry)
        run_world_boss(cap, engine, log, times=3)

    elif key in ("stamina1", "stamina2"):
        from .daily import clear_stamina
        out = clear_stamina(cap, engine, log,
                            max_rounds=args.max_stamina_rounds,
                            double=True, rng=random.Random(),
                            diamond_buys=args.diamond_buys)
        log(f"  结果: {out}")

    elif key in ("presence1", "presence2"):
        from .daily import claim_task_rewards
        log(f"  结果: {claim_task_rewards(cap, engine, log, weekly=(key == 'presence2'))}")

    elif key == "social":
        from .daily import claim_social
        log(f"  结果: {claim_social(cap, engine, log)}")

    else:
        raise RuntimeError(f"未知任务 {key}")


if __name__ == "__main__":
    raise SystemExit(main())
