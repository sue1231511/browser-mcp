import asyncio
import json
import io
import os
import time
import random
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, BrowserContext, Page
from PIL import Image as PILImage
 
# ── 兼容两种 FastMCP ──────────────────────────────────────
# 优先用独立 fastmcp 包（功能更全，支持 path / middleware）
# 如果没装就 fallback 到 MCP SDK 内置的版本
try:
    from fastmcp import FastMCP, Image
    USING_STANDALONE_FASTMCP = True
except ImportError:
    from mcp.server.fastmcp import FastMCP, Image
    USING_STANDALONE_FASTMCP = False
 
PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "browser-profile"
SECRET_PATH = os.environ.get("MCP_SECRET", "")
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
 
mcp = FastMCP("browser", host="0.0.0.0", port=PORT)
 
# ── 如果独立 fastmcp 包可用，加载速率限制中间件 ──────────
if USING_STANDALONE_FASTMCP:
    try:
        from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
        mcp.add_middleware(RateLimitingMiddleware(max_requests_per_second=2))
    except ImportError:
        pass
 
# ── 全局状态 ───────────────────────────────────────────────
 
_playwright = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_lock: Optional[asyncio.Lock] = None
 
# 评论冷却控制
_last_comment_ts: float = 0
_next_cooldown: int = 0  # 上次评论时就确定好的下次冷却秒数
 
 
def get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock
 
 
# ── 反检测 init_script ────────────────────────────────────
 
ANTI_DETECT_JS = """
// 1. 隐藏 webdriver 标志
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
 
// 2. Canvas 指纹噪声 —— 注入肉眼不可见的微小噪点，让每次指纹 hash 都不同
const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const style = ctx.fillStyle;
        ctx.fillStyle = 'rgba(1,1,1,0.01)';
        ctx.fillRect(0, 0, 1, 1);
        ctx.fillStyle = style;
    }
    return _origToDataURL.apply(this, arguments);
};
 
// 3. WebGL 渲染器信息模糊
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    // UNMASKED_VENDOR_WEBGL
    if (param === 37445) return 'Intel Inc.';
    // UNMASKED_RENDERER_WEBGL
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.apply(this, arguments);
};
 
// 4. 覆盖 plugins 和 languages，让浏览器看起来更真实
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en']
});
"""
 
 
# ── 浏览器生命周期 ─────────────────────────────────────────
 
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
    """获取可用页面，自动处理崩溃恢复（三层降级）"""
    global _playwright, _context, _page
 
    # 第一层：当前 page 还活着就直接用
    if _page is not None and not _page.is_closed():
        try:
            await _page.evaluate("1")
            return _page
        except Exception:
            pass
 
    # 第二层：从 context 里捞一个存活的 page
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
 
    # 第三层：全部推倒重建
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
    # 注入反检测脚本（每个新页面都会自动执行）
    await _context.add_init_script(ANTI_DETECT_JS)
 
    pages = _context.pages
    _page = pages[0] if pages else await _context.new_page()
    return _page
 
 
# ── 辅助函数 ───────────────────────────────────────────────
 
COMMENT_COOLDOWN_MIN = 60   # 最短冷却秒数
COMMENT_COOLDOWN_MAX = 180  # 最长冷却秒数
 
 
def _is_xhs_note_page(url: str) -> bool:
    """检查当前是否在小红书笔记详情页"""
    return "xiaohongshu.com" in url and ("/explore/" in url or "/discovery/item/" in url)
 
 
async def _simulate_reading(page: Page, skip: bool = False):
    """
    模拟真人阅读行为：随机滚动 + 停留。
    在点赞/评论前调用，让行为轨迹更自然。
    skip: 如果刚 read_xhs_note 过（已经"阅读"了），可以跳过。
    """
    if skip:
        return
 
    # 随机滚动 2-4 次
    scroll_times = random.randint(2, 4)
    for _ in range(scroll_times):
        dy = random.randint(200, 500)
        await page.evaluate(f"window.scrollBy(0, {dy})")
        await page.wait_for_timeout(random.randint(800, 2500))
 
    # 回到顶部附近（真人看完会滑回去）
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(random.randint(500, 1500))
 
 
async def _human_type(page: Page, selector: str, text: str):
    """
    拟人化输入：先点击输入框，然后逐字输入，每个字的间隔随机。
    偶尔会在某个字后面多停顿一下，模拟思考。
    """
    await page.click(selector, timeout=10000)
    await page.fill(selector, "")
    for i, char in enumerate(text):
        await page.keyboard.type(char)
        # 基础间隔 30-120ms
        delay = random.randint(30, 120)
        # 每隔几个字有概率多停顿一下（模拟思考）
        if i > 0 and random.random() < 0.15:
            delay += random.randint(300, 800)
        await page.wait_for_timeout(delay)
 
 
# ── 通用浏览器工具 ──────────────────────────────────────────
 
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
            # 参数安全校验
            quality = max(1, min(100, quality))
 
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
                await _human_type(page, selector, text)
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
    读取小红书 Explore 首页的笔记列表。
    如果当前不在小红书页面会自动导航过去。
    返回每篇笔记的标题、作者、点赞数和带 xsec_token 的链接。
    count: 返回条数，默认 10。
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
    读取一篇小红书笔记的完整内容（标题、正文、作者、标签、评论等）。
    url: 笔记的完整链接（建议用 read_xhs_feed 返回的带 xsec_token 的链接）。
    comment_count: 读取评论条数，默认 10。
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
async def like_xhs_note(skip_simulate: bool = False) -> str:
    """
    给当前打开的小红书笔记点赞或取消点赞。
    需要先用 read_xhs_note 打开一篇笔记。
    skip_simulate: 如果刚 read_xhs_note 读完马上点赞，设为 True 跳过阅读模拟。
    """
    async with get_lock():
        try:
            page = await ensure_page()
 
            # 页面校验：确认当前在笔记详情页
            current_url = page.url
            if not _is_xhs_note_page(current_url):
                return f"[错误] 当前不在笔记详情页，无法点赞。当前 URL: {current_url}"
 
            # 模拟阅读（可跳过）
            await _simulate_reading(page, skip=skip_simulate)
 
            await page.click(
                ".interact-container .like-wrapper", timeout=5000
            )
            await page.wait_for_timeout(random.randint(300, 800))
            is_liked = await page.evaluate(
                "document.querySelector('.interact-container .like-wrapper')"
                ".classList.contains('like-active')"
            )
            return f"{'已点赞 ❤️' if is_liked else '已取消点赞'}"
        except Exception as e:
            return f"[错误] like_xhs_note 失败: {e}"
 
 
@mcp.tool()
async def comment_xhs_note(text: str, skip_simulate: bool = False) -> str:
    """
    在当前打开的小红书笔记下发评论。
    需要先用 read_xhs_note 打开一篇笔记。
    内置冷却时间、阅读模拟、拟人化输入。
    text: 评论内容。
    skip_simulate: 如果刚 read_xhs_note 读完马上评论，设为 True 跳过阅读模拟。
    """
    global _last_comment_ts, _next_cooldown
 
    async with get_lock():
        try:
            page = await ensure_page()
 
            # ── 页面校验 ──
            current_url = page.url
            if not _is_xhs_note_page(current_url):
                return f"[错误] 当前不在笔记详情页，无法评论。当前 URL: {current_url}"
 
            # ── 冷却时间检查（用上次评论时确定好的固定值） ──
            now = time.time()
            if _last_comment_ts > 0 and _next_cooldown > 0:
                elapsed = now - _last_comment_ts
                if elapsed < _next_cooldown:
                    remaining = int(_next_cooldown - elapsed)
                    return f"[冷却中] 距离上次评论太近，还需等待约 {remaining} 秒。"
 
            # ── 模拟阅读行为（可跳过） ──
            await _simulate_reading(page, skip=skip_simulate)
 
            # ── 拟人化输入（调用 _human_type） ──
            await _human_type(page, "#content-textarea", text)
 
            await page.wait_for_timeout(random.randint(300, 600))
 
            # ── 等发送按钮可用再点击 ──
            try:
                await page.wait_for_function(
                    "!document.querySelector('.btn.submit').classList.contains('gray')",
                    timeout=3000,
                )
            except Exception:
                return "[错误] 发送按钮未变为可用状态，评论可能未输入成功。"
 
            await page.click(".btn.submit", timeout=5000)
            await page.wait_for_timeout(random.randint(1000, 2000))
 
            # ── 验证评论是否发送成功 ──
            verify_js = f"""
            (() => {{
                const comments = document.querySelectorAll('.comment-item .note-text');
                for (const c of comments) {{
                    if (c.textContent.trim().includes({json.dumps(text[:20])})) return true;
                }}
                return false;
            }})()
            """
            success = await page.evaluate(verify_js)
 
            # 更新冷却时间戳，并立刻确定下次冷却时长（不再每次检查时随机）
            _last_comment_ts = time.time()
            _next_cooldown = random.randint(COMMENT_COOLDOWN_MIN, COMMENT_COOLDOWN_MAX)
 
            if success:
                return f"已发送评论: {text}（下次评论冷却 {_next_cooldown} 秒）"
            else:
                return f"[警告] 评论已提交但未在页面上确认到，可能被风控拦截或页面未刷新。评论内容: {text}"
 
        except Exception as e:
            return f"[错误] comment_xhs_note 失败: {e}"
 
 
@mcp.tool()
async def check_xhs_login() -> str:
    """
    检测小红书登录状态是否正常。
    通过访问个人主页判断是否已登录、cookie 是否过期。
    """
    async with get_lock():
        try:
            page = await ensure_page()
 
            # 尝试访问"我的"页面
            await page.goto(
                "https://www.xiaohongshu.com/user/profile/me",
                wait_until="domcontentloaded", timeout=15000,
            )
            await page.wait_for_timeout(2000)
 
            current_url = page.url
            # 如果被重定向到登录页，说明未登录
            if "login" in current_url or "passport" in current_url:
                return "❌ 未登录或登录态已过期，需要重新扫码登录。"
 
            # 尝试读取用户名
            username = await page.evaluate(
                "document.querySelector('.user-name')?.textContent?.trim() || "
                "document.querySelector('.name-box .name')?.textContent?.trim() || ''"
            )
            if username:
                return f"✅ 已登录，当前用户: {username}"
            else:
                return "⚠️ 页面已加载但未读取到用户名，登录状态不确定。"
 
        except Exception as e:
            return f"[错误] check_xhs_login 失败: {e}"
 
 
# ── 启动 ───────────────────────────────────────────────────
 
if __name__ == "__main__":
    run_kwargs = {"transport": "streamable-http"}
 
    if SECRET_PATH:
        if USING_STANDALONE_FASTMCP:
            # 独立 fastmcp 包支持 path 参数
            run_kwargs["path"] = f"/{SECRET_PATH}/mcp"
        else:
            # MCP SDK 内置版不支持 path，打印警告并使用默认路径
            # 如需路径密钥，请安装独立 fastmcp 包: pip install fastmcp
            print(
                f"⚠️ 当前使用 MCP SDK 内置 FastMCP，不支持自定义 path。\n"
                f"   密钥路径 /{SECRET_PATH}/mcp 未生效！\n"
                f"   请安装独立包: pip install fastmcp"
            )
    else:
        print("⚠️ 未设置 MCP_SECRET 环境变量，MCP 端点未加密！")
 
    mcp.run(**run_kwargs)
