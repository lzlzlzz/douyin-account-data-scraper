"""诊断：真实点击「获取验证码」，抓取网络响应 + 页面反馈，找出没发短信的原因。"""
import json, os, time
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
PROFILE = "output/dy-browser-profile/2736"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE, headless=True, channel="chrome",
        viewport={"width": 1280, "height": 900}, user_agent=UA,
        locale="zh-CN", timezone_id="Asia/Shanghai",
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    responses = []
    def on_resp(r):
        u = r.url
        if any(k in u for k in ("passport", "send", "sms", "code", "captcha", "verify")):
            item = {"url": u[:220], "status": r.status}
            try:
                h = r.headers or {}
                if "json" in (h.get("content-type") or ""):
                    item["body"] = json.dumps(r.json() or {}, ensure_ascii=False)[:500]
            except Exception:
                pass
            responses.append(item)
    page.on("response", on_resp)
    console = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text[:150]}"))

    print("打开登录页...")
    page.goto("https://creator.douyin.com/creator-micro/data-center/operation",
              timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(7000)
    body = page.locator("body").inner_text(timeout=5000)
    print("  登录卡片:", "验证码登录" in body)

    print("切换验证码登录 tab...")
    page.get_by_text("验证码登录", exact=True).first.click()
    page.wait_for_timeout(1500)

    print("填手机号（从 DY_LOGIN_PHONE 读取）...")
    inp = page.locator("#normal-input")
    if not inp.count():
        inp = page.locator("input[placeholder*='手机号']")
    inp.click()
    inp.fill("")
    inp.type(os.getenv("DY_LOGIN_PHONE", ""), delay=40)
    page.wait_for_timeout(600)

    before = page.locator("body").inner_text(timeout=5000)
    # 检查按钮状态
    btn_state = page.evaluate("""() => {
        const s = document.querySelector('#douyin_login_comp_button_input_id span');
        if (!s) return null;
        const r = s.getBoundingClientRect();
        return {text: s.textContent.trim(), cls: String(s.className), w: r.width, h: r.height,
                cs: getComputedStyle(s).color, pe: getComputedStyle(s).pointerEvents,
                disabled: s.getAttribute('aria-disabled') || s.getAttribute('disabled') || 'no'};
    }""")
    print("  按钮状态:", json.dumps(btn_state, ensure_ascii=False))

    print("点击「获取验证码」...")
    responses.clear()
    span = page.locator("#douyin_login_comp_button_input_id span").first
    try:
        span.click(timeout=5000)
        print("  点击已发出")
    except Exception as e:
        print("  点击异常:", type(e).__name__, str(e)[:150])
    page.wait_for_timeout(6000)

    after = page.locator("body").inner_text(timeout=5000)
    # 文本 diff
    import difflib
    diff = [l for l in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="") if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    print("  页面文本变化(前15行):")
    for l in diff[:15]:
        print("    ", l[:110])

    # 按钮后续状态
    btn_after = page.evaluate("""() => {
        const s = document.querySelector('#douyin_login_comp_button_input_id span');
        return s ? s.textContent.trim() : null;
    }""")
    print("  按钮点击后文字:", btn_after)

    # iframe 检查（隐形风控 iframe）
    iframes = page.evaluate("() => [...document.querySelectorAll('iframe')].map(f => ({src: (f.src||'').slice(0,150), w: f.getBoundingClientRect().width, h: f.getBoundingClientRect().height}))")
    print("  iframes:", json.dumps(iframes, ensure_ascii=False)[:400])

    page.screenshot(path="output/diag_after_click.png")
    print("  截图: output/diag_after_click.png")

    print()
    print("=== 捕获到的相关网络请求 ===")
    for it in responses[-15:]:
        print(" ", json.dumps(it, ensure_ascii=False)[:320])
    if not responses:
        print("  (无相关请求 —— 点击可能未触发网络)")
    if console:
        print()
        print("=== console 消息 ===")
        for c in console[-10:]:
            print("  ", c)
    ctx.close()
print("DONE")
