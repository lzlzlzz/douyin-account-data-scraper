"""离线验证 SUBMIT_PROBE_JS：仿造弹窗 DOM（含返回/取消/隐藏等陷阱），
应只命中「验证」提交按钮。"""
from playwright.sync_api import sync_playwright
from browser_login import DouyinBrowserSession

FAKE = """
<html><body style="margin:0">
<div class="uc_verification_component" style="position:relative;width:600px;height:520px;background:#fff;margin:20px auto">
  <button class="uc_verification_component_btn_back" style="position:absolute;left:20px;top:20px;width:40px;height:40px">‹</button>
  <div style="position:absolute;top:60px;left:0;right:0;text-align:center;font-size:20px">接收短信验证码</div>
  <div style="position:absolute;top:120px;left:140px;right:140px;height:44px;background:#f5f5f5;line-height:44px;color:#999">请输入验证码（44s后重新发送）</div>
  <div style="position:absolute;top:200px;left:0;right:0;text-align:center;color:#333">无法验证通过？选择其他验证方式</div>
  <div style="position:absolute;bottom:40px;left:60px;right:60px;display:flex;gap:20px">
    <button class="uc_verification_component_btn_cancel" style="flex:1;height:44px">取消</button>
    <button class="uc_verification_component_btn_confirm" style="flex:1;height:44px"><span>验证</span></button>
  </div>
  <button style="display:none">验证</button>
  <button style="position:absolute;top:300px;left:200px;width:150px;height:40px">验证登录密码</button>
</div>
</body></html>
"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.set_content(FAKE)
    res = pg.evaluate(DouyinBrowserSession.SUBMIT_PROBE_JS)
    print("探测结果:", res)
    assert len(res) == 1, f"应只命中 1 个候选，实际 {len(res)}"
    c = res[0]
    assert "confirm" in c["cls"], f"应命中 confirm 按钮，实际 {c['cls']}"
    assert c["cx"] > 640, f"应为右侧按钮，实际 x={c['cx']}"
    print(f"\n✅ 通过：只命中「验证」提交按钮 cls={c['cls']!r} @({c['cx']},{c['cy']}) 未命中返回/取消/隐藏/长文本")
    b.close()
