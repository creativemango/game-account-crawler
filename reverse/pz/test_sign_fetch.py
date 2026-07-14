"""验证: webpack 生成的签名 + 浏览器 fetch 发请求

方案:
  1. webpack 拦截获取签名模块
  2. 调用 generateSign 生成 sign/timestamp/random
  3. 用 fetch 发请求, 手动设置签名头 (不依赖 1.js/2.js 的 hook)
  4. 验证 API 返回有效数据
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

    # 注入 webpack 拦截
    await context.add_init_script("""
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
    """)

    page = await context.new_page()
    logger.info("=== 导航到盼之首页 ===")
    await page.goto("https://www.pzds.com/", wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    if not await page.evaluate("() => !!window.__webpack_require__"):
        logger.error("未获取到 webpack require")
        return

    logger.info("webpack require 获取成功")

    # 在浏览器中: 生成签名 + 用 fetch 发请求
    logger.info("=== 生成签名并发请求 ===")
    result = await page.evaluate("""
    async ({apiUrl, bodyStr}) => {
        const cache = window.__webpack_cache__;

        // 1. 找到签名模块
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

        // 2. 生成签名
        const isWasm = signModule.isWasmSignApi('/web-client/v2/public/goodsPublic/page');
        const signFn = await signModule.getSignFunction('/web-client/v2/public/goodsPublic/page');

        if (!signFn?.generateSign) return { error: '无 generateSign', signFn };

        const timestamp = Date.now();
        const random = Math.floor(100000 + Math.random() * 900000);
        const sign = signFn.generateSign(bodyStr, 'POST', String(timestamp), String(random));

        // 3. 获取 deviceid
        let deviceid = '';
        let globalid = '';
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                if (k.toLowerCase().includes('device')) deviceid = v;
                if (k.toLowerCase().includes('global')) globalid = v;
            }
        } catch(e) {}

        // 4. 用 fetch 发请求, 手动设置签名头
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
            },
            body: bodyStr,
            credentials: 'include',
        });

        const text = await resp.text();
        return {
            status: resp.status,
            body: text,
            sign_info: {
                sign, timestamp, random, version: signFn.version, isWasm,
                deviceid, globalid,
            },
        };
    }
    """, {"apiUrl": GOODS_PAGE_API, "bodyStr": json.dumps({
        "order": "ASC", "sort": None, "page": 1, "pageSize": 10,
        "action": {"gameId": "7", "merchantMark": None, "keywords": [],
                   "searchWords": [], "searchPropertyIds": [],
                   "recommendSearchConfigIds": [], "unionGameIds": [],
                   "goodsSearchActions": [], "metas": {"single1": []},
                   "goodsCatalogueId": 6, "goodsSubCatalogueIds": [],
                   "countFlag": False, "conditionSearch": False}
    })})

    logger.info("签名信息: %s", json.dumps(result.get("sign_info", {}), ensure_ascii=False, indent=2))
    logger.info("HTTP %d", result.get("status"))
    logger.info("响应前1000字: %s", result.get("body", "")[:1000])

    if result.get("status") == 200:
        try:
            data = json.loads(result["body"])
            items = data.get("data", {}).get("list", [])
            logger.info("=== 成功! 返回 %d 条商品 ===", len(items))
            for i, item in enumerate(items[:3]):
                logger.info("--- 商品 %d ---", i + 1)
                logger.info("  ID: %s", item.get("goodsId") or item.get("id"))
                logger.info("  标题: %s", item.get("goodsTitle") or item.get("title"))
                logger.info("  价格: %s", item.get("goodsPrice") or item.get("price"))
        except Exception as e:
            logger.error("JSON 解析失败: %s", e)
    else:
        logger.error("请求失败")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
