"""原神: 解析螃蟹商品详情为结构化账号数据"""
from __future__ import annotations

import re

from .wuwa import ParsedAccount


_GENSHIN_ATTR_MAP = {
    "黄数": "yellow",
    "冒险等级": "level",
    "原石": "star_sounds",
    "纠缠之缘": "fuujin_waves",
    "相遇之缘": "zhuchao_waves",
    "星辉": "yubo_coral",
}


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
    product_name = detail.get("productName", "") or ""
    m = re.search(r"原石[：:](\d+)", product_name)
    if m and pa.star_sounds == 0:
        pa.star_sounds = int(m.group(1))
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
                char = ParsedAccount.Character(name=name, constellation=constellation, weapon_refine=weapon_refine)
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
                pa.five_star_weapons.append(ParsedAccount.Weapon(name=name, refine=refine))
    return pa
