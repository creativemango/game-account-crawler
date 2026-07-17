# 游戏账号交易平台爬虫 — 设计文档

## 概述

爬取 pxb7.com（螃蟹）和 [已移除].com（螃蟹）的游戏账号列表，用户配置目标游戏 ID，定时轮询最新上架账号，提供 API 查询。

## 架构

```
config.yaml      →  爬虫引擎  →  SQLite  →  FastAPI
(游戏ID/间隔)        (Scrapling)   (JSON存储)   (搜索/筛选)
```

## 两个网站的 API

### [已移除].com

```
GET https://www.[已移除].com/api/goodsList?gameId={gameId}&platform={platform}
```

- platform: 6（账号）、其他值待确认
- 返回: `{ platform, products[], pagination }`
- 商品字段: `id, title, price, accountType, level, ...（不同游戏字段不同）`, `bindings`, `badges`, `url`

### pxb7.com

```
POST https://api-pc.pxb7.com/api/search/product/v2/selectSearchPageList
Content-Type: application/json

{
  "query": "",
  "gameId": "10302",
  "pageIndex": 1,
  "pageSize": 16,
  "bizProd": 1,
  "type": "4",
  "posType": 1,
  "filterDTOList": [],
  "combineFilterList": []
}
```

- 返回: `{ success, data: { properties: { rcToken, pageToken }, list[] } }`
- 分页: `pageIndex` 递增，或使用 `pageToken` 游标
- 商品字段: `productId, productUniqueNo, price, showTitle, mainImageUrl, createTime, hotCount, attrNameList, important, ...`

## 数据存储

SQLite 单表：

```sql
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- 'pxb7' | '[已移除]'
    game_id TEXT NOT NULL,             -- 平台原始游戏 ID
    product_id TEXT NOT NULL,          -- 商品唯一标识
    title TEXT,                        -- 标题（生成列）
    price REAL,                        -- 价格（生成列）
    raw_data TEXT NOT NULL,            -- 完整 JSON
    created_at TEXT DEFAULT (datetime('now')),
    first_seen_at TEXT,
    UNIQUE(source, product_id)
);

CREATE INDEX idx_accounts_source_game ON accounts(source, game_id);
CREATE INDEX idx_accounts_price ON accounts(price);
CREATE VIRTUAL TABLE accounts_fts USING fts5(title, content='accounts');
```

- `raw_data` 存完整 JSON，游戏特有字段通过 `json_extract()` 查询
- price/title 建生成列 + 索引加速常用筛选
- `first_seen_at` 记录首次入库时间（同商品可多次采集更新）

## 爬虫引擎

Scrapling `Fetcher` 发 HTTP 请求。两个网站各一个函数：

```python
def crawl_pxb7(game_id: str) -> list[dict]
def crawl_[已移除](game_id: str) -> list[dict]
```

- 翻页直至无新数据或到达上限
- 新商品 INSERT，已有商品 UPDATE（价格/标题可能变化）
- 请求间隔 2-5 秒，避免触发限流

## 调度

```python
while True:
    for game in config.games:
        crawl(game)
        sleep(config.interval / len(config.games))
```

不引入 APScheduler。

## API 接口

FastAPI，端口 8000：

| 端点 | 说明 |
|------|------|
| `GET /api/accounts` | 搜索：`?source=&game_id=&keyword=&min_price=&max_price=&page=1&size=20` |
| `GET /api/accounts/{id}` | 单条详情 |
| `GET /api/stats` | 各来源/游戏数量统计 |

## 配置文件

```yaml
# config.yaml
sources:
  pxb7:
    enabled: true
    games:
      - "10302"  # 鸣潮
  [已移除]:
    enabled: true
    games:
      - "303"    # 鸣潮
    platform: "6"

crawl:
  interval_seconds: 300  # 5 分钟
  page_size: 20

api:
  host: "0.0.0.0"
  port: 8000
```

## 项目文件

```
game-account-crawler/
├── config.yaml
├── crawler/
│   ├── __init__.py
│   ├── pxb7.py
│   ├── [已移除].py
│   └── base.py
├── db.py
├── api.py
├── main.py
└── requirements.txt
```

## 依赖

- scrapling[fetchers]
- fastapi + uvicorn
- pyyaml

