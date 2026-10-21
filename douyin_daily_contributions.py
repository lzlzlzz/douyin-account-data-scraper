#!/usr/bin/env python3
"""抓取抖音创作者中心的每日投稿量和粉丝数据。

只调用创作者中心数据页使用的只读接口，不执行发布、编辑或账号操作。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


PAGE_URL = "https://creator.douyin.com/creator-micro/data-center/operation"
API_URL = "https://creator.douyin.com/janus/douyin/creator/data/overview/dashboard"
FANS_API_URL = "https://creator.douyin.com/janus/douyin/creator/data/overview/dashboard/fans"
DEFAULT_OUTPUT = Path("data/daily_creator_metrics.csv")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
ALLOWED_DAYS = (1, 7, 30, 60, 90)


@dataclass(frozen=True)
class DailyContribution:
    """单日投稿量，保留接口原始日期和来源指标名便于审计。"""

    date: str
    contribution_count: int
    metric_name: str = "投稿量"
    source_metric: str = "publish_cnt"


@dataclass(frozen=True)
class DailyMetrics:
    """合并后的单日投稿和粉丝指标。"""

    date: str
    contribution_count: int
    total_fans: int | None
    net_fans: int | None
    increase_fans: int | None
    cancel_fans: int | None
    return_fans: int | None


class DouyinApiError(RuntimeError):
    """抖音接口返回非成功状态或响应格式异常。"""


def _non_empty(value: Any) -> bool:
    return value is not None and value != ""


def build_cookie_jar():
    """读取 Chrome 登录态；domain_name 用父域以包含 .douyin.com 会话 Cookie。"""

    try:
        import browser_cookie3
    except ImportError as exc:  # pragma: no cover - 环境安装问题
        raise RuntimeError(
            "缺少 browser-cookie3，请先执行 python3 -m pip install -e ."
        ) from exc

    try:
        jar = browser_cookie3.chrome(domain_name="douyin.com")
        cookies = list(jar)
    except Exception as exc:  # pragma: no cover - 取决于本机钥匙串授权
        raise RuntimeError(
            "读取 Chrome Cookie 失败；请确认 Chrome 已登录 creator.douyin.com，"
            "并允许当前 Python 访问 Chrome Cookie。"
        ) from exc

    if not any(cookie.name in {"sessionid", "sessionid_ss", "sid_tt"} for cookie in cookies):
        raise RuntimeError(
            "未找到抖音登录 Cookie。请先在 Chrome 登录抖音创作者中心后重试。"
        )
    return jar


def _cookie_header_from_env() -> str | None:
    cookie = os.environ.get("DOUYIN_COOKIE", "").strip()
    return cookie or None


def _cookie_from_file(path) -> str | None:
    """从文件读取 Cookie：支持 'k=v; k2=v2' 单行文本，或浏览器导出的 JSON 数组。"""
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"Cookie 文件不存在：{p}")
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Cookie 文件为空：{p}")
    if text.startswith("["):
        try:
            items = json.loads(text)
            pairs = [f"{c['name']}={c['value']}" for c in items if c.get("name")]
            return "; ".join(pairs) or None
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Cookie 文件 JSON 格式无法解析：{exc}") from exc
    return text.replace("\r", " ").replace("\n", " ").strip()


def parse_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def normalize_date(raw: Any) -> str:
    """接口返回 YYYYMMDD；同时兼容 ISO 日期，统一为 YYYY-MM-DD。"""

    text = str(raw or "")
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise DouyinApiError(f"接口返回了无法识别的日期：{raw!r}") from exc


def extract_publish_metric(payload: Mapping[str, Any]) -> tuple[str, list[Mapping[str, Any]]]:
    """从 dashboard 响应中定位 publish_cnt 指标。"""

    return _extract_metric(payload, ("publish_cnt",), "publish_cnt", "投稿量")


FAN_METRICS: dict[str, tuple[tuple[str, ...], str]] = {
    "total_fans": (("total_fans_cnt", "fans_all"), "总粉丝量"),
    "net_fans": (("net_fans_cnt", "net_follow_fans"), "粉丝净增"),
    "increase_fans": (("increase_fans_cnt", "new_fans_cnt", "follow_fans_cnt"), "吸粉量"),
    "cancel_fans": (("cancel_fans_cnt", "unfollow_fans"), "脱粉量"),
    "return_fans": (("return_fans_cnt", "revisit_fans_cnt", "home_view_fans_cnt"), "回访粉丝量"),
}


def _extract_metric(
    payload: Mapping[str, Any],
    metric_keys: tuple[str, ...],
    source_name: str,
    display_name: str,
) -> tuple[str, list[Mapping[str, Any]]]:
    if payload.get("status_code") not in (None, 0):
        raise DouyinApiError(
            f"抖音接口错误 status_code={payload.get('status_code')!r} "
            f"status_msg={payload.get('status_msg')!r}"
        )

    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        raise DouyinApiError("接口响应缺少 metrics 列表")

    for metric in metrics:
        if isinstance(metric, Mapping) and metric.get("english_metric_name") in metric_keys:
            trends = metric.get("trends") or []
            if not isinstance(trends, list):
                raise DouyinApiError(f"{source_name}.trends 不是列表")
            return str(metric.get("metric_name") or display_name), trends

    raise DouyinApiError(f"接口响应中没有 {source_name}；可能是权限或登录态已失效")


def normalize_rows(payload: Mapping[str, Any]) -> list[DailyContribution]:
    metric_name, trends = extract_publish_metric(payload)
    rows: list[DailyContribution] = []
    for trend in trends:
        if not isinstance(trend, Mapping) or not _non_empty(trend.get("date_time")):
            continue
        try:
            count = int(trend.get("value") or 0)
        except (TypeError, ValueError) as exc:
            raise DouyinApiError(f"投稿量不是整数：{trend.get('value')!r}") from exc
        rows.append(
            DailyContribution(
                date=normalize_date(trend["date_time"]),
                contribution_count=count,
                metric_name=metric_name,
            )
        )
    return sorted(rows, key=lambda row: row.date)


def normalize_fan_rows(payload: Mapping[str, Any]) -> dict[str, dict[str, int | None]]:
    """把粉丝接口的五类趋势整理为 date -> 指标字典。"""

    by_date: dict[str, dict[str, int | None]] = {}
    for field, (metric_keys, display_name) in FAN_METRICS.items():
        _, trends = _extract_metric(payload, metric_keys, "/".join(metric_keys), display_name)
        for trend in trends:
            if not isinstance(trend, Mapping) or not _non_empty(trend.get("date_time")):
                continue
            raw_value = trend.get("value")
            if raw_value is None or raw_value == "":
                value = None
            else:
                try:
                    value = int(raw_value)
                except (TypeError, ValueError) as exc:
                    raise DouyinApiError(f"{display_name}不是整数：{raw_value!r}") from exc
            by_date.setdefault(normalize_date(trend["date_time"]), {})[field] = value
    return by_date


def combine_rows(
    publish_payload: Mapping[str, Any], fans_payload: Mapping[str, Any]
) -> list[DailyMetrics]:
    """按日期合并投稿量和粉丝趋势，避免两个接口的日期顺序差异影响结果。"""

    publish_rows = {row.date: row for row in normalize_rows(publish_payload)}
    fan_rows = normalize_fan_rows(fans_payload)
    dates = sorted(set(publish_rows) | set(fan_rows))
    result: list[DailyMetrics] = []
    for selected_date in dates:
        publish = publish_rows.get(selected_date)
        fans = fan_rows.get(selected_date, {})
        result.append(
            DailyMetrics(
                date=selected_date,
                contribution_count=publish.contribution_count if publish else 0,
                total_fans=fans.get("total_fans"),
                net_fans=fans.get("net_fans"),
                increase_fans=fans.get("increase_fans"),
                cancel_fans=fans.get("cancel_fans"),
                return_fans=fans.get("return_fans"),
            )
        )
    return result


def _date_range(start_date: str | None, end_date: str | None) -> dict[str, str] | None:
    if (start_date is None) != (end_date is None):
        raise ValueError("--start-date 和 --end-date 必须同时提供")
    if start_date and end_date and start_date > end_date:
        raise ValueError("--start-date 不能晚于 --end-date")
    if start_date and end_date:
        # dashboard 接口要求 YYYYMMDD；CLI 仍对外使用更易读的 YYYY-MM-DD。
        return {
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
        }
    return None


class DouyinClient:
    """抖音 dashboard 只读 API 客户端。"""

    def __init__(self, cookie_jar=None, cookie_header: str | None = None, user_agent: str = DEFAULT_USER_AGENT):
        if cookie_jar is None and cookie_header is None:
            cookie_jar = build_cookie_jar()
        self.cookie_jar = cookie_jar
        self.cookie_header = cookie_header
        self.user_agent = user_agent

    def fetch_dashboard(
        self,
        recent_days: int = 7,
        start_date: str | None = None,
        end_date: str | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        if recent_days not in ALLOWED_DAYS:
            raise ValueError(f"--days 只能是 {ALLOWED_DAYS} 之一")
        body: dict[str, Any] = {"recent_days": recent_days}
        selected_range = _date_range(start_date, end_date)
        if selected_range:
            body["date_range"] = selected_range

        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        request = urllib.request.Request(
            API_URL,
            data=payload,
            method="POST",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://creator.douyin.com",
                "Referer": PAGE_URL,
                "User-Agent": self.user_agent,
            },
        )
        if self.cookie_header:
            request.add_header("Cookie", self.cookie_header)

        return self._request_json(request, extract_publish_metric, retries)

    def fetch_fans(
        self,
        recent_days: int = 7,
        start_date: str | None = None,
        end_date: str | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        if recent_days not in ALLOWED_DAYS:
            raise ValueError(f"--days 只能是 {ALLOWED_DAYS} 之一")
        params: dict[str, str | int] = {"recent_days": recent_days}
        selected_range = _date_range(start_date, end_date)
        if selected_range:
            params["date_range"] = json.dumps(selected_range, separators=(",", ":"))
        request = urllib.request.Request(
            f"{FANS_API_URL}?{urllib.parse.urlencode(params)}",
            method="GET",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://creator.douyin.com",
                "Referer": PAGE_URL,
                "User-Agent": self.user_agent,
            },
        )
        if self.cookie_header:
            request.add_header("Cookie", self.cookie_header)
        return self._request_json(request, normalize_fan_rows, retries)

    def _request_json(self, request, validator, retries: int) -> dict[str, Any]:
        context = ssl.create_default_context()
        handlers = [urllib.request.HTTPSHandler(context=context)]
        if self.cookie_jar is not None:
            handlers.insert(0, urllib.request.HTTPCookieProcessor(self.cookie_jar))
        opener = urllib.request.build_opener(*handlers)

        for attempt in range(retries + 1):
            try:
                with opener.open(request, timeout=60) as response:
                    if response.status != 200:
                        raise DouyinApiError(f"HTTP {response.status}")
                    result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise DouyinApiError("接口响应不是 JSON 对象")
                validator(result)
                return result
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    raise DouyinApiError(
                        f"HTTP {exc.code}，请刷新登录态或降低请求频率，不自动重试"
                    ) from exc
                if attempt >= retries:
                    raise DouyinApiError(f"HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= retries:
                    raise DouyinApiError(f"网络或 JSON 错误：{exc}") from exc
            if attempt < retries:
                time.sleep((2**attempt) * random.uniform(0.8, 1.5))

        raise AssertionError("unreachable")


def write_csv(rows: Iterable[DailyMetrics], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "date",
                "contribution_count",
                "total_fans",
                "net_fans",
                "increase_fans",
                "cancel_fans",
                "return_fans",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(rows: Iterable[DailyMetrics], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "source": PAGE_URL,
        "apis": {"contributions": API_URL, "fans": FANS_API_URL},
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": [asdict(row) for row in rows],
    }
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取抖音创作者中心每日投稿量和粉丝数据")
    parser.add_argument("--days", type=int, default=7, choices=ALLOWED_DAYS, help="最近天数，默认 7")
    parser.add_argument("--start-date", type=parse_date, help="自定义起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_date, help="自定义结束日期 YYYY-MM-DD")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cookie", help="可选：直接传入 Cookie；优先建议使用 Chrome 自动读取")
    parser.add_argument("--cookie-file", type=Path,
                        help="可选：从文件读取 Cookie（'k=v; k2=v2' 一行，或浏览器导出的 JSON 数组）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cookie_header = args.cookie or _cookie_from_file(args.cookie_file) or _cookie_header_from_env()
        client = DouyinClient(cookie_header=cookie_header)
        payload = client.fetch_dashboard(
            recent_days=args.days,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        fans_payload = client.fetch_fans(
            recent_days=args.days,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        rows = combine_rows(payload, fans_payload)
        if args.format == "csv":
            write_csv(rows, args.output)
        else:
            write_json(rows, args.output)
        print(f"抓取完成：{len(rows)} 天，输出：{args.output}")
        for row in rows:
            print(
                f"{row.date}\t投稿 {row.contribution_count}\t"
                f"总粉丝 {row.total_fans}\t净增 {row.net_fans}\t"
                f"吸粉 {row.increase_fans}\t脱粉 {row.cancel_fans}\t"
                f"回访 {row.return_fans}"
            )
        return 0
    except (DouyinApiError, RuntimeError, ValueError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
