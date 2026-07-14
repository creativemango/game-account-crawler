"""调试: 查看手动签名 fetch 请求的实际发送内容

对比:
  1. 我们生成的签名请求
  2. 页面真实请求 (goodsPublic/page)
找出 460 的原因
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

    # webpack 拦截
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

    # 拦截所有 api.pzds.com 请求
    captured_requests = []

    def on_request(request):
        if "api.pzds.com" in request.url and "goodsPublic/page" in request.url:
            captured_requests.append({
                "url": request.url,
                "method": request.method,
                "headers": dict(request.headers),
                "post_data": request.post_data,
            })
            logger.info("[拦截] goodsPublic/page 请求")
            logger.info("  URL: %s", request.url[:200])
            logger.info("  有 decode__: %s", "decode__" in request.url)

    page.on("request", on_request)

    logger.info("=== 导航到盼之首页 ===")
    await page.goto("https://www.pzds.com/", wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    # 1. 先导航到商品列表页, 捕获真实的 goodsPublic/page 请求
    logger.info("=== 导航到商品列表页, 捕获真实请求 ===")
    # 尝试多个 gameId, 看哪个触发 goodsPublic/page
    for game_id in ["7", "303", "1"]:
        try:
            await page.goto(f"https://www.pzds.com/goodsList/{game_id}/6",
                          wait_until="networkidle", timeout=15000)
            await page.wait_for_timeout(2000)
            if any("goodsPublic/page" in r["url"] for r in captured_requests):
                logger.info("gameId=%s 触发了 goodsPublic/page 请求", game_id)
                break
        except Exception:
            pass

    logger.info("捕获到 %d 个 goodsPublic/page 真实请求", len(captured_requests))

    # 打印真实请求的详情
    for i, req in enumerate(captured_requests):
        logger.info("--- 真实请求 %d ---", i + 1)
        logger.info("  URL: %s", req["url"][:300])
        logger.info("  有 decode__1174: %s", "decode__" in req["url"])
        sign_headers = {k: v for k, v in req["headers"].items()
                       if k.lower() in ("sign", "pztimestamp", "random", "x-sign-version",
                                         "pzversion", "skey", "deviceid", "globalid",
                                         "pzplatform", "pzos", "content-type")}
        logger.info("  签名头: %s", json.dumps(sign_headers, ensure_ascii=False, indent=4))
        if req["post_data"]:
            logger.info("  body 前200字: %s", req["post_data"][:200])

    # 2. 发送手动签名的请求
    logger.info("=== 发送手动签名请求 ===")
    body = {
        "order": "ASC", "sort": None, "page": 1, "pageSize": 10,
        "action": {"gameId": "7", "merchantMark": None, "keywords": [],
                   "searchWords": [], "searchPropertyIds": [],
                   "recommendSearchConfigIds": [], "unionGameIds": [],
                   "goodsSearchActions": [], "metas": {"single1": []},
                   "goodsCatalogueId": 6, "goodsSubCatalogueIds": [],
                   "countFlag": False, "conditionSearch": False}
    }
    body_str = json.dumps(body)

    result = await page.evaluate("""
    async ({apiUrl, bodyStr}) => {
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

        // 用 XMLHttpRequest 发请求 (避免 2.js 的 fetch hook)
        return await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', apiUrl);
            xhr.setRequestHeader('accept', 'application/json, text/plain, */*');
            xhr.setRequestHeader('content-type', 'application/json');
            xhr.setRequestHeader('deviceid', deviceid);
            if (globalid) xhr.setRequestHeader('globalid', globalid);
            xhr.setRequestHeader('pzos', 'windows');
            xhr.setRequestHeader('pzplatform', 'pc');
            xhr.setRequestHeader('pztimestamp', String(timestamp));
            xhr.setRequestHeader('pzversion', '26.710.1916');
            xhr.setRequestHeader('pzversioncode', '1');
            xhr.setRequestHeader('random', String(random));
            xhr.setRequestHeader('sign', sign);
            xhr.setRequestHeader('skey', 'CLIENT');
            xhr.withCredentials = true;
            xhr.onload = () => resolve({
                status: xhr.status,
                body: xhr.responseText,
                sign, timestamp, random,
            });
            xhr.onerror = () => reject(new Error('XHR error'));
            xhr.send(bodyStr);
        });
    }
    """, {"apiUrl": GOODS_PAGE_API, "bodyStr": body_str})

    logger.info("手动请求结果:")
    logger.info("  sign: %s", result.get("sign"))
    logger.info("  timestamp: %s", result.get("timestamp"))
    logger.info("  random: %s", result.get("random"))
    logger.info("  HTTP: %s", result.get("status"))
    logger.info("  body: %s", result.get("body", "")[:500])

    # 打印拦截到的手动请求
    if captured_requests:
        manual_req = captured_requests[-1]
        logger.info("--- 拦截到的手动请求 ---")
        logger.info("  URL: %s", manual_req["url"][:300])
        logger.info("  有 decode__1174: %s", "decode__" in manual_req["url"])
        sign_headers = {k: v for k, v in manual_req["headers"].items()
                       if k.lower() in ("sign", "pztimestamp", "random", "x-sign-version",
                                         "pzversion", "skey", "deviceid", "globalid")}
        logger.info("  签名头: %s", json.dumps(sign_headers, ensure_ascii=False, indent=4))

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
