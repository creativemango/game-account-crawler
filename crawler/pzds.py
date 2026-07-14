"""pzds.com（盼之）爬虫 — Playwright (列表) + httpx+quickjs (详情)

替换原 webpack 拦截 + WASM 签名方案（盼之改版后 webpack 全局变量不再暴露）。

原理:
  1. patchright 启动浏览器，打开 https://www.pzds.com/goodsList/{game_id}/{catalogue}
  2. 首次加载触发阿里云 WAF JS 挑战，刷新后通过验证
  3. 监听 goodsPublic/page 响应，捕获 JSON 数据 (列表页)
  4. 翻页: 滚动到底部触发下一页加载（页面自身签名逻辑工作）
  5. 详情页: httpx 复用 WAF cookie 请求 HTML + quickjs 解析 __NUXT__ IIFE (不依赖 playwright)
"""
import asyncio
import atexit
import json
import logging
import re
import time
from typing import Any

import httpx
import quickjs

from .base import CrawlerError

logger = logging.getLogger(__name__)

API_BASE = "https://api.pzds.com"
GOODS_PAGE_API = f"{API_BASE}/api/web-client/v2/public/goodsPublic/page"
BASE_URL = "https://www.pzds.com"

# httpx 请求头 (与 playwright context 一致，绕过 WAF 指纹检测)
_HTTPX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


class PzdsApiClient:
    """盼之 API 客户端: Playwright + 响应监听 + 滚动翻页

    启动浏览器加载商品列表页，通过 WAF 验证后，
    监听 goodsPublic/page 响应获取数据，滚动触发翻页。
    """

    def __init__(
        self,
        headless: bool = True,
        game_id: str = "7",
        goods_catalogue_id: int = 6,
        detail_interval: float = 0.0,
        proxy: str | None = None,
    ):
        self._headless = headless
        self._game_id = game_id
        self._catalogue = goods_catalogue_id
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        # 响应监听: 最新一次 goodsPublic/page 的 JSON 响应
        self._last_json_response: dict | None = None
        self._response_event = asyncio.Event()
        # httpx client (详情页用, 复用 playwright 的 WAF cookie)
        self._httpx_client: httpx.AsyncClient | None = None
        self._httpx_cookies: dict[str, str] = {}
        # 详情请求节流: 两次请求间至少间隔 detail_interval 秒, 避免触发 WAF 频率限制
        self._detail_interval = detail_interval
        self._last_detail_time: float = 0.0
        # 代理 (host:port 格式, 同时用于 playwright 和 httpx, 避免 IP 被封)
        self._proxy = proxy

    @property
    def page_url(self) -> str:
        return f"{BASE_URL}/goodsList/{self._game_id}/{self._catalogue}"

    async def start(self):
        """启动浏览器，通过 WAF 验证，建立响应监听"""
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            from playwright.async_api import async_playwright
            logger.warning("patchright 未安装，回退到 playwright")

        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": f"http://{self._proxy}"}
            logger.info("使用代理: %s", self._proxy)
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        # sec-ch-ua 伪装为正常 Chrome，绕过阿里云 WAF 的 HeadlessChrome 检测
        context_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            "locale": "zh-CN",
            "extra_http_headers": {
                "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        }
        if self._proxy:
            # 代理 TLS 隧道证书链不完整, 忽略 HTTPS 错误
            context_kwargs["ignore_https_errors"] = True
        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()

        # 监听 goodsPublic/page 响应（放宽检查: 任何 JSON 响应都捕获）
        async def on_response(response):
            if "goodsPublic/page" in response.url:
                try:
                    body = await response.text()
                    if body.strip().startswith("{"):
                        data = json.loads(body)
                        logger.debug("goodsPublic/page 响应: keys=%s", list(data.keys())[:5])
                        self._last_json_response = data
                        self._response_event.set()
                except Exception:
                    pass

        self._page.on("response", on_response)

        # 首次加载触发 WAF JS 挑战
        logger.info("正在加载 %s ...", self.page_url)
        try:
            await self._page.goto(self.page_url, wait_until="networkidle", timeout=60000)
        except Exception as e:
            logger.debug("首次加载异常 (WAF 挑战可能导致超时, 忽略): %s", e)
        await self._page.wait_for_timeout(2000)

        # 刷新通过 WAF 验证（首次返回 WAF 挑战页，刷新后正常）
        logger.info("刷新页面通过 WAF 验证...")
        await self._page.reload(wait_until="networkidle", timeout=60000)
        await self._page.wait_for_timeout(2000)

        # 等待首个 JSON 响应
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise RuntimeError("WAF 验证失败，未捕获到 JSON 响应")

        logger.info("WAF 通过，响应监听就绪")

        # 导出 cookies 给 httpx (详情页用, 不依赖 playwright 加载)
        await self._refresh_cookies()
        httpx_kwargs = {
            "cookies": self._httpx_cookies,
            "headers": {**_HTTPX_HEADERS, "Referer": self.page_url},
            "timeout": 15,
            "trust_env": False,
            "follow_redirects": True,
        }
        if self._proxy:
            # 代理 TLS 隧道可能证书链不完整, 跳过验证 (代理本身可信)
            httpx_kwargs["proxy"] = f"http://{self._proxy}"
            httpx_kwargs["verify"] = False
        self._httpx_client = httpx.AsyncClient(**httpx_kwargs)

    @property
    def is_alive(self) -> bool:
        """浏览器与页面是否仍可用"""
        return bool(
            self._page
            and not self._page.is_closed()
            and self._browser
            and self._browser.is_connected()
        )

    async def _refresh_cookies(self):
        """从 playwright context 导出最新 cookies (WAF cookie 可能过期)"""
        if self._context:
            cookies = await self._context.cookies()
            self._httpx_cookies = {c['name']: c['value'] for c in cookies}

    async def close(self):
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def fetch_goods_page(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """获取指定页的商品列表

        通过滚动触发页面自身的分页加载，监听响应获取 JSON 数据。
        页面首次加载已是第 1 页，page>1 时滚动到底部触发后续页。

        Args:
            page: 页码（1-based）
            page_size: 每页数量（页面固定为 10，此参数仅用于校验）

        Returns:
            API JSON 响应（含 data.records 列表）
        """
        if not self._page:
            raise RuntimeError("客户端未启动，请先调用 start()")

        if page == 1:
            # 第 1 页在 start() 时已加载，直接返回缓存的响应
            if self._last_json_response:
                data = self._last_json_response
                self._last_json_response = None
                self._response_event.clear()
                return data
            # 缓存已清空，重新加载
            await self._page.goto(self.page_url, wait_until="networkidle", timeout=60000)
            await self._page.wait_for_timeout(2000)
        else:
            # 滚动到底部触发下一页加载
            self._response_event.clear()
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            # 给页面一点时间触发滚动事件和发起请求
            await self._page.wait_for_timeout(500)

        # 等待 JSON 响应
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise CrawlerError(f"等待第 {page} 页响应超时")

        if not self._last_json_response:
            raise CrawlerError(f"未捕获到第 {page} 页响应")

        data = self._last_json_response
        self._last_json_response = None
        self._response_event.clear()

        records = data.get("data", {}).get("records", [])
        current_page = data.get("data", {}).get("page", 0)
        logger.info(
            "请求成功: page=%d (期望) %d (实际), 返回 %d 条, total=%s",
            page, current_page, len(records), data.get("data", {}).get("total"),
        )
        return data

    async def fetch_goods_detail(self, goods_no: str) -> dict[str, Any]:
        """从详情页 SSR __NUXT__ 提取商品详情 (httpx+quickjs 优先, WAF 时降级 playwright)

        盼之详情页是 Nuxt.js SSR 渲染，数据嵌在 window.__NUXT__.data[0].detailsData。
        __NUXT__ 是一个 IIFE: window.__NUXT__=(function(a,b,...){...})(实参...)，
        执行后 window.__NUXT__ 会被删除，但 <script> 标签 textContent 保留原始文本。

        策略:
          1. httpx 拉 HTML + quickjs 解析 IIFE (0.3s/条, 10x 提速)
          2. WAF 频率拦截时, 降级 playwright goto 过 WAF + 提取 + 刷新 cookie
             (playwright 过 WAF 后, 后续 httpx 恢复可用)

        Args:
            goods_no: 商品编号（如 "MC17DN"）

        Returns:
            详情数据 dict，关键字段:
              - goodsNo/title/price/gameId/gameIdName
              - section1~5: 黄数/联觉等级/星声/浮金波纹/金角色数
              - metadataModel.resources: 角色/武器/服饰列表（含 cornerMark 绑定）
              - sellingPointLabels: 队伍+角色武器标签
              - subtitle: 绑定情况
        """
        if not self._httpx_client:
            raise RuntimeError("客户端未启动，请先调用 start()")

        # 节流: 保证两次详情请求间隔 >= detail_interval, 避免触发 WAF 频率限制
        if self._detail_interval > 0:
            elapsed = time.monotonic() - self._last_detail_time
            if elapsed < self._detail_interval:
                await asyncio.sleep(self._detail_interval - elapsed)
        self._last_detail_time = time.monotonic()

        url = f"{BASE_URL}/goodsDetails/{goods_no}/{self._catalogue}"

        # 优先: httpx + quickjs
        try:
            return await self._fetch_detail_httpx(goods_no, url)
        except CrawlerError as e:
            # WAF 拦截/无 __NUXT__: 降级 playwright (过 WAF + 提取 + 刷 cookie)
            logger.warning("httpx 失败, 降级 playwright: %s (%s)", goods_no, e)
            if not self._page or self._page.is_closed():
                raise CrawlerError(f"playwright 不可用, 无法降级: {goods_no}")
            details = await self._fetch_detail_playwright(goods_no, url)
            # playwright 过 WAF 后刷新 cookie, 后续 httpx 恢复可用
            await self._refresh_cookies()
            self._httpx_client.cookies.update(self._httpx_cookies)
            return details

    async def _fetch_detail_httpx(self, goods_no: str, url: str) -> dict[str, Any]:
        """httpx 请求 + quickjs 解析 __NUXT__ (WAF 拦截时抛 CrawlerError 触发降级)"""
        resp = await self._httpx_client.get(url)
        html = resp.text

        # WAF 挑战页特征: 含 aliyun_waf 或无 __NUXT__ 且内容短
        if "aliyun_waf" in html or ("__NUXT__" not in html and len(html) < 5000):
            raise CrawlerError(f"WAF 拦截: {goods_no}")

        # 正则提取所有 <script> 内容，找含 __NUXT__ 的（通常很长）
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        nuxt_script = next(
            (s for s in scripts if '__NUXT__' in s and len(s) > 1000), None
        )
        if not nuxt_script:
            raise CrawlerError(f"无 __NUXT__ 脚本: {goods_no}")

        # quickjs 沙箱执行 IIFE: 声明空 window → 执行脚本 → 取 detailsData (8ms)
        ctx = quickjs.Context()
        js = (
            "var window={};"
            + nuxt_script
            + ";JSON.stringify(window.__NUXT__.data[0].detailsData)"
        )
        try:
            result = ctx.eval(js)
        except Exception as e:
            raise CrawlerError(f"解析 __NUXT__ 失败 {goods_no}: {e}")

        if not result or result == "null":
            raise CrawlerError(f"detailsData 为空: {goods_no}")

        details = json.loads(result)
        logger.info("详情获取成功(httpx): %s, price=%s", goods_no, details.get("price"))
        return details

    async def _fetch_detail_playwright(self, goods_no: str, url: str) -> dict[str, Any]:
        """playwright goto 详情页 + page.evaluate 提取 (WAF 降级路径)

        真实浏览器能执行 WAF JS 挑战，goto 后若检测到 WAF 挑战页则 reload 通过。
        reload 后等待 3s 让 WAF JS 执行完毕, 再尝试提取 __NUXT__。
        """
        logger.info("加载详情页(playwright): %s", url)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await self._page.wait_for_timeout(1000)

        # 检测 WAF 挑战页 (含 aliyun_waf), reload 让浏览器执行 JS 通过验证
        is_waf = await self._page.evaluate(
            "() => document.body && document.body.innerHTML.includes('aliyun_waf')"
        )
        if is_waf:
            logger.info("playwright 遇到 WAF 挑战, reload 通过: %s", goods_no)
            await self._page.reload(wait_until="domcontentloaded", timeout=15000)
            # 等待 3s 让 WAF JS 挑战执行完毕设置 cookie
            await self._page.wait_for_timeout(3000)

        # __NUXT__ 加载后被删除, 通过 eval script 文本重建
        details = await self._page.evaluate("""() => {
            try {
                const scripts = Array.from(document.scripts);
                for (const s of scripts) {
                    const text = s.textContent;
                    if (text && text.includes('__NUXT__') && text.length > 1000) {
                        const fn = new Function(text + '; return window.__NUXT__;');
                        const nuxt = fn();
                        if (nuxt && nuxt.data && nuxt.data[0]) {
                            return nuxt.data[0].detailsData || null;
                        }
                    }
                }
                return null;
            } catch(e) { return null; }
        }""")

        if not details:
            raise CrawlerError(f"playwright 也未提取到详情: {goods_no}")

        logger.info("详情获取成功(playwright): %s, price=%s", goods_no, details.get("price"))
        return details


# ===== 浏览器复用 =====
# 跨多次 crawl() 调用复用浏览器实例，避免每次启动 ~5s 开销
_clients: dict[str, PzdsApiClient] = {}
_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """获取模块级 event loop（懒加载，复用）"""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


async def _get_client(
    game_id: str, platform: str, detail_interval: float = 0.0,
    proxy: str | None = None,
) -> PzdsApiClient:
    """获取或创建复用的 client（跨 crawl 调用复用浏览器）

    Args:
        detail_interval: 详情请求最小间隔（秒），0=不节流；复用时也会更新此值
        proxy: 代理地址 (host:port)，None=不使用代理
    """
    key = f"{game_id}:{platform}"
    client = _clients.get(key)
    if client is not None and client.is_alive:
        client._detail_interval = detail_interval  # 复用时更新节流配置
        return client

    # 已失效或不存在，清理后重建
    if client is not None:
        try:
            await client.close()
        except Exception:
            pass
        _clients.pop(key, None)

    client = PzdsApiClient(
        headless=True,
        game_id=game_id,
        goods_catalogue_id=int(platform),
        detail_interval=detail_interval,
        proxy=proxy,
    )
    await client.start()
    _clients[key] = client
    return client


async def _crawl_async(
    game_id: str,
    platform: str,
    max_pages: int,
    page_size: int,
    start_page: int = 1,
    proxy: str | None = None,
    detail_interval: float = 0.0,
) -> list[dict]:
    """异步爬取商品列表，翻页直到无数据或达到 max_pages

    Args:
        start_page: 起始页码（默认 1，回填场景可从第 N 页开始）
        proxy: 代理地址 (host:port)，None=不使用代理
        detail_interval: 详情请求最小间隔（秒），0=不节流
    """
    results: list[dict] = []
    try:
        client = await _get_client(
            game_id, platform, detail_interval=detail_interval, proxy=proxy,
        )
    except Exception as e:
        raise CrawlerError(f"浏览器启动失败: {e}")

    key = f"{game_id}:{platform}"
    # 从 start_page 开始翻 max_pages 页
    for page in range(start_page, start_page + max_pages):
        try:
            data = await client.fetch_goods_page(page=page, page_size=page_size)
        except Exception as e:
            logger.error("pzds page %d 失败: %s", page, e)
            # 页面/浏览器失效，清理 client 下次重建
            if not client.is_alive:
                _clients.pop(key, None)
            break

        records = data.get("data", {}).get("records", [])
        if not records:
            break

        for r in records:
            results.append({
                "source": "pzds",
                "game_id": game_id,
                "product_id": r.get("goodsNo", ""),
                "title": r.get("title", ""),
                "price": float(r.get("price", 0) or 0),
                "raw_data": r,
            })

        # 不足一页，说明已是最后一页
        if len(records) < page_size:
            break

    return results


def crawl(
    game_id: str,
    platform: str = "6",
    max_pages: int = 1,
    page_size: int = 10,
    start_page: int = 1,
    proxy: str | None = None,
    detail_interval: float = 0.0,
) -> list[dict]:
    """爬取商品列表（同步接口，供 main.py 线程调用）

    Args:
        game_id: 盼之游戏ID（如 "303" 鸣潮）
        platform: 商品分类ID（"6"=成品号）
        max_pages: 最多翻页数
        page_size: 每页数量
        start_page: 起始页码（默认 1，回填场景可从第 N 页开始）
        proxy: 代理地址 (host:port)，None=不使用代理
        detail_interval: 详情请求最小间隔（秒），0=不节流

    Returns:
        标准化 dict 列表: [{source, game_id, product_id, title, price, raw_data}, ...]
    """
    loop = _get_loop()
    return loop.run_until_complete(
        _crawl_async(game_id, platform, max_pages, page_size,
                     start_page=start_page, proxy=proxy,
                     detail_interval=detail_interval)
    )


def _cleanup():
    """清理所有缓存的浏览器实例"""
    global _loop
    if _loop and not _loop.is_closed():
        for client in _clients.values():
            try:
                _loop.run_until_complete(client.close())
            except Exception:
                pass
        _clients.clear()
        _loop.close()
        _loop = None


atexit.register(_cleanup)


def check_detail(product_id: str, platform: str = "6") -> bool:
    """检查盼之商品是否在售 (同步接口, 供 main.py ThreadPoolExecutor 调用)

    用 httpx 请求详情页 SSR, 能提取到 __NUXT__.detailsData 则视为在售。
    异常/WAF/无数据时返回 True (保留在售状态, 下次再查, 与 pxb7 行为一致)。

    Args:
        product_id: 商品编号 (goodsNo)
        platform: 商品分类ID (默认 "6")
    """
    url = f"{BASE_URL}/goodsDetails/{product_id}/{platform}"
    try:
        with httpx.Client(
            timeout=15, trust_env=False, headers=_HTTPX_HEADERS, follow_redirects=True,
        ) as client:
            resp = client.get(url)
            html = resp.text

        # WAF 挑战页或无 __NUXT__ → 可能已下架或被拦截, 保守视为在售
        if "aliyun_waf" in html or "__NUXT__" not in html:
            return True

        # 提取 __NUXT__ 脚本
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        nuxt_script = next(
            (s for s in scripts if '__NUXT__' in s and len(s) > 1000), None
        )
        if not nuxt_script:
            return True

        # quickjs 解析 detailsData, 能解析到非空数据则在售
        ctx = quickjs.Context()
        js = (
            "var window={};"
            + nuxt_script
            + ";JSON.stringify(window.__NUXT__.data[0].detailsData)"
        )
        result = ctx.eval(js)
        if not result or result == "null":
            return False  # detailsData 为空 → 已售出/下架

        return True

    except Exception:
        return True  # 异常时保留在售状态
