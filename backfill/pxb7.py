"""螃蟹(pxb7)向前爬虫 CLI

按 --start-page 起始页向前翻页爬取历史在售账号数据。
职责: 列表 → upsert_account → process_account (详情→解析→特征→入库, 不含价值)

与 main.py 定时爬虫互补: main.py 固定从第 1 页爬最新, 本脚本从指定页向前
翻页补充存量数据。价值评估 (value/score/value_ratio) 留空, 交 main.py 的
run_valuer_loop 通过 get_unvalued_accounts 异步补全。

用法:
    uv run python -m backfill.pxb7 --game-id 10302 --start-page 4 --max-pages 50
"""
import argparse
import logging
import time

from crawler.base import CrawlerError
from crawler.pxb7 import crawl
from db import upsert_account
from backfill.common import process_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="螃蟹(pxb7)向前爬虫 - 从指定页码向前翻页爬取历史账号数据"
    )
    parser.add_argument("--game-id", required=True, help="游戏ID (如 10302=鸣潮)")
    parser.add_argument("--start-page", type=int, required=True, help="起始页码")
    parser.add_argument("--max-pages", type=int, required=True, help="最多翻页数 (防失控)")
    parser.add_argument("--page-size", type=int, default=16, help="每页条数 (默认 16)")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="详情请求间隔秒数 (默认 0.5, 避免触发风控)")
    args = parser.parse_args()

    game_id = args.game_id
    start_page = args.start_page
    max_pages = args.max_pages
    page_size = args.page_size
    interval = args.interval

    total_fetched = 0
    total_processed = 0
    pages_done = 0

    for offset in range(max_pages):
        current_page = start_page + offset
        try:
            # 每次只取一页, 便于按页处理和控制停止条件
            accounts = crawl(
                game_id, page_size=page_size, max_pages=1, start_page=current_page
            )
        except CrawlerError as e:
            print(f"[pxb7] page={current_page} API 错误: {e} → 终止")
            break

        fetched = len(accounts)
        if fetched == 0:
            print(f"[pxb7] page={current_page} fetched=0 → 结束")
            break

        processed = 0
        failed = 0
        for acc in accounts:
            # 1. 入库 (UNIQUE 去重, 已存在则更新价格/在售状态)
            upsert_account(**acc)
            # 2. 详情→解析→特征→入 account_details (不含价值, 交 valuer_loop 补全)
            if process_account(
                source="pxb7",
                product_id=acc["product_id"],
                game_id=acc["game_id"],
                price=acc["price"],
            ):
                processed += 1
            else:
                failed += 1
            # 节流: 避免高频请求触发风控
            if interval > 0:
                time.sleep(interval)

        total_fetched += fetched
        total_processed += processed
        pages_done += 1

        # 进度输出 (有失败时附注失败数)
        if failed:
            print(f"[pxb7] page={current_page} fetched={fetched} "
                  f"processed={processed} ({failed} detail failed)")
        else:
            print(f"[pxb7] page={current_page} fetched={fetched} processed={processed}")

        # 不足一页 → 最后一页, 处理完即停
        if fetched < page_size:
            break

    print(f"[pxb7] 完成: pages={pages_done}, total_fetched={total_fetched}, "
          f"total_processed={total_processed}")


if __name__ == "__main__":
    main()
