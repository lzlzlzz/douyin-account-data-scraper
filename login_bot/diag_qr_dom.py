"""诊断：抖音登录页二维码元素的真实结构（定位修复依据）。
跑一次抓全：img/canvas 清单 + 扫码登录容器 HTML + 网络请求 + 整页截图。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "output" / "dy-browser-profile" / "SiqaqL9nGf"
LOGIN_URL = "https://creator.douyin.com/creator-micro/data-center/operation"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE), headless=False, channel="chrome",
        viewport={"width": 1280, "height": 900}, user_agent=UA,
        locale="zh-CN", timezone_id="Asia/Shanghai",
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    reqs: list[str] = []
    page.on("response", lambda r: reqs.append(r.url)
            if any(k in r.url.lower() for k in ("qr", "image", "captcha", "verify"))
            else None)

    # 打开登录页（空白时刷新重试，最多 3 轮）
    ok = False
    for attempt in range(3):
        if attempt == 0:
            page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
        else:
            print(f"[{attempt}] 卡片空白，刷新重试...", flush=True)
            page.reload(timeout=45000)
            page.wait_for_timeout(3000)
        deadline = time.time() + (20 if attempt == 0 else 15)
        while time.time() < deadline:
            try:
                text = page.locator("body").inner_text(timeout=3000)[:3000]
            except Exception:
                text = ""
            if "扫码登录" in text or "验证码登录" in text:
                ok = True
                break
            page.wait_for_timeout(1500)
        if ok:
            break

    print(f"页面就绪: {ok}")
    page.wait_for_timeout(4000)

    # 等二维码渲染（最多 30 秒）
    for i in range(15):
        has = page.evaluate("""() => {
            let found = false;
            document.querySelectorAll('img, canvas').forEach(el => {
                const r = el.getBoundingClientRect();
                if (Math.abs(r.width - r.height) < 20 && r.width > 130 && r.width < 260
                    && r.x > 150 && r.x < 700 && r.y > 100 && r.y < 700) found = true;
            });
            return found;
        }""")
        if has:
            print(f"方形候选出现于 {i * 2}s")
            break
        page.wait_for_timeout(2000)

    # ============ 1) img/canvas 全清单 ============
    info = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('img, canvas').forEach((el, i) => {
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 40) return;
            out.push({
                i, tag: el.tagName,
                cls: String(el.className).slice(0, 110),
                id: el.id || '',
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
                src: String(el.currentSrc || el.src || '').slice(0, 120),
                inLogin: !!el.closest('[class*="login"]'),
                inCard: !!el.closest('[class*="card"]'),
                parent: String(el.parentElement ? el.parentElement.className : '').slice(0, 90),
            });
        });
        return out;
    }""")
    print("\n=== [1] img/canvas 清单 ===")
    for x in info:
        print(json.dumps(x, ensure_ascii=False))

    # ============ 2) "扫码登录" 容器 HTML ============
    card = page.evaluate("""() => {
        let node = null;
        document.querySelectorAll('*').forEach(el => {
            if (!node && el.childElementCount === 0 && (el.textContent||'').trim() === '扫码登录') node = el;
        });
        if (!node) return 'not found';
        let p = node;
        for (let i = 0; i < 8 && p.parentElement; i++) p = p.parentElement;
        return p.outerHTML.slice(0, 6000);
    }""")
    print("\n=== [2] 扫码登录 容器 HTML ===")
    print(card)

    # ============ 3) 读二维码元素本身的信息 ============
    qrd = page.evaluate("""() => {
        // 找二维码最有可能是哪个：查看所有在左侧区域的方形元素
        const cands = [];
        document.querySelectorAll('img, canvas').forEach(el => {
            const r = el.getBoundingClientRect();
            const square = Math.abs(r.width - r.height) < 20;
            const sizeOk = r.width > 130 && r.width < 260;
            if (square && sizeOk) {
                cands.push({
                    tag: el.tagName, cls: String(el.className).slice(0, 110),
                    x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
                    src: String(el.currentSrc || el.src || '').slice(0, 90),
                    html: el.outerHTML.slice(0, 300),
                });
            }
        });
        return cands;
    }""")
    print("\n=== [3] 方形候选（130-260px）详情 ===")
    for x in qrd:
        print(json.dumps(x, ensure_ascii=False))

    # ============ 4) 网络请求 ============
    print("\n=== [4] qr/image 相关请求（最后30条） ===")
    for u in reqs[-30:]:
        print("  ", u[:170])

    page.screenshot(path=str(HERE / "output" / "diag_page_now.png"))
    print("\n=== 整页截图: output/diag_page_now.png ===")
    ctx.close()
print("DONE")
