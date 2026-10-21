"""抖音创作者中心 · 浏览器登录器（SMS 验证码登录 + 扫码登录）。

结构对齐小红书登录助手 xhs_browser_sms_login.py，抖音定制：
- SMS：切「验证码登录」→ 填手机号 → 点获取验证码 → 等群内回传 → 填码提交 → 轮询登录态
- 扫码：截取登录二维码 → 等待扫码 → 轮询登录态
- 登录成功判定：sessionid cookie 出现/变化；失败/风控返回结构化结果由上层降级

抖音登录页实测（2026-10-09）：
- Tab：「扫码登录」/「验证码登录」/「密码登录」
- 手机号：input#normal-input  验证码：input#button-input  登录按钮：#douyin_login_comp_btn_id
- 「获取验证码」：span 文案，位于 #douyin_login_comp_button_input_id
- 协议：「登录即代表同意…」无需勾选
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import config as C


@dataclass
class StartResult:
    status: str            # waiting_code / waiting_scan / failed
    challenge: str = ""
    qr_path: str = ""
    reason: str = ""


@dataclass
class LoginResult:
    status: str            # succeeded / retryable / blocked / waiting_scan / failed / expired
    cookie: str = ""
    reason: str = ""
    qr_path: str = ""
    diagnostic_path: str = ""


# ---------------------------------------------------------------- helpers
def get_session_cookie(context) -> str:
    for c in context.cookies():
        if c.get("name") == "sessionid" and c.get("value"):
            return str(c["value"])
    return ""


def extract_cookie_string(context) -> str:
    parts = []
    seen = set()
    for c in context.cookies():
        dom = c.get("domain", "") or ""
        if not dom.endswith("douyin.com"):
            continue
        key = (c["name"], c["value"])
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def get_visible_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def find_captcha(page) -> dict | None:
    """检测滑块/安全验证元素。返回命中的几何信息或 None。"""
    try:
        info = page.evaluate("""() => {
            const sels = ['#captcha_container', '[class*="captcha_verify"]', '[class*="captcha-verify"]',
                          'iframe[src*="captcha"]', '[class*="secsdk-captcha"]', '[id*="captcha-verify"]'];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return {sel: s, x: r.x, y: r.y, w: r.width, h: r.height};
                }
            }
            return null;
        }""")
        return info
    except Exception:
        return None


def _first_visible(locators):
    for loc in locators:
        try:
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def _profile_dir(profile_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", profile_key or "default")[:60] or "default"
    path = C.PROFILE_ROOT / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _looks_like_qr(path: Path | str) -> bool:
    """快速校验截图内容像不像二维码：黑白两极化像素占比高（排除渐变装饰图/占位图）。"""
    try:
        from PIL import Image
        img = Image.open(path).convert("L")
        w, h = img.size
        if w < 80 or h < 80 or abs(w - h) > 40:
            return False
        px = list(img.getdata())
        n = len(px)
        if n == 0:
            return False
        dark = sum(1 for p in px if p < 60)
        light = sum(1 for p in px if p > 200)
        return (dark + light) / n > 0.45
    except Exception:
        return True  # 校验不可用时放行，不阻塞主流程


# ---------------------------------------------------------------- session
class DouyinBrowserSession:
    """驱动一个抖音登录页会话（persistent context，headless）。"""

    @classmethod
    def open(cls, profile_key: str):
        from playwright.sync_api import sync_playwright

        runtime = sync_playwright().start()
        launched = False
        try:
            profile_dir = _profile_dir(profile_key)
            base_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ]
            # 默认真实窗口（headless 会被抖音风控拦截：send_code 返回"系统异常"）
            headless = os.getenv("DY_BROWSER_HEADLESS", "0").strip().lower() in ("1", "true", "yes")
            common = dict(
                headless=headless,
                viewport={"width": 1280, "height": 900},
                user_agent=C.USER_AGENT,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=base_args,
            )
            context = None
            last_err: Exception | None = None
            # 优先系统 Chrome（真实内核，更过了风控），失败回退 Playwright Chromium
            for channel in ("chrome", None):
                try:
                    kwargs = dict(common)
                    if channel:
                        kwargs["channel"] = channel
                    context = runtime.chromium.launch_persistent_context(str(profile_dir), **kwargs)
                    launched = True
                    break
                except Exception as exc:
                    last_err = exc
                    continue
            if context is None:
                raise RuntimeError(f"浏览器启动失败: {last_err}")
        except Exception:
            runtime.stop()
            raise
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return cls(runtime, context, page)

    def __init__(self, runtime, context, page):
        self.runtime = runtime
        self.context = context
        self.page = page
        self.closed = False
        self.last_responses: list[dict] = []   # 登录相关接口响应留档（诊断用）

    # ---------- 内部工具 ----------
    def _attach_response_logger(self):
        def on_response(response):
            try:
                url = response.url
                if not any(k in url for k in ("passport", "login", "send_code", "sms", "captcha")):
                    return
                item = {"url": url[:160], "status": response.status, "ts": time.time()}
                ctype = (response.headers or {}).get("content-type", "")
                if "json" in ctype:
                    try:
                        payload = response.json() or {}
                        item["payload"] = json.dumps(payload, ensure_ascii=False)[:500]
                    except Exception:
                        pass
                self.last_responses.append(item)
                if len(self.last_responses) > 40:
                    self.last_responses = self.last_responses[-40:]
            except Exception:
                pass

        self.page.on("response", on_response)

    def _goto_login(self):
        """打开登录页并等登录卡片；卡片空白（软风控）时自动刷新重试（实测有效）。"""
        for attempt in range(3):
            try:
                if attempt == 0:
                    self.page.goto(C.LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
                else:
                    print(f"[browser] 登录卡片空白，刷新重试（第 {attempt} 次）", flush=True)
                    self.page.reload(timeout=45000)
                    self.page.wait_for_timeout(3000)
            except Exception:
                pass
            deadline = time.time() + (20 if attempt == 0 else 15)
            while time.time() < deadline:
                text = get_visible_text(self.page)
                if "验证码登录" in text or "扫码登录" in text:
                    return True
                self.page.wait_for_timeout(1000)
        return False

    def _open_sms_tab(self) -> bool:
        tab = _first_visible([self.page.get_by_text("验证码登录", exact=True)])
        if not tab:
            return False
        tab.click()
        self.page.wait_for_timeout(1200)
        return True

    # ---------- SMS ----------
    def request_code(self, phone: str) -> tuple[bool, str]:
        """打开登录页 -> 切验证码登录 -> 填手机号 -> 点获取验证码。"""
        try:
            if not self._goto_login():
                return False, "登录页未加载（可能触发风控，请稍后重试）"
            if not self._open_sms_tab():
                return False, "未找到「验证码登录」入口（页面结构变化）"
            phone_input = _first_visible([
                self.page.locator("#normal-input"),
                self.page.locator("input[placeholder*='手机号']"),
                self.page.locator("input[type='tel']"),
            ])
            if not phone_input:
                return False, "未找到手机号输入框（页面结构变化）"
            phone_input.click()
            phone_input.fill("")
            phone_input.type(phone, delay=30)
            self.page.wait_for_timeout(500)
            before_text = get_visible_text(self.page)
            self._attach_response_logger()
            # 监听 send_code 接口响应（精确判定发送结果）
            send_results: list[dict] = []

            def _on_send(response):
                try:
                    if "/passport/web/send_code" in response.url:
                        send_results.append(response.json() or {})
                except Exception:
                    pass

            self.page.on("response", _on_send)
            send_btn = _first_visible([
                self.page.locator("#douyin_login_comp_button_input_id span"),
                self.page.get_by_text("获取验证码", exact=True),
            ])
            if not send_btn:
                return False, "未找到「获取验证码」按钮（页面结构变化）"
            send_btn.click()
            self.page.wait_for_timeout(3500)
            try:
                self.page.remove_listener("response", _on_send)
            except Exception:
                pass
            # 1) 优先用接口响应判定（精确）
            if send_results:
                payload = send_results[-1]
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                err = data.get("error_code", payload.get("error_code", 0))
                desc = str(data.get("description") or payload.get("description")
                           or payload.get("message") or "")
                if str(err) not in ("0", "None", ""):
                    hint = ""
                    if "系统异常" in desc or "系统繁忙" in desc or "重试" in desc:
                        hint = "（风控拦截，建议用真实窗口模式重试）"
                    return False, f"发送验证码失败：{desc or f'错误码 {err}'}{hint}"
                return True, ""
            # 2) 兜底：页面文本判定
            return self._classify_send_result(before_text)
        except Exception as exc:
            return False, f"发送验证码失败：{type(exc).__name__}: {str(exc)[:120]}"

    def _classify_send_result(self, before_text: str) -> tuple[bool, str]:
        """点击「获取验证码」后的结果判定（兜底）。"""
        text = get_visible_text(self.page)
        new_text = text.replace(before_text, "") if before_text else text
        for keyword in ("错误", "失败", "频繁", "限制", "无效", "不正确", "繁忙", "异常", "重试", "稍后"):
            if keyword in new_text:
                snippet = " | ".join(seg.strip() for seg in new_text.splitlines()
                                      if keyword in seg)[:160]
                return False, f"发送验证码被拒：{snippet or keyword}"
        if find_captcha(self.page):
            return False, "发送验证码触发滑块验证（暂需人工处理）"
        # 成功信号：倒计时 / 重新获取 / 文案变化即为已发送
        return True, ""

    # ---------- 扫码 ----------
    # 2026-10-09 实测校准：登录页有 orb-* 装饰图会干扰"找方形元素"的启发式扫描（曾把装饰图发进群），
    # 必须用精准选择器：二维码 = #animate_qrcode_container 内的 img[aria-label="二维码"]（base64 src）
    MARK_QR_JS = """
    () => {
        document.querySelectorAll('[data-qr-target]').forEach(e => e.removeAttribute('data-qr-target'));
        const sels = [
            'img[aria-label="二维码"]',
            '#animate_qrcode_container img',
            '#douyin_login_comp_scan_code img',
            '[id*="qrcode" i] img',
            '[class*="qrcode" i] img',
        ];
        for (const s of sels) {
            let el = null;
            try { el = document.querySelector(s); } catch (e) { continue; }
            if (!el) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 100 || r.height < 100 || r.width > 400 || r.height > 400) continue;
            if (Math.abs(r.width - r.height) > 30) continue;
            const src = String(el.currentSrc || el.src || '');
            if (!src) continue;                                          // 未渲染完成
            if (src.startsWith('data:') && src.length < 1500) continue;  // 空占位图
            el.setAttribute('data-qr-target', '1');
            return {x: r.x, y: r.y, w: r.width, h: r.height, sel: s};
        }
        return null;
    }
    """

    def capture_qr(self) -> tuple[bool, str]:
        """登录页默认展示扫码二维码，截取到文件（截图后校验内容确为二维码，不合格自动刷新重试）。"""
        try:
            if not self._goto_login():
                return False, "登录页未加载（可能触发风控，请稍后重试）"
            out_dir = C.HERE / "output" / "qrcodes"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"login-qr-{int(time.time())}-{secrets.token_hex(4)}.png"
            for round_no in range(3):
                qr = None
                for _ in range(10):
                    qr = self.page.evaluate(self.MARK_QR_JS)
                    if qr:
                        break
                    self.page.wait_for_timeout(1500)
                if qr:
                    el = self.page.locator('[data-qr-target="1"]').first
                    try:
                        el.screenshot(path=str(out_path))
                    except Exception:
                        self.page.screenshot(path=str(out_path),
                                             clip={"x": qr["x"], "y": qr["y"],
                                                   "width": qr["w"], "height": qr["h"]})
                    if _looks_like_qr(out_path):
                        print(f"[browser] 二维码截取成功（selector={qr.get('sel')}）", flush=True)
                        return True, str(out_path)
                    print("[browser] 截图内容校验未通过（疑似占位/装饰图），准备重试", flush=True)
                # 未就绪/校验失败 → 刷新页面重试（实测：空白卡片刷新即恢复）
                if round_no < 2:
                    print(f"[browser] 二维码未就绪，刷新页面重试（第 {round_no + 1} 次）", flush=True)
                    try:
                        self.page.reload(timeout=45000)
                        self.page.wait_for_timeout(4000)
                    except Exception:
                        pass
            try:
                self.page.screenshot(path=str(out_path))  # 最终失败留档
            except Exception:
                pass
            return False, "未找到登录二维码（页面结构变化或风控）"
        except Exception as exc:
            return False, f"获取登录二维码失败：{type(exc).__name__}"

    # ---------- 提交验证码 ----------
    # 「验证」提交按钮探测（2026-10-09 修复）：
    # 旧逻辑用模糊类名 [class*="uc_verification_component_btn"].first —— 弹窗内「返回/取消」的类名
    # 也包含该串且排在前面，误命中后一点就退回选项列表。改为只认精确文本「验证」，
    # 限定在验证弹窗范围内，父子嵌套（按钮+内层文字节点）按同中心点去重保留最大盒子。
    SUBMIT_PROBE_JS = r"""
    () => {
        const norm = s => (s || '').replace(/[\s\u200b\u200c\u200d\ufeff]/g, '');
        const collect = (scoped) => {
            const out = [];
            document.querySelectorAll('button, div, span, a, [role="button"]').forEach(el => {
                if (norm(el.innerText || el.textContent) !== '验证') return;
                if (scoped && !el.closest('[class*="uc_verification"], [class*="identity-verif"]')) return;
                const r = el.getBoundingClientRect();
                if (r.width < 16 || r.height < 12 || r.width > 460 || r.height > 150) return;
                const cs = getComputedStyle(el);
                if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity || '1') < 0.1) return;
                out.push({x: r.x, y: r.y, w: r.width, h: r.height,
                          tag: el.tagName, cls: String(el.className || '').slice(0, 120)});
            });
            return out;
        };
        let raw = collect(true);
        if (!raw.length) raw = collect(false);   // 兜底：不限范围
        raw.sort((a, b) => (b.w * b.h) - (a.w * a.h));   // 大盒子优先（外层按钮）
        const groups = [];
        for (const c of raw) {
            const cx = c.x + c.w / 2, cy = c.y + c.h / 2;
            if (groups.some(g => Math.abs(g.cx - cx) < 10 && Math.abs(g.cy - cy) < 10)) continue;
            groups.push({cx: Math.round(cx), cy: Math.round(cy),
                         w: Math.round(c.w), h: Math.round(c.h), tag: c.tag, cls: c.cls});
        }
        groups.sort((a, b) => b.cy - a.cy);   // 弹窗底部按钮区优先
        return groups;
    }
    """

    def _probe_submit_buttons(self) -> list[dict]:
        """探测「验证」提交按钮候选（同时诊断旧模糊类名的命中情况）。"""
        try:
            cls_loc = self.page.locator('[class*="uc_verification_component_btn"]')
            n = cls_loc.count()
            if n:
                det = []
                for i in range(min(n, 6)):
                    try:
                        t = (cls_loc.nth(i).inner_text(timeout=400) or "").strip().replace("\n", " ")[:16]
                        box = cls_loc.nth(i).bounding_box() or {}
                        det.append(f"#{i}:{t!r}@({int(box.get('x', 0))},{int(box.get('y', 0))})")
                    except Exception:
                        det.append(f"#{i}:?")
                print(f"[browser] [诊断] uc_类名命中 {n} 个: {det}", flush=True)
        except Exception:
            pass
        try:
            cands = self.page.evaluate(self.SUBMIT_PROBE_JS) or []
            return [c for c in cands if isinstance(c, dict)]
        except Exception as exc:
            print(f"[browser] 提交按钮探测异常: {type(exc).__name__}", flush=True)
            return []

    def _dialog_state(self) -> str:
        """弹窗状态：input(等验证码输入) / options(选项列表) / closed / unknown。"""
        try:
            text = self._dialog_text()
            if "短信已发送至" in text or "后重新发送" in text:
                return "input"
            if "手机刷脸验证" in text or "验证登录密码" in text:
                return "options"
            if "接收短信验证码" in text:
                return "options"
            if not text:
                return "closed"
            return "unknown"
        except Exception:
            return "unknown"

    def _recover_and_resubmit(self, code: str) -> bool:
        """界面被误触退回选项列表后：重进短信验证 → 重填验证码 → 依次尝试候选点击。"""
        try:
            print("[browser] 恢复步骤1：重新进入「接收短信验证码」…", flush=True)
            if not self._click_dialog_sms_option():
                return False
            self.page.wait_for_timeout(800)
            if self._dialog_state() != "input":
                print(f"[browser] 恢复失败：界面状态={self._dialog_state()}", flush=True)
                return False
            print("[browser] 恢复步骤2：重新填入验证码…", flush=True)
            inp = self._dialog_code_input()
            if inp is None:
                return False
            inp.click()
            try:
                inp.fill("")
            except Exception:
                pass
            inp.type(code, delay=60)
            self.page.wait_for_timeout(400)
            cands = self._probe_submit_buttons()
            if not cands:
                return False
            for idx in range(min(len(cands), 3)):
                c = cands[idx]
                print(f"[browser] 恢复步骤3：重试点候选{idx} cls={c['cls'][:70]!r} @({c['cx']},{c['cy']})", flush=True)
                self.page.mouse.click(c["cx"], c["cy"])
                self.page.wait_for_timeout(1800)
                st = self._dialog_state()
                print(f"[browser] 重试后弹窗状态={st}", flush=True)
                self._screenshot(f"resubmit{idx}-after")
                if st != "options":
                    return True
            return False
        except Exception as exc:
            print(f"[browser] 恢复重试异常: {type(exc).__name__}: {exc}", flush=True)
            return False

    def _identity_dialog_open(self) -> bool:
        return self._text_has(("身份验证",)) or self.page.locator('[class*="uc_verification"]').count() > 0

    def _click_dialog_sms_option(self) -> bool:
        """在「身份验证」弹窗中进入「接收短信验证码」，并确认界面推进到输入步骤。
        2026-10-09 复盘：弹窗刚出现时点击可能无效（组件未就绪），需点击后验证 + 重试。
        安全约束：只点「接收短信验证码」——绝不点「发送短信验证」（那是重发短信）。"""
        for attempt in range(1, 4):  # 最多 3 轮
            clicked = False
            for label in ("接收短信验证码", "接收短信验证"):
                try:
                    loc = self.page.get_by_text(label, exact=False)
                    n = loc.count()
                except Exception:
                    continue
                for i in range(min(n, 6)):
                    try:
                        if not loc.nth(i).is_visible():
                            continue
                        if attempt == 1:
                            loc.nth(i).click(timeout=3000)
                        else:
                            # 重试轮：用坐标真实点击（绕过元素句柄/动画问题）
                            box = loc.nth(i).bounding_box()
                            if box and box.get("width"):
                                self.page.mouse.click(box["x"] + box["width"] / 2,
                                                      box["y"] + box["height"] / 2)
                            else:
                                loc.nth(i).click(timeout=3000)
                        clicked = True
                    except Exception:
                        continue
                    if clicked:
                        break
                if clicked:
                    break
            if not clicked:
                return False
            # 验证界面推进：弹窗输入框出现 或 「短信已发送」提示出现
            for _ in range(12):  # 最多 ~12 秒
                self.page.wait_for_timeout(1000)
                if self._dialog_code_input() is not None:
                    print(f"[browser] 短信验证输入界面就绪（第 {attempt} 次点击）", flush=True)
                    return True
                if self._text_has(("短信已发送至", "后重新发送")):
                    print(f"[browser] 短信已发送提示出现（第 {attempt} 次点击）", flush=True)
                    return True
            print(f"[browser] 点击后界面未推进（第 {attempt} 次尝试），准备重试", flush=True)
            self.page.wait_for_timeout(1500)
        return False

    def _dialog_code_input(self):
        """身份验证弹窗内的验证码输入框（可视且宽度明显大于页面表单的那个）。"""
        inputs = self.page.locator('input[placeholder*="验证码"]')
        try:
            cnt = inputs.count()
        except Exception:
            return None
        for i in range(cnt):
            try:
                box = inputs.nth(i).bounding_box()
            except Exception:
                continue
            if box and box.get("width", 0) > 200:
                return inputs.nth(i)
        return None

    def _dialog_text(self) -> str:
        """身份验证弹窗容器可见文本（诊断用：错误提示原文进日志）。"""
        try:
            for sel in ('[class*="uc_verification"]', '[class*="identity-verif"]'):
                loc = self.page.locator(sel)
                if loc.count():
                    try:
                        txt = loc.first.inner_text(timeout=2000)
                        if txt:
                            return " | ".join(t.strip() for t in txt.split("\n") if t.strip())[:300]
                    except Exception:
                        continue
        except Exception:
            pass
        return ""

    def submit_code(self, code: str) -> dict:
        """提交验证码：兼容「输入界面已就绪」与「弹窗停在选项列表」两种状态。"""
        try:
            target = self._dialog_code_input()
            mode = "dialog" if target is not None else ""
            if target is None and self._identity_dialog_open():
                # 弹窗还在但停在选项列表（首次点击无效的情况）→ 重新进入「接收短信验证码」
                print("[browser] 弹窗停在选项列表，尝试重新进入短信验证界面…", flush=True)
                self._click_dialog_sms_option()
                target = self._dialog_code_input()
                mode = "dialog" if target is not None else ""
            if target is None:
                for sel in ("#button-input", "input[placeholder*='验证码']"):
                    loc = self.page.locator(sel).first
                    try:
                        if loc.count() and loc.is_visible():
                            target = loc
                            mode = "form"
                            break
                    except Exception:
                        continue
            if target is None:
                if self._identity_dialog_open():
                    return {"status": "expired",
                            "reason": "验证界面未能进入，请重新@机器人获取新二维码"}
                phone_input = _first_visible([
                    self.page.locator("#normal-input"),
                    self.page.locator("input[placeholder*='手机号']"),
                ])
                if phone_input:
                    return {"status": "expired", "reason": "登录页面会话已过期，请重新@机器人"}
                return {"status": "failed", "reason": "未找到验证码输入框（页面结构变化）"}

            print(f"[browser] 提交验证码（mode={mode}）…", flush=True)
            # 填入验证码（真实键入，兼容受控组件），读回自证
            target.click()
            try:
                target.fill("")
            except Exception:
                pass
            target.type(code, delay=60)
            self.page.wait_for_timeout(400)
            entered = ""
            try:
                entered = str(target.input_value() or "")
            except Exception:
                pass
            print(f"[browser] 输入框读回值={entered!r}（应为 {code!r}）", flush=True)
            if entered != code:
                try:
                    target.fill(code)
                    self.page.wait_for_timeout(300)
                    entered = str(target.input_value() or "")
                    print(f"[browser] fill 兜底后读回值={entered!r}", flush=True)
                except Exception:
                    pass

            # 定位提交按钮（dialog 模式：精确文本「验证」，不用模糊类名——旧逻辑误命中返回/取消，
            # 2026-10-09 修复：点击后界面曾退回选项列表）
            button = None
            dialog_cands: list[dict] = []
            if mode == "dialog":
                dialog_cands = self._probe_submit_buttons()
                for i, c in enumerate(dialog_cands[:5]):
                    print(f"[browser] 「验证」候选{i}: {c['tag']} cls={c['cls'][:70]!r} @({c['cx']},{c['cy']})", flush=True)
                if not dialog_cands:
                    print("[browser] 未探测到「验证」候选，文本选择器兜底…", flush=True)
                    t = self.page.get_by_text("验证", exact=True)
                    for i in range(min(t.count(), 6)):
                        try:
                            if t.nth(i).is_visible() and t.nth(i).bounding_box():
                                button = t.nth(i)
                                break
                        except Exception:
                            continue
            else:
                button = _first_visible([
                    self.page.locator("#douyin_login_comp_btn_id"),
                    self.page.get_by_role("button", name="登录"),
                ])
            if button is None and not dialog_cands:
                self._screenshot("no-submit-button")
                return {"status": "failed", "reason": "未找到提交按钮（页面结构变化）"}

            print(f"[browser] 提交前弹窗文本: {self._dialog_text()!r}", flush=True)
            self._screenshot("submit-before")
            baseline = get_session_cookie(self.context)
            if dialog_cands:
                c0 = dialog_cands[0]
                self.page.mouse.click(c0["cx"], c0["cy"])
            else:
                button.click()
            print("[browser] 已点击提交，等待登录结果（最多45秒）…", flush=True)
            self.page.wait_for_timeout(1800)
            self._screenshot("submit-after")
            state_after = self._dialog_state()
            print(f"[browser] 点击后弹窗状态={state_after}", flush=True)
            if mode == "dialog" and state_after == "options":
                print("[browser] ⚠️ 界面被误触退回选项列表 → 自动恢复重试…", flush=True)
                if not self._recover_and_resubmit(code):
                    path = self._screenshot("recover-failed")
                    return {"status": "failed",
                            "reason": "提交点击误触返回且自动恢复失败，请重发验证码或重新@机器人",
                            "diagnostic_path": path}
            last_text = self._dialog_text()
            last_state = self._dialog_state()
            deadline = time.time() + 45
            tick = 0
            while time.time() < deadline:
                session = get_session_cookie(self.context)
                if session and session != baseline:
                    return {"status": "succeeded", "cookie": extract_cookie_string(self.context)}
                if find_captcha(self.page) or self._text_has(("拖动滑块", "滑动的验证")):
                    path = self._screenshot("captcha")
                    return {"status": "blocked", "reason": "触发安全验证（滑块）",
                            "diagnostic_path": path}
                tick += 1
                if tick % 6 == 0:  # 每约3秒追踪弹窗状态 + 文案变化（证据进日志）
                    st = self._dialog_state()
                    if st != last_state:
                        print(f"[browser] 弹窗状态: {last_state} → {st}", flush=True)
                        last_state = st
                    cur = self._dialog_text()
                    if cur and cur != last_text:
                        print(f"[browser] 弹窗文本变化: {cur!r}", flush=True)
                        last_text = cur
                if self._text_has(("验证码错误", "验证码不正确", "验证码已失效", "验证码过期",
                                   "验证码有误", "输入错误", "正确的验证码", "验证失败",
                                   "校验失败", "请重新输入", "重新获取")):
                    return {"status": "retryable",
                            "reason": "验证码未通过（错误或已失效），请用【最新一条短信】的验证码重新发送"}
                self.page.wait_for_timeout(500)
            path = self._screenshot("timeout")
            return {"status": "failed", "reason": "登录超时未完成", "diagnostic_path": path}
        except Exception as exc:
            return {"status": "failed", "reason": f"提交验证码失败：{type(exc).__name__}"}

    def _text_has(self, keywords: tuple[str, ...]) -> bool:
        text = get_visible_text(self.page)
        return any(k in text for k in keywords)

    def _screenshot(self, tag: str) -> str:
        try:
            out_dir = C.HERE / "output" / "diagnostics"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{tag}-{int(time.time())}-{secrets.token_hex(4)}.png"
            self.page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return ""

    def wait_for_scan(self, timeout_seconds: int = 180) -> dict:
        """扫码后轮询：直接成功 / 过期 / 弹「身份验证」→ 自动选「接收短信验证码」→ waiting_code。
        浏览器被关闭等异常不再冒泡，转为 failed 状态让上层正常回复用户。"""
        try:
            baseline = get_session_cookie(self.context)
        except Exception:
            return {"status": "failed", "reason": "浏览器会话不可用，请重新@机器人"}
        deadline = time.time() + timeout_seconds
        dialog_handled = False
        while time.time() < deadline:
            try:
                session = get_session_cookie(self.context)
                if session and session != baseline:
                    return {"status": "succeeded", "cookie": extract_cookie_string(self.context)}
                if self._text_has(("二维码已过期", "二维码已失效", "点击刷新")):
                    return {"status": "expired", "reason": "二维码已过期，请重新@机器人获取"}
                # 身份验证弹窗（扫码后风控）：自动进入「接收短信验证码」
                if not dialog_handled and self._identity_dialog_open():
                    path = self._screenshot("identity-dialog")
                    entered = self._click_dialog_sms_option()
                    if entered or self._identity_dialog_open():
                        dialog_handled = True
                        print(f"[browser] 身份验证处理完成：进入短信验证={'是' if entered else '未确认（提交时将重试）'}",
                              flush=True)
                        return {"status": "waiting_code",
                                "reason": "身份验证：已进入短信验证" if entered else "身份验证：界面待确认",
                                "diagnostic_path": path}
                    return {"status": "blocked",
                            "reason": "扫码后需身份验证（弹窗已消失且未进入短信验证）",
                            "diagnostic_path": path}
                self.page.wait_for_timeout(1000)
            except Exception as exc:
                # TargetClosedError 等：浏览器窗口被关闭（手动关/崩溃/风控）
                return {"status": "failed",
                        "reason": f"浏览器窗口已关闭（{type(exc).__name__}），请重新@机器人"}
        return {"status": "expired", "reason": "等待超时，请重新@机器人获取"}

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.context.close()
        finally:
            self.runtime.stop()


# ---------------------------------------------------------------- facade
class DouyinBrowserLogin:
    """高层 API：会话生命周期管理（challenge = session_id 引用）。"""

    def __init__(self, session_factory=None, ttl_seconds: int = 900):
        self.session_factory = session_factory or (lambda key: DouyinBrowserSession.open(key))
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[str, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _session_id(challenge: str) -> str:
        try:
            return str((json.loads(challenge) or {}).get("session_id") or "")
        except (TypeError, ValueError):
            return ""

    def _store(self, session) -> str:
        session_id = secrets.token_hex(16)
        with self._lock:
            self.sessions[session_id] = {"session": session, "born": time.time()}
        return json.dumps({"version": 1, "session_id": session_id}, separators=(",", ":"))

    # ---------- 启动 ----------
    def start_sms(self, phone: str, profile_key: str = "") -> StartResult:
        phone = re.sub(r"\D", "", phone or "")
        if not re.fullmatch(r"1\d{10}", phone):
            return StartResult(status="failed", reason="绑定手机号格式不正确")
        try:
            session = self.session_factory(profile_key)
        except Exception as exc:
            return StartResult(status="failed", reason=f"浏览器启动失败：{type(exc).__name__}")
        ok, reason = session.request_code(phone)
        if not ok:
            session.close()
            return StartResult(status="failed", reason=reason or "发送验证码失败")
        return StartResult(status="waiting_code", challenge=self._store(session))

    def start_qr(self, profile_key: str = "") -> StartResult:
        try:
            session = self.session_factory(profile_key)
        except Exception as exc:
            return StartResult(status="failed", reason=f"浏览器启动失败：{type(exc).__name__}")
        ok, result = session.capture_qr()
        if not ok:
            session.close()
            return StartResult(status="failed", reason=result or "获取登录二维码失败")
        return StartResult(status="waiting_scan", challenge=self._store(session), qr_path=result)

    # ---------- 完成 ----------
    def complete(self, challenge: str, code: str) -> LoginResult:
        with self._lock:
            entry = self.sessions.pop(self._session_id(challenge), None)
        if entry is None:
            return LoginResult(status="failed", reason="登录会话已失效，请重新@机器人")
        session = entry["session"]
        keep = False
        try:
            outcome = session.submit_code(code)
            status = str(outcome.get("status") or "failed")
            if status == "succeeded":
                return LoginResult(status="succeeded", cookie=str(outcome.get("cookie") or ""))
            if status == "retryable":
                # 验证码错误：保留同一会话，允许直接重新提交
                keep = True
                with self._lock:
                    self.sessions[self._session_id(challenge)] = entry
                return LoginResult(status="retryable",
                                   reason=str(outcome.get("reason") or "验证码错误或已失效"))
            return LoginResult(status=status if status in ("blocked", "expired") else "failed",
                               reason=str(outcome.get("reason") or "登录失败"),
                               diagnostic_path=str(outcome.get("diagnostic_path") or ""))
        finally:
            if not keep:
                try:
                    session.close()
                except Exception:
                    pass

    def wait_for_scan(self, challenge: str, timeout_seconds: int = 180) -> LoginResult:
        with self._lock:
            entry = self.sessions.pop(self._session_id(challenge), None)
        if entry is None:
            return LoginResult(status="failed", reason="登录会话已失效，请重新@机器人")
        session = entry["session"]
        keep = False
        try:
            outcome = session.wait_for_scan(timeout_seconds=timeout_seconds)
            status = str(outcome.get("status") or "failed")
            if status == "succeeded":
                return LoginResult(status="succeeded", cookie=str(outcome.get("cookie") or ""))
            if status == "waiting_code":
                # 身份验证已触发短信：保留会话等待用户回传验证码
                keep = True
                with self._lock:
                    self.sessions[self._session_id(challenge)] = entry
                return LoginResult(status="waiting_code",
                                   reason=str(outcome.get("reason") or ""),
                                   diagnostic_path=str(outcome.get("diagnostic_path") or ""))
            return LoginResult(status=status if status in ("blocked", "expired") else "failed",
                               reason=str(outcome.get("reason") or "登录失败"),
                               diagnostic_path=str(outcome.get("diagnostic_path") or ""))
        finally:
            if not keep:
                try:
                    session.close()
                except Exception:
                    pass

    def release(self, challenge: str) -> bool:
        """任务结束时释放会话（成功/失败都调用）。"""
        with self._lock:
            entry = self.sessions.pop(self._session_id(challenge), None)
        if entry is None:
            return False
        try:
            entry["session"].close()
        except Exception:
            pass
        return True

    def cancel(self, challenge: str) -> bool:
        return self.release(challenge)

    def close_all(self) -> None:
        while True:
            with self._lock:
                if not self.sessions:
                    return
                _sid, entry = self.sessions.popitem()
            try:
                entry["session"].close()
            except Exception:
                pass
