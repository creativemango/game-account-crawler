"""提取 WASM 签名相关配置和文件 URL

目的: 从页面运行时提取 WASM 配置, 下载 WASM 二进制和 glue JS
后续可用 Node.js 本地执行签名, 脱离浏览器

只做一次信息收集, 后续签名不再需要浏览器
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

OUT_DIR = Path(__file__).parent / "wasm_files"
OUT_DIR.mkdir(exist_ok=True)


async def main():
    async with PzdsApiClient(headless=True) as client:
        page = client._page

        # 1. 提取模块 232(f) 和 230 的配置
        logger.info("=== 提取环境配置 ===")
        env_config = await page.evaluate("""
        () => {
            // 尝试从 webpack 模块缓存中获取配置
            const result = {};

            // 方法1: 拦截 fetch 请求, 找 wasm config URL
            // 方法2: 从已加载的模块中提取
            // 尝试通过 webpackRequire 访问模块
            const chunks = Object.keys(window).filter(k =>
                k.includes('webpack') || k.includes('chunk')
            );
            result.webpack_keys = chunks;

            // 查找所有 script 标签的 src
            result.scripts = Array.from(document.querySelectorAll('script[src]'))
                .map(s => s.src);

            return result;
        }
        """)
        logger.info("环境配置: %s", json.dumps(env_config, ensure_ascii=False, indent=2))

        # 2. 拦截所有 wasm 相关的网络请求
        wasm_urls = []
        config_urls = []

        async def on_response(response):
            url = response.url
            if ".wasm" in url or "wasm" in url.lower():
                wasm_urls.append(url)
                logger.info("发现 WASM 请求: %s", url)
            if "config" in url.lower() and ("wasm" in url.lower() or "sign" in url.lower()):
                config_urls.append(url)
                logger.info("发现配置请求: %s", url)
                try:
                    body = await response.text()
                    logger.info("配置内容: %s", body[:1000])
                    # 保存配置
                    (OUT_DIR / "wasm_config.json").write_text(body, encoding="utf-8")
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # 3. 导航到商品列表页, 触发 WASM 加载
        logger.info("=== 导航到商品列表页触发 WASM 加载 ===")
        try:
            await page.goto("https://www.pzds.com/goodsList/7/6",
                          wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning("导航失败: %s", e)

        # 4. 从页面上下文提取已加载的 WASM 配置
        logger.info("=== 提取已加载的 WASM 配置 ===")
        wasm_info = await page.evaluate("""
        async () => {
            const result = {};

            // 尝试访问 webpack 模块缓存
            // webpack 通常存储在 window.webpackJsonp 或 __webpack_modules__
            try {
                if (window.webpackJsonp) {
                    result.has_webpackJsonp = true;
                }
            } catch(e) {}

            // 查找 WebAssembly.Module 实例
            result.has_WebAssembly = typeof WebAssembly;

            // 查找 performance entries 中的 wasm 请求
            const entries = performance.getEntriesByType('resource');
            result.wasm_resources = entries
                .filter(e => e.name.includes('.wasm') || e.name.includes('wasm'))
                .map(e => e.name);

            result.all_api_resources = entries
                .filter(e => e.name.includes('api.pzds.com') || e.name.includes('pzds.com'))
                .filter(e => e.name.endsWith('.js') || e.name.endsWith('.wasm') || e.name.endsWith('.json'))
                .map(e => e.name)
                .slice(0, 30);

            return result;
        }
        """)
        logger.info("WASM 信息: %s", json.dumps(wasm_info, ensure_ascii=False, indent=2))

        # 5. 尝试直接从页面内部调用 getSignFunction
        logger.info("=== 尝试提取 generate_sign 函数 ===")
        sign_test = await page.evaluate("""
        async () => {
            try {
                // 尝试通过 webpack 内部模块系统访问
                // 查找 webpack 的 require 函数
                let webpackRequire = null;

                // 方法: 通过 webpack chunk 拦截
                if (window.webpackJsonp) {
                    const push = window.webpackJsonp.push;
                    // webpackJsonp 的格式是 [[chunkId], modules]
                    // 或 [[chunkId, moreModules], ...]
                }

                return {
                    success: false,
                    message: "需要通过其他方式提取",
                    has_webpackJsonp: !!window.webpackJsonp,
                    webpackJsonp_type: typeof window.webpackJsonp,
                    webpackJsonp_length: window.webpackJsonp?.length,
                };
            } catch (e) {
                return { error: e.message };
            }
        }
        """)
        logger.info("签名函数提取: %s", json.dumps(sign_test, ensure_ascii=False, indent=2))

        # 6. 列出所有捕获的 URL
        logger.info("=== 捕获汇总 ===")
        logger.info("WASM URLs: %s", wasm_urls)
        logger.info("Config URLs: %s", config_urls)

        # 7. 从 performance entries 下载所有相关文件
        logger.info("=== 下载相关文件 ===")
        resources = await page.evaluate("""
        () => {
            const entries = performance.getEntriesByType('resource');
            return entries
                .filter(e => e.name.includes('.wasm') ||
                             (e.name.includes('.js') && e.name.includes('sign')) ||
                             (e.name.includes('.js') && e.name.includes('wasm')))
                .map(e => e.name);
        }
        """)
        logger.info("相关资源: %s", json.dumps(resources, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
