"""pxb7.com（螃蟹）爬虫"""
import httpx
from .base import CrawlerError

LIST_API = "https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList"
DETAIL_API = "https://api-pc.pxb7.com/api/product/web/product/detailPost"
TITLE_API = "https://api-pc.pxb7.com/api/search/product/selectTitleByCode"


def crawl(game_id: str, page_size: int = 16, max_pages: int = 3, start_page: int = 1) -> list[dict]:
    """爬取列表页，返回标准化 dict

    Args:
        game_id: 游戏 ID
        page_size: 每页条数
        max_pages: 最多爬取的页数
        start_page: 起始页码（默认 1，向后兼容；backfill 场景可从第 N 页开始）
    """
    results = []

    with httpx.Client(timeout=30, trust_env=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": "https://www.pxb7.com/",
        "Origin": "https://www.pxb7.com",
    }) as client:
        # 从 start_page 开始向后翻 max_pages 页
        for page_index in range(start_page, start_page + max_pages):
            resp = client.post(LIST_API, json={
                "query": "",
                "gameId": game_id,
                "pageIndex": page_index,
                "pageSize": page_size,
                "bizProd": 1,
                "type": "4",
                "posType": 1,
                "filterDTOList": [],
                "combineFilterList": [],
            })
            if resp.status_code != 200:
                raise CrawlerError(f"pxb7 API returned {resp.status_code}")

            body = resp.json()
            if not body.get("success"):
                raise CrawlerError(f"pxb7 API error: {body.get('errCode')}")

            products = body.get("data", {}).get("list", [])
            if not products:
                break

            for p in products:
                results.append({
                    "source": "pxb7",
                    "game_id": game_id,
                    "product_id": str(p.get("productId", "")),
                    "title": p.get("showTitle", ""),
                    "price": float(p.get("price", 0)) / 100,
                    "raw_data": p,
                })

            if len(products) < page_size:
                break

    return results


def check_detail(product_id: str) -> bool:
    """检查商品详情，返回 True=在售, False=已售出"""
    with httpx.Client(timeout=15, trust_env=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Referer": "https://www.pxb7.com/",
    }) as client:
        resp = client.post(DETAIL_API, json={
            "productId": product_id,
            "showKey": "",
            "rcToken": "",
            "searchKeyword": "",
            "selectOptions": [],
            "zoneJumpType": 0,
        })
        if resp.status_code != 200:
            return True  # 接口异常时保留在售状态，下次再查

        body = resp.json()
        if not body.get("success"):
            return True

        status = body.get("data", {}).get("status")
        return status == 1


def fetch_detail(product_id: str) -> dict | None:
    """获取商品详情（公开接口，无需登录态/签名）

    返回 detailPost 的完整 data，包含:
      - productName: 完整标题文本
      - reportTitleAttr: 结构化数值（黄数/浮金波纹/铸潮波纹/余波珊瑚/联觉等级）
      - productAttrs: 角色武器列表（按命座/精炼分组）
      - price/status 等

    Args:
        product_id: 商品 ID（纯数字，列表 API 返回的 productId）

    Returns:
        详情 dict，下架/不存在返回 None
    """
    with httpx.Client(timeout=15, trust_env=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.pxb7.com",
        "Referer": "https://www.pxb7.com/",
    }) as client:
        resp = client.post(DETAIL_API, json={
            "productId": product_id,
            "showKey": "",
            "rcToken": "",
            "searchKeyword": "",
            "selectOptions": [],
            "zoneJumpType": 0,
        })
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not body.get("success"):
            return None
        return body.get("data")


def fetch_title(product_unique_no: str) -> str | None:
    """根据商品唯一编号查询标题（公开接口，无需登录态/签名）

    可用于轻量级商品状态检测：返回 None 表示商品已下架或不存在。

    Args:
        product_unique_no: 商品唯一编号（如 "MYHOA5247"），
                           列表 API 返回的 productUniqueNo 字段
    """
    with httpx.Client(timeout=15, trust_env=False, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.pxb7.com",
        "Referer": "https://www.pxb7.com/",
    }) as client:
        resp = client.post(TITLE_API, json={"productUniqueNo": product_unique_no})
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not body.get("success"):
            return None
        return body.get("data", {}).get("showTitle")
