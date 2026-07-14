"""修复版: 添加 x-sign-version 头, 获取 globalid

对比真实请求发现缺失:
  1. x-sign-version: v17
  2. globalid (从 cookie/全局变量获取)
  3. pzos, pzplatform (可能被 2.js hook 丢失)
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
        extra_http_headers={
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )

    # webpack 拦截 + 保存原生 fetch
    await context.add_init_script("""
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
    """)

    page = await context.new_page()

    # 拦截请求, 从真实请求中获取 deviceid/globalid
    real_headers = {}

    def on_request(request):
        if "goodsPublic/page" in request.url and not real_headers:
            real_headers.update(dict(request.headers))
            real_headers["_url"] = request.url

    page.on("request", on_request)

    logger.info("=== 导航到商品列表页 ===")
    await page.goto("https://www.pzds.com/goodsList/7/6", wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(3000)

    if not real_headers:
        logger.error("未捕获到真实请求")
        return

    logger.info("真实请求头: %s", json.dumps(
        {k: v for k, v in real_headers.items() if not k.startswith("_")},
        ensure_ascii=False, indent=2
    ))

    # 提取 deviceid, globalid
    deviceid = real_headers.get("deviceid", "")
    globalid = real_headers.get("globalid", "")
    logger.info("deviceid: %s", deviceid)
    logger.info("globalid: %s", globalid)

    # 用原生 fetch + 完整签名头发请求
    logger.info("=== 发送请求 (原生 fetch + 完整头) ===")
    body = {
        "order": "ASC", "sort": None, "page": 1, "pageSize": 10,
        "action": {"gameId": "7", "merchantMark": None, "keywords": [],
                   "searchWords": [], "searchPropertyIds": [],
                   "recommendSearchConfigIds": [], "unionGameIds": [],
                   "goodsSearchActions": [], "metas": {"single1": []},
                   "goodsCatalogueId": 6, "goodsSubCatalogueIds": [],
                   "countFlag": False, "conditionSearch": False}
    }
    # 用紧凑格式 (与 JS JSON.stringify 一致)
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    result = await page.evaluate("""
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
        // 手动设置签名头
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
    """, {
        "apiUrl": GOODS_PAGE_API,
        "bodyStr": body_str,
        "deviceid": deviceid,
        "globalid": globalid,
    })

    logger.info("签名: sign=%s, ts=%s, random=%s, version=%s",
                result.get("sign"), result.get("timestamp"),
                result.get("random"), result.get("version"))
    logger.info("HTTP %d", result.get("status"))
    logger.info("响应: %s", result.get("body", "")[:1000])

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

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
