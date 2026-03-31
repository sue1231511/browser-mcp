import asyncio
import json
import io
import os
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, BrowserContext, Page
from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
 
PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "browser-profile"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
 
mcp = FastMCP("browser", host="0.0.0.0", port=PORT)
 
_playwright = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_lock: Optional[asyncio.Lock] = None
 
 
def get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock
 
 
async def _cleanup():
    """清理所有浏览器资源"""
    global _playwright, _context, _page
    _page = None
    if _context is not None:
        try:
            await _context.close()
        except Exception:
            pass
        _context = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
 
 
async def ensure_page() -> Page:
    """获取可用页面，自动处理崩溃恢复"""
    global _playwright, _context, _page
 
    # 快速路径：页面还活着
    if _page is not None and not _page.is_closed():
        try:
            await _page.evaluate("1")
            return _page
        except Exception:
            pass
 
    # 检查 context 是否还活着
    if _context is not None:
        try:
            pages = _context.pages
            for p in pages:
                if not p.is_closed():
                    _page = p
                    await _page.evaluate("1")
                    return _page
            _page = await _context.new_page()
            return _page
        except Exception:
            pass
 
    # 全部重建
    await _cleanup()
 
    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    await _context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
 
    pages = _context.pages
    _page = pages[0] if pages else await _context.new_page()
    return _page
 
 
@mcp.tool()
async def navigate(url: str) -> str:
    """打开指定 URL，等待页面基本加载完成"""
    async with get_lock():
        try:
            page = await ensure_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else "unknown"
            return f"已打开: {page.url}  状态码: {status}"
        except Exception as e:
            return f"[错误] navigate 失败: {e}"
 
 
@mcp.tool()
async def screenshot(quality: int = 60):
    """
    截图当前可见区域，返回压缩后的 JPEG 图片。
    quality: 1-100，默认 60，扫码时建议用 90。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            png_data = await page.screenshot(type="png", full_page=False)
            img = PILImage.open(io.BytesIO(png_data))
            if img.width > 1000:
                ratio = 1000 / img.width
                img = img.resize((1000, int(img.height * ratio)), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
            return Image(data=buf.getvalue(), format="jpeg")
        except Exception as e:
            return f"[错误] screenshot 失败: {e}"
 
 
@mcp.tool()
async def execute_js(script: str) -> str:
    """
    在当前页面执行 JavaScript，返回 JSON 结果。
    用于直接读取文字、评论、作者名等结构化数据，比截图快且省 token。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            result = await page.evaluate(script)
            if result is None:
                return "null"
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"[错误] execute_js 失败: {e}"
 
 
@mcp.tool()
async def click(selector: str) -> str:
    """点击页面元素，支持 CSS selector"""
    async with get_lock():
        try:
            page = await ensure_page()
            await page.click(selector, timeout=10000)
            await page.wait_for_timeout(500)
            return f"已点击: {selector}"
        except Exception as e:
            return f"[错误] click 失败: {e}"
 
 
@mcp.tool()
async def type_text(selector: str, text: str, human_like: bool = False) -> str:
    """
    聚焦输入框并输入文字（会先清空原有内容）。
    human_like: True 时逐字输入模拟真人节奏（过反自动化），False 时直接 fill（更稳定）。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            if human_like:
                await page.click(selector, timeout=10000)
                await page.fill(selector, "")
                await page.keyboard.type(text, delay=40)
            else:
                await page.fill(selector, text, timeout=10000)
            return f"已输入到 {selector}: {text}"
        except Exception as e:
            return f"[错误] type_text 失败: {e}"
 
 
@mcp.tool()
async def scroll(direction: str = "down", amount: int = 600) -> str:
    """
    滚动当前页面。
    direction: up 或 down
    amount: 像素数，默认 600
    """
    async with get_lock():
        try:
            page = await ensure_page()
            dy = amount if direction == "down" else -amount
            await page.evaluate(f"window.scrollBy(0, {dy})")
            await page.wait_for_timeout(400)
            scroll_y = await page.evaluate("window.scrollY")
            return f"已滚动 {direction} {amount}px，当前 scrollY: {scroll_y}"
        except Exception as e:
            return f"[错误] scroll 失败: {e}"
 
 
@mcp.tool()
async def wait_for(selector: str, timeout: int = 10000) -> str:
    """等待某个元素出现在页面中"""
    async with get_lock():
        try:
            page = await ensure_page()
            await page.wait_for_selector(selector, timeout=timeout)
            return f"元素已出现: {selector}"
        except Exception as e:
            return f"[错误] wait_for 失败: {e}"
 
 
@mcp.tool()
async def get_url() -> str:
    """获取当前页面 URL"""
    async with get_lock():
        try:
            page = await ensure_page()
            return page.url
        except Exception as e:
            return f"[错误] get_url 失败: {e}"
 
 
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
