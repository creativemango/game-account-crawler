"""鸣潮账号数据结构化解析

统一解析螃蟹/盼之的原始商品数据，输出标准化的账号资产信息。

数据来源:
  - 螃蟹: detailPost 接口的 productAttrs + reportTitleAttr + productName
  - 盼之: 详情页 __NUXT__.detailsData 的 metadataModel.resources + section* + sellingPointLabels

统一输出 ParsedAccount:
  yellow/level/star_sounds/fuujin_waves/zhuchao_waves/yubo_coral/total_pulls
  five_star_chars: [{name, constellation, weapon_refine}]
  four_star_chars: [{name, constellation}]
  five_star_weapons: [{name, refine}]  (无角色绑定的武器)
  teams: [str]
  skins: [str]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict


@dataclass
class Character:
    """角色"""
    name: str
    constellation: int = 0           # 命座 (0-6, 6=满命)
    weapon_refine: int | None = None  # 绑定武器精炼 (None=无武器)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Weapon:
    """武器（无角色绑定的）"""
    name: str
    refine: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedAccount:
    """解析后的账号资产（统一格式）"""
    # 基础数值
    yellow: int = 0               # 黄数
    level: int = 0                # 联觉等级
    star_sounds: int = 0          # 星声
    fuujin_waves: int = 0         # 浮金波纹
    zhuchao_waves: int = 0        # 铸潮波纹
    yubo_coral: int = 0           # 余波珊瑚
    # 角色/武器
    five_star_chars: list[Character] = field(default_factory=list)
    four_star_chars: list[Character] = field(default_factory=list)
    five_star_weapons: list[Weapon] = field(default_factory=list)
    # 其他
    teams: list[str] = field(default_factory=list)   # 队伍（盼之独有）
    skins: list[str] = field(default_factory=list)    # 服饰

    @property
    def total_pulls(self) -> float:
        """总抽数 = 星声/160 + 浮金波纹"""
        return self.star_sounds / 160 + self.fuujin_waves

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_pulls"] = self.total_pulls
        d["five_star_chars"] = [c.to_dict() for c in self.five_star_chars]
        d["four_star_chars"] = [c.to_dict() for c in self.four_star_chars]
        d["five_star_weapons"] = [w.to_dict() for w in self.five_star_weapons]
        return d


# ===== 螃蟹解析 =====


def parse_pxb7(detail: dict) -> ParsedAccount:
    """解析螃蟹 detailPost 返回的商品详情

    数据源:
      - reportTitleAttr: 黄数/联觉等级/浮金波纹/铸潮波纹/余波珊瑚
      - productName: 星声（reportTitleAttr 里缺星声，需正则提取）
      - reportTabInfo.groupList: 角色/武器结构化列表（含绑定关系）

    reportTabInfo.groupList 分组:
      - groupName="五星角色" roleRarity=5 → genshinImpactRoleDTO
      - groupName="四星角色" roleRarity=4 → genshinImpactRoleDTO
      - groupName="五星武器" elementType=2 → genshinImpactWeaponDTO

    genshinImpactRoleDTO 字段:
      - roleName/roleLevel/roleRarity
      - mingZuo: 命座数（字符串）
      - weaponDTO: 绑定武器（可能 null），含 weaponName/weaponRefineNum
      - specializedWeapon: 是否专武

    Args:
        detail: fetch_detail() 返回的 data dict

    Returns:
        ParsedAccount
    """
    pa = ParsedAccount()

    # 1. reportTitleAttr → 数值
    for attr in detail.get("reportTitleAttr", []) or []:
        name = attr.get("attrName", "")
        val = attr.get("attrValue", "")
        try:
            v = int(val)
        except (ValueError, TypeError):
            continue
        if name == "黄数":
            pa.yellow = v
        elif name == "联觉等级":
            pa.level = v
        elif name == "浮金波纹":
            pa.fuujin_waves = v
        elif name == "铸潮波纹":
            pa.zhuchao_waves = v
        elif name == "余波珊瑚":
            pa.yubo_coral = v

    # 2. productName → 星声（reportTitleAttr 里缺星声）
    product_name = detail.get("productName", "") or ""
    m = re.search(r"星声[：:](\d+)", product_name)
    if m:
        pa.star_sounds = int(m.group(1))

    # 3. reportTabInfo.groupList → 角色/武器（含绑定关系）
    rti = detail.get("reportTabInfo") or {}
    for g in rti.get("groupList", []) or []:
        group_name = g.get("groupName", "")
        element_type = g.get("elementType", 0)

        for el in g.get("groupElementList", []) or []:
            if element_type == 1:
                # 角色组
                role_dto = el.get("genshinImpactRoleDTO")
                if not role_dto:
                    continue
                name = role_dto.get("roleName", "")
                if not name:
                    continue
                rarity = int(role_dto.get("roleRarity") or 5)
                constellation = int(role_dto.get("mingZuo") or 0)

                # 绑定武器
                weapon_refine = None
                weapon_dto = role_dto.get("weaponDTO")
                if weapon_dto and weapon_dto.get("weaponName"):
                    weapon_refine = int(weapon_dto.get("weaponRefineNum") or 1)

                char = Character(name=name, constellation=constellation,
                                 weapon_refine=weapon_refine)
                if rarity >= 5:
                    pa.five_star_chars.append(char)
                else:
                    pa.four_star_chars.append(char)

            elif element_type == 2:
                # 武器组（无角色绑定的独立武器）
                weapon_dto = el.get("genshinImpactWeaponDTO")
                if not weapon_dto:
                    continue
                name = weapon_dto.get("weaponName", "")
                if not name:
                    continue
                refine = int(weapon_dto.get("weaponRefineNum") or 1)
                pa.five_star_weapons.append(Weapon(name=name, refine=refine))

    return pa


# ===== 盼之解析 =====

def _parse_corner_mark(mark: str | None) -> tuple[int, int | None]:
    """解析盼之 resources 的 cornerMark

    格式:
      "6+1" → (命座=6, 精炼=1)
      "6"   → (命座=6, 精炼=None)
      "1"   → 武器精炼=1（code 以 MC1 开头时）

    Returns:
        (constellation, weapon_refine)
    """
    if not mark:
        return (0, None)
    if "+" in mark:
        parts = mark.split("+")
        try:
            c = int(parts[0])
            r = int(parts[1]) if len(parts) > 1 else None
            return (c, r)
        except ValueError:
            return (0, None)
    try:
        return (int(mark), None)
    except ValueError:
        return (0, None)


def _is_weapon_code(code: str) -> bool:
    """武器 code 以 MC1 开头（MC100xx / MC101xx）"""
    return code.startswith("MC1") if code else False


def _is_skin_code(code: str) -> bool:
    """服饰 code 以 MC2 开头（MC200xx）"""
    return code.startswith("MC2") if code else False


def _parse_pzds_team(label: str) -> str | None:
    """从 sellingPointLabels 中识别队伍名

    队伍标签特征: 以"队"结尾（如 "绯琳莫队"、"爱弥斯震谐队"）
    非 "X+Y" 格式且非 "X命XXX" 格式
    """
    label = label.strip()
    if not label:
        return None
    # 排除 "X+Y" 格式（角色武器绑定）
    if re.match(r"^[\u4e00-\u9fa5・]+\d+\+\d+$", label):
        return None
    # 排除 "X命XXX" / "满命XXX" 格式
    if re.match(r"^(\d+命|满命)", label):
        return None
    # 排除 "满命角色xN"
    if "满命角色" in label:
        return None
    # 以"队"结尾的是队伍
    if label.endswith("队"):
        return label
    return None


def parse_pzds(detail: dict) -> ParsedAccount:
    """解析盼之详情页 __NUXT__.detailsData

    Args:
        detail: fetch_goods_detail() 返回的 detailsData dict

    Returns:
        ParsedAccount
    """
    pa = ParsedAccount()

    # 1. section* → 数值
    # section1=黄数, section2=联觉等级, section3=星声, section4=浮金波纹, section5=金角色数
    pa.yellow = int(detail.get("section1") or 0)
    pa.level = int(detail.get("section2") or 0)
    pa.star_sounds = int(detail.get("section3") or 0)
    pa.fuujin_waves = int(detail.get("section4") or 0)
    # section5 是金角色数，不直接用（从 resources 统计）

    # 2. metadataModel.resources → 角色/武器/服饰
    meta = detail.get("metadataModel", {}) or {}
    resources = meta.get("resources", []) or []

    for r in resources:
        name = r.get("name", "")
        code = r.get("code", "")
        mark = r.get("cornerMark")

        if not name:
            continue

        if _is_skin_code(code):
            # 服饰
            pa.skins.append(name)
        elif _is_weapon_code(code):
            # 武器（无角色绑定的独立武器）
            refine, _ = _parse_corner_mark(mark)
            pa.five_star_weapons.append(Weapon(name=name, refine=refine))
        else:
            # 角色
            constellation, weapon_refine = _parse_corner_mark(mark)
            pa.five_star_chars.append(
                Character(name=name, constellation=constellation,
                          weapon_refine=weapon_refine)
            )

    # 3. sellingPointLabels → 队伍
    for label in detail.get("sellingPointLabels", []) or []:
        team = _parse_pzds_team(label)
        if team:
            pa.teams.append(team)

    return pa
