"""抖音登录助手 · 钉钉 Stream 机器人入口。

启动：
  python dy_bot.py

需要在 .env 配置：
  DOUYIN_LOGIN_BOT_CLIENT_ID / DOUYIN_LOGIN_BOT_CLIENT_SECRET  （新建的「抖音登录助手」机器人应用）
  DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_OPERATOR_ID  （AI 表写入，与小红书共用主应用）
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

# ⚠️ 禁代理直连（2026-10-09 实锤修复）：
# 钉钉/COS/抖音均为国内服务，若继承 shell 的本地代理（如 http://127.0.0.1:51679），
# 一切流量绕本地代理——代理一抖动，Stream 长连接假死（进程活着但消息全丢）。
# 这里清掉继承的代理环境变量，让所有网络调用直连。
for _k in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)

from dotenv import load_dotenv

import config as C

load_dotenv(C.HERE / ".env")

import dingtalk_stream  # noqa: E402

from aitable import DouyinTable  # noqa: E402
from browser_login import DouyinBrowserLogin  # noqa: E402
from dy_monitor import monitor_loop  # noqa: E402
from dy_notify import Messenger  # noqa: E402
from dy_service import DouyinLoginService, normalize_event  # noqa: E402
from dy_state import LoginState  # noqa: E402


class Handler(dingtalk_stream.ChatbotHandler):
    def __init__(self, service: DouyinLoginService, pool: ThreadPoolExecutor):
        super().__init__()
        self.service = service
        self.pool = pool

    async def process(self, callback_message):
        event = normalize_event(callback_message)
        if event is not None:
            self.pool.submit(self.service.handle_event, event)
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"


def main() -> None:
    client_id = (os.environ.get("DOUYIN_LOGIN_BOT_CLIENT_ID", "").strip()
                 or os.environ.get("DINGTALK_LOGIN_BOT_CLIENT_ID", "").strip())
    client_secret = (os.environ.get("DOUYIN_LOGIN_BOT_CLIENT_SECRET", "").strip()
                     or os.environ.get("DINGTALK_LOGIN_BOT_CLIENT_SECRET", "").strip())
    if not (client_id and client_secret):
        raise SystemExit(
            "缺少机器人凭据：请在 .env 配置 DOUYIN_LOGIN_BOT_CLIENT_ID / "
            "DOUYIN_LOGIN_BOT_CLIENT_SECRET（或复用 DINGTALK_LOGIN_BOT_CLIENT_ID / SECRET）")

    table = DouyinTable()
    browser = DouyinBrowserLogin()
    messenger = Messenger()
    state = LoginState()
    service = DouyinLoginService(table, browser, messenger, state)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dy-login")

    client = dingtalk_stream.DingTalkStreamClient(
        dingtalk_stream.Credential(client_id, client_secret))
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler(service, pool))

    # Cookie 巡检守护线程（失效自动群告警；随 bot 生命周期，被看门狗统一守护）
    threading.Thread(target=monitor_loop, daemon=True, name="dy-monitor").start()

    print(f"[bot] {C.BOT_NAME} 已启动（Stream 模式，等待群内 @消息）")
    try:
        client.start_forever()
    finally:
        pool.shutdown(wait=False)
        browser.close_all()


if __name__ == "__main__":
    main()
