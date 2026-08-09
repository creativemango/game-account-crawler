"""原神: 解析螃蟹商品详情为结构化账号数据

数据来源:
  - reportTitleAttr: 结构化数值（黄数/冒险等级/原石/纠缠之缘/相遇之缘/星辉）
  - productName: 完整标题文本（含原石/纠缠/圣遗物/时装，以及角色武器列表）
  - reportTabInfo.groupList: 角色/武器结构化列表（含绑定关系）

标题兜底: 部分详情缺少结构化角色/数值时，从标题正则提取。
"""
from __future__ import annotations

import re

from .wuwa import ParsedAccount, Character, Weapon


_GENSHIN_ATTR_MAP = {
    "黄数": "yellow",
    "冒险等级": "level",
    "原石": "star_sounds",
    "纠缠之缘": "fuujin_waves",
    "相遇之缘": "zhuchao_waves",
    "星辉": "yubo_coral",
}

# 标题文本中的数值字段（结构化字段缺失时用标题补）
_TITLE_NUM_PATTERNS: dict[str, list[str]] = {
    "yellow": [r"黄数[：: ]*(\d+)", r"(\d+)\s*黄"],
    "level": [r"等级[：: ]*(\d+)", r"(\d+)\s*级"],
    "star_sounds": [r"原石[：: ]*(\d+)"],
    "fuujin_waves": [r"纠缠之[源缘][：: ]*(\d+)"],
    "zhuchao_waves": [r"相遇之缘[：: ]*(\d+)"],
    "yubo_coral": [r"星辉[：: ]*(\d+)"],
    "artifact_count": [r"圣遗物[：: ]*(\d+)"],
    "skin_count": [r"时装数量[：: ]*(\d+)"],
}

_CHAR_SECTION = r"(?:五星|5星)角色"
_FOUR_STAR_SECTION = r"(?:四星|4星)角色"
_WEAPON_SECTION = r"(?:五星|5星)武器"


def _section(text: str, label: str) -> str:
    """截取标题中某分节的内容（角色/武器列表），到【 ；; 或结尾为止"""
    m = re.search(rf"{label}[：:]\s*([^【]*?)(?=【|；|;|$)", text)
    return m.group(1) if m else ""


def _extract_nums(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for field, patterns in _TITLE_NUM_PATTERNS.items():
        for p in patterns:
            m = re.search(p, text)
            if m:
                out[field] = int(m.group(1))
                break
    return out


def parse_chars_from_text(text: str, rarity: int = 5) -> list[Character]:
    """从标题文本解析角色列表（如 满命玛薇卡, 2命刻晴, 神里绫华）"""
    label = _CHAR_SECTION if rarity == 5 else _FOUR_STAR_SECTION
    seg = _section(text, label)
    chars: list[Character] = []
    for token in re.split(r"[，,、]", seg):
        token = token.strip()
        if not token:
            continue
        # 过滤武器/属性等非角色片段（如 精5若水、原石 280、圣遗物 7、59个五星武器）
        if re.match(r"^精\d", token) or re.search(
            r"原石|纠缠|相遇之缘|星辉|圣遗物|时装|武器|角色|男主|女主", token
        ):
            continue
        # 6+1奈芙尔 / 6+2哥伦比娅 格式：C6 + 专武精炼 N
        m6 = re.match(r"^(\d+)\+(\d+)\s*(.+)$", token)
        if m6:
            name6 = m6.group(3).strip()
            if name6:
                chars.append(Character(
                    name=name6,
                    constellation=min(int(m6.group(1)), 6),
                    weapon_refine=min(int(m6.group(2)), 5),
                ))
            continue
        m = re.match(r"^(满命|(\d+)命)?\s*(.+)$", token)
        if not m or not m.group(3).strip():
            continue
        name = m.group(3).strip()
        if m.group(1) == "满命":
            constellation = 6
        elif m.group(2) is not None:
            constellation = min(int(m.group(2)), 6)
        else:
            constellation = 0
        chars.append(Character(name=name, constellation=constellation))
    return chars


def parse_weapons_from_text(text: str) -> list[Weapon]:
    """从标题文本解析武器列表（如 精5若水, 精1冬极白星）"""
    seg = _section(text, _WEAPON_SECTION)
    weapons: list[Weapon] = []
    for token in re.split(r"[，,、]", seg):
        token = token.strip()
        m = re.match(r"^精(\d+)\s*(.+)$", token)
        if not m or not m.group(2).strip():
            continue
        refine = min(int(m.group(1)), 5)
        weapons.append(Weapon(name=m.group(2).strip(), refine=refine))
    return weapons


def parse_pxb7(detail: dict) -> ParsedAccount:
    pa = ParsedAccount()
    for attr in detail.get("reportTitleAttr", []) or []:
        name = attr.get("attrName", "")
        val = attr.get("attrValue", "")
        try:
            v = int(val)
        except (ValueError, TypeError):
            continue
        field = _GENSHIN_ATTR_MAP.get(name)
        if field == "yellow":
            pa.yellow = v
        elif field == "level":
            pa.level = v
        elif field == "star_sounds":
            pa.star_sounds = v
        elif field == "fuujin_waves":
            pa.fuujin_waves = v
        elif field == "zhuchao_waves":
            pa.zhuchao_waves = v
        elif field == "yubo_coral":
            pa.yubo_coral = v

    product_name = (
        detail.get("productName") or detail.get("title") or detail.get("showTitle") or ""
    )
    if product_name:
        nums = _extract_nums(product_name)
        if pa.yellow == 0:
            pa.yellow = nums.get("yellow", 0)
        if pa.level == 0:
            pa.level = nums.get("level", 0)
        if pa.star_sounds == 0:
            pa.star_sounds = nums.get("star_sounds", 0)
        if pa.fuujin_waves == 0:
            pa.fuujin_waves = nums.get("fuujin_waves", 0)
        if pa.zhuchao_waves == 0:
            pa.zhuchao_waves = nums.get("zhuchao_waves", 0)
        if pa.yubo_coral == 0:
            pa.yubo_coral = nums.get("yubo_coral", 0)
        if pa.artifact_count == 0:
            pa.artifact_count = nums.get("artifact_count", 0)
        if not pa.skins and nums.get("skin_count"):
            pa.skins = ["时装"] * nums["skin_count"]

    rti = detail.get("reportTabInfo") or {}
    for g in rti.get("groupList", []) or []:
        element_type = g.get("elementType", 0)
        for el in g.get("groupElementList", []) or []:
            if element_type == 1:
                role_dto = el.get("genshinImpactRoleDTO")
                if not role_dto:
                    continue
                name = role_dto.get("roleName", "")
                if not name:
                    continue
                rarity = int(role_dto.get("roleRarity") or 5)
                constellation = int(role_dto.get("mingZuo") or 0)
                weapon_refine = None
                weapon_dto = role_dto.get("weaponDTO")
                if weapon_dto and weapon_dto.get("weaponName"):
                    weapon_refine = int(weapon_dto.get("weaponRefineNum") or 1)
                char = Character(name=name, constellation=constellation, weapon_refine=weapon_refine)
                if rarity >= 5:
                    pa.five_star_chars.append(char)
                else:
                    pa.four_star_chars.append(char)
            elif element_type == 2:
                weapon_dto = el.get("genshinImpactWeaponDTO")
                if not weapon_dto:
                    continue
                name = weapon_dto.get("weaponName", "")
                if not name:
                    continue
                refine = int(weapon_dto.get("weaponRefineNum") or 1)
                pa.five_star_weapons.append(Weapon(name=name, refine=refine))

    # 标题兜底：结构化数据缺失时用标题补
    if product_name:
        if not pa.five_star_chars:
            pa.five_star_chars = parse_chars_from_text(product_name, rarity=5)
        if not pa.four_star_chars:
            pa.four_star_chars = parse_chars_from_text(product_name, rarity=4)
        if not pa.five_star_weapons:
            pa.five_star_weapons = parse_weapons_from_text(product_name)

    return pa
