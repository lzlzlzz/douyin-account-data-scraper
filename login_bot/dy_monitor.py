#!/usr/bin/env python3
"""Cookie 健康巡检 + 群内失效告警（2026-10-10）。

对标小红书巡检模式（batch_stores.py:1029）：
  - 定时（默认 6h）读取登记表全部启用账号 → 逐个探测 cookie 有效性
  - 失效账号汇总为【一条】群消息（格式对齐小红书）：
        以下抖音账号登录授权到期，请马上 @抖音机器人 进行验证：
        - 账号A（抖音号 xx、尾号xxxx、负责人）
  - 每个账号每轮失效周期只提醒一次（「失效通知时间」为已通知标记；发送失败下轮自动重试）
  - Cookie 恢复有效 → 自动改回「正常」

告警通道（按优先级，任一成功即止）：
  1) 抖音群专属自定义机器人 webhook（DY_ALERT_WEBHOOK_URL，主通道）
  2) 机器人直发群消息（groupMessages/send，备用；群ID从群消息自动捕获，无需配置）

用法：
  python dy_monitor.py --once             # 手动跑一轮（会真实发送）
  python dy_monitor.py --once --no-send   # 只检测/改表，不发送
  python dy_monitor.py --test-alert       # 发送测试告警（验证通道）
  python dy_monitor.py --loop             # 手动常驻（生产由 dy_bot.py 守护线程自动运行）
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import config as C
from aitable import DouyinTable
from dy_notify import DingTalkWebhook, RobotGroupSender

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "output" / "monitor.log"

DEFAULT_INTERVAL = int(os.getenv("DY_MONITOR_INTERVAL", "21600"))          # 6 小时
DEFAULT_INITIAL_DELAY = int(os.getenv("DY_MONITOR_INITIAL_DELAY", "180"))  # 启动后首轮延迟


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [monitor] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- cookie 探测
def check_cookie(cookie: str, timeout: int = 20) -> str:
    """探测 cookie 有效性 → valid / invalid / unknown（与抓数脚本同口径的 fans 接口）。"""
    if not cookie or "sessionid=" not in cookie:
        return "invalid"
    req = urllib.request.Request(
        C.FANS_API_URL + "?recent_days=7",
        headers={"Accept": "application/json, text/plain, */*",
                 "Referer": C.LOGIN_URL,
                 "User-Agent": C.USER_AGENT,
                 "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return "invalid" if exc.code in (401, 403) else "unknown"
    except Exception:
        return "unknown"
    try:
        data = json.loads(body)
    except Exception:
        # 非 JSON：多为被重定向到登录页（HTML）
        return "invalid" if ("登录" in body or "login" in body.lower()) else "unknown"
    return "valid" if data.get("status_code") == 0 else "invalid"


# ---------------------------------------------------------------- 消息构建
def compose_expired_alert(pending: list[dict]) -> str:
    """一条消息汇总所有到期账号（格式对齐小红书截图）。"""
    lines = [f"以下抖音账号登录授权到期，请马上 @{C.BOT_NAME} 进行验证："]
    for a in pending:
        name = (a.get("account") or a.get("douyin_id")
                or a.get("phone") or a.get("record_id") or "未知账号")
        extra = []
        if a.get("douyin_id"):
            extra.append(f"抖音号 {a['douyin_id']}")
        phone = (a.get("phone") or "").strip()
        if phone:
            extra.append(f"尾号{phone[-4:]}")
        person = a.get("manager_name") or a.get("creator_name") or ""
        if person:
            extra.append(person)
        suffix = f"（{'、'.join(extra)}）" if extra else ""
        lines.append(f"- {name}{suffix}")
    return "\n".join(lines)


def send_alert(text: str) -> str:
    """按优先级发送告警，返回成功通道名（webhook / robot），全失败返回 ''。"""
    webhook = DingTalkWebhook()
    if webhook.ready:
        ok = webhook.send_text(text)
        log(f"群告警·webhook: {'成功' if ok else '失败'}")
        if ok:
            return "webhook"
    else:
        log("群告警·webhook: 未配置（DY_ALERT_WEBHOOK_URL 为空）")
    robot = RobotGroupSender()
    if robot.ready:
        ok = robot.send_text(text)
        log(f"群告警·机器人直发: {'成功' if ok else '失败'}")
        if ok:
            return "robot"
    else:
        log("群告警·机器人直发: 未就绪（缺凭据或尚未捕获群ID）")
    return ""


# ---------------------------------------------------------------- 单轮巡检
def run_round(send: bool = True) -> dict:
    stats = {"checked": 0, "valid": 0, "invalid": 0, "unknown": 0,
             "notified": 0, "healed": 0, "already": 0, "no_cookie": 0}
    table = DouyinTable()
    try:
        accounts = table.read_accounts(only_enabled=True, require_phone=False)
    except Exception as exc:
        log(f"读表失败: {exc}")
        return stats

    pending: list[dict] = []  # 新失效、未通知过的
    for a in accounts:
        cookie = (a.get("cookie") or "").strip()
        if not cookie:
            stats["no_cookie"] += 1
            continue
        name = a.get("account") or a.get("phone") or a.get("record_id")
        verdict = check_cookie(cookie)
        stats["checked"] += 1
        if verdict == "unknown":
            stats["unknown"] += 1
            log(f"{name}: 探测异常（网络/未知），本轮跳过")
            continue
        if verdict == "valid":
            stats["valid"] += 1
            if a.get("status") == C.STATUS_EXPIRED:
                try:
                    table.update_record(a["record_id"], status=C.STATUS_NORMAL)
                    stats["healed"] += 1
                    log(f"{name}: Cookie 已恢复 → 状态改回「正常」")
                except Exception as exc:
                    log(f"{name}: 回写「正常」失败: {exc}")
            continue

        # 失效
        stats["invalid"] += 1
        if a.get("status") != C.STATUS_EXPIRED:
            try:
                table.update_record(a["record_id"], status=C.STATUS_EXPIRED)
                log(f"{name}: Cookie 失效 → 状态标记「过期」")
            except Exception as exc:
                log(f"{name}: 回写「过期」失败: {exc}")
        if a.get("notice_at"):
            stats["already"] += 1
            log(f"{name}: 已通知过（{a['notice_at']}），跳过")
            continue
        pending.append(a)

    if not pending:
        log(f"本轮结束（无新增失效）: {stats}")
        return stats

    message = compose_expired_alert(pending)
    if not send:
        log(f"[no-send] 应发群告警（{len(pending)} 个账号）：\n{message}")
        return stats

    channel = send_alert(message)
    if channel:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for a in pending:
            try:
                table.update_record(a["record_id"], notice_at=now)
            except Exception as exc:
                log(f"{a.get('account')}: 回写通知时间失败: {exc}")
        stats["notified"] = len(pending)
        log(f"✅ 告警已发送（通道={channel}，{len(pending)} 个账号）")
    else:
        log("⚠️ 所有告警通道不可用，下轮自动重试。待发内容：\n" + message)
    log(f"本轮结束: {stats}")
    return stats


# ---------------------------------------------------------------- 测试告警
def test_alert() -> None:
    now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    message = (f"🧪【测试消息】抖音账号巡检告警通道测试（{now}）\n"
               f"以下抖音账号登录授权到期，请马上 @{C.BOT_NAME} 进行验证：\n"
               f"- 测试账号（抖音号 test_demo、尾号0000、测试员）\n"
               f"—— 测试消息，收到即代表通道正常，忽略即可。")
    channel = send_alert(message)
    log(f"测试告警结果: {channel or '失败（所有通道不可用/未配置）'}")


# ---------------------------------------------------------------- 常驻循环
def monitor_loop() -> None:
    """常驻循环（由 dy_bot.py 以守护线程启动）。"""
    if os.getenv("DY_MONITOR_ENABLED", "1").strip() in ("0", "false", "False"):
        log("巡检线程已禁用（DY_MONITOR_ENABLED=0）")
        return
    log(f"巡检线程启动：{DEFAULT_INITIAL_DELAY}s 后首轮，之后每 {DEFAULT_INTERVAL}s 一轮")
    time.sleep(DEFAULT_INITIAL_DELAY)
    while True:
        try:
            run_round(send=True)
        except Exception as exc:
            log(f"巡检异常: {type(exc).__name__}: {exc}")
        time.sleep(DEFAULT_INTERVAL)


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音 Cookie 健康巡检（失效自动群告警）")
    parser.add_argument("--once", action="store_true", help="跑一轮检测（默认动作）")
    parser.add_argument("--no-send", action="store_true", help="只检测/改表，不发送告警")
    parser.add_argument("--test-alert", action="store_true", help="发送测试告警验证通道")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    args = parser.parse_args()
    if args.test_alert:
        test_alert()
    elif args.loop:
        monitor_loop()
    else:
        run_round(send=not args.no_send)
    return 0


if __name__ == "__main__":
    sys.exit(main())
