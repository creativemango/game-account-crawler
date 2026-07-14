"""盼之接口签名客户端 — Playwright + webpack 拦截

原理:
  1. patchright 启动浏览器, 打开 https://www.pzds.com/goodsList/7/6
  2. webpack 拦截获取 generateSign 函数 (WASM 签名)
  3. 从页面真实请求中获取 deviceid/globalid
  4. 每次请求: 生成签名 → 用页面 fetch 发请求 (2.js 自动添加 decode__1174)
  5. 手动设置完整签名头 (sign/pztimestamp/random/x-sign-version 等)

签名流程:
  - goodsPublic/page 是 WASM 签名 API, 无法纯算
  - 通过 webpack 模块 220 的 getSignFunction 获取 generateSign
  - generateSign(bodyStr, method, timestamp, random) → sign

依赖: patchright (反检测 Playwright)
"""
from __future__ import annotations

import json
import logging
import random as py_random
import time
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://api.pzds.com"
GOODS_PAGE_API = f"{API_BASE}/api/web-client/v2/public/goodsPublic/page"
PAGE_URL = "https://www.pzds.com/goodsList/7/6"

# webpack 拦截脚本
WEBPACK_INJECT = """
// 保存原生 fetch (在 2.js hook 之前)
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

# 签名 + 请求 JS
SIGN_AND_FETCH_JS = """
async ({apiUrl, bodyStr, deviceid, globalid}) => {
    const cache = window.__webpack_cache__;

    // 找签名模块
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

    // 用页面 fetch (被 2.js hook, 会添加 decode__1174)
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
    return {
        status: resp.status,
        body: text,
        sign, timestamp, random,
        version: signFn.version,
    };
}
"""


class PzdsApiClient:
    """盼之 API 客户端: Playwright + webpack 拦截 + WASM 签名"""

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._deviceid = ""
        self._globalid = ""

    async def start(self):
        """启动浏览器, 建立 webpack 签名环境"""
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            from playwright.async_api import async_playwright
            logger.warning("patchright 未安装, 回退到 playwright")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
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

        # 注入 webpack 拦截
        await self._context.add_init_script(WEBPACK_INJECT)

        self._page = await self._context.new_page()

        # 拦截真实请求, 获取 deviceid/globalid
        real_headers = {}

        def on_request(request):
            if "goodsPublic/page" in request.url and not real_headers:
                real_headers.update(dict(request.headers))

        self._page.on("request", on_request)

        logger.info("正在加载 %s ...", PAGE_URL)
        await self._page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        await self._page.wait_for_timeout(2000)

        # 提取 deviceid/globalid
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
            """, )

        # 验证 webpack 拦截
        has_require = await self._page.evaluate("() => !!window.__webpack_require__")
        if not has_require:
            raise RuntimeError("webpack 拦截失败, 无法获取签名函数")

        logger.info(
            "签名环境就绪: deviceid=%s, globalid=%s",
            self._deviceid[:12], self._globalid[:12],
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
        game_id: str = "7",
        page: int = 1,
        page_size: int = 10,
        goods_catalogue_id: int = 6,
    ) -> dict[str, Any]:
        """调用商品列表 API

        Args:
            game_id: 游戏ID
            page: 页码
            page_size: 每页数量
            goods_catalogue_id: 商品分类ID

        Returns:
            API JSON 响应
        """
        if not self._page:
            raise RuntimeError("客户端未启动, 请先调用 start()")

        body = {
            "order": "ASC",
            "sort": None,
            "page": page,
            "pageSize": page_size,
            "action": {
                "gameId": game_id,
                "merchantMark": None,
                "keywords": [],
                "searchWords": [],
                "searchPropertyIds": [],
                "recommendSearchConfigIds": [],
                "unionGameIds": [],
                "goodsSearchActions": [],
                "metas": {"single1": []},
                "goodsCatalogueId": goods_catalogue_id,
                "goodsSubCatalogueIds": [],
                "countFlag": False,
                "conditionSearch": False,
            },
        }
        # 紧凑格式 (与 JS JSON.stringify 一致)
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
            raise RuntimeError(f"签名/请求失败: {result['error']}")

        if result["status"] != 200:
            logger.error("API 返回 %d: %s", result["status"], result["body"][:500])
            raise RuntimeError(f"API 错误: HTTP {result['status']}")

        data = json.loads(result["body"])

        records = data.get("data", {}).get("records", [])
        logger.info(
            "请求成功: page=%d, 返回 %d 条, total=%s",
            page, len(records), data.get("data", {}).get("total"),
        )
        return data

    async def fetch_api(
        self,
        path: str,
        body: dict | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        """通用 API 调用

        Args:
            path: API 路径
            body: 请求体
            method: HTTP 方法

        Returns:
            API JSON 响应
        """
        if not self._page:
            raise RuntimeError("客户端未启动, 请先调用 start()")

        url = f"{API_BASE}{path}"
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""

        # 判断是否是 WASM 签名 API
        is_wasm_api = "/goodsPublic/page" in path or "/userCenter/saveGoods" in path

        js_code = """
        async ({url, method, bodyStr, deviceid, globalid, isWasmApi}) => {
            const cache = window.__webpack_cache__;
            let signModule = null;
            for (const [id, mod] of Object.entries(cache)) {
                try {
                    const exp = mod.exports;
                    if (exp && typeof exp.getSignFunction === 'function') {
                        signModule = exp;
                        break;
                    }
                } catch(e) {}
            }

            let sign = '', timestamp = Date.now();
            let random = Math.floor(100000 + Math.random() * 900000);
            let version = 'v17';

            if (signModule && isWasmApi) {
                const signFn = await signModule.getSignFunction(url);
                if (signFn?.generateSign) {
                    sign = signFn.generateSign(bodyStr, method, String(timestamp), String(random));
                    version = signFn.version || version;
                }
            } else if (signModule) {
                // 普通 API 用 MD5 签名
                const signFn = await signModule.getSignFunction(url);
                if (signFn?.generateSign) {
                    sign = signFn.generateSign(bodyStr, method, String(timestamp), String(random));
                    version = signFn.version || version;
                }
            }

            const headers = {
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
                'skey': 'CLIENT',
            };
            if (sign) {
                headers['sign'] = sign;
                headers['x-sign-version'] = version;
            }

            const opts = {
                method: method,
                headers: headers,
                credentials: 'include',
            };
            if (bodyStr) opts.body = bodyStr;

            const resp = await fetch(url, opts);
            const text = await resp.text();
            return { status: resp.status, body: text };
        }
        """

        result = await self._page.evaluate(
            js_code,
            {
                "url": url,
                "method": method,
                "bodyStr": body_str,
                "deviceid": self._deviceid,
                "globalid": self._globalid,
                "isWasmApi": is_wasm_api,
            },
        )

        if result["status"] != 200:
            raise RuntimeError(f"API 错误: HTTP {result['status']}: {result['body'][:300]}")

        return json.loads(result["body"])
