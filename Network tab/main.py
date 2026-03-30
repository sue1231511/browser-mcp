import asyncio
import json
import base64
import os
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_PATH = DATA_DIR / "cookies.json"

mcp = FastMCP("browser", host="0.0.0.0", port=PORT)

_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_lock: Optional[asyncio.Lock] = None


def get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def ensure_page() -> Page:
    global _playwright, _browser, _context, _page

    if _page is not None and not _page.is_closed():
        return _page

    if _playwright is None:
        _playwright = await async_playwright().start()

    if _browser is None or not _browser.is_connected():
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

    _context = await _browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )

    if COOKIE_PATH.exists():
        try:
            cookies = json.loads(COOKIE_PATH.read_text())
            if cookies:
                await _context.add_cookies(cookies)
        except Exception:
            pass

    _page = await _context.new_page()
    return _page


@mcp.tool()
async def navigate(url: str) -> str:
    """打开指定 URL，等待页面基本加载完成"""
    async with get_lock():
        page = await ensure_page()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = resp.status if resp else "unknown"
        return f"已打开: {page.url}  状态码: {status}"


@mcp.tool()
async def screenshot() -> str:
    """截图当前可见区域，返回 base64 PNG（用于扫码、看图片内容）"""
    async with get_lock():
        page = await ensure_page()
        data = await page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        return f"data:image/png;base64,{b64}"


@mcp.tool()
async def execute_js(script: str) -> str:
    """
    在当前页面执行 JavaScript，返回 JSON 结果。
    用于直接读取文字、评论、作者名等结构化数据，比截图快且省 token。
    示例: document.querySelectorAll('.comment').length
    """
    async with get_lock():
        page = await ensure_page()
        result = await page.evaluate(script)
        if result is None:
            return "null"
        return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def click(selector: str) -> str:
    """点击页面元素，支持 CSS selector"""
    async with get_lock():
        page = await ensure_page()
        await page.click(selector, timeout=10000)
        await page.wait_for_timeout(500)
        return f"已点击: {selector}"


@mcp.tool()
async def type_text(selector: str, text: str) -> str:
    """聚焦输入框并输入文字（会先清空原有内容）"""
    async with get_lock():
        page = await ensure_page()
        await page.click(selector, timeout=10000)
        await page.fill(selector, "")
        await page.keyboard.type(text, delay=40)
        return f"已输入到 {selector}: {text}"


@mcp.tool()
async def scroll(direction: str = "down", amount: int = 600) -> str:
    """
    滚动当前页面。
    direction: up 或 down
    amount: 像素数，默认 600
    """
    async with get_lock():
        page = await ensure_page()
        dy = amount if direction == "down" else -amount
        await page.evaluate(f"window.scrollBy(0, {dy})")
        await page.wait_for_timeout(400)
        return f"已滚动 {direction} {amount}px，当前 scrollY: " + str(
            await page.evaluate("window.scrollY")
        )


@mcp.tool()
async def wait_for(selector: str, timeout: int = 10000) -> str:
    """等待某个元素出现在页面中"""
    async with get_lock():
        page = await ensure_page()
        await page.wait_for_selector(selector, timeout=timeout)
        return f"元素已出现: {selector}"


@mcp.tool()
async def get_url() -> str:
    """获取当前页面 URL"""
    async with get_lock():
        page = await ensure_page()
        return page.url


@mcp.tool()
async def save_cookies() -> str:
    """
    把当前浏览器的所有 cookie 保存到持久化存储（/data/cookies.json）。
    登录完成后调用一次，下次启动服务时自动恢复登录态。
    """
    async with get_lock():
        if _context is None:
            return "浏览器还没启动，请先用 navigate 打开一个页面"
        cookies = await _context.cookies()
        COOKIE_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
        return f"已保存 {len(cookies)} 个 cookies → {COOKIE_PATH}"


@mcp.tool()
async def load_cookies() -> str:
    """从持久化存储重新加载 cookies（比如需要刷新登录态时使用）"""
    async with get_lock():
        if not COOKIE_PATH.exists():
            return "没有找到已保存的 cookies，请先登录并调用 save_cookies"
        cookies = json.loads(COOKIE_PATH.read_text())
        if _context is not None:
            await _context.add_cookies(cookies)
            return f"已加载 {len(cookies)} 个 cookies 到当前会话"
        return f"文件存在（{len(cookies)} 个 cookies），将在浏览器下次初始化时自动加载"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
