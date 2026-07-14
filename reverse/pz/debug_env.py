"""调试盼之页面环境: 查找 axios 实例、签名函数、全局变量

目的:
  - 确认 1.js 是否用 axios 拦截器添加 sign 等头
  - 找到可用的 axios 实例或签名函数
  - 检查页面全局对象
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pzds_api import PzdsApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main():
    headless = "--show" not in sys.argv
    async with PzdsApiClient(headless=headless) as client:
        page = client._page

        # 1. 检查 axios 全局对象
        logger.info("=== 检查 axios ===")
        axios_info = await page.evaluate("""
        () => {
            const result = {};
            result.has_axios = typeof window.axios;
            if (window.axios) {
                result.axios_keys = Object.keys(window.axios).slice(0, 20);
                result.has_interceptors = !!window.axios.interceptors;
                if (window.axios.interceptors) {
                    result.request_interceptors = window.axios.interceptors.request?.handlers?.length ?? 'N/A';
                    result.response_interceptors = window.axios.interceptors.response?.handlers?.length ?? 'N/A';
                }
            }
            // 查找 Vue 实例上的 $http 或 axios
            const app = document.querySelector('#app')?.__vue_app__;
            if (app) {
                const config = app.config;
                result.has_vue_app = true;
                result.globalProperties = Object.keys(config.globalProperties).slice(0, 30);
            }
            return result;
        }
        """)
        logger.info("axios 信息: %s", json.dumps(axios_info, ensure_ascii=False, indent=2))

        # 2. 查找 window 上的可能签名相关全局变量
        logger.info("=== 检查 window 上的签名相关变量 ===")
        sign_vars = await page.evaluate("""
        () => {
            const keys = Object.keys(window).filter(k => {
                const lower = k.toLowerCase();
                return lower.includes('sign') || lower.includes('pz') ||
                       lower.includes('encrypt') || lower.includes('crypto') ||
                       lower.includes('axios') || lower.includes('http') ||
                       lower.includes('request') || lower.includes('api');
            });
            const result = {};
            for (const k of keys) {
                try {
                    result[k] = typeof window[k];
                } catch (e) {
                    result[k] = 'error: ' + e.message;
                }
            }
            return result;
        }
        """)
        logger.info("签名相关变量: %s", json.dumps(sign_vars, ensure_ascii=False, indent=2))

        # 3. 检查 XMLHttpRequest 是否被 hook
        logger.info("=== 检查 XHR/fetch hook ===")
        hook_info = await page.evaluate("""
        () => {
            const result = {};
            // 检查 fetch 是否被 hook
            result.fetch_toString = window.fetch.toString().substring(0, 200);
            // 检查 XMLHttpRequest 是否被 hook
            result.xhr_open_toString = XMLHttpRequest.prototype.open.toString().substring(0, 200);
            result.xhr_send_toString = XMLHttpRequest.prototype.send.toString().substring(0, 200);
            result.xhr_setHeader_toString = XMLHttpRequest.prototype.setRequestHeader.toString().substring(0, 200);
            return result;
        }
        """)
        logger.info("hook 信息: %s", json.dumps(hook_info, ensure_ascii=False, indent=2))

        # 4. 监听页面自己发起的请求 (导航到商品列表页, 触发真实请求)
        logger.info("=== 监听页面真实请求 ===")
        captured_real = []

        def on_request(request):
            if "goodsPublic/page" in request.url or "api.pzds.com" in request.url:
                captured_real.append({
                    "url": request.url[:200],
                    "method": request.method,
                    "headers": dict(request.headers),
                })

        page.on("request", on_request)

        # 导航到鸣潮商品列表页 (gameId=7 是原神? 实际看 curl 是 gameId=7)
        # 根据 config.yaml, pzds 鸣潮是 303, 但 curl 里 gameId=7
        # 让页面自己发请求
        try:
            await page.goto("https://www.pzds.com/goodsList/7/6", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning("导航失败: %s", e)

        logger.info("捕获到 %d 个真实请求", len(captured_real))
        for i, req in enumerate(captured_real[:5]):
            logger.info("--- 请求 %d ---", i + 1)
            logger.info("  URL: %s", req["url"])
            logger.info("  method: %s", req["method"])
            sign_headers = {k: v for k, v in req["headers"].items()
                           if k.lower() in ("sign", "pztimestamp", "random", "x-sign-version",
                                             "pzversion", "skey", "deviceid", "globalid",
                                             "pzplatform", "pzos")}
            logger.info("  签名头: %s", json.dumps(sign_headers, ensure_ascii=False, indent=4))


if __name__ == "__main__":
    asyncio.run(main())
