"""登录任务状态（简化版：单活跃任务 + JSON 持久化）。

设计：同一时间最多一个进行中的登录任务（与浏览器会话一一对应）。
重启后未完成任务视为失效（浏览器会话已丢失）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config as C

ACTIVE_STATUSES = ("waiting_code", "validating")


class LoginState:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or (C.HERE / "output" / "login_state.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.task: dict | None = None
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        task = data.get("task")
        if isinstance(task, dict) and task.get("status") in ACTIVE_STATUSES:
            # 重启后浏览器会话不存在，作废
            task["status"] = "interrupted"

    def _save(self):
        self.path.write_text(json.dumps({"task": self.task}, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    # ---------- 生命周期 ----------
    def create_task(self, *, sender_key: str, record_id: str, account: str, phone: str,
                    challenge: str, mode: str = "sms") -> dict:
        self.task = {
            "task_id": f"t{int(time.time())}",
            "sender_key": sender_key,
            "record_id": record_id,
            "account": account,
            "phone": phone,
            "challenge": challenge,
            "mode": mode,               # sms / qr
            "status": "waiting_code",   # waiting_code / waiting_scan / validating
            "created_at": time.time(),
        }
        self._save()
        return self.task

    def get_task(self) -> dict | None:
        return self.task

    def find_active_by_sender(self, sender_key: str) -> dict | None:
        if self.task and self.task.get("sender_key") == sender_key \
                and self.task.get("status") in ("waiting_code", "waiting_scan", "validating"):
            return self.task
        return None

    def update_task(self, **kw) -> dict | None:
        if self.task is None:
            return None
        self.task.update(kw)
        self._save()
        return self.task

    def clear_task(self):
        self.task = None
        self._save()

    def expire_stale(self, ttl_seconds: int = 900) -> bool:
        """超过 TTL 的任务作废。返回是否有任务被清理。"""
        if self.task and (time.time() - float(self.task.get("created_at") or 0)) > ttl_seconds:
            self.clear_task()
            return True
        return False
