"""抖音登录助手 · 全局配置常量。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent

# 模块级常量（如 BOT_NAME）依赖 .env —— 在这里及早加载，避免 import 顺序导致读不到
load_dotenv(HERE / ".env")

# ── 抖音登录 ─────────────────────────────────────────────
LOGIN_URL = "https://creator.douyin.com/creator-micro/data-center/operation"
ACCOUNT_API_URL = "https://creator.douyin.com/web/api/media/user/info/"
FANS_API_URL = "https://creator.douyin.com/janus/douyin/creator/data/overview/dashboard/fans"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 浏览器 profile 目录（按账号隔离，降低风控）
PROFILE_ROOT = Path(os.getenv("DY_PROFILE_DIR", str(HERE / "output" / "dy-browser-profile")))

# ── 钉钉 AI 表（抖音店铺账号信息表）────────────────────────
AITABLE_BASE = "1R7q3QmWeYjrqeANi63Xdm9pWxkXOEP2"
AITABLE_SHEET = "hERWDMS"

# 字段（写值用「显示名」做 key，读值同）
F_PHONE = "绑定手机号"      # 主字段（primaryDoc）
F_ACCOUNT = "账号名称"
F_DY_ID = "抖音号"          # unique_id
F_UID = "账号UID"           # 内部 uid
F_COOKIE = "Cookie"
F_STATUS = "状态"
F_ENABLED = "启用状态"
F_MANAGER = "负责人姓名"     # 人员字段（旧名「负责人」兼容读取）
F_MANAGER_OLD = "负责人"
F_NOTICE_AT = "失效通知时间"
F_UPDATE_AT = "数据更新时间"
# 创建人：记录级 createdBy.unionId（非单元格字段）

# 单选项 ID（实测 2026-10-09）
STATUS_NORMAL = "B9hWbCouYl"      # 正常
STATUS_EXPIRED = "5cTUigJVfE"     # 过期
STATUS_SCRAPING = "axwXiB2ANC"    # 抓取中
STATUS_ERROR = "oANNi8kdr8"       # 错误
ENABLED_ON = "K0U0efHNPu"         # 启用
ENABLED_OFF = "tUH7NNAQDW"        # 停用

STATUS_NAME = {
    STATUS_NORMAL: "正常",
    STATUS_EXPIRED: "过期",
    STATUS_SCRAPING: "抓取中",
    STATUS_ERROR: "错误",
}

# ── 机器人文案 ───────────────────────────────────────────
BOT_NAME = os.getenv("DY_LOGIN_BOT_NAME", "抖音登录助手")
MSG_ASK_CODE = "验证码已发送到手机号尾号 {suffix}，请在群里发送：@{bot} {code_placeholder}"
MSG_LOGIN_OK = "{account} 登录验证成功"
MSG_LOGIN_FAIL = "{account} 登录验证失败：{reason}"
MSG_NO_ACCOUNT = "未找到由你登记（创建）的抖音账号，请先在登记表中提交账号信息"
MSG_MULTI_ACCOUNT = "发现多个待验证账号：{labels}\n请发送：@{bot} 校验 {example}（把「{example}」换成你要授权的那个）"
MSG_NO_TASK = "没有等待验证码的登录任务，请先 @{bot} 获取验证码"
MSG_BUSY = "当前有登录任务进行中，请稍后再试"
