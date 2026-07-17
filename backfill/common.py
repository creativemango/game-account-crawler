"""向前爬虫共享流程: 详情→解析→特征→入库 (不含价值)

process_account 是单条商品处理函数:
  1. 调螃蟹详情接口 (httpx)
  2. parse → ParsedAccount
  3. extract_features
  4. upsert_detail (features 填充, value/score/value_ratio = None)

不调 predict_value / compute_score, 价值评估交 main.py 的 run_valuer_loop。
"""
from __future__ import annotations

import logging

from crawler.pxb7 import fetch_detail as fetch_pxb7_detail
from parser import parse_pxb7
from valuer import extract_features
from db import get_account_id, upsert_detail

logger = logging.getLogger(__name__)



def process_account(source: str, product_id: str, game_id: str,
                    price: float, platform: str = "6") -> bool:
    """同步版: 获取螃蟹详情→解析→提取特征→入 account_details (不计算价值)
    """
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

        features = extract_features(parsed, "pxb7", price, game_id=game_id)
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




