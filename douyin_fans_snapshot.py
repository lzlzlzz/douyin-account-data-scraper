#!/usr/bin/env python3
"""抖音创作者中心 - 粉丝数据快照（近7天 + 近30天）。

每次运行产出「一行」数据：近7天 5 个指标 + 近30天 5 个指标。
- 写入 rpa.douyin_fans_snapshot（每天一行；同日重跑覆盖，幂等）
- 同时追加/更新本地 CSV（data/fans_snapshot.csv，同日覆盖）

指标：总粉丝量 / 粉丝净增 / 吸粉量 / 脱粉量 / 回访粉丝量

用法:
  python douyin_fans_snapshot.py --cookie-file douyin_cookies.txt
  python douyin_fans_snapshot.py --dry-run            # 只抓取打印，不写库不写文件
  DOUYIN_COOKIE='sessionid=xxx; ...' python douyin_fans_snapshot.py
  不带 cookie 参数时尝试从本机 Chrome 读取登录态（需 browser-cookie3）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

PAGE_URL = "https://creator.douyin.com/creator-micro/data-center/operation"
FANS_API_URL = "https://creator.douyin.com/janus/douyin/creator/data/overview/dashboard/fans"
ACCOUNT_API_URL = "https://creator.douyin.com/web/api/media/user/info/"
DEFAULT_CSV = Path("data/fans_snapshot.csv")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 指标映射：字段 -> (候选接口key, 中文名)
FAN_FIELDS: dict[str, tuple[tuple[str, ...], str]] = {
    "total_fans": (("total_fans_cnt", "fans_all"), "总粉丝量"),
    "net_fans": (("net_fans_cnt", "net_follow_fans"), "粉丝净增"),
    "increase_fans": (("increase_fans_cnt", "new_fans_cnt", "follow_fans_cnt"), "吸粉量"),
    "cancel_fans": (("cancel_fans_cnt", "unfollow_fans"), "脱粉量"),
    "return_fans": (("return_fans_cnt", "revisit_fans_cnt", "home_view_fans_cnt"), "回访粉丝量"),
}

CSV_HEADER = [
    "抓取日期", "账号", "抖音号",
    "近7天_周期开始", "近7天_周期结束", "近7天_总粉丝量", "近7天_粉丝净增",
    "近7天_吸粉量", "近7天_脱粉量", "近7天_回访粉丝量",
    "近30天_周期开始", "近30天_周期结束", "近30天_总粉丝量", "近30天_粉丝净增",
    "近30天_吸粉量", "近30天_脱粉量", "近30天_回访粉丝量",
    "更新时间",
]


class DouyinApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- cookie
def load_cookie(cookie_file: str | None, cookie_arg: str | None) -> str:
    """优先级：--cookie > --cookie-file > DOUYIN_COOKIE 环境变量 > 本机 Chrome。"""
    if cookie_arg:
        return cookie_arg.strip()
    if cookie_file:
        p = Path(cookie_file)
        if not p.exists():
            raise RuntimeError(f"Cookie 文件不存在：{p}")
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"Cookie 文件为空：{p}")
        if text.startswith("["):  # 浏览器导出的 JSON 数组
            items = json.loads(text)
            return "; ".join(f"{c['name']}={c['value']}" for c in items if c.get("name"))
        return text.replace("\r", " ").replace("\n", " ").strip()
    env = os.environ.get("DOUYIN_COOKIE", "").strip()
    if env:
        return env
    # 兜底：Chrome 自动读
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError("缺少 Cookie：请用 --cookie-file 传入，或安装 browser-cookie3") from exc
    jar = browser_cookie3.chrome(domain_name="douyin.com")
    parts = [f"{c.name}={c.value}" for c in jar if c.name in
             {"sessionid", "sessionid_ss", "sid_tt", "passport_csrf_token", "ttwid", "odin_tt", "sid_guard", "uid_tt"}]
    if not any(p.startswith("sessionid=") for p in parts):
        raise RuntimeError("本机 Chrome 未找到抖音登录态，请用 --cookie-file 传入 Cookie")
    return "; ".join(parts)


def cookie_expiry(cookie: str) -> datetime | None:
    """从 sid_guard 解析登录态硬到期时间（value|创建TS|有效期秒|到期GMT）。

    sid_guard 是抖音登录态的硬期限凭据（当前为 60 天），实测无自动续期机制。
    """
    m = re.search(r"sid_guard=([^;]+)", cookie)
    if not m:
        return None
    parts = re.split(r"%7C|\|", m.group(1))
    if len(parts) < 3:
        return None
    try:
        ts, age = int(parts[1]), int(parts[2])
        return datetime.fromtimestamp(ts + age)
    except (ValueError, OSError):
        return None


# 直连 opener：绕过环境代理（creator.douyin.com 为国内站，直连更稳；
# 本机/沙箱代理抖动时会导致 Tunnel 502 → 抓取随机失败，2026-10-12 实锤）
_SSL_CTX = ssl.create_default_context()
_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_SSL_CTX),
)


def fetch_account_info(cookie: str) -> dict:
    """识别当前登录账号：{nickname, unique_id, uid, sec_uid}。失败返回 {}。"""
    req = urllib.request.Request(
        ACCOUNT_API_URL, method="GET",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://creator.douyin.com/creator-micro/home",
            "User-Agent": USER_AGENT,
            "Cookie": cookie,
        },
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        u = payload.get("user") or {}
        if not u:
            return {}
        return {
            "nickname": u.get("nickname"),
            "unique_id": u.get("unique_id"),
            "uid": str(u.get("uid") or "") or None,
            "sec_uid": u.get("sec_uid"),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------- 抓取
def fetch_fans(cookie: str, recent_days: int, retries: int = 2) -> dict:
    params = urllib.parse.urlencode({"recent_days": recent_days})
    req = urllib.request.Request(
        f"{FANS_API_URL}?{params}", method="GET",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://creator.douyin.com",
            "Referer": PAGE_URL,
            "User-Agent": USER_AGENT,
            "Cookie": cookie,
        },
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _NO_PROXY_OPENER.open(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            code = payload.get("status_code")
            if code not in (None, 0):
                raise DouyinApiError(f"接口错误 status_code={code} status_msg={payload.get('status_msg')!r}")
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise DouyinApiError(f"HTTP {exc.code}：登录态失效或风控，请更新 Cookie（不自动重试）") from exc
            last_err = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
        if attempt < retries:
            time.sleep((2 ** attempt) * random.uniform(0.8, 1.5))
    raise DouyinApiError(f"请求失败：{last_err}")


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_fans(payload: dict) -> dict[str, int | None]:
    """从 fans 响应提取 5 个指标值：优先 metric_value，缺失时用 trends 兜底聚合。"""
    by_name: dict[str, dict] = {}
    for m in payload.get("metrics") or []:
        if isinstance(m, dict) and m.get("english_metric_name"):
            by_name[str(m["english_metric_name"])] = m
    out: dict[str, int | None] = {}
    for field, (keys, _label) in FAN_FIELDS.items():
        metric = next((by_name[k] for k in keys if k in by_name), None)
        if metric is None:
            out[field] = None
            continue
        value = _to_int(metric.get("metric_value"))
        if value is None:
            trends = [t for t in (metric.get("trends") or []) if isinstance(t, dict) and t.get("date_time")]
            trends.sort(key=lambda t: str(t["date_time"]))
            vals = [_to_int(t.get("value")) for t in trends]
            vals = [v for v in vals if v is not None]
            if vals:
                value = vals[-1] if field == "total_fans" else sum(vals)
        out[field] = value
    return out


# ---------------------------------------------------------------- 组装与输出
def build_row(account: str, d7: dict, d30: dict, today: date, now_str: str) -> dict:
    row = {"account": account, "snapshot_date": today.isoformat(), "update_time": now_str}
    # 近7天统计周期：含昨天往前 7 天；近30天同理
    for tag, days, values in (("d7", 7, d7), ("d30", 30, d30)):
        row[f"{tag}_period_start"] = (today - timedelta(days=days)).isoformat()
        row[f"{tag}_period_end"] = (today - timedelta(days=1)).isoformat()
        for field in FAN_FIELDS:
            row[f"{tag}_{field}"] = values.get(field)
    return row


def write_csv(path: Path, row: dict) -> None:
    """CSV 维护：同「日期+账号」覆盖、其他行保留（按日期排序）。

    兼容旧格式：旧行仅按日期作键（无 '|'），读取时自动归入 '日期|账号' 键。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                d = r.get("抓取日期", "")
                if not d:
                    continue
                a = r.get("账号", "")
                key = f"{d}|{a}"
                existing[key] = r
    new_key = f"{row['snapshot_date']}|{row['account']}"
    existing[new_key] = {h: row.get(field_name(h)) for h in CSV_HEADER}
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for key in sorted(existing):
            writer.writerow(existing[key])


def field_name(header: str) -> str:
    """CSV 中文表头 -> row 字段名。"""
    direct = {
        "抓取日期": "snapshot_date", "账号": "account", "抖音号": "account_id",
        "更新时间": "update_time",
    }
    if header in direct:
        return direct[header]
    # 近7天_总粉丝量 -> d7_total_fans
    part, _, rest = header.partition("_")
    if part in ("近7天", "近30天"):
        tag = "d7" if part == "近7天" else "d30"
        cn_to_field = {
            "周期开始": "period_start", "周期结束": "period_end",
            "总粉丝量": "total_fans", "粉丝净增": "net_fans",
            "吸粉量": "increase_fans", "脱粉量": "cancel_fans", "回访粉丝量": "return_fans",
        }
        return f"{tag}_{cn_to_field.get(rest, rest)}"
    return header


def print_group(label: str, values: dict) -> None:
    parts = [f"{FAN_FIELDS[f][1]}={values.get(f)}" for f in FAN_FIELDS]
    print(f"  {label}: " + " | ".join(parts))


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抖音粉丝数据快照（近7天+近30天，一行）")
    parser.add_argument("--cookie-file", help="Cookie 文件（'k=v; k2=v2' 一行，或 JSON 数组）")
    parser.add_argument("--cookie", help="直接传入 Cookie 字符串")
    parser.add_argument("--account", default=None,
                        help="账号标识（默认自动识别；识别失败时回退 default）")
    parser.add_argument("--output", type=Path, default=DEFAULT_CSV, help="本地 CSV 路径")
    parser.add_argument("--dry-run", action="store_true", help="只抓取打印，不写库不写文件")
    args = parser.parse_args(argv)

    now = datetime.now()
    today = now.date()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        cookie = load_cookie(args.cookie_file, args.cookie)
        expiry = cookie_expiry(cookie)
        if expiry:
            days_left = (expiry.date() - today).days
            print(f"Cookie 有效期至 {expiry.strftime('%Y-%m-%d %H:%M')}（剩 {days_left} 天）")
            if days_left <= 0:
                print("警告：Cookie 已过期，请更新后重试", file=sys.stderr)
            elif days_left <= 7:
                print(f"⚠️ Cookie 即将过期（剩 {days_left} 天），请及时更新 douyin_cookies.txt", file=sys.stderr)
        # 自动识别账号身份（昵称/抖音号/uid）
        account_info = fetch_account_info(cookie)
        if account_info.get("nickname") or account_info.get("unique_id"):
            print(f"账号识别: {account_info.get('nickname')}（抖音号 {account_info.get('unique_id')}，"
                  f"uid {account_info.get('uid')}）")
        else:
            print("警告：账号识别失败，account 将用 fallback", file=sys.stderr)
        if not args.account:
            args.account = account_info.get("nickname") or account_info.get("unique_id")
        print("抓取「近7天」粉丝数据...")
        d7 = parse_fans(fetch_fans(cookie, 7))
        print("抓取「近30天」粉丝数据...")
        d30 = parse_fans(fetch_fans(cookie, 30))
    except (DouyinApiError, RuntimeError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1

    print_group("近7天", d7)
    print_group("近30天", d30)

    row = build_row(args.account or "default", d7, d30, today, now_str)
    if account_info:
        row["account_id"] = account_info.get("unique_id")
        row["account_uid"] = account_info.get("uid")

    if args.dry_run:
        print("\n[dry-run] 不写库不写文件。即将写入的行：")
        print(json.dumps(row, ensure_ascii=False, indent=1))
        return 0

    try:
        from douyin_ingest import ensure_table, ingest_snapshot, table_count
        ensure_table()
        n = ingest_snapshot(row)
        print(f"\n[入库] rpa.douyin_fans_snapshot 写入 {n} 行 | 表总行数: {table_count()}")
    except ImportError:
        print("警告：未找到 douyin_ingest.py（与脚本同目录），跳过入库", file=sys.stderr)
    except Exception as exc:
        print(f"入库失败：{exc}", file=sys.stderr)
        return 1

    write_csv(args.output, row)
    print(f"[本地] {args.output} 已更新（同日覆盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
