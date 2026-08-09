"""特征提取: 从 ParsedAccount 提取数值特征向量

特征维度:
  基础数值 (7): yellow, level, star_sounds, fuujin_waves, zhuchao_waves, yubo_coral, total_pulls
  角色命座分布 (8): c0~c6 数量 + four_star_full_const 数量
  武器精炼分布 (6): r1~r5 数量 + high_refine_count (精3+)
  稀有度指标 (4): team_count, hot_char_count, skin_count, five_star_char_count
  来源 (1): source_pzds
  高级价值指标 (5): weighted_char_value, dead_weight_count, team_synergy_bonus,
                     sig_weapon_count, key_char_high_const
"""
from __future__ import annotations

import math
from typing import Any

# ===== 原神角色价值数据库 =====
# weight: 市场价值权重 (0.0=无价值, 1.0=T0体系核心)
# 规则: 常驻角色与须弥及以前角色一律为 0；仅新角色（枫丹/纳塔及以后）计价值
# sig_weapon: 专武名称 (None=未知或无私武)


# 原神热门角色（用于 hot_char_count 后向兼容）
HOT_CHARS_GENSHIN: set[str] = {
    "玛薇卡", "丝柯克", "哥伦比娅", "茜特菈莉", "兹白", "菲林斯",
    "奈芙尔", "菈乌玛", "尼可", "桑多涅", "爱可菲",
    "芙宁娜", "莉奈亚", "杜林", "法尔伽", "恰斯卡", "希诺宁",
    "瓦雷莎", "伊涅芙", "那维莱特", "阿蕾奇诺", "基尼奇",
}

# 游戏 ID 到热门角色集的映射
GAME_HOT_CHARS = {
    "10026": HOT_CHARS_GENSHIN,
}

CHARACTER_DB_GENSHIN: dict[str, dict] = {
    # === 6.7 高价值角色（根据最新版本确认）===
    # T0 - 体系核心
    "玛薇卡":   {"weight": 1.00, "sig_weapon": "焚曜千阳"},
    "丝柯克":   {"weight": 1.00, "sig_weapon": None},
    "哥伦比娅": {"weight": 0.85, "sig_weapon": None},
    "茜特菈莉": {"weight": 0.95, "sig_weapon": "祭星者之望"},
    "兹白":     {"weight": 0.95, "sig_weapon": None},
    "菲林斯":   {"weight": 0.80, "sig_weapon": None},
    "奈芙尔":   {"weight": 1.00, "sig_weapon": None},
    "菈乌玛":   {"weight": 1.00, "sig_weapon": None},
    "尼可":     {"weight": 1.00, "sig_weapon": None},
    "桑多涅":   {"weight": 0.80, "sig_weapon": None},
    "爱可菲":   {"weight": 0.75, "sig_weapon": None},

    # T1 - 高价值
    "芙宁娜":   {"weight": 0.80, "sig_weapon": "静水流涌之辉", "max_const": 2},
    "莉奈亚":   {"weight": 0.70, "sig_weapon": None},
    "杜林":     {"weight": 0.70, "sig_weapon": None},
    "法尔伽":   {"weight": 0.65, "sig_weapon": None},
    "恰斯卡":   {"weight": 0.90, "sig_weapon": "星鹫赤羽"},
    "希诺宁":   {"weight": 0.75, "sig_weapon": "岩峰巡歌"},
    "瓦雷莎":   {"weight": 0.60, "sig_weapon": None},
    "伊涅芙":   {"weight": 0.60, "sig_weapon": None},
    "那维莱特": {"weight": 0.80, "sig_weapon": "万世流涌大典"},
    "阿蕾奇诺": {"weight": 0.90, "sig_weapon": "赤月之形"},
    "基尼奇":   {"weight": 0.60, "sig_weapon": "山王的长柄斧"},

    # T2 - 仍有价值
    "夜兰":     {"weight": 0.20, "sig_weapon": "若水"},
    "纳西妲":   {"weight": 0.00, "sig_weapon": "千夜浮梦"},
    "雷电将军": {"weight": 0.00, "sig_weapon": "薙草之稻光"},
    "枫原万叶": {"weight": 0.00, "sig_weapon": "苍古自由之誓"},
    "钟离":     {"weight": 0.00, "sig_weapon": "贯虹之槊"},
    "克洛琳德": {"weight": 0.40, "sig_weapon": "赦罪"},
    "千织":     {"weight": 0.35, "sig_weapon": "有乐御簾切"},
    "娜维娅":   {"weight": 0.35, "sig_weapon": "裁断"},
    "艾尔海森": {"weight": 0.00, "sig_weapon": "裁叶萃光"},
    "希格雯":   {"weight": 0.30, "sig_weapon": "白雨心弦"},
}

# 无效金: 几乎不增加市场价值的角色
DEAD_WEIGHT_CHARS_GENSHIN: set[str] = {
    "迪卢克", "刻晴", "七七", "琴", "莫娜",
    "提纳里", "迪希雅",
    "埃洛伊", "阿贝多", "优菈", "可莉",
    "宵宫", "荒泷一斗", "达达利亚", "温迪",
    "魈", "申鹤",
    "神里绫华", "胡桃", "妮露", "流浪者",
    "八重神子", "甘雨", "珊瑚宫心海",
    "闲云", "赛诺", "白术", "调香师",
    "林尼", "莱欧斯利",
}

# 6.7 版本体系组合溢价
# bonus: 完整组合额外加权值
TEAM_ARCHETYPES_GENSHIN: list[dict] = [
    {"name": "冰系融化队", "chars": {"玛薇卡", "茜特菈莉"}, "bonus": 0.50},
    {"name": "星超导体系", "chars": {"桑多涅"}, "bonus": 0.0},
    {"name": "月反应体系", "chars": {"哥伦比娅"}, "bonus": 0.0},
]

# 命座价值乘数（按市场习惯：0/1 命基本不值钱，关键命座才显著增值）
CONSTELLATION_MULTIPLIERS = {0: 0.0, 1: 0.25, 2: 0.50, 3: 0.90, 4: 1.30, 5: 1.70, 6: 2.50}


FEATURE_NAMES: list[str] = [
    "yellow", "level", "star_sounds", "fuujin_waves",
    "zhuchao_waves", "yubo_coral", "total_pulls",
    "artifact_count",
    "c0_count", "c1_count", "c2_count", "c3_count",
    "c4_count", "c5_count", "c6_count", "four_star_full_count",
    "r1_count", "r2_count", "r3_count", "r4_count", "r5_count",
    "high_refine_count",
    "team_count", "hot_char_count", "skin_count", "five_star_char_count",
    "source_pzds",
    "weighted_char_value", "dead_weight_count",
    "team_synergy_bonus", "sig_weapon_count", "key_char_high_const",
]

MIN_SAMPLES = 200


def _compute_genshin_value(
    five_star_chars: list[dict],
    five_star_weapons_raw: list[dict],
) -> dict:
    db = CHARACTER_DB_GENSHIN
    dead_set = DEAD_WEIGHT_CHARS_GENSHIN

    char_info = {}
    sig_names = {}
    for name, entry in db.items():
        sig = entry.get("sig_weapon")
        if sig:
            sig_names[sig] = name

    for c in five_star_chars:
        name = c.get("name", "")
        if name in db or name in dead_set:
            char_info[name] = {
                "constellation": int(c.get("constellation") or 0),
                "has_bound_weapon": bool(c.get("weapon_refine")),
            }

    # 独立五星武器匹配专武
    bound_sig_count = 0
    for w in five_star_weapons_raw:
        wname = w.get("name", "")
        if wname in sig_names:
            bound_sig_count += 1

    weighted_value = 0.0
    dead_count = 0
    max_key_const = 0

    for name, info in char_info.items():
        entry = db.get(name)
        if entry:
            weight = entry["weight"]
            const = info["constellation"]
            # 部分角色命座价值有上限（如芙宁娜 2 命以上价值不高）
            eff_const = min(const, entry.get("max_const", 6))
            mult = CONSTELLATION_MULTIPLIERS.get(eff_const, 1.0)

            if weight <= 0.0 or name in dead_set:
                dead_count += 1
            else:
                weighted_value += weight * mult
                if eff_const > max_key_const and weight >= 0.6:
                    max_key_const = eff_const
        elif name in dead_set:
            dead_count += 1

    owned_set = set(char_info.keys())
    synergy_bonus = 0.0
    for archetype in TEAM_ARCHETYPES_GENSHIN:
        if archetype["chars"].issubset(owned_set):
            synergy_bonus += archetype["bonus"]

    return {
        "weighted_value": weighted_value,
        "dead_count": dead_count,
        "synergy_bonus": synergy_bonus,
        "sig_count": bound_sig_count,
        "max_key_const": max_key_const,
    }


GAME_FEATURE_EXTRACTORS = {
    "10026": _compute_genshin_value,
}

# 有价值角色名（权重 > 0）：仅这些角色的满命计入 c6_count
_VALUABLE_NAMES = {
    name for name, entry in CHARACTER_DB_GENSHIN.items()
    if entry.get("weight", 0) > 0
}


def extract_features(
    parsed: dict | Any,
    source: str = "",
    price: float = 0.0,
    game_id: str = "",
) -> dict[str, float]:
    hot_chars = GAME_HOT_CHARS.get(game_id, set())
    value_fn = GAME_FEATURE_EXTRACTORS.get(game_id)

    if hasattr(parsed, "to_dict"):
        d = parsed.to_dict()
    else:
        d = parsed

    yellow = int(d.get("yellow") or 0)
    level = int(d.get("level") or 0)
    star_sounds = int(d.get("star_sounds") or 0)
    fuujin = int(d.get("fuujin_waves") or 0)
    zhuchao = int(d.get("zhuchao_waves") or 0)
    yubo = int(d.get("yubo_coral") or 0)
    total_pulls = float(d.get("total_pulls") or 0.0)
    artifact_count = int(d.get("artifact_count") or 0)

    five_star_chars = d.get("five_star_chars") or []
    four_star_chars = d.get("four_star_chars") or []

    c_counts = [0] * 7
    hot_count = 0
    valuable_c6 = 0
    for c in five_star_chars:
        const = int(c.get("constellation") or 0)
        name = c.get("name", "")
        if const <= 6:
            c_counts[const] += 1
        if const == 6 and name in _VALUABLE_NAMES:
            valuable_c6 += 1
        if name in hot_chars:
            hot_count += 1
    # c6_count 只计有价值角色的满命（常驻/老角色满命不计价值）
    c_counts[6] = valuable_c6

    four_star_full = sum(
        1 for c in four_star_chars if int(c.get("constellation") or 0) >= 6
    )

    five_star_weapons = [w for w in (d.get("five_star_weapons") or [])]
    for c in five_star_chars:
        wr = c.get("weapon_refine")
        if wr:
            five_star_weapons.append({"refine": wr})

    r_counts = [0] * 5
    high_refine = 0
    for w in five_star_weapons:
        refine = int(w.get("refine") or 1)
        if 1 <= refine <= 5:
            r_counts[refine - 1] += 1
        if refine >= 3:
            high_refine += 1

    teams = d.get("teams") or []
    skins = d.get("skins") or []

    features = {
        "yellow": float(yellow), "level": float(level),
        "star_sounds": float(star_sounds), "fuujin_waves": float(fuujin),
        "zhuchao_waves": float(zhuchao), "yubo_coral": float(yubo),
        "total_pulls": total_pulls,
        "artifact_count": float(artifact_count),
        "c0_count": float(c_counts[0]), "c1_count": float(c_counts[1]),
        "c2_count": float(c_counts[2]), "c3_count": float(c_counts[3]),
        "c4_count": float(c_counts[4]), "c5_count": float(c_counts[5]),
        "c6_count": float(c_counts[6]), "four_star_full_count": float(four_star_full),
        "r1_count": float(r_counts[0]), "r2_count": float(r_counts[1]),
        "r3_count": float(r_counts[2]), "r4_count": float(r_counts[3]),
        "r5_count": float(r_counts[4]), "high_refine_count": float(high_refine),
        "team_count": float(len(teams)), "hot_char_count": float(hot_count),
        "skin_count": float(len(skins)),
        "five_star_char_count": float(len(five_star_chars)),
        "source_pzds": 0.0,
        "weighted_char_value": 0.0, "dead_weight_count": 0.0,
        "team_synergy_bonus": 0.0, "sig_weapon_count": 0.0,
        "key_char_high_const": 0.0,
    }

    if value_fn:
        try:
            adv = value_fn(five_star_chars, five_star_weapons)
            features["weighted_char_value"] = adv["weighted_value"]
            features["dead_weight_count"] = float(adv["dead_count"])
            features["team_synergy_bonus"] = adv["synergy_bonus"]
            features["sig_weapon_count"] = float(adv["sig_count"])
            features["key_char_high_const"] = float(adv["max_key_const"])
        except Exception:
            pass

    if price > 0:
        features["price"] = float(price)
        features["log_price"] = math.log(price)

    return features


def features_to_vector(features: dict) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]
