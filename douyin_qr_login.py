#!/usr/bin/env python3
"""抖音创作者中心 · 扫码登录 → 自动保存 Cookie + 账号身份。

为数据抓取刷新登录态（cookie 失效时的更新操作，后续扩展到 30+ 账号）。

流程：
  1. 打开 creator.douyin.com（全新会话，无历史 cookie）
  2. 定位并截取「扫码登录」二维码 → output/qr_login/douyin_qr.png
  3. 等待抖音 App 扫码确认（轮询登录态；二维码过期自动重截）
  4. 若弹出「安全验证」（选择验证码登录）→ 自动点选 → 等待验证码回传
     （--sms-code 参数 或 写入 output/qr_login/verify_code.txt）→ 填入提交
  5. 成功后提取 cookie 保存 → douyin_cookies.txt（覆盖）+ history 备份
     + 识别账号身份（昵称/抖音号/uid）存 .account.json

用法：
  python douyin_qr_login.py                    # 弹出真实 Chrome 窗口，等 5 分钟
  python douyin_qr_login.py --timeout 180      # 自定义等待时长（秒）
  python douyin_qr_login.py --headless         # 无头模式（不弹窗，截二维码文件）
  python douyin_qr_login.py --remark MADGOOD   # 标记本次登录的账号名（存档用）
  python douyin_qr_login.py --sms-code 123456  # 预置短信验证码（或用 verify_code.txt 回传）
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "output" / "qr_login"
HISTORY_DIR = OUT_DIR / "history"
COOKIE_OUT = HERE / "douyin_cookies.txt"
QR_PATH = OUT_DIR / "douyin_qr.png"
CODE_FILE = OUT_DIR / "verify_code.txt"
LOGIN_URL = "https://creator.douyin.com/creator-micro/data-center/operation"
ACCOUNT_API_URL = "https://creator.douyin.com/web/api/media/user/info/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# 安全验证页面特征词（扫码后可能出现的「选择验证码登录 → 回传验证码」环节）
VERIFY_PATTERNS = ("安全验证", "身份验证", "请完成验证", "为了你的账号安全", "验证手机号", "安全校验",
                   "请输入验证码", "请输入短信验证码")

# 在登录卡片里找方形元素（二维码 178x178 左右），打标记
MARK_QR_JS = """
() => {
    document.querySelectorAll('[data-qr-target]').forEach(e => e.removeAttribute('data-qr-target'));
    let best = null;
    document.querySelectorAll('img, canvas').forEach(el => {
        const r = el.getBoundingClientRect();
        const square = Math.abs(r.width - r.height) < 24;
        const sizeOk = r.width > 120 && r.width < 280;
        const inCard = !!el.closest('[class*="login-card"], [class*="login-"], [data-testid*="login"]');
        if (square && sizeOk && inCard) {
            if (!best || r.width > best.w) { best = {el, w: r.width, x: r.x, y: r.y, h: r.height}; }
        }
    });
    if (best) {
        best.el.setAttribute('data-qr-target', '1');
        return {x: best.x, y: best.y, w: best.w, h: best.h};
    }
    return null;
}
"""


def find_and_capture_qr(page) -> dict | None:
    """定位二维码并截图 -> QR_PATH。返回几何信息或 None。"""
    try:
        info = page.evaluate(MARK_QR_JS)
    except Exception:
        return None
    if not info:
        return None
    try:
        el = page.locator('[data-qr-target="1"]').first
        el.screenshot(path=str(QR_PATH))
        return info
    except Exception:
        # 元素截图失败则按坐标裁剪整页
        try:
            page.screenshot(path=str(QR_PATH), clip={"x": info["x"], "y": info["y"],
                                                     "width": info["w"], "height": info["h"]})
            return info
        except Exception:
            return None


def get_session_cookie(context) -> str:
    for c in context.cookies():
        if c.get("name") == "sessionid" and c.get("value"):
            return str(c["value"])
    return ""


def extract_cookie_string(context) -> str:
    parts = []
    for c in context.cookies():
        dom = c.get("domain", "") or ""
        if dom.endswith("douyin.com"):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def fetch_account_info(cookie: str) -> dict:
    """识别登录账号：{nickname, unique_id, uid, sec_uid}。失败返回 {}。"""
    try:
        req = urllib.request.Request(
            ACCOUNT_API_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://creator.douyin.com/creator-micro/home",
                "User-Agent": UA,
                "Cookie": cookie,
            },
        )
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        u = d.get("user") or {}
        return {"nickname": u.get("nickname"), "unique_id": u.get("unique_id"),
                "uid": str(u.get("uid") or "") or None, "sec_uid": u.get("sec_uid")}
    except Exception:
        return {}


def wait_for_code_file(args, timeout: int = 240) -> str | None:
    """等待验证码回传：优先 --sms-code；否则轮询 verify_code.txt（读后删除）。"""
    if getattr(args, "sms_code", None):
        code = str(args.sms_code).strip()
        args.sms_code = None  # 只用一次
        print(f"   使用命令行验证码：{code[:2]}******")
        return code
    print(f"   ⏳ 等待验证码回传：请把短信验证码写入 {CODE_FILE}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if CODE_FILE.exists():
            try:
                code = CODE_FILE.read_text(encoding="utf-8").strip()
                CODE_FILE.unlink(missing_ok=True)
                if code:
                    print(f"   已收到验证码（{code[:2]}******），处理中...")
                    return code
            except Exception:
                pass
        time.sleep(2)
    return None


def handle_verification(page, args, hit: str) -> bool:
    """处理「安全验证」：自动点选验证码登录 → 等待验证码 → 填入提交。"""
    print(f"\n[{datetime.now():%H:%M:%S}] ⚠️ 检测到安全验证（{hit}）")
    # 1) 自动选择「验证码登录 / 短信验证」方式（若有此选项）
    for txt in ("验证码登录", "短信验证码登录", "短信验证", "验证码验证", "获取验证码", "发送验证码", "发送短信"):
        try:
            loc = page.get_by_text(txt).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=2500)
                print(f"   已点击「{txt}」")
                page.wait_for_timeout(2000)
                break
        except Exception:
            continue
    # 2) 等待验证码回传（--sms-code 或 verify_code.txt）
    code = wait_for_code_file(args)
    if not code:
        print("   ❌ 验证码等待超时（可重新触发，或手动在浏览器窗口完成）")
        return False
    # 3) 填入验证码
    filled = False
    for sel in ("input[placeholder*='验证码']", "input[placeholder*='短信']",
                "input[type='tel']", "input[inputmode='numeric']", "input[type='text']"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=2000)
                loc.fill(code)
                filled = True
                print("   验证码已填入输入框")
                break
        except Exception:
            continue
    if not filled:
        print("   ❌ 未找到验证码输入框")
        return False
    # 4) 提交
    for txt in ("登录", "确认", "提交", "验证", "确定"):
        try:
            btn = page.get_by_role("button", name=txt).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=2500)
                print(f"   已点击「{txt}」提交")
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        print("   已回车提交")
        return True
    except Exception:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抖音创作者中心扫码登录，保存 Cookie")
    parser.add_argument("--timeout", type=int, default=300, help="等待扫码秒数（默认 300）")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（默认有头：弹真实 Chrome 窗口，可直接扫屏幕）")
    parser.add_argument("--remark", default="", help="账号备注（存档文件名用）")
    parser.add_argument("--sms-code", help="预置短信验证码（安全验证环节用；也可运行时写入 verify_code.txt 回传）")
    parser.add_argument("--output", default=str(COOKIE_OUT), help="cookie 输出文件")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=args.headless)
            print("浏览器: 系统 Chrome（真实内核）")
        except Exception as _e:
            print(f"系统 Chrome 启动失败({str(_e)[:80]})，回退 Playwright Chromium")
            browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=UA)
        page = ctx.new_page()
        print(f"[{datetime.now():%H:%M:%S}] 打开抖音创作者中心登录页...")
        page.goto(LOGIN_URL, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 若已处于登录态（极少见：全新会话不会），直接提取
        if get_session_cookie(ctx) and "login" not in page.url.lower():
            print("检测到已是登录态，直接提取 Cookie")

        # 轮询等待二维码渲染（最长 40 秒）
        qr = None
        for _ in range(20):
            page.wait_for_timeout(2000)
            qr = find_and_capture_qr(page)
            if qr:
                break
        if not qr:
            print("未找到二维码元素（页面截图已留档到 output/qr_login/）。\n"
                  "提示：若登录卡片空白，通常是抖音对短时间内频繁访问的临时风控，等 5~10 分钟冷却后重试即可。",
                  file=sys.stderr)
            page.screenshot(path=str(OUT_DIR / "page_fallback.png"))
            browser.close()
            return 2
        print(f"二维码已截取: {QR_PATH}  (约 1 分钟有效，过期自动刷新)")
        print(f"👉 请用抖音 App 扫码（打开抖音 → 左上角扫一扫）→ 手机确认登录")
        print(f"等待扫码中（最长 {args.timeout} 秒）...")

        last_refresh = time.time()
        last_verify_check = 0.0
        logged_in = False
        # 先记录初始 sessionid（应为空）
        while time.time() - started < args.timeout:
            page.wait_for_timeout(2500)
            sid = get_session_cookie(ctx)
            url = page.url
            if sid:
                logged_in = True
                break
            # 安全验证检测：扫码确认后可能弹出「选择验证码登录 → 回传验证码」
            now_ts = time.time()
            if now_ts - last_verify_check > 8:
                last_verify_check = now_ts
                try:
                    body_txt = page.locator("body").inner_text(timeout=2500)
                except Exception:
                    body_txt = ""
                hit = next((p for p in VERIFY_PATTERNS if p in body_txt), None)
                if hit:
                    handle_verification(page, args, hit)
            # 每 25 秒重截二维码（页面可能自动刷新了码）
            if time.time() - last_refresh > 25:
                info = find_and_capture_qr(page)
                if info:
                    print(f"[{datetime.now():%H:%M:%S}] 二维码已刷新（重新截取）")
                last_refresh = time.time()
                # 页面若提示过期也未刷新，重载一次
                try:
                    body = page.locator("body").inner_text(timeout=3000)
                    if ("失效" in body or "过期" in body) and "点击刷新" in body:
                        page.reload(timeout=30000)
                        page.wait_for_timeout(5000)
                        find_and_capture_qr(page)
                        print(f"[{datetime.now():%H:%M:%S}] 二维码已失效，页面已重载并重新截取")
                except Exception:
                    pass

        if not logged_in:
            print("等待超时：未检测到扫码登录。二维码文件仍在，可重跑脚本。", file=sys.stderr)
            browser.close()
            return 3

        # 登录成功：等待跳转稳定 + 提取 cookie
        print(f"[{datetime.now():%H:%M:%S}] 扫码登录成功！等待会话稳定...")
        page.wait_for_timeout(4000)
        cookie_str = extract_cookie_string(ctx)
        n_cookie = len(cookie_str.split(";")) if cookie_str else 0

        # 识别账号身份（昵称/抖音号/uid）——身份字段一并存档
        acct = fetch_account_info(cookie_str)
        if acct.get("nickname") or acct.get("unique_id"):
            print(f"  登录账号: {acct.get('nickname')}（抖音号 {acct.get('unique_id')}，uid {acct.get('uid')}）")
        name = args.remark or acct.get("nickname") or acct.get("unique_id") or "douyin"

        # 保存（覆盖 + 历史备份）
        out_path = Path(args.output)
        out_path.write_text(cookie_str, encoding="utf-8")
        backup_name = f"{stamp}_{name}.txt"
        (HISTORY_DIR / backup_name).write_text(cookie_str, encoding="utf-8")
        # cookie json（备用格式）
        cookies_json = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                         "path": c.get("path", "/"), "secure": c.get("secure", False),
                         "httpOnly": c.get("httpOnly", False)}
                        for c in ctx.cookies() if (c.get("domain") or "").endswith("douyin.com")]
        (HISTORY_DIR / backup_name.replace(".txt", ".json")).write_text(
            json.dumps(cookies_json, ensure_ascii=False, indent=1), encoding="utf-8")
        # 身份档案（账号 - cookie 对应关系，多账号管理用）
        if acct:
            acct_doc = dict(acct)
            acct_doc.update({"saved_at": stamp, "remark": args.remark})
            (HISTORY_DIR / backup_name.replace(".txt", ".account.json")).write_text(
                json.dumps(acct_doc, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  身份存档: {HISTORY_DIR / backup_name.replace('.txt', '.account.json')}")

        print(f"[{datetime.now():%H:%M:%S}] Cookie 已保存:")
        print(f"  覆盖写入: {out_path}（{n_cookie} 条）")
        print(f"  历史备份: {HISTORY_DIR / backup_name}")
        # 有效期提示（sid_guard）
        import re
        m = re.search(r"sid_guard=([^;]+)", cookie_str)
        if m:
            seg = re.split(r"%7C|\|", m.group(1))
            if len(seg) >= 3:
                try:
                    exp = datetime.fromtimestamp(int(seg[1]) + int(seg[2]))
                    print(f"  Cookie 有效期至: {exp:%Y-%m-%d %H:%M}（剩 {(exp.date() - datetime.now().date()).days} 天）")
                except Exception:
                    pass
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
