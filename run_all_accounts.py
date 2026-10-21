#!/usr/bin/env python3
"""抖音粉丝快照 - 多账号批量抓取（一键跑全表）。

读钉钉登记表所有「启用状态=启用」且有 cookie 的账号 → 逐个执行
douyin_fans_snapshot.py（子进程，互相隔离，单账号失败不影响其他）。

每次运行：
  1. 从登记表拉取每个账号的最新 cookie（授权后自动同步的即最新）
  2. 归档到 output/cookies/{record_id}_{名称}.txt
  3. 逐个抓取近7天+近30天 → 入库 rpa.douyin_fans_snapshot + 更新本地 CSV
  （同日重跑幂等覆盖，不产生重复行）

用法:
  python run_all_accounts.py              # 全量抓取
  python run_all_accounts.py --dry-run    # 只抓取打印，不写库不写文件
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "login_bot"))

from dotenv import load_dotenv  # noqa: E402

import config as C  # noqa: E402

load_dotenv(C.HERE / "login_bot/.env")

from aitable import DouyinTable  # noqa: E402

SCRIPT = HERE / "douyin_fans_snapshot.py"
COOKIE_DIR = HERE / "output" / "cookies"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抖音粉丝快照 - 多账号批量抓取")
    parser.add_argument("--dry-run", action="store_true", help="只抓取打印，不写库不写文件")
    args = parser.parse_args(argv)

    table = DouyinTable()
    accounts = [a for a in table.read_accounts(only_enabled=True, require_phone=False)
                if a["phone"] and a["cookie"]]

    print(f"=== 抖音粉丝快照 · 批量抓取 ===")
    print(f"登记表启用账号（有 cookie）: {len(accounts)} 个")
    if not accounts:
        print("没有可执行账号（检查表内「启用状态」与 cookie）")
        return 1

    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    results: list[tuple[str, bool, str]] = []
    for idx, a in enumerate(accounts, 1):
        name = a["account"] or f"尾号{a['phone'][-4:]}"
        cookie_file = COOKIE_DIR / f"{a['record_id']}_{name}.txt"
        cookie_file.write_text(a["cookie"], encoding="utf-8")

        print()
        print(f"──── {idx}/{len(accounts)} {name}（手机 {a['phone']}）────")
        cmd = [sys.executable, str(SCRIPT), "--cookie-file", str(cookie_file)]
        if args.dry_run:
            cmd.append("--dry-run")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300, env=env,
                               cwd=str(HERE))
            out = (r.stdout or "").strip()
            if out:
                print(out)
            if r.returncode == 0:
                # 回写登记表「数据更新时间」（dry-run 不回写，保证零写入）
                if not args.dry_run:
                    try:
                        table.update_record(a["record_id"],
                                            update_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
                    except Exception as exc:
                        print(f"  [警告] 回写「数据更新时间」失败：{type(exc).__name__}")
                results.append((name, True, "OK"))
            else:
                err = (r.stderr or "").strip()
                print(err[:400])
                results.append((name, False, err.splitlines()[-1][:120] if err else "未知错误"))
        except subprocess.TimeoutExpired:
            print("抓取超时（300s）")
            results.append((name, False, "超时"))
        time.sleep(1)

    print()
    print("=" * 52)
    print("=== 汇总 ===")
    ok = sum(1 for _, o, _ in results if o)
    for name, o, note in results:
        print(f"  {'✅' if o else '❌'} {name}" + ("" if o else f" → {note}"))
    print(f"成功 {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
