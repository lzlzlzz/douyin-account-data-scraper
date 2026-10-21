"""抖音登录助手 · 看门狗（watchdog）。

职责：监控 dy_bot.py 的健康状态，异常时自动重启。
判定标准（每 45 秒检查一次）：
  1. dy_bot 进程不存在 → 判定死亡 → 重启
  2. dy_bot 进程存在但无任何「到外网的非本地 ESTABLISHED 连接」→ 判定假死（Stream 断线）→ 重启
     （连续 2 次检查失败才动手，避免瞬断误杀）

运行：
  python dy_bot_watchdog.py        # 常驻，日志 output/watchdog.log
"""
from __future__ import annotations

import datetime
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
PY = Path(sys.executable)          # 用当前解释器（default venv）启动 bot
LOG = HERE / "output" / "watchdog.log"
BOT_LOG = HERE / "output" / "bot_run.log"
(HERE / "output").mkdir(parents=True, exist_ok=True)  # 新环境首次运行自动建目录
CHECK_INTERVAL = 30                # 秒
FAIL_THRESHOLD = 2                 # 连续失败次数达阈值才重启（仅用于"假死"判定）


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def find_bot_procs() -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if "dy_bot.py" in cmd and "watchdog" not in cmd:
                out.append(p)
        except Exception:
            continue
    return out


def _proxy_ports(proc: psutil.Process) -> set[int]:
    """从 bot 进程环境变量读取本地代理端口（连接走代理时的健康判定依据）。"""
    ports: set[int] = set()
    try:
        env = proc.environ()
    except Exception:
        return ports
    for key in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        v = env.get(key, "") or ""
        m = re.search(r"127\.0\.0\.1:(\d+)", v)
        if m:
            ports.add(int(m.group(1)))
    return ports


def has_healthy_conn(proc: psutil.Process) -> bool:
    """连接健康判定（兼容两种模式）：
    - 直连：存在非 127.* 的 ESTABLISHED 连接（如 wss 到钉钉）
    - 代理：存在到进程环境变量所配本地代理端口（127.0.0.1:PORT）的 ESTABLISHED 连接
    """
    proxy_ports = _proxy_ports(proc)
    try:
        for c in proc.net_connections(kind="tcp"):
            if c.status != "ESTABLISHED":
                continue
            raddr = getattr(c, "raddr", None)
            if not raddr:
                continue
            ip = str(raddr.ip)
            if ip.startswith("127.") or ip in ("::1", "0.0.0.0"):
                if proxy_ports and raddr.port in proxy_ports:
                    return True
                continue
            return True
    except Exception:
        pass
    return False


def start_bot() -> None:
    log("启动 dy_bot …")
    with open(BOT_LOG, "a", encoding="utf-8") as lf:
        lf.write(f"\n----- [{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] 由看门狗启动 -----\n")
        lf.flush()
        flags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(PY), "-u", "dy_bot.py"], cwd=str(HERE),
                         stdout=lf, stderr=lf, creationflags=flags)
    time.sleep(10)
    procs = find_bot_procs()
    log(f"启动完成，当前 dy_bot 进程数={len(procs)}")


def kill_bot(procs: list[psutil.Process]) -> None:
    for p in procs:
        try:
            p.kill()
            log(f"已杀掉旧进程 PID={p.pid}")
        except Exception as e:
            log(f"杀 PID={p.pid} 失败: {e}")
    time.sleep(3)


def main() -> None:
    log("=== 看门狗启动（每 %ds 检查一次，连续 %d 次异常则重启）===" % (CHECK_INTERVAL, FAIL_THRESHOLD))
    # 启动时立即检查：bot 不在就立刻拉起（不等两轮）
    try:
        if not find_bot_procs():
            log("启动时未发现 dy_bot → 立即拉起")
            start_bot()
    except Exception as exc:
        log(f"启动检查异常：{type(exc).__name__}: {exc}")
    fail_count = 0
    while True:
        try:
            procs = find_bot_procs()
            if not procs:
                # 进程不存在：没有"瞬断误判"的可能，立即拉起
                log("dy_bot 进程不存在 → 立即重启")
                start_bot()
                fail_count = 0
            else:
                main_p = max(procs, key=lambda p: p.memory_info().rss)
                if has_healthy_conn(main_p):
                    if fail_count:
                        log("连接正常，计数清零")
                    fail_count = 0
                else:
                    fail_count += 1
                    log(f"dy_bot(PID={main_p.pid}) 连接异常（假死，连续 {fail_count} 次）")
                    if fail_count >= FAIL_THRESHOLD:
                        log("达到重启阈值 → 重启 dy_bot")
                        kill_bot(procs)
                        start_bot()
                        fail_count = 0
        except Exception as exc:
            log(f"检查循环异常：{type(exc).__name__}: {exc}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
