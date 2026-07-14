"""向前爬虫共享流程: 详情→解析→特征→入库 (不含价值)

process_account 是两源共享的单条商品处理函数:
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


def process_account(source: str, product_id: str, game_id: str,
                    price: float, platform: str = "6") -> bool:
    """获取详情→解析→提取特征→入 account_details (不计算价值)

    Args:
        source: "pxb7" 或 "pzds"
        product_id: 商品ID (螃蟹为纯数字 productId, 盼之为 goodsNo 如 "MC17DN")
        game_id: 游戏ID
        price: 实际价格 (用于 extract_features 的训练标签)
        platform: 盼之商品分类ID (默认 "6"=成品号, 螃蟹不使用)

    Returns:
        True=成功, False=详情获取/解析失败
    """
    # 1. 获取 account_id (upsert_account 已在外部调用, 这里只查 id)
    account_id = get_account_id(source, product_id)
    if not account_id:
        logger.error("未找到账号记录: %s/%s", source, product_id)
        return False

    try:
        # 2. 按 source 分发调详情接口
        if source == "pxb7":
            detail = fetch_pxb7_detail(product_id)
            if not detail:
                logger.warning("螃蟹详情为空: %s", product_id)
                return False
            parsed = parse_pxb7(detail).to_dict()

        elif source == "pzds":
            # 盼之详情需要浏览器, 复用 crawler.pzds 的浏览器实例
            import asyncio
            from crawler.pzds import _get_client, _get_loop
            loop = _get_loop()
            client = asyncio.run_coroutine_threadsafe(
                _get_client(game_id, platform), loop
            ).result(timeout=60)
            detail = asyncio.run_coroutine_threadsafe(
                client.fetch_goods_detail(product_id), loop
            ).result(timeout=60)
            parsed = parse_pzds(detail).to_dict()

        else:
            logger.error("未知 source: %s", source)
            return False

        # 3. 提取特征 (含 price/log_price 训练标签)
        features = extract_features(parsed, source, price)

        # 4. 入库 (value/score/value_ratio 留 None, 交 valuer_loop 补全)
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
