"""UI 视觉验证：登录 → 对话 → 知识库 截图。

用途：改版后快速确认「界面没坏」——比读 CSS 可靠，也比人工点一遍省事。
截图落在 output/ui/（已被 gitignore）。

前置条件（需先手动起两个服务）：
    # 后端
    cd app && uvicorn app_main:app --port 8000
    # 前端 dev server（含 /api 代理到 8000）
    cd agent_front && npm run dev

用法:
    python scripts/capture_ui.py

注意：脚本会实际走一次登录（用 .env 里 AUTH_USERS 配置的开发账号），
账号口令写死在脚本里，仅适用于本机开发环境。
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:5173"


def shoot(page, name: str) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  {name}: {path.name}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ── 桌面 ──
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(600)
        print("1) 登录页")
        shoot(page, "01-login")
        print("   标题:", page.title())
        print("   主标题:", page.locator("h1.hero-title").inner_text())

        # 错误口令 → 内联报错
        page.fill("#login-user", "user01")
        page.fill("#login-password", "wrong-password")
        page.click(".login-submit")
        page.wait_for_timeout(1200)
        print("   错误提示:", page.locator(".login-error").inner_text())
        shoot(page, "02-login-error")

        # 正确口令 → 进入对话页
        page.fill("#login-password", "devpass01")
        page.click(".login-submit")
        page.wait_for_timeout(3000)
        print("2) 对话页 | URL:", page.url)
        shoot(page, "03-chat")

        # 知识库
        page.click("a[href='/knowledge']")
        page.wait_for_timeout(2500)
        print("3) 知识库 | URL:", page.url)
        shoot(page, "04-knowledge")

        # 登出
        page.click(".account-logout")
        page.wait_for_timeout(1200)
        print("4) 登出后 URL:", page.url)

        ctx.close()

        # ── 移动端 ──
        m = browser.new_context(viewport={"width": 390, "height": 844})
        mp = m.new_page()
        mp.goto(BASE, wait_until="networkidle")
        mp.wait_for_timeout(600)
        print("5) 移动端登录页")
        shoot(mp, "05-login-mobile")
        m.close()

        browser.close()

        real_errors = [e for e in errors if "favicon" not in e.lower()]
        print("\n控制台错误:", real_errors if real_errors else "无")
        return 0


if __name__ == "__main__":
    sys.exit(main())
