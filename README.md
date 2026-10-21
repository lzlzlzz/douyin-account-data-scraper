# 抖音账号管理 + 数据抓取系统

**一句话**：店员扫表单登记 → 群内 @机器人 扫码授权 → 每天自动抓粉丝数据入库 → cookie 失效自动群提醒。

> 部署到新电脑 → 见 **[DEPLOY.md](DEPLOY.md)**（完整部署手册）
> AI 助手部署 → 直接按 **[开始部署（给AI助手）.md](开始部署（给AI助手）.md)** 执行

## 闭环逻辑

```
【账号管理】
  店员扫「抖音店铺账号登记」表单（手机号+负责人）→ 自动进钉钉表
        ↓
  群内 @抖音机器人 → 机器人匹配登记行 → 弹二维码 → 扫码 → 短信验证
        ↓
  cookie + 账号名称 + 抖音号 + UID 写回钉钉表（本地同步归档）
        ↓
  机器人每 6 小时巡检所有账号 cookie
      ├─ 有效 → 状态=正常
      └─ 失效 → 状态=过期 + 群内自动提醒「请重新验证」→ 回到授权环节

【数据抓取】
  每日定时（默认 11:00）运行 run_all_accounts.py
        ↓
  读取钉钉表全部「启用」账号的 cookie → 逐个抓「近7天+近30天粉丝数据」
        ↓
  入库 rpa.douyin_fans_snapshot（每天一行/账号）+ 本地 CSV + 回写表「数据更新时间」
```

**唯一人工环节**：扫码授权（cookie 有效期 60 天，到期前巡检提前提醒）。

## 快速命令

```bat
cd 项目目录

rem 抓取（全部账号，一键）
python run_all_accounts.py            rem 或双击 run_all_accounts.bat

rem 安装/更新每日定时任务
双击 install_scheduler.bat

rem 启动登录机器人（常驻，看门狗自动守护）
双击 login_bot\启动机器人.bat

rem 单账号抓取
python douyin_fans_snapshot.py --cookie-file output\cookies\<账号>.txt
```

## 目录说明

| 文件 | 作用 |
| --- | --- |
| `run_all_accounts.py` / `.bat` | **抓取总入口**：读表全部启用账号 → 逐个抓取 → 入库 + 回写 |
| `douyin_fans_snapshot.py` | 单账号抓取核心（近7天+近30天 → 一行） |
| `douyin_ingest.py` | 入库模块（rpa.douyin_fans_snapshot） |
| `install_scheduler.bat` | 注册 Windows 每日定时抓取任务 |
| `login_bot/` | 登录机器人全套（扫码授权 / 巡检 / 群提醒）；入口 `login_bot/启动机器人.bat` |
| `douyin_qr_login.py` | 单机扫码登录器（备用，无机器人时手动换 cookie） |
| `build_package.py` | 打包部署包（dist/*.zip） |
| `DEPLOY.md` | 部署手册（新电脑从零部署） |

## 关键事实

- **cookie 生命周期**：`sid_guard` 硬期限 60 天、**不自动续期**；巡检每 6 小时探测，失效即群提醒
- **数据周期**：近7天/近30天粉丝数据（总粉丝量/净增/吸粉/脱粉/回访），每日 10 点后更新前一日
- **幂等**：抓取可任意重跑（库内 REPLACE、CSV 同日覆盖，不产生重复行）
- **入库服务**：`http://124.222.242.218:8777`（直连不走代理）
- **登记表**：钉钉 AI 表 base=`1R7q3QmWeYjrqeANi63Xdm9pWxkXOEP2`，sheet=`hERWDMS`
- **日志**：`login_bot/output/`（bot_run / watchdog / monitor）
