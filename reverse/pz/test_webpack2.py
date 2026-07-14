"""通过 webpack 拦截获取 require 函数 (改进版)

改进: 包装入口模块, 在其执行时窃取 __webpack_require__
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pzds_api import GOODS_PAGE_API

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main():
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

    # 注入 webpack 拦截脚本
    # 策略: 包装第一个被 push 的模块, 在其执行时窃取 require
    await context.add_init_script("""
    window.__webpack_require__ = null;
    window.__all_modules__ = {};
    window.__steal_done__ = false;

    let webpackJsonpValue = [];
    Object.defineProperty(window, 'webpackJsonp', {
        get() { return webpackJsonpValue; },
        set(val) {
            webpackJsonpValue = val;
            const origPush = val.push.bind(val);
            val.push = function(chunk) {
                try {
                    // chunk 格式: [chunkIds, modules] 或 [[chunkIds], modules, runtime]
                    let chunkIds, modules;
                    if (Array.isArray(chunk[0])) {
                        chunkIds = chunk[0];
                        modules = chunk[1];
                    } else {
                        chunkIds = chunk[0];
                        modules = chunk[1];
                    }

                    if (modules && typeof modules === 'object' && !window.__steal_done__) {
                        // 保存所有模块
                        Object.assign(window.__all_modules__, modules);

                        // 包装第一个模块 (入口模块) 来窃取 require
                        const ids = Object.keys(modules);
                        if (ids.length > 0) {
                            const firstId = ids[0];
                            const origMod = modules[firstId];
                            modules[firstId] = function(module, exports, __webpack_require__) {
                                if (!window.__steal_done__) {
                                    window.__webpack_require__ = __webpack_require__;
                                    window.__webpack_cache__ = __webpack_require__.c;
                                    window.__steal_done__ = true;
                                    console.log('[steal] got webpack require!');
                                }
                                // 调用原始模块函数
                                origMod(module, exports, __webpack_require__);
                            };
                        }
                    } else if (modules && typeof modules === 'object') {
                        Object.assign(window.__all_modules__, modules);
                    }
                } catch(e) {
                    console.error('[steal] error:', e);
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
    await page.wait_for_timeout(3000)

    info = await page.evaluate("""
    () => ({
        steal_done: window.__steal_done__,
        has_require: !!window.__webpack_require__,
        cache_size: window.__webpack_cache__ ? Object.keys(window.__webpack_cache__).length : 0,
        all_modules_size: Object.keys(window.__all_modules__).length,
    })
    """)
    logger.info("webpack 拦截: %s", json.dumps(info, ensure_ascii=False))

    if not info["has_require"]:
        logger.error("未获取到 webpack require")
        await browser.close()
        await pw.stop()
        return

    # 遍历所有已加载模块, 查找签名函数
    logger.info("=== 查找签名模块 ===")
    sign_result = await page.evaluate("""
    async () => {
        const cache = window.__webpack_cache__;
        const result = { found_modules: {} };

        for (const [id, mod] of Object.entries(cache)) {
            try {
                const exp = mod.exports;
                if (exp && typeof exp === 'object' && !Array.isArray(exp)) {
                    const keys = Object.keys(exp);
                    // 查找签名相关导出
                    if (keys.some(k => k.includes('WasmSign') || k.includes('SignFunction') ||
                                      k.includes('generateSign') || k.includes('invalidateConfig') ||
                                      k.includes('preloadWasm') || k.includes('ensureWasm'))) {
                        result.found_modules[id] = keys;

                        // 找到了! 测试签名生成
                        if (typeof exp.isWasmSignApi === 'function' &&
                            typeof exp.getSignFunction === 'function') {

                            result.sign_module_id = id;
                            result.is_wasm_page = exp.isWasmSignApi('/web-client/v2/public/goodsPublic/page');

                            try {
                                const signFn = await exp.getSignFunction('/web-client/v2/public/goodsPublic/page');
                                result.has_generateSign = !!signFn?.generateSign;
                                result.version = signFn?.version;

                                if (signFn?.generateSign) {
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
                    }
                }
            } catch(e) {}
        }
        return result;
    }
    """)
    logger.info("签名结果: %s", json.dumps(sign_result, ensure_ascii=False, indent=2))

    # 如果成功生成签名, 用 httpx 测试
    if sign_result.get("test_sign"):
        logger.info("=== 用生成的签名 + httpx 发请求 ===")
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

        cookies = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        # 获取 deviceid/globalid
        device_info = await page.evaluate("""
        () => {
            const result = {};
            // localStorage
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                if (k.toLowerCase().includes('device')) result.deviceid = v;
                if (k.toLowerCase().includes('global')) result.globalid = v;
            }
            result.all_keys = Object.keys(localStorage);
            return result;
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
            logger.info("响应前500字: %s", resp.text[:500])

            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", {}).get("list", [])
                logger.info("成功! 返回 %d 条商品", len(items))
                if items:
                    logger.info("第一条: %s", json.dumps(items[0], ensure_ascii=False)[:500])

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
