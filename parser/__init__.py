"""游戏账号数据结构化解析模块

将螃蟹的原始商品数据解析为统一的结构化格式。
按游戏分文件：parser/genshin.py = 原神
"""
from .genshin import parse_pxb7 as parse_pxb7_genshin
from .wuwa import ParsedAccount

__all__ = ["parse_pxb7_genshin", "ParsedAccount"]
