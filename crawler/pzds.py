"""pzds.com（盼之）爬虫 — Playwright + webpack 拦截 + WASM 签名

替换原 SSR HTML 解析方案，改用 API 直接获取结构化数据。

原理:
  1. patchright 启动浏览器，打开 https://www.pzds.com/goodsList/{game_id}/{catalogue}
  2. webpack 拦截获取 generateSign 函数 (WASM 签名)
  3. 从页面真实请求中获取 deviceid/globalid
  4. 每次请求: generateSign(bodyStr, 'POST', timestamp, random) → 用页面 fetch 发请求
     (2.js hook 自动添加 decode__1174 URL 参数)
  5. 手动设置完整签名头 (sign/pztimestamp/random/x-sign-version 等)

goodsPublic/page 是 WASM 签名 API，无法纯算，必须浏览器内执行。
依赖: patchright (反检测 Playwright)
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
from typing import Any

import httpx

from .base import CrawlerError

logger = logging.getLogger(__name__)

API_BASE = "https://api.pzds.com"
GOODS_PAGE_API = f"{API_BASE}/api/web-client/v2/public/goodsPublic/page"
BASE_URL = "https://www.pzds.com"

# webpack 拦截脚本 (注入到页面 init_script，在页面 JS 之前执行)
WEBPACK_INJECT = """
window.__nativeFetch__ = window.fetch.bind(window);
window.__webpack_require__ = null;
window.__steal_done__ = false;
let wpv = [];
Object.defineProperty(window, 'webpackJsonp', {
    get() { return wpv; },
    set(val) {
        wpv = val;
        const op = val.push.bind(val);
        val.push = function(chunk) {
            try {
                let modules = Array.isArray(chunk[0]) ? chunk[1] : chunk[1];
                if (modules && typeof modules === 'object' && !window.__steal_done__) {
                    const ids = Object.keys(modules);
                    if (ids.length > 0) {
                        const fid = ids[0];
                        const orig = modules[fid];
                        modules[fid] = function(m, e, r) {
                            if (!window.__steal_done__) {
                                window.__webpack_require__ = r;
                                window.__webpack_cache__ = r.c;
                                window.__steal_done__ = true;
                            }
                            orig(m, e, r);
                        };
                    }
                }
            } catch(err) {}
            return op(chunk);
        };
    },
    configurable: true,
});
"""

# 签名 + 请求 JS (在浏览器上下文执行)
SIGN_AND_FETCH_JS = """
async ({apiUrl, bodyStr, deviceid, globalid}) => {
    const cache = window.__webpack_cache__;

    let signModule = null;
    for (const [id, mod] of Object.entries(cache)) {
        try {
            const exp = mod.exports;
            if (exp && typeof exp.getSignFunction === 'function' &&
                typeof exp.isWasmSignApi === 'function') {
                signModule = exp;
                break;
            }
        } catch(e) {}
    }
    if (!signModule) return { error: '未找到签名模块' };

    const signFn = await signModule.getSignFunction('/web-client/v2/public/goodsPublic/page');
    if (!signFn?.generateSign) return { error: '无 generateSign' };

    const timestamp = Date.now();
    const random = Math.floor(100000 + Math.random() * 900000);
    const sign = signFn.generateSign(bodyStr, 'POST', String(timestamp), String(random));

    // 用页面 fetch (被 2.js hook, 会自动添加 decode__1174 URL 参数)
    const resp = await fetch(apiUrl, {
        method: 'POST',
        headers: {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'deviceid': deviceid,
            'globalid': globalid,
            'pzos': 'windows',
            'pzplatform': 'pc',
            'pztimestamp': String(timestamp),
            'pzversion': '26.710.1916',
            'pzversioncode': '1',
            'random': String(random),
            'sign': sign,
            'skey': 'CLIENT',
            'x-sign-version': signFn.version || 'v17',
        },
        body: bodyStr,
        credentials: 'include',
    });

    const text = await resp.text();
    return { status: resp.status, body: text };
}
"""


class PzdsApiClient:
    """盼之 API 客户端: Playwright + webpack 拦截 + WASM 签名

    启动浏览器加载商品列表页，通过 webpack 拦截窃取签名模块，
    每次请求生成 WASM 签名并用页面 fetch 发送（2.js 自动添加 decode__1174）。
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
        self._deviceid = ""
        self._globalid = ""

    @property
    def page_url(self) -> str:
        return f"{BASE_URL}/goodsList/{self._game_id}/{self._catalogue}"

    async def start(self):
        """启动浏览器，建立 webpack 签名环境"""
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

        # 注入 webpack 拦截脚本（在每个页面加载前执行）
        await self._context.add_init_script(WEBPACK_INJECT)
        self._page = await self._context.new_page()

        # 拦截页面真实请求，提取 deviceid/globalid
        real_headers: dict[str, str] = {}

        def on_request(request):
            if "goodsPublic/page" in request.url and not real_headers:
                real_headers.update(dict(request.headers))

        self._page.on("request", on_request)

        logger.info("正在加载 %s ...", self.page_url)
        await self._page.goto(self.page_url, wait_until="networkidle", timeout=60000)
        await self._page.wait_for_timeout(2000)

        self._deviceid = real_headers.get("deviceid", "")
        self._globalid = real_headers.get("globalid", "")

        if not self._deviceid:
            # fallback: 从 localStorage 获取
            self._deviceid = await self._page.evaluate("""
            () => {
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    if (k.toLowerCase().includes('device')) return localStorage.getItem(k);
                }
                return '';
            }
            """)

        # 验证 webpack 拦截是否成功
        has_require = await self._page.evaluate("() => !!window.__webpack_require__")
        if not has_require:
            raise RuntimeError("webpack 拦截失败，无法获取签名函数")

        logger.info(
            "签名环境就绪: deviceid=%s, globalid=%s",
            self._deviceid[:12], self._globalid[:12],
        )

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
        """调用商品列表 API

        Args:
            page: 页码
            page_size: 每页数量

        Returns:
            API JSON 响应（含 data.records 列表）
        """
        if not self._page:
            raise RuntimeError("客户端未启动，请先调用 start()")

        body = {
            "order": "ASC",
            "sort": None,
            "page": page,
            "pageSize": page_size,
            "action": {
                "gameId": self._game_id,
                "merchantMark": None,
                "keywords": [],
                "searchWords": [],
                "searchPropertyIds": [],
                "recommendSearchConfigIds": [],
                "unionGameIds": [],
                "goodsSearchActions": [],
                "metas": {"single1": []},
                "goodsCatalogueId": self._catalogue,
                "goodsSubCatalogueIds": [],
                "countFlag": False,
                "conditionSearch": False,
            },
        }
        # 紧凑格式（与 JS JSON.stringify 一致）
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        result = await self._page.evaluate(
            SIGN_AND_FETCH_JS,
            {
                "apiUrl": GOODS_PAGE_API,
                "bodyStr": body_str,
                "deviceid": self._deviceid,
                "globalid": self._globalid,
            },
        )

        if result.get("error"):
            raise CrawlerError(f"签名/请求失败: {result['error']}")

        if result["status"] != 200:
            logger.error("API 返回 %d: %s", result["status"], result["body"][:500])
            raise CrawlerError(f"API 错误: HTTP {result['status']}")

        data = json.loads(result["body"])
        records = data.get("data", {}).get("records", [])
        logger.info(
            "请求成功: page=%d, 返回 %d 条, total=%s",
            page, len(records), data.get("data", {}).get("total"),
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
        await self._page.goto(url, wait_until="networkidle", timeout=60000)
        await self._page.wait_for_timeout(1500)

        details = await self._page.evaluate("""() => {
            try {
                const nuxt = window.__NUXT__;
                if (!nuxt || !nuxt.data || !nuxt.data[0]) return null;
                return nuxt.data[0].detailsData || null;
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
    """进程退出时关闭所有浏览器"""
    global _loop
    loop = _loop
    if loop is None or loop.is_closed():
        return
    for client in _clients.values():
        try:
            loop.run_until_complete(client.close())
        except Exception:
            pass
    _clients.clear()
    try:
        loop.close()
    except Exception:
        pass


atexit.register(_cleanup)


def check_detail(product_id: str, platform: str = "6") -> bool:
    """检查商品详情页，返回 True=在售, False=已售出

    保留 SSR HTML 方式（httpx 直接请求详情页，检查"已出售"标记）。
    """
    with httpx.Client(timeout=15, trust_env=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }) as client:
        try:
            resp = client.get(f"{BASE_URL}/goodsDetails/{product_id}/{platform}")
            return "已出售" not in resp.text
        except Exception:
            return True  # 异常时保留在售状态，下次再查
