#!/usr/bin/env python3
"""回传短信验证码给等待中的扫码登录器。

把验证码写入 output/qr_login/verify_code.txt，登录器会自动读取并删除该文件。
用法：
  python submit_code.py 123456      # 直接带验证码
  python submit_code.py             # 运行后输入（或双击 回传验证码.bat）
"""
import sys
from pathlib import Path

CODE_FILE = Path(__file__).resolve().parent / "output" / "qr_login" / "verify_code.txt"


def main() -> int:
    code = " ".join(sys.argv[1:]).strip()
    if not code:
        try:
            code = input("请输入短信验证码: ").strip()
        except EOFError:
            code = ""
    if not code:
        print("未输入验证码", file=sys.stderr)
        return 1
    CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CODE_FILE.write_text(code, encoding="utf-8")
    print(f"验证码已回传: {code[:2]}******（登录器等待中会自动读取）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
