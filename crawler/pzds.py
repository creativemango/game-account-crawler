"""pzds.com（盼之）爬虫 — Playwright + 响应监听 + 滚动翻页

替换原 webpack 拦截 + WASM 签名方案（盼之改版后 webpack 全局变量不再暴露）。

原理:
  1. patchright 启动浏览器，打开 https://www.pzds.com/goodsList/{game_id}/{catalogue}
  2. 首次加载触发阿里云 WAF JS 挑战，刷新后通过验证
  3. 监听 goodsPublic/page 响应，捕获 JSON 数据
  4. 翻页: 滚动到底部触发下一页加载（页面自身签名逻辑工作）
  5. 详情页: 从 __NUXT__.data[0].detailsData 提取 SSR 数据
"""
import asyncio
import json
import logging
from typing import Any

import httpx

from .base import CrawlerError

logger = logging.getLogger(__name__)

API_BASE = "https://api.pzds.com"
GOODS_PAGE_API = f"{API_BASE}/api/web-client/v2/public/goodsPublic/page"
BASE_URL = "https://www.pzds.com"


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
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        # sec-ch-ua 伪装为正常 Chrome，绕过阿里云 WAF 的 HeadlessChrome 检测
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            extra_http_headers={
                "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
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
        except Exception:
            pass  # WAF 可能导致超时，忽略
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

    @property
    def is_alive(self) -> bool:
        """浏览器与页面是否仍可用"""
        return bool(
            self._page
            and not self._page.is_closed()
            and self._browser
            and self._browser.is_connected()
        )

    async def close(self):
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
        """从详情页 SSR __NUXT__ 提取商品详情

        盼之详情页是 Nuxt.js SSR 渲染，数据嵌在 window.__NUXT__.data[0].detailsData，
        无需调用 API，页面加载后直接读取。

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
        if not self._page:
            raise RuntimeError("客户端未启动，请先调用 start()")

        url = f"{BASE_URL}/goodsDetails/{goods_no}/{self._catalogue}"
        logger.info("加载详情页: %s", url)
        # domcontentloaded 足够: __NUXT__ script 在 DOM 解析阶段就注入
        # networkidle 会因页面长连接/轮询永不触发，导致 60s 超时
        await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await self._page.wait_for_timeout(1000)

        # __NUXT__ 在页面加载后被删除，但 script 标签内容保留
        # 通过 eval script 文本重建 __NUXT__ 对象，提取 detailsData
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
            raise CrawlerError(f"未提取到详情数据: {goods_no}")

        logger.info("详情获取成功: %s, price=%s", goods_no, details.get("price"))
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


async def _get_client(game_id: str, platform: str) -> PzdsApiClient:
    """获取或创建复用的 client（跨 crawl 调用复用浏览器）"""
    key = f"{game_id}:{platform}"
    client = _clients.get(key)
    if client is not None and client.is_alive:
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
) -> list[dict]:
    """异步爬取商品列表，翻页直到无数据或达到 max_pages

    Args:
        start_page: 起始页码（默认 1，回填场景可从第 N 页开始）
    """
    results: list[dict] = []
    try:
        client = await _get_client(game_id, platform)
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
) -> list[dict]:
    """爬取商品列表（同步接口，供 main.py 线程调用）

    Args:
        game_id: 盼之游戏ID（如 "303" 鸣潮）
        platform: 商品分类ID（"6"=成品号）
        max_pages: 最多翻页数
        page_size: 每页数量
        start_page: 起始页码（默认 1，回填场景可从第 N 页开始）

    Returns:
        标准化 dict 列表: [{source, game_id, product_id, title, price, raw_data}, ...]
    """
    loop = _get_loop()
    return loop.run_until_complete(
        _crawl_async(game_id, platform, max_pages, page_size, start_page=start_page)
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


import atexit
atexit.register(_cleanup)
