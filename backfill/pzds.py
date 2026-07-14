"""盼之向前爬虫 CLI

从指定页码向前翻页爬取历史在售账号, 列表→详情→解析→特征→入库。
复用 crawler.pzds 的浏览器实例 (WASM 签名必需)。
不计算价值 (交 main.py valuer_loop 补全)。

用法:
  uv run python -m backfill.pzds --game-id 303 --start-page 4 --max-pages 50
"""
from __future__ import annotations

import argparse
import logging
import sys

from crawler.pzds import crawl as crawl_pzds
from db import upsert_account
from .common import process_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="盼之向前爬虫 (历史数据回填)")
    parser.add_argument("--game-id", required=True, help="游戏ID (如 303=鸣潮)")
    parser.add_argument("--start-page", type=int, required=True, help="起始页码")
    parser.add_argument("--max-pages", type=int, required=True, help="最多翻页数")
    parser.add_argument("--page-size", type=int, default=10, help="每页条数 (默认 10)")
    parser.add_argument("--platform", default="6", help="商品分类ID (默认 6=成品号)")
    args = parser.parse_args()

    game_id = args.game_id
    start_page = args.start_page
    max_pages = args.max_pages
    page_size = args.page_size
    platform = args.platform

    logger.info("开始: game=%s start_page=%d max_pages=%d page_size=%d platform=%s",
                game_id, start_page, max_pages, page_size, platform)

    total_fetched = 0
    total_processed = 0
    pages_done = 0

    for page_offset in range(max_pages):
        current_page = start_page + page_offset
        try:
            # 翻一页 (crawl 内部会翻 max_pages 页, 这里每次传 1 页)
            accounts = crawl_pzds(game_id, platform=platform, max_pages=1,
                                  page_size=page_size, start_page=current_page)
        except Exception as e:
            logger.error("page=%d 列表 API 失败: %s, 停止", current_page, e)
            break

        if not accounts:
            logger.info("page=%d 无数据 → 结束", current_page)
            break

        processed = 0
        for acc in accounts:
            # 1. 入库 accounts 表 (UNIQUE 去重)
            upsert_account(**acc)
            # 2. 获取详情→解析→特征→入 account_details
            if process_account(acc["source"], acc["product_id"],
                               acc["game_id"], acc["price"], platform=platform):
                processed += 1

        total_fetched += len(accounts)
        total_processed += processed
        pages_done += 1

        logger.info("page=%d fetched=%d processed=%d",
                    current_page, len(accounts), processed)

        # 不足一页 → 最后一页
        if len(accounts) < page_size:
            logger.info("page=%d 不足一页 → 结束", current_page)
            break

    logger.info("完成: pages=%d, total_fetched=%d, total_processed=%d",
                pages_done, total_fetched, total_processed)


if __name__ == "__main__":
    sys.exit(main())
