"""抖音登录助手 · 本地实测脚本（不依赖钉钉群消息）。

用法（用托管 venv：C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe）：
  python test_local_flow.py list                              # 读表，列出账号（含创建人）
  python test_local_flow.py login --phone 13800138000 --dry   # 浏览器交互演练（不真发短信）
  python test_local_flow.py login --phone 13800138000         # 真发短信 -> 交互输入验证码 -> 写表
  python test_local_flow.py qr --account lzlzlzz              # 扫码登录（打开二维码图片，等待扫码）-> 写表
  python test_local_flow.py simulate --sender 李壮 --dry       # 模拟 @机器人 事件（打印将回复的消息）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

from dotenv import load_dotenv

import config as C

load_dotenv(C.HERE / ".env")

from aitable import DouyinTable  # noqa: E402
from browser_login import DouyinBrowserLogin  # noqa: E402
from dy_notify import Messenger  # noqa: E402
from dy_service import (BotEvent, DouyinLoginService, fetch_account_info,  # noqa: E402
                        normalize_name, sender_key)
from dy_state import LoginState  # noqa: E402


class PrintMessenger(Messenger):
    """本地模拟：把本该发到群里的消息打印出来。"""

    def __init__(self):
        super().__init__()

    def reply_text(self, reply_url: str, text: str) -> bool:
        print(f"  [群回复] {text}")
        return True


def cmd_list(args) -> int:
    table = DouyinTable()
    accounts = table.read_accounts(only_enabled=False, require_phone=False)
    print(f"表内共 {len(accounts)} 行账号：")
    for a in accounts:
        print(f"  - record={a['record_id']} | {a['account'] or '(未命名)'} | 手机={a['phone'] or '-'} "
              f"| 抖音号={a['douyin_id'] or '-'} | 状态={a['status_name'] or '-'} "
              f"| 启用={'是' if a['enabled'] == C.ENABLED_ON else '否' if a['enabled'] else '(空→默认启用)'} "
              f"| 负责人={a['manager_name'] or '-'} | 创建人union={a['creator_union_id'][:12] + '...' if a['creator_union_id'] else '-'} "
              f"| cookie={'有' if a['cookie'] else '无'}")
    return 0


def cmd_login(args) -> int:
    phone = args.phone
    browser = DouyinBrowserLogin()
    print(f"[1/4] 启动浏览器，打开抖音登录页并切换验证码登录（profile: {phone[-4:]}）...")
    if args.dry:
        # 演练模式：只验证（打开页/切tab/填手机号），不点「获取验证码」
        from browser_login import DouyinBrowserSession
        session = DouyinBrowserSession.open(phone[-4:])
        try:
            ok = session._goto_login()
            print(f"  登录页加载: {'OK' if ok else '失败'}")
            if not ok:
                return 1
            if not session._open_sms_tab():
                print("  切换验证码登录 tab 失败")
                return 1
            inp = session.page.locator("#normal-input")
            if not inp.count():
                inp = session.page.locator("input[placeholder*='手机号']")
            inp.fill(phone)
            print(f"  已填入手机号 {phone[:3]}****{phone[-4:]}（dry-run 未点击发送）")
            session.page.screenshot(path=str(C.HERE / "output" / "dry-run-sms.png"))
            print(f"  截图: output/dry-run-sms.png")
        finally:
            session.close()
        print("dry-run 完成 ✓")
        return 0

    result = browser.start_sms(phone, profile_key=phone[-4:])
    if result.status != "waiting_code":
        print(f"  失败: {result.reason}")
        return 1
    print(f"[2/4] 验证码已发送到 {phone[:3]}****{phone[-4:]}（请查收短信）")
    if args.code_file:
        code = _wait_code_file(C.HERE / "output" / "code.txt", timeout=600)
    else:
        try:
            code = input("      请输入收到的验证码（回车放弃）: ").strip()
        except EOFError:
            code = ""
    if not code:
        print("  放弃，关闭会话")
        browser.cancel(result.challenge)
        return 1
    print(f"[3/4] 提交验证码 {code[:2]}**** ...")
    lr = browser.complete(result.challenge, code)
    print(f"  结果: {lr.status} | {lr.reason or ''}")
    if lr.status == "retryable":
        try:
            code2 = input("      验证码错误，重新输入（回车放弃）: ").strip()
        except EOFError:
            code2 = ""
        if code2:
            lr = browser.complete(result.challenge, code2)
            print(f"  重试结果: {lr.status} | {lr.reason or ''}")
    if lr.status != "succeeded":
        return 1
    print(f"[4/4] 登录成功！cookie 长度 {len(lr.cookie)}，识别身份...")
    info = fetch_account_info(lr.cookie)
    print(f"  身份: 昵称={info.get('nickname')} 抖音号={info.get('unique_id')} uid={info.get('uid')}")
    _write_back(args, lr.cookie, info)
    return 0


def cmd_qr(args) -> int:
    browser = DouyinBrowserLogin()
    print("[1/3] 启动浏览器，截取登录二维码...")
    result = browser.start_qr(profile_key=args.account or "qr-test")
    if result.status != "waiting_scan":
        print(f"  失败: {result.reason}")
        return 1
    print(f"  二维码已保存: {result.qr_path}")
    try:
        os.startfile(result.qr_path)  # 自动打开图片，直接扫屏
        print("  已自动打开二维码图片（请用抖音 App 扫码）")
    except Exception:
        pass
    print("[2/3] 等待扫码（最多 300 秒）...")
    lr = browser.wait_for_scan(result.challenge, timeout_seconds=300)
    print(f"  结果: {lr.status} | {lr.reason or ''}")
    if lr.status == "waiting_code":
        print("  >> 扫码后触发身份验证：短信已发送（请查收）")
        if args.code_file:
            code = _wait_code_file(C.HERE / "output" / "code.txt", timeout=600)
        else:
            try:
                code = input("      请输入短信验证码（回车放弃）: ").strip()
            except EOFError:
                code = ""
        if not code:
            print("  放弃，关闭会话")
            browser.cancel(result.challenge)
            return 1
        print(f"[3/3] 提交验证码 {code[:2]}**** ...")
        lr = browser.complete(result.challenge, code)
        print(f"  提交结果: {lr.status} | {lr.reason or ''}")
    if lr.status != "succeeded":
        browser.cancel(result.challenge)
        return 1
    print(f"[3/3] 扫码登录成功！cookie 长度 {len(lr.cookie)}，识别身份...")
    info = fetch_account_info(lr.cookie)
    print(f"  身份: 昵称={info.get('nickname')} 抖音号={info.get('unique_id')} uid={info.get('uid')}")
    _write_back(args, lr.cookie, info)
    return 0


def _wait_code_file(path, timeout: int = 600) -> str:
    """等待验证码回传文件（读后删除，模拟群内回传的最后一跳）。"""
    print(f"      等待验证码回传：把验证码写入 {path}（最长 {timeout//60} 分钟）")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if path.exists():
            try:
                code = path.read_text(encoding="utf-8").strip()
                path.unlink(missing_ok=True)
                if code:
                    print(f"      已收到验证码: {code[:2]}****")
                    return code
            except Exception:
                pass
        time.sleep(2)
    return ""


def _write_back(args, cookie: str, info: dict) -> None:
    if args.no_write:
        print("  (--no-write) 跳过写表")
        return
    table = DouyinTable()
    accounts = table.read_accounts(only_enabled=False, require_phone=False)
    target = None
    if args.account:
        target = next((a for a in accounts if normalize_name(a["account"]) == normalize_name(args.account)), None)
    if target is None and getattr(args, "phone", None):
        target = next((a for a in accounts if a["phone"] == args.phone), None)
    if target is None:
        print("  未找到目标行（用 --account 名称或 --phone 指定要写回的行）")
        return
    from datetime import datetime
    fields = dict(cookie=cookie, status=C.STATUS_NORMAL, notice_at="",
                  update_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if info.get("unique_id"):
        fields["douyin_id"] = info["unique_id"]
    if info.get("uid"):
        fields["uid"] = info["uid"]
    table.update_record(target["record_id"], **fields)
    print(f"  已写回表: {target['account'] or target['phone']}（cookie + 身份 + 状态=正常）")


def cmd_simulate(args) -> int:
    """模拟一条 @机器人 事件，走完整服务逻辑（默认不真发短信）。"""
    table = DouyinTable()
    browser = DouyinBrowserLogin() if not args.dry else None
    messenger = PrintMessenger()
    state = LoginState(path=C.HERE / "output" / "login_state_test.json")
    service = DouyinLoginService(table, browser, messenger, state)

    event = BotEvent(
        message_id=f"sim-{int(time.time())}",
        conversation_id="sim-conversation",
        text=f"@{C.BOT_NAME} {args.text or ''}".strip(),
        sender_id="sim-user",
        sender_open_id="sim-open",
        sender_name=args.sender,
        reply_url="https://oapi.dingtalk.com/robot/send?access_token=SIMULATED",
    )
    print(f"模拟事件: sender={args.sender} text={event.text!r} dry={args.dry}")
    if args.dry:
        # 干跑：只测匹配逻辑，不启动浏览器
        accounts = service._accounts_of(event)
        print(f"  匹配到 {len(accounts)} 个账号:")
        for a in accounts:
            print(f"    - {a['account']} | 手机={a['phone']} | 状态={a['status_name']}")
        if not accounts:
            print(f"  → 将回复: {C.MSG_NO_ACCOUNT}")
        elif len(accounts) > 1:
            print(f"  → 将回复多账号提示")
        else:
            print(f"  → 将启动短信登录并回复: {C.MSG_ASK_CODE.format(suffix=accounts[0]['phone'][-4:], bot=C.BOT_NAME, code_placeholder='xxxxxx')}")
        return 0
    service.handle_event(event)
    print("  模拟事件处理完成")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音登录助手 · 本地实测")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="读表列出账号")

    p_login = sub.add_parser("login", help="真实短信登录测试（交互输入验证码）")
    p_login.add_argument("--phone", required=True)
    p_login.add_argument("--dry", action="store_true", help="演练：不点发送验证码")
    p_login.add_argument("--account", default="", help="写回目标账号名称（表内）")
    p_login.add_argument("--code-file", action="store_true",
                         help="验证码文件回传模式（等待 output/code.txt，用于后台实测）")
    p_login.add_argument("--no-write", action="store_true")

    p_qr = sub.add_parser("qr", help="扫码登录测试（先扫码，风控弹验证时回传短信码）")
    p_qr.add_argument("--account", default="", help="写回目标账号名称（表内）")
    p_qr.add_argument("--code-file", action="store_true",
                      help="验证码文件回传模式（等待 output/code.txt，用于后台实测）")
    p_qr.add_argument("--no-write", action="store_true")

    p_sim = sub.add_parser("simulate", help="模拟@机器人事件")
    p_sim.add_argument("--sender", required=True, help="发送人姓名（对应表内创建人）")
    p_sim.add_argument("--text", default="", help="消息文本（默认空=裸@）")
    p_sim.add_argument("--dry", action="store_true", help="只测匹配逻辑，不启动浏览器")

    args = parser.parse_args()
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "qr":
        return cmd_qr(args)
    if args.cmd == "simulate":
        return cmd_simulate(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
