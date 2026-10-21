"""消息回复与通知（钉钉）。

- reply_text：回复群内 @消息（用 Stream 回调里的 sessionWebhook）
- reply_login_qr：登录二维码（COS 图床 + markdown 图片）
- DingTalkWebhook：抖音群专属自定义机器人（巡检告警主通道，DY_ALERT_WEBHOOK_URL）
- RobotGroupSender：机器人直发群消息（巡检告警备用通道）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from base64 import b64encode
from pathlib import Path
from urllib.parse import quote_plus

import requests

import config as C


def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def build_signed_url(webhook_url: str, secret: str = "", now_ms: int | None = None) -> str:
    """自定义机器人加签。"""
    if not secret:
        return webhook_url
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    sign = quote_plus(b64encode(digest).decode("utf-8"))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={ts}&sign={sign}"


class CosImageUploader:
    """上传二维码到 COS 图床（复用组织现有云资源）。"""

    def __init__(self):
        self.secret_id = os.getenv("COS_SECRET_ID", "").strip()
        self.secret_key = os.getenv("COS_SECRET_KEY", "").strip()
        self.region = os.getenv("COS_REGION", "ap-shanghai").strip()
        self.bucket = os.getenv("COS_BUCKET", "").strip()
        self.public_base_url = (
            os.getenv("COS_PUBLIC_BASE_URL", "").strip()
            or f"https://{self.bucket}.cos.{self.region}.myqcloud.com"
        ).rstrip("/")
        if not (self.secret_id and self.secret_key and self.bucket):
            raise RuntimeError("COS 配置缺失（COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET）")

    def upload(self, path: Path | str, key: str) -> str:
        from qcloud_cos import CosConfig, CosS3Client

        client = CosS3Client(CosConfig(Region=self.region, SecretId=self.secret_id,
                                       SecretKey=self.secret_key, Token=None))
        client.upload_file(Bucket=self.bucket, LocalFilePath=str(path),
                           Key=key, EnableMD5=False)
        return f"{self.public_base_url}/{key.lstrip('/')}"


class Messenger:
    """群内会话回复 + 一次性的失效告警。"""

    def __init__(self, post_json=None, sleep=time.sleep, reply_attempts: int = 3):
        self.post_json = post_json or _post_json
        self.sleep = sleep
        self.reply_attempts = max(1, reply_attempts)
        self._uploader = None

    def _reply(self, reply_url: str, payload: dict) -> bool:
        if not reply_url or not reply_url.startswith("https://oapi.dingtalk.com/"):
            print(f"[notify] 非法 reply_url，拒绝发送: {str(reply_url)[:60]}")
            return False
        last = "unknown"
        for attempt in range(1, self.reply_attempts + 1):
            try:
                result = self.post_json(reply_url, payload, 10)
                code = result.get("errcode", result.get("code", 0))
                if code in (0, "0") or result.get("success") is True:
                    return True
                last = f"errcode={code}"
            except Exception as exc:
                last = type(exc).__name__
            if attempt < self.reply_attempts:
                self.sleep(0.25 * attempt)
        print(f"[notify] 群回复失败: {last}")
        return False

    def reply_text(self, reply_url: str, text: str) -> bool:
        """回复 @消息（sessionWebhook）。"""
        return self._reply(reply_url, {"msgtype": "text",
                                       "text": {"content": text},
                                       "at": {"isAtAll": False}})

    def reply_markdown(self, reply_url: str, title: str, text: str) -> bool:
        return self._reply(reply_url, {"msgtype": "markdown",
                                       "markdown": {"title": title, "text": text},
                                       "at": {"isAtAll": False}})

    def upload_qr(self, image_path: Path | str, task_id: str) -> str:
        if self._uploader is None:
            self._uploader = CosImageUploader()
        return self._uploader.upload(image_path, f"douyin-login-qrcodes/{task_id}.png")

    def reply_login_qr(self, reply_url: str, account: str, image_url: str) -> bool:
        title = f"{account} 扫码登录"
        text = (
            f"### {title}\n\n"
            "请使用该账号的抖音 App 扫码，并按手机提示完成验证。\n\n"
            f"![登录二维码]({image_url})\n\n"
            f"二维码约 1 分钟有效，过期后请重新 @{C.BOT_NAME}"
        )
        return self.reply_markdown(reply_url, title, text)


class DingTalkWebhook:
    """抖音群专属自定义机器人（巡检告警主通道）。

    配置：DY_ALERT_WEBHOOK_URL / DY_ALERT_WEBHOOK_SECRET
    ⚠️ 切勿使用小红书群的 webhook（会串群）；旧 DINGTALK_WEBHOOK_* 键已弃用不再读取。
    """

    def __init__(self, url: str = "", secret: str = ""):
        self.url = url or os.getenv("DY_ALERT_WEBHOOK_URL", "").strip()
        self.secret = secret if secret else os.getenv("DY_ALERT_WEBHOOK_SECRET", "").strip()

    @property
    def ready(self) -> bool:
        return bool(self.url)

    def send_text(self, content: str) -> bool:
        if not self.ready:
            print(f"[webhook 未配置] 应发送通知: {content[:100]}")
            return False
        payload = {"msgtype": "text", "text": {"content": content},
                   "at": {"isAtAll": False}}
        try:
            result = _post_json(build_signed_url(self.url, self.secret), payload, 15)
            ok = result.get("errcode", result.get("code", 0)) in (0, "0")
            if not ok:
                print(f"[webhook] 发送失败: {result}")
            return ok
        except Exception as exc:
            print(f"[webhook] 发送异常: {type(exc).__name__}")
            return False


class RobotGroupSender:
    """机器人直发群消息（巡检告警备用通道）。

    - 接口：POST /v1.0/robot/groupMessages/send
      ⚠️ 该接口不支持 @ 提及——告警文案不含个人 @（只 @机器人名称文本），可用
    - openConversationId：从群内消息回调自动捕获（output/group_context.json）
    """

    def __init__(self):
        self.client_id = (os.getenv("DOUYIN_LOGIN_BOT_CLIENT_ID", "").strip()
                          or os.getenv("DINGTALK_LOGIN_BOT_CLIENT_ID", "").strip())
        self.client_secret = (os.getenv("DOUYIN_LOGIN_BOT_CLIENT_SECRET", "").strip()
                              or os.getenv("DINGTALK_LOGIN_BOT_CLIENT_SECRET", "").strip())
        self._token = ""
        self._token_deadline = 0.0

    @property
    def group_id(self) -> str:
        try:
            data = json.loads((C.HERE / "output" / "group_context.json").read_text(encoding="utf-8"))
            return str(data.get("conversation_id") or "")
        except Exception:
            return ""

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret and self.group_id)

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_deadline - 60:
            return self._token
        resp = requests.post("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                             json={"appKey": self.client_id, "appSecret": self.client_secret},
                             timeout=20)
        data = resp.json()
        self._token = str(data.get("accessToken") or "")
        self._token_deadline = time.time() + int(data.get("expireIn") or 7200)
        return self._token

    def send_text(self, content: str) -> bool:
        if not self.ready:
            return False
        try:
            resp = requests.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers={"x-acs-dingtalk-access-token": self._access_token()},
                json={"robotCode": self.client_id,
                      "openConversationId": self.group_id,
                      "msgKey": "sampleText",
                      "msgParam": json.dumps({"content": content}, ensure_ascii=False)},
                timeout=20)
            data = resp.json()
            ok = bool(data.get("processQueryKey"))
            if not ok:
                print(f"[robot-group] 发送失败: {str(data)[:220]}")
            return ok
        except Exception as exc:
            print(f"[robot-group] 发送异常: {type(exc).__name__}: {exc}")
            return False
