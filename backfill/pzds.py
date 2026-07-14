"""盼之向前爬虫 CLI

从指定页码向前翻页爬取历史在售账号, 列表→详情→解析→特征→入库。
复用 crawler.pzds 的浏览器实例 (WAF + 响应监听)。
不计算价值 (交 main.py valuer_loop 补全)。

关键:
  1. 列表爬取和详情处理在同一个 async event loop 内 (避免 Playwright 跨 loop 卡死)
  2. 先爬完所有列表页数据, 再处理详情 (详情页会离开列表页, 破坏滚动翻页状态)

用法:
  uv run python -m backfill.pzds --game-id 303 --start-page 4 --max-pages 50
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from crawler.pzds import _crawl_async, _cleanup
from db import upsert_account
from .common import process_account_async

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def run(game_id: str, start_page: int, max_pages: int,
              page_size: int, platform: str) -> None:
    """异步主函数: 先爬列表, 再处理详情 (同一 loop 内)"""
    # ===== 阶段 1: 爬取所有列表页数据 =====
    all_accounts: list[dict] = []
    pages_done = 0

    for page_offset in range(max_pages):
        current_page = start_page + page_offset
        try:
            accounts = await _crawl_async(
                game_id, platform, max_pages=1,
                page_size=page_size, start_page=current_page,
            )
        except Exception as e:
            logger.error("page=%d 列表 API 失败: %s, 停止", current_page, e)
            break

        if not accounts:
            logger.info("page=%d 无数据 → 列表爬取结束", current_page)
            break

        all_accounts.extend(accounts)
        pages_done += 1
        logger.info("page=%d fetched=%d (累计 %d)",
                    current_page, len(accounts), len(all_accounts))

        if len(accounts) < page_size:
            logger.info("page=%d 不足一页 → 列表爬取结束", current_page)
            break

    logger.info("列表爬取完成: pages=%d, total_fetched=%d", pages_done, len(all_accounts))

    # ===== 阶段 2: 处理详情 =====
    total_processed = 0
    for i, acc in enumerate(all_accounts, 1):
        upsert_account(**acc)
        if await process_account_async(
            acc["source"], acc["product_id"],
            acc["game_id"], acc["price"], platform=platform,
        ):
            total_processed += 1
        if i % 10 == 0:
            logger.info("详情进度: %d/%d (成功 %d)", i, len(all_accounts), total_processed)

    logger.info("完成: pages=%d, total_fetched=%d, total_processed=%d",
                pages_done, len(all_accounts), total_processed)


def main():
    parser = argparse.ArgumentParser(description="盼之向前爬虫 (历史数据回填)")
    parser.add_argument("--game-id", required=True, help="游戏ID (如 303=鸣潮)")
    parser.add_argument("--start-page", type=int, required=True, help="起始页码")
    parser.add_argument("--max-pages", type=int, required=True, help="最多翻页数")
    parser.add_argument("--page-size", type=int, default=10, help="每页条数 (默认 10)")
    parser.add_argument("--platform", default="6", help="商品分类ID (默认 6=成品号)")
    args = parser.parse_args()

    logger.info("开始: game=%s start_page=%d max_pages=%d page_size=%d platform=%s",
                args.game_id, args.start_page, args.max_pages,
                args.page_size, args.platform)

    try:
        asyncio.run(run(args.game_id, args.start_page, args.max_pages,
                        args.page_size, args.platform))
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.exit(main())
