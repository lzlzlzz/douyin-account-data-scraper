# 抖音账号管理 + 数据抓取系统 · 部署手册（DEPLOY）

> 本文档面向**部署人员/其他 AI**：在新 Windows 电脑上从零把这套系统跑起来的完整说明。
> 自包含：不需要阅读任何其他文档或历史对话。

---

## 0. 这套系统是干什么的（30 秒版）

```
店员扫表单登记抖音账号 → 群内 @机器人 扫码授权（生成 cookie）
→ 每天定时自动抓「近7天/近30天粉丝数据」入库 → cookie 失效时群内自动提醒重新授权
```

全流程闭环，唯一需要人工的环节 = 扫码授权（其他全自动）。

## 1. 系统架构

```
┌──────────────────────── 钉钉 AI 表「抖音店铺账号登记表」────────────────────────┐
│  base=1R7q3QmWeYjrqeANi63Xdm9pWxkXOEP2  sheet=hERWDMS（10 字段）              │
│  表单视图（店员提交）→ 自动进表                                                  │
└──────┬────────────────────────────────┬──────────────────────────────┬────────┘
       │ 读登记行/写 cookie             │ 读 cookie（抓取）              │ 写「数据更新时间」
       ▼                                ▼                              ▼
┌─────────────────┐          ┌──────────────────────┐
│ 登录机器人       │          │ 抓取脚本              │
│ login_bot/      │          │ run_all_accounts.py  │
│ （常驻进程）     │          │ （每日定时任务）       │
│ · 群 @→扫码授权  │          └──────────┬───────────┘
│ · 每6h巡检cookie│                      │
│ · 失效→群提醒    │                      ▼
└─────────────────┘          ┌──────────────────────┐
                             │ rpa 入库服务          │
                             │ 124.222.242.218:8777 │
                             │ → rpa.douyin_fans_   │
                             │   snapshot 表         │
                             └──────────────────────┘
```

**数据落点**：
- 数据库：`rpa.douyin_fans_snapshot`（MySQL，经入库服务写入；主键 account+snapshot_date，每天一行/账号，重复跑幂等覆盖）
- 本地：`data/fans_snapshot.csv`
- 证据链：`login_bot/output/*.log`（各组件日志）

## 2. 组件与文件清单

| 文件/目录 | 作用 | 是否常驻 |
| --- | --- | --- |
| `run_all_accounts.py` / `.bat` | **多账号批量抓取**（日常入口）：读表全部启用账号 → 逐个抓取 → 入库 + 回写表 | 每日定时 |
| `douyin_fans_snapshot.py` | 单账号抓取核心（近7天+近30天 → 一行数据） | 被上面调用 |
| `douyin_ingest.py` | 入库模块（rpa.douyin_fans_snapshot） | 被调用 |
| `install_scheduler.bat` | 注册/更新 Windows 每日定时任务 | 一次性 |
| `login_bot/` | **抖音登录机器人全套**（Stream 机器人 + 扫码授权 + cookie 巡检告警） | 常驻（看门狗守护） |
| `login_bot/启动机器人.bat` | 机器人启动入口（双击；看门狗+bot 自动互保） | 手动启动一次 |
| `login_bot/.env` | **全部凭据配置**（✅ 已随部署包提供，解压即用；`.env.example` 为空白模板备查） | - |
| `douyin_qr_login.py` + `submit_code.py` + `回传验证码.bat` | 单机版扫码登录器（备用工具，无机器人时手动换 cookie） | 备用 |
| `requirements.txt` | Python 依赖清单 | - |

## 3. 环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 |
| Python | **3.12+**（建议 3.13，安装时勾选 Add to PATH） |
| 浏览器 | **Google Chrome**（机器人扫码授权用；缺失则用 `python -m playwright install chromium` 兜底） |
| 网络 | 能访问 `creator.douyin.com`、`oapi.dingtalk.com`、`api.dingtalk.com`、`124.222.242.218:8777` |
| 其他 | 机器长期开机 + 保持用户登录（任务计划与机器人常驻） |

## 4. 部署步骤

### 4.1 拷贝文件
把整个项目文件夹拷到新机器任意目录（建议英文/中文路径均可，避开特殊字符）。

### 4.2 安装依赖
```bat
cd /d <项目目录>
pip install -r requirements.txt
```

### 4.3 配置凭据（本包已内置，无需操作）
本部署包**已内含** `login_bot\.env`（钉钉/机器人/COS/webhook 全部凭据齐备），解压后即可直接使用、无需任何配置。

- 如后续凭据变更（如更换机器人应用）：对照 `login_bot\.env.example` 内注释逐项更新；
- 如需从其他机器同步最新凭据：直接覆盖 `login_bot\.env`。

> ⚠️ `.env` 含全部密钥：请勿转发到公开渠道，仅通过内部安全方式传递整包/该文件。

### 4.4 启动机器人（常驻）
双击 `login_bot\启动机器人.bat`（或直接运行 `python login_bot\dy_bot_watchdog.py`）。
- 看门狗会拉起机器人并每 30 秒健康检查、异常自动重启
- 启动日志：`login_bot\output\watchdog.log`、`bot_run.log`

### 4.5 注册每日自动抓取
双击 `install_scheduler.bat`（注册 Windows 任务计划，默认每天 **11:00** 自动抓取）。
- 改时间：编辑 bat 里 `TASK_TIME=11:00` 后重新运行
- 立即验证：`schtasks /Run /TN "DouyinFansSnapshot_Daily"`
- 卸载：`schtasks /Delete /TN "DouyinFansSnapshot_Daily" /F`

### 4.6 验证部署（验收清单）
| 检查项 | 方法 | 通过标准 |
| --- | --- | --- |
| 依赖完整 | `pip install -r requirements.txt` | 无报错 |
| 表连接 | `python login_bot\test_local_flow.py` 或手动读表 | 能读到登记表账号 |
| 机器人在线 | 群里 @机器人 发「你好」 | 机器人有回复 |
| 抓取链路 | `python run_all_accounts.py` | 每个账号输出「入库 1 行」+ 汇总成功 |
| 定时任务 | `schtasks /Query /TN "DouyinFansSnapshot_Daily"` | 任务存在、状态就绪 |
| 回写 | 抓取后看登记表「数据更新时间」 | = 抓取时间 |

## 5. 凭据与配置项说明（login_bot/.env）

| 配置项 | 用途 | 备注 |
| --- | --- | --- |
| `DINGTALK_CLIENT_ID/SECRET/OPERATOR_ID` | 读写钉钉 AI 表（登记表） | REST 调用，**可多台电脑共用** |
| `DOUYIN_LOGIN_BOT_CLIENT_ID/SECRET` | 抖音登录机器人 Stream 应用 | ⚠️ **见 6.1 凭据铁律** |
| `DY_LOGIN_BOT_NAME` | 群内显示名 | 默认「抖音机器人」 |
| `COS_SECRET_ID/KEY/REGION/BUCKET/PUBLIC_BASE_URL` | 腾讯云 COS 图床（二维码转外链） | 桶需公共读 |
| `DY_ALERT_WEBHOOK_URL/SECRET` | 群告警 webhook（cookie 失效提醒） | 自定义机器人+加签；**各群独立，严禁混用** |
| `DY_MONITOR_*` | 巡检间隔等 | 默认 6 小时，一般不动 |

## 6. ⚠️ 铁律与坑清单（必读）

### 6.1 Stream 凭据铁律（最重要）
钉钉 Stream 机器人凭据是**应用级资源**：同一个应用开多个连接时，钉钉会把群消息**随机平均投递**到任一连接 → 两个机器人「抢消息」、各自都出错。
- **换机场景**：新机接管 → 旧机必须**停止**机器人（关闭看门狗窗口/结束 python 进程）；
- **双机并行**（不同群/不同账号池）→ 必须**新建机器人应用**，各用各的凭据。
- AI 表应用（`DINGTALK_*`）是 REST 调用，**不受此限制**，可安全多端共用。

### 6.2 代理坑
- 抓取脚本与入库服务**必须直连**（代码内已强制 `ProxyHandler({})`）；
- 若运行环境存在 `http_proxy/https_proxy` 系统代理变量，会让请求走代理隧道，抖动时报 `Tunnel connection failed: 502`（随机失败）。部署机如无特殊需要**不要设置代理变量**。

### 6.3 抖音风控
- 扫码环节必须**真实窗口**（headless 会被风控拦截）；
- 短时间内频繁打开登录页 → 登录卡片空白 → **冷却 10~15 分钟**再试；
- cookie（`sid_guard`）**硬期限 60 天、不自动续期**，到期前巡检会提前提醒。

### 6.4 幂等与防重
- 抓取可任意重跑：库内按「账号+日期」REPLACE 覆盖、CSV 同日覆盖，不产生重复行；
- 但**避免两台机器同时挂定时抓取**（内容不重，但浪费额度 + 提高风控概率）。建议只有一台机器承担抓取调度。

### 6.5 单实例守护
- 机器人只能跑一份：`启动机器人.bat` 已内置防重复启动检查；
- 本项目相关进程：`dy_bot_watchdog.py`（1 个）+ `dy_bot.py`（venv 下显示 2 个进程为正常结构，实为 1 实例）。

## 7. 日常运维

| 场景 | 操作 |
| --- | --- |
| 新增账号 | 店员扫表单提交（手机号+负责人）→ 群里 @机器人 说「校验 + 手机尾号」→ 扫码/短信授权 |
| cookie 失效 | 巡检自动群提醒 → 相关人 @机器人 重新授权（状态自动「过期→正常」） |
| 手动补跑抓取 | 双击 `run_all_accounts.bat`；单账号 `python douyin_fans_snapshot.py --cookie-file output/cookies/<文件>` |
| 机器人假死 | 看门狗 30 秒内自动拉起；仍异常 → 关闭守护窗口 → 重新双击 `启动机器人.bat` |
| 日志排查 | `login_bot/output/`（bot_run / watchdog / monitor / 二维码与截图证据） |
| 数据核对 | 库：`rpa.douyin_fans_snapshot`；表内「数据更新时间」= 最近成功抓取时间 |
| 停用账号 | 登记表「启用状态」改为「停用」→ 抓取自动跳过（巡检也跳过） |

## 8. 常见问题（FAQ）

| 现象 | 原因/处理 |
| --- | --- |
| `ModuleNotFoundError: dingtalk_stream` | 依赖未装：`pip install -r requirements.txt` |
| 抓取报 `Tunnel connection failed: 502` | 环境代理变量导致：清除 `http_proxy/https_proxy` 或直连运行 |
| 机器人不回复群消息 | ①确认 bot 进程在跑（看 `watchdog.log`）②确认机器人已加入该群 ③凭据是否被别的机器抢占（见 6.1） |
| 二维码发不出到群 | COS 配置错误（密钥/桶/地域）→ 检查 `COS_*` 四项 |
| 扫码时提示「身份验证」 | 正常风控环节：机器人自动点「接收短信验证码」→ 群里发验证码即可 |
| 抓取某账号失败（cookie 相关 401/403） | cookie 失效：等巡检提醒或 @机器人 重新授权，换新 cookie 后重跑 |
