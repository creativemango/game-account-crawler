"""向前爬虫共享流程: 详情→解析→特征→入库 (不含价值)

process_account / process_account_async 是两源共享的单条商品处理函数:
  1. 按 source 分发调详情接口 (pxb7=httpx, pzds=浏览器)
  2. parse → ParsedAccount
  3. extract_features
  4. upsert_detail (features 填充, value/score/value_ratio = None)

不调 predict_value / compute_score, 价值评估交 main.py 的 run_valuer_loop。
"""
from __future__ import annotations

import logging

from crawler.pxb7 import fetch_detail as fetch_pxb7_detail
from parser import parse_pxb7, parse_pzds
from valuer import extract_features
from db import get_account_id, upsert_detail

logger = logging.getLogger(__name__)


async def process_account_async(source: str, product_id: str, game_id: str,
                                 price: float, platform: str = "6",
                                 detail_interval: float = 0.0,
                                 proxy: str | None = None) -> bool:
    """异步版: 获取详情→解析→提取特征→入 account_details (不计算价值)

    pzds 源在当前 event loop 内复用浏览器实例 (不跨 loop)。
    pxb7 源内部是同步 httpx 调用，此处直接执行。

    Args:
        detail_interval: 详情请求最小间隔（秒），仅 pzds 生效，0=不节流
        proxy: 代理地址 (host:port)，仅 pzds 生效，None=不使用代理

    Returns:
        True=成功, False=详情获取/解析失败
    """
    account_id = get_account_id(source, product_id)
    if not account_id:
        logger.error("未找到账号记录: %s/%s", source, product_id)
        return False

    try:
        if source == "pxb7":
            detail = fetch_pxb7_detail(product_id)
            if not detail:
                logger.warning("螃蟹详情为空: %s", product_id)
                return False
            parsed = parse_pxb7(detail).to_dict()

        elif source == "pzds":
            from crawler.pzds import _get_client
            client = await _get_client(
                game_id, platform,
                detail_interval=detail_interval, proxy=proxy,
            )
            detail = await client.fetch_goods_detail(product_id)
            parsed = parse_pzds(detail).to_dict()

        else:
            logger.error("未知 source: %s", source)
            return False

        features = extract_features(parsed, source, price)
        upsert_detail(
            account_id=account_id,
            game_id=game_id,
            source=source,
            parsed_data=parsed,
            features=features,
            value=None,
            score=None,
            value_ratio=None,
        )
        return True

    except Exception as e:
        logger.error("处理失败 %s/%s: %s", source, product_id, e)
        return False


def process_account(source: str, product_id: str, game_id: str,
                    price: float, platform: str = "6") -> bool:
    """同步版: 仅供 pxb7 使用 (pxb7 不涉及浏览器/async)

    pzds 源请用 process_account_async 在 async 上下文内调用，
    避免 Playwright page 对象跨 event loop 导致卡死。
    """
    if source == "pzds":
        raise RuntimeError("pzds 源请用 process_account_async (避免跨 loop)")

    account_id = get_account_id(source, product_id)
    if not account_id:
        logger.error("未找到账号记录: %s/%s", source, product_id)
        return False

    try:
        detail = fetch_pxb7_detail(product_id)
        if not detail:
            logger.warning("螃蟹详情为空: %s", product_id)
            return False
        parsed = parse_pxb7(detail).to_dict()

        features = extract_features(parsed, source, price)
        upsert_detail(
            account_id=account_id,
            game_id=game_id,
            source=source,
            parsed_data=parsed,
            features=features,
            value=None,
            score=None,
            value_ratio=None,
        )
        return True

    except Exception as e:
        logger.error("处理失败 %s/%s: %s", source, product_id, e)
        return False
