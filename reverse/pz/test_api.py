"""测试盼之 API 签名流程

验证:
  1. webpack 拦截获取 generateSign 函数
  2. 生成 WASM 签名 + 用页面 fetch 发请求
  3. API 返回有效数据

用法:
  cd F:\\project\\game-account-crawler
  .venv\\Scripts\\python.exe reverse\\pz\\test_api.py          # headless
  .venv\\Scripts\\python.exe reverse\\pz\\test_api.py --show   # 显示浏览器
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pzds_api import PzdsApiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    headless = "--show" not in sys.argv

    async with PzdsApiClient(headless=headless) as client:
        # 调用商品列表 API
        logger.info("=== 调用 goodsPublic/page API ===")
        try:
            data = await client.fetch_goods_page(
                game_id="7",       # 王者荣耀
                page=1,
                page_size=10,
                goods_catalogue_id=6,
            )
        except Exception as e:
            logger.error("API 调用失败: %s", e)
            return

        # 打印结果
        logger.info("=== API 响应 ===")
        logger.info("code: %s", data.get("code"))
        logger.info("info: %s", data.get("info"))

        page_data = data.get("data", {})
        total = page_data.get("total")
        total_pages = page_data.get("totalPages")
        logger.info("total: %s, totalPages: %s", total, total_pages)

        items = page_data.get("records") or []
        logger.info("返回条数: %d", len(items))

        if items:
            logger.info("=== 前3条数据 ===")
            for i, item in enumerate(items[:3]):
                logger.info("--- 商品 %d ---", i + 1)
                logger.info("  goodsNo: %s", item.get("goodsNo"))
                logger.info("  标题: %s", item.get("title"))
                logger.info("  价格: %s", item.get("price"))
                logger.info("  游戏: %s", item.get("gameIdName"))
                logger.info("  分类: %s", item.get("goodsCatalogueIdName"))

                # 打印完整 JSON (第一条)
                if i == 0:
                    logger.info("  完整JSON:\n%s",
                                json.dumps(item, ensure_ascii=False, indent=2)[:2000])
        else:
            logger.warning("无数据返回 (可能该游戏无商品)")


if __name__ == "__main__":
    asyncio.run(main())
