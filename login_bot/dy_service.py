"""抖音登录助手 · 编排逻辑（对标小红书 login_service.py 精简版）。

流程：
- @机器人（无参）→ 按「创建人」匹配待验证账号 → 启动短信登录 →
  群内提示「验证码已发送到尾号 XXXX，请发送：@抖音登录助手 xxxxxx」
- 回传 6 位验证码 → 提交 → 登录成功 → cookie + 身份字段写回 AI 表 → 回复成功
- @机器人 校验 <账号名称> → 指定账号发起（多账号时用）
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import config as C


# ---------------------------------------------------------------- 事件与解析
@dataclass
class BotEvent:
    message_id: str
    conversation_id: str
    text: str
    sender_id: str          # senderStaffId（企业内部 userId）
    sender_open_id: str     # senderId（openDingTalkId）
    sender_name: str        # senderNick
    reply_url: str          # sessionWebhook


def normalize_event(callback_message) -> BotEvent | None:
    data = callback_message.data or {}
    conversation_id = str(data.get("conversationId") or "")
    if not conversation_id:
        return None
    headers = getattr(callback_message, "headers", None)
    message_id = str(getattr(headers, "message_id", "") or data.get("msgId") or "")
    return BotEvent(
        message_id=message_id,
        conversation_id=conversation_id,
        text=str((data.get("text") or {}).get("content") or "").strip(),
        sender_id=str(data.get("senderStaffId") or ""),
        sender_open_id=str(data.get("senderId") or ""),
        sender_name=str(data.get("senderNick") or "").strip(),
        reply_url=str(data.get("sessionWebhook") or ""),
    )


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def parse_sms_code(text: str) -> str:
    """@机器人 后的 6 位数字验证码。"""
    cleaned = re.sub(r"^\s*@\S+\s*", "", text or "").strip()
    return cleaned if re.fullmatch(r"\d{6}", cleaned) else ""


def parse_check_command(text: str) -> str:
    """@机器人 校验 <账号>（分隔空格可有可无；账号可填 名称/抖音号/手机尾号）。"""
    cleaned = re.sub(r"^\s*@\S+\s*", "", text or "").strip()
    match = re.fullmatch(r"校验\s*(.+?)\s*", cleaned)
    return match.group(1).strip() if match else ""


def sender_key(event: BotEvent) -> str:
    if event.sender_id:
        return f"user:{event.sender_id}"
    if event.sender_open_id:
        return f"open:{event.sender_open_id}"
    return f"name:{normalize_name(event.sender_name)}"


def save_group_context(conversation_id: str) -> None:
    """记录群会话 ID（巡检告警备用通道『机器人直发群消息』用）。"""
    if not conversation_id:
        return
    try:
        path = C.HERE / "output" / "group_context.json"
        old = ""
        if path.exists():
            try:
                old = str((json.loads(path.read_text(encoding="utf-8")) or {})
                          .get("conversation_id") or "")
            except Exception:
                old = ""
        if old == conversation_id:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "conversation_id": conversation_id,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[service] 已记录群会话ID: {conversation_id[:28]}…", flush=True)
    except Exception as exc:
        print(f"[service] 记录群会话ID失败: {exc}", flush=True)


# ---------------------------------------------------------------- cookie 身份
def fetch_account_info(cookie: str, timeout: int = 25) -> dict:
    """登录后拉取账号身份：{nickname, unique_id, uid}。失败返回 {}。"""
    try:
        req = urllib.request.Request(
            C.ACCOUNT_API_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://creator.douyin.com/creator-micro/home",
                "User-Agent": C.USER_AGENT,
                "Cookie": cookie,
            },
        )
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
        user = payload.get("user") or {}
        return {
            "nickname": user.get("nickname"),
            "unique_id": user.get("unique_id"),
            "uid": str(user.get("uid") or "") or None,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------- 服务
class DouyinLoginService:
    def __init__(self, table, browser, messenger, state):
        self.table = table
        self.browser = browser
        self.messenger = messenger
        self.state = state

    # ---------- 入口 ----------
    def handle_event(self, event: BotEvent) -> None:
        try:
            self.state.expire_stale()
            save_group_context(event.conversation_id)
            code = parse_sms_code(event.text)
            if code:
                self._handle_code(event, code)
                return
            account_name = parse_check_command(event.text)
            if account_name:
                self._handle_check(event, account_name)
                return
            self._handle_bare(event)
        except Exception as exc:
            print(f"[service] 处理事件异常: {type(exc).__name__}: {exc}")
            if event.reply_url:
                self.messenger.reply_text(event.reply_url, "登录服务异常，请稍后重试")

    # ---------- 裸 @机器人 ----------
    def _handle_bare(self, event: BotEvent) -> None:
        accounts = self._accounts_of(event)
        if not accounts:
            self.messenger.reply_text(event.reply_url, C.MSG_NO_ACCOUNT)
            return
        if len(accounts) > 1:
            labels = "、".join(a["account"] or f"尾号{a['phone'][-4:]}" for a in accounts[:5])
            example = accounts[0]["account"] or f"尾号{accounts[0]['phone'][-4:]}"
            self.messenger.reply_text(
                event.reply_url,
                C.MSG_MULTI_ACCOUNT.format(labels=labels, bot=C.BOT_NAME, example=example))
            return
        self._start_login(event, accounts[0])

    # ---------- 校验 <账号名称> ----------
    def _handle_check(self, event: BotEvent, account_name: str) -> None:
        """校验 <账号>：支持 账号名称 / 抖音号 / 手机尾号（如 校验尾号6614、校验 6614）。"""
        accounts = self._accounts_of(event)
        wanted = normalize_name(account_name)
        digits = re.sub(r"\D", "", account_name or "")
        exact, fuzzy = [], []
        for a in accounts:
            name = normalize_name(a.get("account") or "")
            dy_id = normalize_name(a.get("douyin_id") or "")
            phone = a.get("phone") or ""
            if (wanted and wanted in (name, dy_id)) or (len(digits) >= 4 and phone.endswith(digits)):
                exact.append(a)
            elif wanted and (wanted in name or wanted in dy_id):
                fuzzy.append(a)
        if len(exact) == 1:
            self._start_login(event, exact[0])
            return
        pool = exact or fuzzy
        if len(pool) == 1:
            self._start_login(event, pool[0])
            return
        if not pool:
            labels = "、".join(a["account"] or f"尾号{a['phone'][-4:]}" for a in accounts[:6])
            self.messenger.reply_text(
                event.reply_url, f"未找到与「{account_name}」匹配的账号，可选：{labels}")
            return
        labels = "、".join(a["account"] or f"尾号{a['phone'][-4:]}" for a in pool[:5])
        self.messenger.reply_text(event.reply_url, f"多个账号匹配「{account_name}」：{labels}，请写更完整的名称")

    # ---------- 启动登录（主流程：扫码 -> 风控时自动短信验证 -> 回传验证码） ----------
    def _start_login(self, event: BotEvent, account: dict) -> None:
        self._start_qr(event, account)

    def _start_qr(self, event: BotEvent, account: dict) -> None:
        current = self.state.get_task()
        if current and current.get("status") in ("waiting_code", "validating", "waiting_scan"):
            self.messenger.reply_text(event.reply_url, C.MSG_BUSY)
            return
        account_label = account["account"] or f"尾号{account['phone'][-4:]}"
        result = self.browser.start_qr(profile_key=account["record_id"])
        if result.status != "waiting_scan":
            self.messenger.reply_text(
                event.reply_url, f"{account_label} 获取登录二维码失败：{result.reason}")
            return
        # 二维码上传图床并发群
        try:
            image_url = self.messenger.upload_qr(result.qr_path, f"qr-{int(time.time())}")
            if not self.messenger.reply_login_qr(event.reply_url, account_label, image_url):
                raise RuntimeError("二维码消息发送失败")
        except Exception as exc:
            print(f"[service] 二维码发送失败: {exc}")
            self.browser.cancel(result.challenge)
            self.messenger.reply_text(event.reply_url, f"{account_label} 二维码发送失败，请联系管理员")
            return
        # 等待扫码（扫码后如弹「身份验证」自动选「接收短信验证码」）
        scan = self.browser.wait_for_scan(result.challenge, timeout_seconds=180)
        if scan.status == "succeeded":
            self.browser.cancel(result.challenge)
            self._write_back_and_reply(account["record_id"], scan.cookie, account_label, event)
            return
        if scan.status == "waiting_code":
            self.state.create_task(
                sender_key=sender_key(event),
                record_id=account["record_id"],
                account=account_label,
                phone=account["phone"],
                challenge=result.challenge,
                mode="qr",
            )
            suffix = account["phone"][-4:]
            self.messenger.reply_text(
                event.reply_url,
                C.MSG_ASK_CODE.format(suffix=suffix, bot=C.BOT_NAME, code_placeholder="xxxxxx"))
            return
        self.browser.cancel(result.challenge)
        self.messenger.reply_text(event.reply_url, f"{account_label} 登录失败：{scan.reason}")

    def _start_sms(self, event: BotEvent, account: dict) -> None:
        """备用：直接短信登录（部分环境可用）。"""
        current = self.state.get_task()
        if current and current.get("status") in ("waiting_code", "validating", "waiting_scan"):
            self.messenger.reply_text(event.reply_url, C.MSG_BUSY)
            return
        result = self.browser.start_sms(account["phone"], profile_key=account["record_id"])
        if result.status != "waiting_code":
            self.messenger.reply_text(
                event.reply_url,
                f"{account['account'] or '账号'} 获取验证码失败：{result.reason}")
            return
        self.state.create_task(
            sender_key=sender_key(event),
            record_id=account["record_id"],
            account=account["account"] or f"尾号{account['phone'][-4:]}",
            phone=account["phone"],
            challenge=result.challenge,
            mode="sms",
        )
        suffix = account["phone"][-4:]
        self.messenger.reply_text(
            event.reply_url,
            C.MSG_ASK_CODE.format(suffix=suffix, bot=C.BOT_NAME, code_placeholder="xxxxxx"))

    # ---------- 回传验证码 ----------
    def _handle_code(self, event: BotEvent, code: str) -> None:
        task = self.state.find_active_by_sender(sender_key(event))
        if task is None or task.get("status") != "waiting_code":
            self.messenger.reply_text(event.reply_url, C.MSG_NO_TASK.format(bot=C.BOT_NAME))
            return
        self.state.update_task(status="validating")
        result = self.browser.complete(task["challenge"], code)
        account_label = task.get("account") or "账号"
        if result.status == "succeeded":
            self.state.clear_task()
            self.browser.cancel(task["challenge"])
            self._write_back_and_reply(task["record_id"], result.cookie, account_label, event)
            return
        if result.status == "retryable":
            self.state.update_task(status="waiting_code")
            self.messenger.reply_text(event.reply_url, result.reason or "验证码错误，请核对后重新发送")
            return
        # blocked / expired / failed
        self.state.clear_task()
        self.browser.cancel(task["challenge"])
        detail = result.reason or "登录验证失败"
        if result.diagnostic_path:
            detail += "（已留存诊断截图）"
        self.messenger.reply_text(event.reply_url, C.MSG_LOGIN_FAIL.format(account=account_label, reason=detail))

    # ---------- 成功后写表 ----------
    def _write_back_and_reply(self, record_id: str, cookie: str, account_label: str, event: BotEvent) -> bool:
        info = fetch_account_info(cookie)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fields = dict(
            cookie=cookie,
            status=C.STATUS_NORMAL,
            notice_at="",
            update_at=now_str,
        )
        if info.get("unique_id"):
            fields["douyin_id"] = info["unique_id"]
        if info.get("uid"):
            fields["uid"] = info["uid"]
        if info.get("nickname"):
            fields["account"] = info["nickname"]
        try:
            self.table.update_record(record_id, **fields)
        except Exception as exc:
            print(f"[service] 写表失败: {exc}")
            self.messenger.reply_text(event.reply_url, f"{account_label} 登录成功但写表失败，请联系管理员")
            return False
        print(f"[service] 写回成功: {account_label} -> {record_id} | 抖音号={info.get('unique_id')}")
        # 本地归档 + 同步主抓取脚本 cookie 文件（抓数脚本 douyin_fans_snapshot 从 douyin_cookies.txt 取登录态）
        try:
            cookie_dir = C.HERE / "output" / "cookies"
            cookie_dir.mkdir(parents=True, exist_ok=True)
            nick = str(info.get("nickname") or info.get("unique_id") or "unknown")
            (cookie_dir / f"{record_id}_{nick}.txt").write_text(cookie, encoding="utf-8")
            (C.HERE.parent / "douyin_cookies.txt").write_text(cookie, encoding="utf-8")
            print(f"[service] cookie 已同步: output/cookies/{record_id}_{nick}.txt + douyin_cookies.txt", flush=True)
        except Exception as exc:
            print(f"[service] cookie 本地同步失败（不影响表写入）: {exc}", flush=True)
        self.messenger.reply_text(event.reply_url, C.MSG_LOGIN_OK.format(account=account_label))
        return True

    # ---------- 工具 ----------
    def _accounts_of(self, event: BotEvent) -> list[dict]:
        """该 @ 用户 创建（登记）的账号：优先 unionId 精确匹配，退化为姓名弱匹配。"""
        try:
            accounts = self.table.read_accounts(only_enabled=True)
        except Exception as exc:
            print(f"[service] 读表失败: {exc}")
            return []
        if not accounts:
            return []
        # 1) unionId 精确匹配（senderStaffId -> unionId -> 记录 createdBy / 负责人姓名）
        union = ""
        try:
            union = self.table.resolve_union_id(event.sender_id)
        except Exception:
            union = ""
        if union:
            matched = [a for a in accounts
                       if a.get("creator_union_id") == union or a.get("manager_union_id") == union]
            if matched:
                return matched
        # 2) 姓名弱匹配（对比负责人姓名）
        name = normalize_name(event.sender_name)
        if name:
            matched = [a for a in accounts if normalize_name(a.get("manager_name") or "") == name]
            if matched:
                print(f"[service] 姓名弱匹配授权: sender={event.sender_name}")
                return matched
        return []
