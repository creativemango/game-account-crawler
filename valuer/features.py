"""特征提取: 从 ParsedAccount 提取数值特征向量

特征维度 (共 28 维):
  基础数值 (7): yellow, level, star_sounds, fuujin_waves, zhuchao_waves, yubo_coral, total_pulls
  角色命座分布 (8): c0~c6 数量 + four_star_full_const 数量
  武器精炼分布 (6): r1~r5 数量 + high_refine_count (精3+)
  稀有度指标 (4): team_count, hot_char_count, skin_count, five_star_char_count
  来源 (1): source_pzds (当前仅为螃蟹, 保留用于多源扩展)
  价格 (1): price (训练用标签)
  对数价格 (1): log_price (训练用标签, 长尾分布稳定训练)
"""
from __future__ import annotations

import math
from typing import Any

# 鸣潮热门角色 (市场上高需求, 价值溢价)
HOT_CHARS_WUWA = {
    "今汐", "守岸人", "椿", "卡卡罗", "爱弥斯", "长离",
    "吟霖", "折枝", "相里要", "珂莱塔", "菲比", "赞妮",
    "夏空", "卡提希娅", "奥古斯塔", "弗洛洛", "坎特蕾拉",
    "嘉贝莉娜", "鉴心", "凌阳",
}

# 原神热门角色 (市场上高需求, 价值溢价)
HOT_CHARS_GENSHIN = {
    "芙宁娜", "那维莱特", "夜兰", "纳西妲", "雷电将军", "钟离",
    "枫原万叶", "阿蕾奇诺", "克洛琳德", "千织", "娜维娅",
    "闲云", "艾尔海森", "流浪者", "神里绫华", "胡桃", "甘雨",
    "八重神子", "珊瑚宫心海", "申鹤", "妮露", "赛诺",
    "白术", "林尼", "莱欧斯利", "希格雯", "调香师",
}

# 游戏 ID 到热门角色集的映射
GAME_HOT_CHARS = {
    "10302": HOT_CHARS_WUWA,
    "10026": HOT_CHARS_GENSHIN,
}

# 特征名 (不含 price/log_price, 这两个是训练标签)
FEATURE_NAMES: list[str] = [
    # 基础数值 (7)
    "yellow", "level", "star_sounds", "fuujin_waves",
    "zhuchao_waves", "yubo_coral", "total_pulls",
    # 角色命座分布 (8)
    "c0_count", "c1_count", "c2_count", "c3_count",
    "c4_count", "c5_count", "c6_count", "four_star_full_count",
    # 武器精炼分布 (6)
    "r1_count", "r2_count", "r3_count", "r4_count", "r5_count",
    "high_refine_count",
    # 稀有度指标 (4)
    "team_count", "hot_char_count", "skin_count", "five_star_char_count",
    # 来源 (1)
    "source_pzds",
]

# 训练最小样本数 (少于此数不训练, 留空待回填)
MIN_SAMPLES = 200


def extract_features(
    parsed: dict | Any,
    source: str = "",
    price: float = 0.0,
    game_id: str = "",
) -> dict[str, float]:
    """从 ParsedAccount.dict 或 dict 提取特征

    Args:
        parsed: ParsedAccount.to_dict() 的结果, 或 ParsedAccount 对象
        source: 数据来源 ("pxb7")
        price: 实际价格 (训练时用作标签)
        game_id: 游戏 ID (如 "10302"=鸣潮, "10026"=原神)

    Returns:
        特征 dict, 包含 FEATURE_NAMES 中的所有特征 + price + log_price
    """
    hot_chars = GAME_HOT_CHARS.get(game_id, HOT_CHARS_WUWA)
    # 接受 ParsedAccount 对象或 dict
    if hasattr(parsed, "to_dict"):
        d = parsed.to_dict()
    else:
        d = parsed

    # 基础数值
    yellow = int(d.get("yellow") or 0)
    level = int(d.get("level") or 0)
    star_sounds = int(d.get("star_sounds") or 0)
    fuujin = int(d.get("fuujin_waves") or 0)
    zhuchao = int(d.get("zhuchao_waves") or 0)
    yubo = int(d.get("yubo_coral") or 0)
    total_pulls = float(d.get("total_pulls") or 0.0)

    # 角色命座分布
    five_star_chars = d.get("five_star_chars") or []
    four_star_chars = d.get("four_star_chars") or []

    c_counts = [0] * 7  # c0~c6
    hot_count = 0
    for c in five_star_chars:
        const = int(c.get("constellation") or 0)
        name = c.get("name", "")
        if const <= 6:
            c_counts[const] += 1
        if name in hot_chars:
            hot_count += 1

    four_star_full = sum(
        1 for c in four_star_chars if int(c.get("constellation") or 0) >= 6
    )

    # 武器精炼分布
    five_star_weapons = d.get("five_star_weapons") or []
    # 也统计角色绑定的武器
    for c in five_star_chars:
        wr = c.get("weapon_refine")
        if wr:
            five_star_weapons.append({"refine": wr})

    r_counts = [0] * 5  # r1~r5
    high_refine = 0
    for w in five_star_weapons:
        refine = int(w.get("refine") or 1)
        if 1 <= refine <= 5:
            r_counts[refine - 1] += 1
        if refine >= 3:
            high_refine += 1

    # 稀有度指标
    teams = d.get("teams") or []
    skins = d.get("skins") or []

    features = {
        "yellow": float(yellow),
        "level": float(level),
        "star_sounds": float(star_sounds),
        "fuujin_waves": float(fuujin),
        "zhuchao_waves": float(zhuchao),
        "yubo_coral": float(yubo),
        "total_pulls": total_pulls,
        "c0_count": float(c_counts[0]),
        "c1_count": float(c_counts[1]),
        "c2_count": float(c_counts[2]),
        "c3_count": float(c_counts[3]),
        "c4_count": float(c_counts[4]),
        "c5_count": float(c_counts[5]),
        "c6_count": float(c_counts[6]),
        "four_star_full_count": float(four_star_full),
        "r1_count": float(r_counts[0]),
        "r2_count": float(r_counts[1]),
        "r3_count": float(r_counts[2]),
        "r4_count": float(r_counts[3]),
        "r5_count": float(r_counts[4]),
        "high_refine_count": float(high_refine),
        "team_count": float(len(teams)),
        "hot_char_count": float(hot_count),
        "skin_count": float(len(skins)),
        "five_star_char_count": float(len(five_star_chars)),
        "source_pzds": 0.0,
    }

    # 训练标签 (预测时不需要)
    if price > 0:
        features["price"] = float(price)
        features["log_price"] = math.log(price)

    return features


def features_to_vector(features: dict) -> list[float]:
    """特征 dict → 固定顺序的特征向量 (只含 FEATURE_NAMES)"""
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]




