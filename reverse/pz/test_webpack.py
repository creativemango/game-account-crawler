"""通过 webpack 拦截获取内部签名函数和 HTTP 客户端

目标:
  1. 获取 webpack require 函数
  2. 调用签名模块生成 sign（不通过 fetch/XHR）
  3. 验证 sign 是否有效
  4. 如果成功, 后续可用 Node.js/Python 纯算 + WASM 执行
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pzds_api import PzdsApiClient, GOODS_PAGE_API

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main():
    # 在页面加载前注入 webpack 拦截脚本
    # 用 add_init_script 在页面上下文执行
    from patchright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        locale="zh-CN",
    )

    # 注入 webpack 拦截脚本 (在所有页面脚本之前执行)
    await context.add_init_script("""
    window.__webpack_require__ = null;
    window.__webpack_modules__ = null;

    // 拦截 webpackJsonp.push
    const origDefine = Object.defineProperty;
    let webpackJsonpValue = [];

    // 监听 webpackJsonp 的设置
    Object.defineProperty(window, 'webpackJsonp', {
        get() { return webpackJsonpValue; },
        set(val) {
            webpackJsonpValue = val;
            const origPush = val.push.bind(val);
            val.push = function(chunk) {
                // chunk 格式: [chunkIds, modules, runtime?]
                // 注入窃取模块
                try {
                    const modules = Array.isArray(chunk[0]) ? chunk[1] : chunk[1];
                    if (modules && typeof modules === 'object') {
                        // 保存所有模块
                        if (!window.__all_modules__) {
                            window.__all_modules__ = {};
                        }
                        Object.assign(window.__all_modules__, modules);

                        // 注入窃取 require 的模块
                        const stealId = 99999;
                        modules[stealId] = function(module, exports, __webpack_require__) {
                            window.__webpack_require__ = __webpack_require__;
                            // 同时保存模块缓存
                            window.__webpack_cache__ = __webpack_require__.c;
                        };
                    }
                } catch(e) {
                    console.error('webpack intercept error:', e);
                }
                return origPush(chunk);
            };
        },
        configurable: true,
    });
    """)

    page = await context.new_page()

    logger.info("=== 导航到盼之首页 ===")
    await page.goto("https://www.pzds.com/", wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    # 检查是否成功拦截 webpack
    info = await page.evaluate("""
    () => ({
        has_require: !!window.__webpack_require__,
        has_cache: !!window.__webpack_cache__,
        cache_size: window.__webpack_cache__ ? Object.keys(window.__webpack_cache__).length : 0,
        all_modules_size: window.__all_modules__ ? Object.keys(window.__all_modules__).length : 0,
    })
    """)
    logger.info("webpack 拦截: %s", json.dumps(info, ensure_ascii=False))

    if not info["has_require"]:
        logger.error("未获取到 webpack require")
        await browser.close()
        await pw.stop()
        return

    # 获取所有模块 ID
    module_ids = await page.evaluate("""
    () => Object.keys(window.__all_modules__ || {}).map(Number).sort((a,b) => a-b)
    """)
    logger.info("模块 IDs (%d 个): %s", len(module_ids), module_ids[:50])

    # 查找签名相关模块
    # 从 1.js 分析: 模块 190 导出 sign 相关函数
    # isWasmSignApi, getSignFunction 在模块中
    sign_test = await page.evaluate("""
    async () => {
        const require = window.__webpack_require__;
        const cache = window.__webpack_cache__;
        const result = {};

        // 尝试加载各模块, 查找签名函数
        const moduleIds = Object.keys(window.__all_modules__ || {}).map(Number);

        // 查找导出 isWasmSignApi 的模块
        for (const id of moduleIds) {
            try {
                if (cache[id]) {
                    const mod = cache[id].exports;
                    if (mod && typeof mod === 'object') {
                        const keys = Object.keys(mod);
                        if (keys.some(k => k.includes('WasmSign') || k.includes('SignFunction') ||
                                          k.includes('generateSign') || k.includes('invalidateConfig'))) {
                            result['sign_module_' + id] = keys;
                        }
                    }
                }
            } catch(e) {}
        }

        // 尝试直接调用 require 加载签名模块
        // 从 1.js: isWasmSignApi 在模块 190 的 u, getSignFunction 在 N
        // 这些导出在 webpack 中可能是模块 189 或 190
        for (const id of [188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200]) {
            try {
                const mod = require(id);
                if (mod && typeof mod === 'object') {
                    const keys = Object.keys(mod);
                    if (keys.length > 0 && keys.length < 20) {
                        result['mod_' + id] = keys;
                    }
                }
            } catch(e) {}
        }

        return result;
    }
    """)
    logger.info("签名模块: %s", json.dumps(sign_test, ensure_ascii=False, indent=2))

    # 尝试直接生成签名
    logger.info("=== 尝试生成签名 ===")
    sign_result = await page.evaluate("""
    async () => {
        const require = window.__webpack_require__;
        const cache = window.__webpack_cache__;
        const result = {};

        // 遍历所有已加载模块, 查找 isWasmSignApi 和 getSignFunction
        for (const [id, mod] of Object.entries(cache)) {
            try {
                const exp = mod.exports;
                if (exp && typeof exp === 'object') {
                    if (typeof exp.isWasmSignApi === 'function' ||
                        typeof exp.getSignFunction === 'function') {
                        result.found_in = id;
                        result.keys = Object.keys(exp);

                        // 测试 isWasmSignApi
                        if (typeof exp.isWasmSignApi === 'function') {
                            result.is_wasm_page = exp.isWasmSignApi('/web-client/v2/public/goodsPublic/page');
                        }

                        // 测试 getSignFunction
                        if (typeof exp.getSignFunction === 'function') {
                            try {
                                const signFn = await exp.getSignFunction('/web-client/v2/public/goodsPublic/page');
                                result.has_generateSign = !!signFn?.generateSign;
                                result.version = signFn?.version;

                                if (signFn?.generateSign) {
                                    // 生成测试签名
                                    const body = JSON.stringify({
                                        order: "ASC", sort: null, page: 1, pageSize: 10,
                                        action: { gameId: "7", merchantMark: null, keywords: [],
                                                  searchWords: [], searchPropertyIds: [],
                                                  recommendSearchConfigIds: [], unionGameIds: [],
                                                  goodsSearchActions: [], metas: { single1: [] },
                                                  goodsCatalogueId: 6, goodsSubCatalogueIds: [],
                                                  countFlag: false, conditionSearch: false }
                                    });
                                    const timestamp = Date.now();
                                    const random = Math.floor(100000 + Math.random() * 900000);
                                    const sign = signFn.generateSign(body, 'POST', String(timestamp), String(random));
                                    result.test_sign = sign;
                                    result.test_timestamp = timestamp;
                                    result.test_random = random;
                                }
                            } catch(e) {
                                result.getSignFunction_error = e.message;
                            }
                        }
                        break;
                    }
                }
            } catch(e) {}
        }

        return result;
    }
    """)
    logger.info("签名结果: %s", json.dumps(sign_result, ensure_ascii=False, indent=2))

    # 如果成功生成签名, 用 httpx 发请求测试
    if sign_result.get("test_sign"):
        logger.info("=== 用生成的签名发请求 ===")
        import httpx

        body = {
            "order": "ASC", "sort": None, "page": 1, "pageSize": 10,
            "action": {"gameId": "7", "merchantMark": None, "keywords": [],
                       "searchWords": [], "searchPropertyIds": [],
                       "recommendSearchConfigIds": [], "unionGameIds": [],
                       "goodsSearchActions": [], "metas": {"single1": []},
                       "goodsCatalogueId": 6, "goodsSubCatalogueIds": [],
                       "countFlag": False, "conditionSearch": False}
        }

        # 从浏览器获取 cookie 和 deviceid
        cookies = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        # 获取 deviceid/globalid (从页面或生成)
        device_info = await page.evaluate("""
        () => {
            // 从 localStorage 获取 deviceid
            let deviceid = '';
            let globalid = '';
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    if (k.toLowerCase().includes('device')) deviceid = localStorage.getItem(k);
                    if (k.toLowerCase().includes('global')) globalid = localStorage.getItem(k);
                }
            } catch(e) {}
            return { deviceid, globalid, localStorage_keys: Object.keys(localStorage) };
        }
        """)
        logger.info("设备信息: %s", json.dumps(device_info, ensure_ascii=False, indent=2))

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "cookie": cookie_str,
            "deviceid": device_info.get("deviceid", ""),
            "globalid": device_info.get("globalid", ""),
            "origin": "https://www.pzds.com",
            "referer": "https://www.pzds.com/",
            "pzos": "windows",
            "pzplatform": "pc",
            "pztimestamp": str(sign_result["test_timestamp"]),
            "pzversion": "26.710.1916",
            "pzversioncode": "1",
            "random": str(sign_result["test_random"]),
            "sign": sign_result["test_sign"],
            "skey": "CLIENT",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(GOODS_PAGE_API, json=body, headers=headers)
            logger.info("HTTP %d", resp.status_code)
            logger.info("响应: %s", resp.text[:500])

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
