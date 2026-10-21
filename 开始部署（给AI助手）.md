# 开始部署（给 AI 助手）

> 本文件是**任务说明书**：你（AI 助手）负责把这套系统部署到当前 Windows 电脑。
> 更详细的背景资料在 `DEPLOY.md`（部署手册）与 `README.md`（系统说明），遇到问题先查它们。

---

## 你的任务

把本包中的「抖音账号管理 + 数据抓取系统」部署到本机，并完成验收（见文末清单）。
预计 10 分钟；除「需要人类配合」的 2 处外，其余你都可以自动完成。

## 系统是什么（30 秒）

店员表单登记抖音账号 → 钉钉群内 @机器人 扫码授权 → 每天自动抓粉丝数据入库 → cookie 失效自动群提醒。
全自动闭环，唯一人工环节是扫码。两个主要组件：**登录机器人**（常驻）+ **抓取脚本**（每日定时）。

## 环境检查（先做）

```bat
python --version          :: 需要 3.12+（没有则应先安装 Python 并勾选 Add to PATH）
where python              :: 若找不到，尝试 py -3 --version 或检查常见安装目录
```

- 网络需能访问：`creator.douyin.com`、`api.dingtalk.com`、`124.222.242.218:8777`
- 建议安装 Google Chrome（机器人扫码用；无 Chrome 时执行 `python -m playwright install chromium` 兜底）
- **不要设置** http_proxy/https_proxy 等代理环境变量（会导致抓取随机失败）

## 部署步骤（按序执行）

### 第 1 步 · 解压并进入目录
解压到任意目录（建议短路径，如 `D:\douyin_bot`），后续命令都在项目根目录执行。

### 第 2 步 · 安装依赖
```bat
pip install -r requirements.txt
```
（6 个包：dingtalk-stream / requests / python-dotenv / psutil / playwright / cos-python-sdk-v5）

### 第 3 步 · 验证环境与凭据（零写入试跑）
```bat
python run_all_accounts.py --dry-run
```
预期输出：每个登记账号的「近7天/近30天」粉丝数据，末尾 `成功 n/n`。
- 报「钉钉凭据缺失」→ .env 缺失，检查 `login_bot\.env` 是否存在（本包应已内置）
- 报「Tunnel / 代理错误」→ 清除代理环境变量后重试
- 报网络超时 → 检查上述三项网络连通性

### 第 4 步 · 启动登录机器人（常驻）
- 有图形界面：双击 `login_bot\启动机器人.bat`
- 命令行/后台方式：
```bat
cd login_bot
start "dy-bot" /min python -u dy_bot_watchdog.py
```
验证：`login_bot\output\watchdog.log` 出现「启动完成」；`bot_run.log` 出现「[bot] 抖音机器人 已启动（Stream 模式…）」。
⚠️ 机器人只能同时有**一台电脑**在跑（见文末规则 1）。

### 第 5 步 · 注册每日自动抓取（11:00）
运行 `install_scheduler.bat`（自动探测 Python 并注册 Windows 任务计划）。
验证：
```bat
schtasks /Query /TN "DouyinFansSnapshot_Daily"
```

### 第 6 步 · 完整验收（真实抓取）
```bat
python run_all_accounts.py
```
预期：所有账号抓取成功 + 入库 + 登记表「数据更新时间」更新为当前时间。

## 需要人类配合的 2 处

| 事项 | 做法 |
| --- | --- |
| ① 群内 @机器人 冒烟测试 | 请人在钉钉群 @抖音机器人 发一句「你好」，机器人有回复即在线 |
| ② （仅当账号需要重新授权）扫码 | 机器人发二维码到群 → 对应手机扫码 → 短信验证码回传群里（系统自动完成其余步骤） |

> 若账号 cookie 均有效（当前两个账号有效至 12 月），②暂时不需要做。

## ⚠️ 关键规则（务必遵守）

1. **机器人凭据铁律**：本系统使用的钉钉机器人应用凭据是「应用级」的——**两台电脑同时跑会互相抢消息**。
   旧电脑（192.168.4.3）的机器人已于 2026-10-15 停用；本机接管后，**旧机不得再启动机器人**。
2. **不要修改 `login_bot\.env`**（所有凭据已配好）。
3. **数据安全**：不要删除/重命名本目录任何文件；不要外传 .env（含全部密钥）。
4. 抓取/入库均**直连**（代码已强制），无需也不应配代理。
5. 若发现旧机仍在每日自动抓取（双跑），在旧机上停用其任务：
   `schtasks /Delete /TN "DouyinFansSnapshot_Daily" /F`

## 验收清单（完成标志）

- [ ] `python run_all_accounts.py --dry-run` 成功（无配置错误）
- [ ] `python run_all_accounts.py` 成功（全部账号入库、回写更新时间）
- [ ] 机器人常驻运行（watchdog.log 正常，群内 @有响应）
- [ ] 每日任务已注册（schtasks /Query 可见）
- [ ] 旧机机器人保持停止状态

## 排障速查

| 现象 | 处理 |
| --- | --- |
| 凭据缺失类报错 | 确认 `login_bot\.env` 存在；对照 `.env.example` 查缺哪项 |
| Tunnel / 代理错误 | `set http_proxy=` 等清空后重试；或换直连网络 |
| pip 装不上 | `python -m pip install -U pip` 后重试；公司网络问题换源 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 机器人不回复 | 查 `login_bot\output\watchdog.log` 与 `bot_run.log`；确认群内有它；确认旧机没有同跑 |
| 扫码卡片空白 | 抖音软风控，等 10~15 分钟再试 |
| 其他 | 读 `DEPLOY.md`（含完整 FAQ 与架构说明） |
