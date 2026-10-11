"""Read-only ARM64/browser/network smoke test; no login or reservation writes."""
import asyncio
import platform
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def main():
    assert platform.machine() in {"aarch64", "arm64"}, platform.machine()
    print("architecture:", platform.machine(), flush=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content("<title>ARM64 smoke</title>")
            assert await page.title() == "ARM64 smoke"
            print("chromium:", browser.version, flush=True)
            for host in ["libzw.ustb.edu.cn", "jwgl.ustb.edu.cn"]:
                response = await page.goto("https://" + host + "/", wait_until="domcontentloaded", timeout=30000)
                print("navigation:", host, "status:", response.status if response else None,
                      "final_host:", urlsplit(page.url).hostname, flush=True)
        finally:
            await browser.close()


asyncio.run(main())
