#!/usr/bin/env python3
"""打包部署包：dist/抖音账号管理数据抓取-YYYYMMDD.zip。

用于把整个系统交付到新 Windows 电脑（交给其他 AI/人员部署）。
- 包含：全部脚本、启动/安装 bat、部署文档（DEPLOY.md）、README、依赖清单、
       **login_bot/.env（真实凭据，2026-10-15 起随包提供，解压即用）**
- 排除：cookie 归档文件、运行产物（日志/缓存/浏览器 profile/数据）
- 打包后自检：① .env 在包内且关键凭据齐全 ② 无 cookie 文件混入

用法:
  python build_package.py
"""
from __future__ import annotations

import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"

# 排除目录（任何层级命中即跳过）
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "output", "data", "dist", ".venv", ".git"}
# 排除文件（精确名）。注：.env 自 2026-10-15 起随包提供（内部整包迁移场景，解压即用）
EXCLUDE_FILES = {"douyin_cookies.txt"}
# 排除后缀（运行产物）
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip"}

# 凭据自检清单（.env 中必须非空的关键键）
ENV_REQUIRED_KEYS = [
    "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_OPERATOR_ID",
    "DOUYIN_LOGIN_BOT_CLIENT_ID", "DOUYIN_LOGIN_BOT_CLIENT_SECRET",
    "COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET",
    "DY_ALERT_WEBHOOK_URL", "DY_ALERT_WEBHOOK_SECRET",
]


def collect_files() -> list[Path]:
    files: list[Path] = []
    for p in sorted(HERE.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(HERE)
        if set(rel.parts) & EXCLUDE_DIRS:
            continue
        if rel.name in EXCLUDE_FILES:
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIX:
            continue
        files.append(rel)
    return files


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    zip_name = f"抖音账号管理数据抓取-{stamp}.zip"
    DIST.mkdir(exist_ok=True)
    zip_path = DIST / zip_name
    root = zip_name[:-4]  # zip 内顶层目录名

    files = collect_files()
    if not files:
        print("没有可打包的文件")
        return 1

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(HERE / rel, arcname=f"{root}/{rel.as_posix()}")

    # ── 清单 ──
    print(f"=== 打包完成: {zip_path} ===")
    print(f"文件数: {len(files)} | 大小: {zip_path.stat().st_size / 1024:.1f} KB")
    print()
    for rel in files:
        print(f"  {rel.as_posix()}")

    # ── 自检 1：.env 必须在包内且关键凭据齐全 ──
    with zipfile.ZipFile(zip_path) as zf:
        env_name = next((n for n in zf.namelist() if n.endswith("login_bot/.env")), "")
        if not env_name:
            print("❌ 自检失败：login_bot/.env 不在包内（部署将无法连接钉钉/机器人）")
            return 1
        env_text = zf.read(env_name).decode("utf-8", errors="replace")
        missing = []
        for k in ENV_REQUIRED_KEYS:
            m = re.search(rf"^{k}\s*=\s*(\S+)", env_text, re.M)
            if not m or not m.group(1).strip():
                missing.append(k)
        if missing:
            print(f"❌ 自检失败：.env 中关键配置缺失或为空：{missing}")
            return 1
        print(f"✅ 凭据自检通过：login_bot/.env 已随包提供（{len(ENV_REQUIRED_KEYS)} 项关键配置齐全）")
        print("⚠️  提醒：本包内含真实凭据，请勿转发到公开渠道")

    # ── 自检 2：无 cookie 文件混入 ──
    bad = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if "cookie" in base.lower():
                bad.append(name)
    if bad:
        print("❌ 自检失败：发现 cookie 类文件：")
        for b in bad:
            print("   ", b)
        return 1
    print("✅ 文件自检通过：无 cookie 文件混入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
