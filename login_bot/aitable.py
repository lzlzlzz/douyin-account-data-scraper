"""抖音店铺账号信息表 · 读写客户端（钉钉 AI 表 OpenAPI）。

复用小红书登录助手的 OpenAPI 调用模式（dingtalk_aitable.py），抖音表定制。
凭据来自环境变量：DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_OPERATOR_ID
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import requests

import config as C


class AITableError(RuntimeError):
    pass


class DouyinTable:
    def __init__(self, client_id: str = "", client_secret: str = "", operator_id: str = "",
                 api_base: str = "https://api.dingtalk.com"):
        self.client_id = client_id or os.getenv("DINGTALK_CLIENT_ID", "").strip()
        self.client_secret = client_secret or os.getenv("DINGTALK_CLIENT_SECRET", "").strip()
        self.operator_id = operator_id or os.getenv("DINGTALK_OPERATOR_ID", "").strip()
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self._token = ""
        self._expires_at = 0.0
        if not (self.client_id and self.client_secret and self.operator_id):
            raise AITableError("钉钉凭据缺失（DINGTALK_CLIENT_ID / SECRET / OPERATOR_ID）")

    # ---------- 基础 ----------
    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 300:
            return self._token
        resp = self.session.post(
            f"{self.api_base}/v1.0/oauth2/accessToken",
            json={"appKey": self.client_id, "appSecret": self.client_secret},
            timeout=30,
        )
        data = self._parse(resp)
        token = data.get("accessToken")
        if not token:
            raise AITableError(f"获取 accessToken 失败: {data}")
        self._token = token
        self._expires_at = now + int(data.get("expireIn", 7200))
        return token

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as exc:
            raise AITableError(f"钉钉返回非 JSON: HTTP {resp.status_code}") from exc
        if resp.status_code >= 400:
            raise AITableError(f"钉钉 HTTP {resp.status_code}: {str(data)[:400]}")
        if data.get("code") and str(data.get("code")) not in ("0", "OK"):
            raise AITableError(f"钉钉返回失败: {str(data)[:400]}")
        return data

    def _request(self, method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
        url = f"{self.api_base}{path}"
        last_err: Exception | None = None
        for attempt in range(3):
            headers = {"Content-Type": "application/json",
                       "x-acs-dingtalk-access-token": self._access_token()}
            try:
                resp = self.session.request(method, url, headers=headers,
                                            json=body if body is not None else {}, timeout=timeout)
                return self._parse(resp)
            except Exception as exc:
                last_err = exc
                if any(k in str(exc).lower() for k in ("token", "401", "unauthorized")):
                    self._token = ""
                    self._expires_at = 0.0
                if attempt == 2:
                    break
                time.sleep(1.5 * (attempt + 1))
        raise AITableError(f"钉钉请求失败: {last_err}") from last_err

    def _records_path(self, suffix: str = "") -> str:
        base = quote(C.AITABLE_BASE, safe="")
        sheet = quote(C.AITABLE_SHEET, safe="")
        op = quote(self.operator_id, safe="")
        return f"/v1.0/notable/bases/{base}/sheets/{sheet}/records{suffix}?operatorId={op}"

    # ---------- 读 ----------
    def query_all(self, timeout: int = 120) -> list[dict]:
        """读取全表原始记录（含分页）。归一化为 {record_id, cells, created_by_union_id}。"""
        records: list[dict] = []
        next_token = ""
        while True:
            body: dict[str, Any] = {"maxResults": 100}
            if next_token:
                body["nextToken"] = next_token
            data = self._request("POST", self._records_path("/list"), body, timeout=timeout)
            page = data.get("records") or (data.get("data") or {}).get("records") or []
            for rec in page:
                created_by = rec.get("createdBy")
                created_by = created_by if isinstance(created_by, dict) else {}
                records.append({
                    "record_id": rec.get("id") or rec.get("recordId") or "",
                    "cells": rec.get("fields") or rec.get("cells") or {},
                    "created_by_union_id": str(created_by.get("unionId") or ""),
                })
            next_token = data.get("nextToken") or data.get("nextCursor") \
                or (data.get("data") or {}).get("nextToken") or ""
            if not next_token:
                break
        return records

    @staticmethod
    def _cell(cells: dict, name: str, default=""):
        if name in cells:
            return cells.get(name, default)
        return default

    @staticmethod
    def _select_id(value) -> str:
        if isinstance(value, dict):
            return str(value.get("id") or "")
        return str(value or "")

    @staticmethod
    def _select_name(value) -> str:
        if isinstance(value, dict):
            return str(value.get("name") or "")
        return ""

    @staticmethod
    def _person(value) -> dict:
        """人员字段 -> {union_id, name}。"""
        item = value[0] if isinstance(value, list) and value else value
        item = item if isinstance(item, dict) else {}
        return {
            "union_id": str(item.get("unionId") or ""),
            "user_id": str(item.get("userId") or item.get("userid") or ""),
            "name": str(item.get("name") or ""),
        }

    def read_accounts(self, only_enabled: bool = True, require_phone: bool = True) -> list[dict]:
        """读全表 -> 规范化的账号列表。"""
        accounts = []
        for rec in self.query_all():
            cells = rec.get("cells") or {}
            phone = str(self._cell(cells, C.F_PHONE, "") or "").strip()
            if require_phone and not phone:
                continue
            enabled = self._select_id(self._cell(cells, C.F_ENABLED, ""))
            if only_enabled and enabled and enabled != C.ENABLED_ON:
                continue
            manager = self._person(self._cell(cells, C.F_MANAGER, "")
                                   or self._cell(cells, C.F_MANAGER_OLD, ""))
            accounts.append({
                "record_id": rec.get("record_id") or "",
                "phone": phone,
                "account": str(self._cell(cells, C.F_ACCOUNT, "") or "").strip(),
                "douyin_id": str(self._cell(cells, C.F_DY_ID, "") or "").strip(),
                "uid": str(self._cell(cells, C.F_UID, "") or "").strip(),
                "cookie": str(self._cell(cells, C.F_COOKIE, "") or "").strip(),
                "status": self._select_id(self._cell(cells, C.F_STATUS, "")),
                "status_name": self._select_name(self._cell(cells, C.F_STATUS, "")),
                "enabled": enabled,
                "creator_union_id": rec.get("created_by_union_id") or "",
                "manager_union_id": manager["union_id"],
                "manager_name": manager["name"],
                "notice_at": str(self._cell(cells, C.F_NOTICE_AT, "") or ""),
                "data_updated_at": str(self._cell(cells, C.F_UPDATE_AT, "") or ""),
            })
        return accounts

    # ---------- 写 ----------
    def update_record(self, record_id: str, timeout: int = 60, **fields):
        """按字段显示名更新记录。fields 示例：cookie=..., status=..., notice_at=''"""
        cells: dict[str, Any] = {}
        mapping = {
            "cookie": C.F_COOKIE,
            "account": C.F_ACCOUNT,
            "douyin_id": C.F_DY_ID,
            "uid": C.F_UID,
            "status": C.F_STATUS,
            "enabled": C.F_ENABLED,
            "notice_at": C.F_NOTICE_AT,
            "update_at": C.F_UPDATE_AT,
        }
        for key, value in fields.items():
            if key in mapping:
                cells[mapping[key]] = value
        if not cells:
            return
        body = {"records": [{"id": record_id, "fields": cells}]}
        self._request("PUT", self._records_path(), body, timeout=timeout)

    # ---------- 通讯录（身份解析） ----------
    _union_cache: dict[str, str] = {}

    def resolve_union_id(self, user_id: str) -> str:
        """userId -> unionId（钉钉通讯录 API，带缓存）。失败返回 ''。"""
        if not user_id:
            return ""
        cached = self._union_cache.get(user_id)
        if cached is not None:
            return cached
        union = ""
        try:
            resp = self.session.get(
                "https://oapi.dingtalk.com/topapi/v2/user/get",
                params={"access_token": self._access_token(), "userid": user_id},
                timeout=20,
            )
            data = resp.json()
            if data.get("errcode") in (0, "0"):
                union = str((data.get("result") or {}).get("unionid") or "")
        except Exception:
            union = ""
        self._union_cache[user_id] = union
        return union

    def resolve_user_id(self, union_id: str) -> str:
        """unionId -> userId（用于告警 @ 提及）。失败返回 ''。"""
        if not union_id:
            return ""
        try:
            resp = self.session.get(
                "https://oapi.dingtalk.com/user/getUseridByUnionid",
                params={"access_token": self._access_token(), "unionid": union_id},
                timeout=20,
            )
            data = resp.json()
            if data.get("errcode") in (0, "0"):
                return str(data.get("userid") or "")
        except Exception:
            pass
        return ""
