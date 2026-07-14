# 向前爬虫（Backfill Crawler）设计

> 日期: 2026-07-15
> 状态: 已确认，待实现

## 目标

提供两个独立运行的 CLI 脚本，按源（螃蟹 / 盼之）向前翻页爬取历史在售账号数据。与 `main.py` 的定时爬虫（固定从第 1 页爬最新）互补，用于补充模型训练所需的存量数据。

**职责边界**：列表 → 详情 → 解析 → 特征 → 入库。**不计算价值**（`value` / `score` / `value_ratio` 留空），交由 `main.py` 的 `run_valuer_loop` 异步补全。

## 非目标

- 不启动任何后台 worker（crawl / detail / valuer / train 四个 loop 均不启动）
- 不做价值预测（不调 `predict_value` / `compute_score`）
- 不替代定时爬虫，两者并存互补
- 不做断点续爬（靠数据库 `UNIQUE(source, product_id)` 去重，重复商品只更新价格）

## 架构

```
backfill/
├── __init__.py
├── common.py     # process_account(): 详情→解析→特征→入库（两源共享，不含价值）
├── pxb7.py       # 螃蟹向前爬虫 CLI
└── pzds.py       # 盼之向前爬虫 CLI
```

### 文件职责

**`backfill/common.py`**

抽出两源共享的"单条商品处理"流程，单一函数：

```python
def process_account(source: str, product_id: str, game_id: str,
                    price: float) -> bool:
    """获取详情→解析→提取特征→入 account_details (不计算价值)

    Args:
        source: "pxb7" 或 "pzds"
        product_id: 商品ID（螃蟹为纯数字 productId，盼之为 goodsNo 如 "MC17DN"）
        game_id: 游戏ID
        price: 实际价格（用于 extract_features 的训练标签）

    Returns:
        True=成功, False=详情获取/解析失败
    """
```

流程：
1. 按 source 分发调详情接口
   - `pxb7` → `crawler.pxb7.fetch_detail(product_id)`（同步 httpx）
   - `pzds` → `crawler.pzds._get_client(game_id, platform)` 复用浏览器 + `client.fetch_goods_detail(goods_no)`
2. `parser.parse_pxb7` / `parser.parse_pzds` → `ParsedAccount.to_dict()`
3. `valuer.extract_features(parsed, source, price)`
4. `db.upsert_detail(parsed_data=parsed, features=features, value=None, score=None, value_ratio=None)`

**`backfill/pxb7.py`**

CLI 脚本，负责螃蟹源特定的列表翻页 + 调 `process_account`。

**`backfill/pzds.py`**

CLI 脚本，负责盼之源特定的列表翻页 + 调 `process_account`。复用 `crawler.pzds._get_client` 的浏览器实例（跨页复用，`atexit` 自动清理）。

## CLI 接口

### 通用参数

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--game-id` | 是 | - | 游戏ID（螃蟹如 `10302`，盼之如 `303`） |
| `--start-page` | 是 | - | 起始页码 |
| `--max-pages` | 是 | - | 最多翻页数（防失控） |
| `--page-size` | 否 | 源默认 | 每页条数（螃蟹 16，盼之 10，与现有 crawl 一致） |
| `--platform` | 否 | `"6"` | 盼之专用，商品分类ID（`6`=成品号） |

### 调用示例

```powershell
# 螃蟹鸣潮：从第 4 页爬 50 页（约 800 条）
uv run python -m backfill.pxb7 --game-id 10302 --start-page 4 --max-pages 50

# 盼之鸣潮：从第 4 页爬 50 页
uv run python -m backfill.pzds --game-id 303 --start-page 4 --max-pages 50 --platform 6
```

## 翻页行为

```
for page in range(start_page, start_page + max_pages):
    1. 调列表 API 取一页商品
    2. 无数据 → break（已到末尾）
    3. 不足一页 → 处理完当前页后 break（最后一页）
    4. 对每条商品：
       a. upsert_account 入库（UNIQUE 去重，已存在则更新价格/在售状态）
       b. process_account 获取详情+解析+特征+入 account_details
    5. 每页打印进度
```

**停止条件**：无数据、不足一页、或达到 `--max-pages`。不靠"遇到已入库商品停止"——向前爬虫的本意是补充历史数据，重复商品只更新价格即可。

### 进度输出

```
[pxb7] page=4 fetched=16 processed=16
[pxb7] page=5 fetched=16 processed=15 (1 detail failed)
...
[pxb7] page=54 fetched=0 → 结束
[pxb7] 完成: pages=50, total_fetched=768, total_processed=752
```

## 数据流

```
backfill.pxb7 / backfill.pzds (CLI, 单次运行)
  │
  ├─ 1. 列表翻页 (源特定)
  │     pxb7 → httpx 直连 selectSearchPageList
  │     pzds → PzdsApiClient.fetch_goods_page (浏览器+WASM签名)
  │
  ├─ 2. 每条商品 → upsert_account (UNIQUE 去重)
  │
  └─ 3. 每条商品 → common.process_account
        ├─ fetch_detail (源特定)
        ├─ parse (源特定) → ParsedAccount
        ├─ extract_features (共享)
        └─ upsert_detail (features 填充, value/score/value_ratio = None)
                                    │
                                    ▼
                        account_details 表 (features 有值, value NULL)
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
          run_valuer_loop (main.py)         run_train_loop (main.py)
          捞 value IS NULL 的账号           每日训练 (features + price)
          补全 value/score/ratio
```

## 与 main.py 的关系

| 方面 | 向前爬虫 | main.py 定时爬虫 |
|---|---|---|
| 运行方式 | 手动 CLI 单次 | 后台 daemon 循环 |
| 翻页起点 | `--start-page` 指定 | 固定第 1 页（爬最新） |
| 停止条件 | 无数据/不足一页/`--max-pages` | 固定 `max_pages` |
| 触发价值 | 否（留空交 valuer_loop） | 是（实时预测） |
| 启动 worker | 不启动 | 启动 4 个后台 worker |
| 共享模块 | crawler + parser + valuer.features + db | 同左 |

**关键边界**：向前爬虫纯单次脚本跑完即退出，不启动任何后台 worker。需要补价值时靠 `main.py` 运行中的 `run_valuer_loop` 自动通过 `get_unvalued_accounts`（检测 `value IS NULL`）捞取补全。

## 错误处理

- **单条详情失败**：记日志跳过，不影响整页其他商品
- **整页 API 失败**：记日志 break（避免持续报错）
- **盼之浏览器崩溃**：清理 client 缓存，break 退出（下次运行重建）

## 现有代码改动

### `crawler/pxb7.py`

`crawl()` 加 `start_page: int = 1` 可选参数：

```python
def crawl(game_id: str, page_size: int = 16, max_pages: int = 3,
          start_page: int = 1) -> list[dict]:
    # for page_index in range(start_page, start_page + max_pages):
```

默认值 `1` 保持向后兼容，`main.py` 无需改动。

### `crawler/pzds.py`

`crawl()` 和 `_crawl_async()` 加 `start_page: int = 1`：

```python
def crawl(game_id: str, platform: str = "6", max_pages: int = 1,
          page_size: int = 10, start_page: int = 1) -> list[dict]:

async def _crawl_async(game_id: str, platform: str, max_pages: int,
                       page_size: int, start_page: int = 1) -> list[dict]:
    # for page in range(start_page, start_page + max_pages):
```

默认值 `1` 保持向后兼容。

### `db.upsert_detail`

当前签名已支持 `value` / `score` / `value_ratio` 为 `None`，SQL 插入 NULL 没问题。无需改动。

## 依赖关系

```
backfill.common
  ├─ crawler.pxb7.fetch_detail
  ├─ crawler.pzds._get_client / _get_loop (复用浏览器)
  ├─ parser.parse_pxb7 / parse_pzds
  ├─ valuer.extract_features
  └─ db.upsert_account / upsert_detail

backfill.pxb7
  ├─ crawler.pxb7.crawl (支持 start_page)
  └─ backfill.common.process_account

backfill.pzds
  ├─ crawler.pzds.crawl (支持 start_page)
  └─ backfill.common.process_account
```

## 文件清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `backfill/__init__.py` | 新建 | 包标识，可空 |
| `backfill/common.py` | 新建 | `process_account()` 共享流程 |
| `backfill/pxb7.py` | 新建 | 螃蟹 CLI 脚本 |
| `backfill/pzds.py` | 新建 | 盼之 CLI 脚本 |
| `crawler/pxb7.py` | 改 | `crawl()` 加 `start_page` 参数 |
| `crawler/pzds.py` | 改 | `crawl()` + `_crawl_async()` 加 `start_page` 参数 |
