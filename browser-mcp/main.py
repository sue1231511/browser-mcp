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
 
    if _page is not None and not _page.is_closed():
        try:
            await _page.evaluate("1")
            return _page
        except Exception:
            pass
 
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
 
    await _cleanup()
 
    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-crash-reporter",          # 新增：禁用 crash reporter，避免依赖 /tmp
            "--no-first-run",                    # 新增：跳过首次运行向导
            "--disable-default-apps",            # 新增：禁用默认应用
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
 
 
# ── 通用浏览器工具 ──────────────────────────────────────────
 
@mcp.tool()
async def navigate(url: str) -> str:
    """打开一个网页"""
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
    截个屏看看当前页面上有什么。quality: 1-100，默认60，扫码建议90。
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
    在当前页面跑一段JS，直接读取文字、评论什么的，比截图快。
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
    """点一下页面上的某个元素，用CSS选择器定位"""
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
    在输入框里打字，会先清空原来的内容。
    human_like: True 的话会像真人一样一个字一个字打，False 就是直接填进去。
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
    往上或往下滚页面。direction: up或down，amount: 滚多少像素，默认600。
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
    """等页面上的某个元素加载出来再继续"""
    async with get_lock():
        try:
            page = await ensure_page()
            await page.wait_for_selector(selector, timeout=timeout)
            return f"元素已出现: {selector}"
        except Exception as e:
            return f"[错误] wait_for 失败: {e}"
 
 
@mcp.tool()
async def get_url() -> str:
    """看看现在在哪个网页"""
    async with get_lock():
        try:
            page = await ensure_page()
            return page.url
        except Exception as e:
            return f"[错误] get_url 失败: {e}"
 
 
# ── 小红书专用工具 ──────────────────────────────────────────
 
XHS_FEED_JS = """
(() => {
    const items = document.querySelectorAll('.note-item');
    return Array.from(items).map((item, i) => {
        const links = item.querySelectorAll('a[href*="/explore/"]');
        let url = '';
        for (const a of links) {
            if (a.href.includes('xsec_token')) { url = a.href; break; }
        }
        if (!url) {
            for (const a of links) {
                if (a.href.includes('/explore/')) { url = a.href; break; }
            }
        }
        const title = item.querySelector('.title span')?.textContent?.trim() || '';
        const desc = item.querySelector('.desc')?.textContent?.trim() || '';
        const author = item.querySelector('.author-wrapper .name')?.textContent?.trim() || '';
        const likes = item.querySelector('.like-wrapper .count')?.textContent?.trim() || '';
        return { index: i, title: title || desc, author, likes, url };
    });
})()
"""
 
XHS_NOTE_JS = """
(() => {
    const title = document.querySelector('#detail-title')?.textContent?.trim() || '';
    const desc = document.querySelector('#detail-desc')?.textContent?.trim() || '';
    const author = document.querySelector('.author-container .username')?.textContent?.trim() || '';
    const date = document.querySelector('.date')?.textContent?.trim() || '';
    const ipLoc = document.querySelector('.ip-container')?.textContent?.trim() || '';
    const likes = document.querySelector('.like-wrapper .count')?.textContent?.trim() || '';
    const collects = document.querySelector('.collect-wrapper .count')?.textContent?.trim() || '';
    const chatCount = document.querySelector('.chat-wrapper .count')?.textContent?.trim() || '';
    const tags = Array.from(document.querySelectorAll('#detail-desc a.tag')).map(
        a => a.textContent?.trim()
    );
    const comments = Array.from(document.querySelectorAll('.parent-comment')).slice(0, __COMMENT_COUNT__).map(c => {
        const item = c.querySelector('.comment-item');
        if (!item) return null;
        const name = item.querySelector('.author-wrapper .name')?.textContent?.trim() || '';
        const tag = item.querySelector('.author-wrapper .tag')?.textContent?.trim() || '';
        const text = item.querySelector('.note-text')?.textContent?.trim() || '';
        const like = item.querySelector('.like')?.textContent?.trim() || '';
        const date = item.querySelector('.info .date span')?.textContent?.trim() || '';
        const location = item.querySelector('.info .location')?.textContent?.trim() || '';
        return { name, tag, text, like, date, location };
    }).filter(Boolean);
    return { title, desc, author, date, ip: ipLoc, likes, collects, comments_count: chatCount, tags, comments };
})()
"""
 
 
@mcp.tool()
async def read_xhs_feed(count: int = 10) -> str:
    """
    刷小红书首页，看看有什么笔记。返回标题、作者、点赞数和链接。
    不在小红书页面的话会自动跳过去。count: 看几条，默认10。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            current = page.url
            if "xiaohongshu.com" not in current:
                await page.goto(
                    "https://www.xiaohongshu.com/explore",
                    wait_until="domcontentloaded", timeout=30000,
                )
            try:
                await page.wait_for_selector(".note-item", timeout=5000)
            except Exception:
                await page.wait_for_timeout(2000)
 
            result = await page.evaluate(XHS_FEED_JS)
            if result:
                result = result[:count]
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"[错误] read_xhs_feed 失败: {e}"
 
 
@mcp.tool()
async def read_xhs_note(url: str, comment_count: int = 10) -> str:
    """
    打开一篇小红书笔记看完整内容：标题、正文、作者、标签、评论都在。
    url: 笔记链接（最好用刷首页时返回的带token的链接）。comment_count: 读几条评论，默认10。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("#detail-desc", timeout=5000)
            except Exception:
                await page.wait_for_timeout(2000)
 
            js = XHS_NOTE_JS.replace("__COMMENT_COUNT__", str(int(comment_count)))
            result = await page.evaluate(js)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"[错误] read_xhs_note 失败: {e}"
 
 
@mcp.tool()
async def like_xhs_note() -> str:
    """
    给当前打开的小红书笔记点赞或取消点赞。
    需要先用 read_xhs_note 打开一篇笔记。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            await page.click(
                ".interact-container .like-wrapper", timeout=5000
            )
            await page.wait_for_timeout(500)
            is_liked = await page.evaluate(
                "document.querySelector('.interact-container .like-wrapper')"
                ".classList.contains('like-active')"
            )
            return f"{'已点赞 ❤️' if is_liked else '已取消点赞'}"
        except Exception as e:
            return f"[错误] like_xhs_note 失败: {e}"
 
 
@mcp.tool()
async def comment_xhs_note(text: str) -> str:
    """
    在当前打开的小红书笔记下发评论。
    需要先用 read_xhs_note 打开一篇笔记。
    text: 评论内容。
    """
    async with get_lock():
        try:
            page = await ensure_page()
            await page.click("#content-textarea", timeout=5000)
            await page.wait_for_timeout(300)
            await page.keyboard.type(text, delay=40)
            await page.wait_for_timeout(300)
            await page.wait_for_function(
                "!document.querySelector('.btn.submit').classList.contains('gray')",
                timeout=3000,
            )
            await page.click(".btn.submit", timeout=5000)
            await page.wait_for_timeout(1000)
            return f"已发送评论: {text}"
        except Exception as e:
            return f"[错误] comment_xhs_note 失败: {e}"
 
 
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
