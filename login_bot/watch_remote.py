"""扫码登录遥控调试器。

启动后打开抖音登录页（真实窗口），持续把页面状态写到 output/watch_status.txt，
并轮询 output/cmd.txt 执行指令（每 2 秒）：
  shot              截图 -> output/remote_shots/
  text              完整页面文本 -> output/remote_text.txt
  click:XXX         点击含 XXX 文案的元素
  cookies           cookie 导出 -> output/live_cookies.txt
  qr                重新截取二维码 -> output/live_qr.png
  eval:JS           执行 JS 并输出结果
  quit              退出
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
STATUS = OUT / "watch_status.txt"
CMD = OUT / "cmd.txt"
RESULT = OUT / "cmd_result.txt"
SHOTS = OUT / "remote_shots"
SHOTS.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

MARK_QR_JS = """
() => {
    document.querySelectorAll('[data-qr-target]').forEach(e => e.removeAttribute('data-qr-target'));
    let best = null;
    document.querySelectorAll('img, canvas').forEach(el => {
        const r = el.getBoundingClientRect();
        const square = Math.abs(r.width - r.height) < 24;
        const sizeOk = r.width > 110 && r.width < 320;
        if (square && sizeOk) {
            if (!best || r.width > best.w) { best = {el, w: r.width, x: r.x, y: r.y, h: r.height}; }
        }
    });
    if (best) { best.el.setAttribute('data-qr-target', '1');
        return {x: Math.round(best.x), y: Math.round(best.y), w: Math.round(best.w), h: Math.round(best.h)}; }
    return null;
}
"""


def write_status(text: str):
    STATUS.write_text(text, encoding="utf-8")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto("https://creator.douyin.com/creator-micro/data-center/operation",
                  timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        write_status("已打开登录页，等待扫码...\n")

        shot_n = 0
        while True:
            try:
                body = page.locator("body").inner_text(timeout=3000)
            except Exception:
                body = "(读取失败)"
            session = ""
            for c in ctx.cookies():
                if c.get("name") == "sessionid":
                    session = (c.get("value") or "")[:16]
            write_status(
                f"time={time.strftime('%H:%M:%S')}\nurl={page.url}\n"
                f"sessionid={session or '(无)'}\n--- text tail ---\n{body[-1500:]}")

            if CMD.exists():
                try:
                    cmd = CMD.read_text(encoding="utf-8").strip()
                    CMD.unlink(missing_ok=True)
                except Exception:
                    cmd = ""
                if cmd:
                    log = []
                    try:
                        if cmd == "quit":
                            log.append("quit")
                            RESULT.write_text("\n".join(log), encoding="utf-8")
                            break
                        elif cmd == "shot":
                            shot_n += 1
                            fp = SHOTS / f"shot_{shot_n:03d}.png"
                            page.screenshot(path=str(fp))
                            log.append(f"shot: {fp}")
                        elif cmd == "text":
                            (OUT / "remote_text.txt").write_text(body, encoding="utf-8")
                            log.append("full text -> remote_text.txt")
                        elif cmd == "cookies":
                            s = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies()
                                          if (c.get("domain") or "").endswith("douyin.com"))
                            (OUT / "live_cookies.txt").write_text(s, encoding="utf-8")
                            log.append(f"cookies saved: {len(s)} chars")
                        elif cmd == "qr":
                            info = page.evaluate(MARK_QR_JS)
                            if info:
                                try:
                                    page.locator('[data-qr-target="1"]').first.screenshot(
                                        path=str(OUT / "live_qr.png"))
                                    log.append(f"qr -> output/live_qr.png {info}")
                                except Exception as exc:
                                    page.screenshot(path=str(OUT / "live_qr.png"),
                                                    clip={"x": info["x"], "y": info["y"],
                                                          "width": info["w"], "height": info["h"]})
                                    log.append(f"qr(clip) -> output/live_qr.png {info}")
                            else:
                                log.append("qr not found")
                        elif cmd.startswith("click:"):
                            label = cmd.split(":", 1)[1]
                            loc = page.get_by_text(label, exact=False)
                            n = loc.count()
                            clicked = False
                            for i in range(min(n, 6)):
                                try:
                                    if loc.nth(i).is_visible():
                                        loc.nth(i).click(timeout=3000)
                                        clicked = True
                                        log.append(f"click[{i}] ok: {label}")
                                        break
                                except Exception:
                                    continue
                            if not clicked:
                                log.append(f"click not found: {label} (count={n})")
                        elif cmd.startswith("eval:"):
                            r = page.evaluate(cmd.split(":", 1)[1])
                            log.append(f"eval: {str(r)[:400]}")
                        else:
                            log.append(f"unknown: {cmd}")
                    except Exception as exc:
                        log.append(f"error: {type(exc).__name__}: {str(exc)[:200]}")
                    RESULT.write_text("\n".join(log), encoding="utf-8")
                    STATUS.write_text(STATUS.read_text(encoding="utf-8") + f"\n[cmd] {cmd} -> " + "; ".join(log),
                                      encoding="utf-8")
            time.sleep(2)

        ctx.close()


if __name__ == "__main__":
    main()
