"""rpa 入库客户端（http://124.222.242.218:8777，直连不走代理）。

表：rpa.douyin_fans_snapshot —— 抖音粉丝数据快照（每天一行：近7天组 + 近30天组）
写入：delete_where 先删同日 + REPLACE INTO 兜底（幂等，可重复跑）
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://124.222.242.218:8777"
TOKEN = "yx2026"
DB = "rpa"
TABLE = "douyin_fans_snapshot"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DDL = """
CREATE TABLE IF NOT EXISTS douyin_fans_snapshot (
  account VARCHAR(128) NOT NULL COMMENT '账号昵称',
  account_id VARCHAR(64) DEFAULT NULL COMMENT '抖音号(unique_id)',
  account_uid VARCHAR(32) DEFAULT NULL COMMENT '账号uid(内部)',
  snapshot_date DATE NOT NULL COMMENT '抓取日期',
  d7_period_start DATE DEFAULT NULL COMMENT '近7天_统计开始',
  d7_period_end DATE DEFAULT NULL COMMENT '近7天_统计结束',
  d7_total_fans BIGINT DEFAULT NULL COMMENT '近7天_总粉丝量',
  d7_net_fans BIGINT DEFAULT NULL COMMENT '近7天_粉丝净增',
  d7_increase_fans BIGINT DEFAULT NULL COMMENT '近7天_吸粉量',
  d7_cancel_fans BIGINT DEFAULT NULL COMMENT '近7天_脱粉量',
  d7_return_fans BIGINT DEFAULT NULL COMMENT '近7天_回访粉丝量',
  d30_period_start DATE DEFAULT NULL COMMENT '近30天_统计开始',
  d30_period_end DATE DEFAULT NULL COMMENT '近30天_统计结束',
  d30_total_fans BIGINT DEFAULT NULL COMMENT '近30天_总粉丝量',
  d30_net_fans BIGINT DEFAULT NULL COMMENT '近30天_粉丝净增',
  d30_increase_fans BIGINT DEFAULT NULL COMMENT '近30天_吸粉量',
  d30_cancel_fans BIGINT DEFAULT NULL COMMENT '近30天_脱粉量',
  d30_return_fans BIGINT DEFAULT NULL COMMENT '近30天_回访粉丝量',
  update_time DATETIME DEFAULT NULL COMMENT '本次刷新时间',
  PRIMARY KEY (account, snapshot_date),
  KEY idx_update_time (update_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='抖音创作者中心-粉丝数据快照(每天一行:近7天+近30天)'
"""

# 已存在的旧表需要补的新列（幂等）
_MIGRATIONS = [
    ("account_id", "ADD COLUMN account_id VARCHAR(64) DEFAULT NULL COMMENT '抖音号(unique_id)' AFTER account"),
    ("account_uid", "ADD COLUMN account_uid VARCHAR(32) DEFAULT NULL COMMENT '账号uid(内部)' AFTER account_id"),
]


class IngestError(RuntimeError):
    pass


def _post(path: str, body: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8", "ignore")).get("detail", ""))
        except Exception:
            pass
        raise IngestError(f"HTTP {exc.code}: {detail[:400]}") from exc


def query(sql: str) -> dict:
    return _post("/api/query", {"db": DB, "sql": sql, "con": "mysql"})


def execute(sql: str) -> dict:
    return _post("/api/query", {"db": DB, "sql": sql, "con": "mysql"})


def ensure_table() -> None:
    execute(DDL)
    migrate()


def migrate() -> None:
    """给已存在的旧表补列（幂等）。"""
    r = query("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
              f"WHERE TABLE_SCHEMA='{DB}' AND TABLE_NAME='{TABLE}'")
    cols = {list(x.values())[0] for x in r["rows"]}
    todo = [clause for col, clause in _MIGRATIONS if col not in cols]
    if todo:
        execute(f"ALTER TABLE `{TABLE}` " + ", ".join(todo))


def _quote(value: str) -> str:
    return str(value).replace("'", "''")


def ingest_snapshot(row: dict) -> int:
    """写入一行快照。row 需含 account / snapshot_date（REPLACE 按主键覆盖，同日重跑幂等）。"""
    body = {
        "db": DB,
        "table": TABLE,
        "data": [row],
        "con": "mysql",
        "format": "json_records",
        "truc": 0,
        "samecol": 1,
        "replace": 1,
        "ignore": 0,
        "chunk_size": 100,
        "delete_where": (f"account = '{_quote(row['account'])}' "
                         f"AND snapshot_date = '{_quote(row['snapshot_date'])}'"),
    }
    result = _post("/api/ingest", body)
    if result.get("status") != "ok":
        raise IngestError(f"ingest 失败: {result}")
    return int(result.get("rows", 0) or 0)


def table_count() -> int:
    r = query(f"SELECT COUNT(*) AS cnt FROM `{TABLE}`")
    return int(r["rows"][0]["cnt"])


if __name__ == "__main__":
    ensure_table()
    print(f"表就绪: rpa.{TABLE} | 当前 {table_count()} 行")
