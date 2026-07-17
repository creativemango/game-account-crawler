# 向前爬虫（Backfill Crawler）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供两个独立 CLI 脚本（螃蟹/螃蟹），按 `--start-page` 向前翻页爬取历史在售账号数据，列表→详情→解析→特征→入库，不计算价值（交 main.py 的 valuer_loop 补全）。

**Architecture:** 新增 `backfill/` 包，含共享模块 `common.py`（`process_account` 函数）和两个源特定 CLI 脚本。对现有 `crawler/pxb7.py`、`crawler/[已移除].py` 的 `crawl()` 加 `start_page` 可选参数（默认 1 兼容）。`db.py` 新增 `get_account_id` 按 (source, product_id) 查询 account_id。

**Tech Stack:** Python 3.12+, httpx, patchright, argparse, 复用现有 crawler/parser/valuer.features/db 模块

**Spec:** [docs/superpowers/specs/2026-07-15-backfill-crawler-design.md](file:///F:/project/game-account-crawler/docs/superpowers/specs/2026-07-15-backfill-crawler-design.md)

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `db.py` | 改 | 新增 `get_account_id(source, product_id)` |
| `crawler/pxb7.py` | 改 | `crawl()` 加 `start_page` 参数 |
| `crawler/[已移除].py` | 改 | `crawl()` + `_crawl_async()` 加 `start_page` 参数 |
| `backfill/__init__.py` | 新建 | 包标识 |
| `backfill/common.py` | 新建 | `process_account()`: 详情→解析→特征→入库（不含价值） |
| `backfill/pxb7.py` | 新建 | 螃蟹向前爬虫 CLI |
| `backfill/[已移除].py` | 新建 | 螃蟹向前爬虫 CLI |

---

### Task 1: db.py 新增 get_account_id

`process_account` 需要 `account_id` 才能调 `upsert_detail`，但 `upsert_account` 只返回 bool。新增按 (source, product_id) 查 id 的函数。

**Files:**
- Modify: `db.py`（在 `get_account` 函数后新增）

- [ ] **Step 1: 新增 get_account_id 函数**

在 `db.py` 的 `get_account` 函数（第 234 行）后新增：

```python
def get_account_id(source: str, product_id: str) -> int | None:
    """按 (source, product_id) 查询 account_id

    用于 backfill 流程: upsert_account 后取 id 供 upsert_detail 使用。
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE source=? AND product_id=?",
        (source, product_id)
    ).fetchone()
    conn.close()
    return row["id"] if row else None
```

- [ ] **Step 2: 验证函数可用**

运行:
```powershell
uv run python -c "from db import get_account_id; print(get_account_id('pxb7', 'nonexistent'))"
```
Expected: `None`（不报错即说明函数定义正确）

- [ ] **Step 3: Commit**

```powershell
git add db.py
git commit -m "feat(db): 新增 get_account_id 按 source+product_id 查询"
```

---

### Task 2: crawler/pxb7.py 的 crawl() 加 start_page 参数

向前爬虫需从指定页码开始翻页，而非固定第 1 页。

**Files:**
- Modify: `crawler/pxb7.py:10`（`crawl` 函数签名 + 循环）

- [ ] **Step 1: 修改 crawl 函数签名和循环**

将第 10 行的签名：
```python
def crawl(game_id: str, page_size: int = 16, max_pages: int = 3) -> list[dict]:
```
改为：
```python
def crawl(game_id: str, page_size: int = 16, max_pages: int = 3,
          start_page: int = 1) -> list[dict]:
```

将第 21 行的循环：
```python
for page_index in range(1, max_pages + 1):
```
改为：
```python
for page_index in range(start_page, start_page + max_pages):
```

- [ ] **Step 2: 验证向后兼容（默认 start_page=1 行为不变）**

运行:
```powershell
uv run python -c "from crawler.pxb7 import crawl; import inspect; sig = inspect.signature(crawl); print('start_page' in sig.parameters); print(sig.parameters['start_page'].default)"
```
Expected:
```
True
1
```

- [ ] **Step 3: Commit**

```powershell
git add crawler/pxb7.py
git commit -m "feat(crawler/pxb7): crawl() 加 start_page 参数支持向前翻页"
```

---

### Task 3: crawler/[已移除].py 的 crawl() 和 _crawl_async() 加 start_page 参数

**Files:**
- Modify: `crawler/[已移除].py:393`（`_crawl_async` 签名 + 循环）
- Modify: `crawler/[已移除].py:438`（`crawl` 签名 + 传参）

- [ ] **Step 1: 修改 _crawl_async 签名和循环**

将第 393 行的签名：
```python
async def _crawl_async(
    game_id: str,
    platform: str,
    max_pages: int,
    page_size: int,
) -> list[dict]:
```
改为：
```python
async def _crawl_async(
    game_id: str,
    platform: str,
    max_pages: int,
    page_size: int,
    start_page: int = 1,
) -> list[dict]:
```

将第 407 行的循环：
```python
for page in range(1, max_pages + 1):
```
改为：
```python
for page in range(start_page, start_page + max_pages):
```

- [ ] **Step 2: 修改 crawl 签名和传参**

将第 438 行的签名：
```python
def crawl(
    game_id: str,
    platform: str = "6",
    max_pages: int = 1,
    page_size: int = 10,
) -> list[dict]:
```
改为：
```python
def crawl(
    game_id: str,
    platform: str = "6",
    max_pages: int = 1,
    page_size: int = 10,
    start_page: int = 1,
) -> list[dict]:
```

将第 456 行的调用：
```python
return loop.run_until_complete(
    _crawl_async(game_id, platform, max_pages, page_size)
)
```
改为：
```python
return loop.run_until_complete(
    _crawl_async(game_id, platform, max_pages, page_size, start_page)
)
```

- [ ] **Step 3: 验证向后兼容**

运行:
```powershell
uv run python -c "from crawler.[已移除] import crawl, _crawl_async; import inspect; sig1 = inspect.signature(crawl); sig2 = inspect.signature(_crawl_async); print('crawl start_page default:', sig1.parameters['start_page'].default); print('_crawl_async start_page default:', sig2.parameters['start_page'].default)"
```
Expected:
```
crawl start_page default: 1
_crawl_async start_page default: 1
```

- [ ] **Step 4: Commit**

```powershell
git add crawler/[已移除].py
git commit -m "feat(crawler/[已移除]): crawl() 和 _crawl_async() 加 start_page 参数"
```

---

### Task 4: backfill 包 + common.py

共享编排流程：详情获取→解析→特征提取→入库（不含价值）。

**Files:**
- Create: `backfill/__init__.py`
- Create: `backfill/common.py`

- [ ] **Step 1: 创建 backfill/__init__.py**

```python
"""向前爬虫: 按源向前翻页爬取历史账号数据

两个独立 CLI 脚本:
  - python -m backfill.pxb7 --game-id 10302 --start-page 4 --max-pages 50
  - python -m backfill.[已移除] --game-id 303 --start-page 4 --max-pages 50

职责: 列表→详情→解析→特征→入库 (不计算价值, 交 main.py valuer_loop 补全)
"""
```

- [ ] **Step 2: 创建 backfill/common.py**

```python
"""向前爬虫共享流程: 详情→解析→特征→入库 (不含价值)

process_account 是两源共享的单条商品处理函数:
  1. 按 source 分发调详情接口 (pxb7=httpx, [已移除]=浏览器)
  2. parse → ParsedAccount
  3. extract_features
  4. upsert_detail (features 填充, value/score/value_ratio = None)

不调 predict_value / compute_score, 价值评估交 main.py 的 run_valuer_loop。
"""
from __future__ import annotations

import logging

from crawler.pxb7 import fetch_detail as fetch_pxb7_detail
from parser import parse_pxb7, parse_[已移除]
from valuer import extract_features
from db import get_account_id, upsert_detail

logger = logging.getLogger(__name__)


def process_account(source: str, product_id: str, game_id: str,
                    price: float, platform: str = "6") -> bool:
    """获取详情→解析→提取特征→入 account_details (不计算价值)

    Args:
        source: "pxb7" 或 "[已移除]"
        product_id: 商品ID (螃蟹为纯数字 productId, 螃蟹为 goodsNo 如 "MC17DN")
        game_id: 游戏ID
        price: 实际价格 (用于 extract_features 的训练标签)
        platform: 螃蟹商品分类ID (默认 "6"=成品号, 螃蟹不使用)

    Returns:
        True=成功, False=详情获取/解析失败
    """
    # 1. 获取 account_id (upsert_account 已在外部调用, 这里只查 id)
    account_id = get_account_id(source, product_id)
    if not account_id:
        logger.error("未找到账号记录: %s/%s", source, product_id)
        return False

    try:
        # 2. 按 source 分发调详情接口
        if source == "pxb7":
            detail = fetch_pxb7_detail(product_id)
            if not detail:
                logger.warning("螃蟹详情为空: %s", product_id)
                return False
            parsed = parse_pxb7(detail).to_dict()

        elif source == "[已移除]":
            # 螃蟹详情需要浏览器, 复用 crawler.[已移除] 的浏览器实例
            import asyncio
            from crawler.[已移除] import _get_client, _get_loop
            loop = _get_loop()
            client = asyncio.run_coroutine_threadsafe(
                _get_client(game_id, platform), loop
            ).result(timeout=60)
            detail = asyncio.run_coroutine_threadsafe(
                client.fetch_goods_detail(product_id), loop
            ).result(timeout=60)
            parsed = parse_[已移除](detail).to_dict()

        else:
            logger.error("未知 source: %s", source)
            return False

        # 3. 提取特征 (含 price/log_price 训练标签)
        features = extract_features(parsed, source, price)

        # 4. 入库 (value/score/value_ratio 留 None, 交 valuer_loop 补全)
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
```

- [ ] **Step 3: 验证模块可导入**

运行:
```powershell
uv run python -c "from backfill.common import process_account; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```powershell
git add backfill/__init__.py backfill/common.py
git commit -m "feat(backfill): 新增 common.process_account 共享编排流程"
```

---

### Task 5: backfill/pxb7.py CLI

螃蟹向前爬虫脚本：列表翻页 + 调 process_account。

**Files:**
- Create: `backfill/pxb7.py`

- [ ] **Step 1: 创建 backfill/pxb7.py**

```python
"""螃蟹向前爬虫 CLI

从指定页码向前翻页爬取历史在售账号, 列表→详情→解析→特征→入库。
不计算价值 (交 main.py valuer_loop 补全)。

用法:
  uv run python -m backfill.pxb7 --game-id 10302 --start-page 4 --max-pages 50
"""
from __future__ import annotations

import argparse
import logging
import sys

from crawler.pxb7 import crawl as crawl_pxb7
from db import upsert_account
from .common import process_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="螃蟹向前爬虫 (历史数据回填)")
    parser.add_argument("--game-id", required=True, help="游戏ID (如 10302=鸣潮)")
    parser.add_argument("--start-page", type=int, required=True, help="起始页码")
    parser.add_argument("--max-pages", type=int, required=True, help="最多翻页数")
    parser.add_argument("--page-size", type=int, default=16, help="每页条数 (默认 16)")
    args = parser.parse_args()

    game_id = args.game_id
    start_page = args.start_page
    max_pages = args.max_pages
    page_size = args.page_size

    logger.info("开始: game=%s start_page=%d max_pages=%d page_size=%d",
                game_id, start_page, max_pages, page_size)

    total_fetched = 0
    total_processed = 0
    pages_done = 0

    for page_offset in range(max_pages):
        current_page = start_page + page_offset
        try:
            # 翻一页 (crawl 内部会翻 max_pages 页, 这里每次传 1 页)
            accounts = crawl_pxb7(game_id, page_size=page_size, max_pages=1,
                                  start_page=current_page)
        except Exception as e:
            logger.error("page=%d 列表 API 失败: %s, 停止", current_page, e)
            break

        if not accounts:
            logger.info("page=%d 无数据 → 结束", current_page)
            break

        processed = 0
        for acc in accounts:
            # 1. 入库 accounts 表 (UNIQUE 去重)
            upsert_account(**acc)
            # 2. 获取详情→解析→特征→入 account_details
            if process_account(acc["source"], acc["product_id"],
                               acc["game_id"], acc["price"]):
                processed += 1

        total_fetched += len(accounts)
        total_processed += processed
        pages_done += 1

        logger.info("page=%d fetched=%d processed=%d",
                    current_page, len(accounts), processed)

        # 不足一页 → 最后一页
        if len(accounts) < page_size:
            logger.info("page=%d 不足一页 → 结束", current_page)
            break

    logger.info("完成: pages=%d, total_fetched=%d, total_processed=%d",
                pages_done, total_fetched, total_processed)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 验证 CLI 帮助可正常输出**

运行:
```powershell
uv run python -m backfill.pxb7 --help
```
Expected: 显示 usage 和参数说明，无报错

- [ ] **Step 3: 小规模实跑验证（1 页）**

运行:
```powershell
uv run python -m backfill.pxb7 --game-id 10302 --start-page 1 --max-pages 1
```
Expected: 输出 `page=1 fetched=N processed=M`，`完成: pages=1, total_fetched=N, total_processed=M`，无报错。数据库 `account_details` 表新增对应记录（`value` 为 NULL）。

- [ ] **Step 4: 验证 account_details 入库且 value 为 NULL**

运行:
```powershell
uv run python -c "from db import get_db; conn=get_db(); r=conn.execute('SELECT COUNT(*) as c, COUNT(value) as v FROM account_details').fetchone(); print(f'total={r[\"c\"]}, valued={r[\"v\"]}')"
```
Expected: `valued` 小于 `total`（向前爬虫写入的记录 value 为 NULL）

- [ ] **Step 5: Commit**

```powershell
git add backfill/pxb7.py
git commit -m "feat(backfill): 新增螃蟹向前爬虫 CLI"
```

---

### Task 6: backfill/[已移除].py CLI

螃蟹向前爬虫脚本：复用浏览器实例 + 调 process_account。

**Files:**
- Create: `backfill/[已移除].py`

- [ ] **Step 1: 创建 backfill/[已移除].py**

```python
"""螃蟹向前爬虫 CLI

从指定页码向前翻页爬取历史在售账号, 列表→详情→解析→特征→入库。
复用 crawler.[已移除] 的浏览器实例 (WASM 签名必需)。
不计算价值 (交 main.py valuer_loop 补全)。

用法:
  uv run python -m backfill.[已移除] --game-id 303 --start-page 4 --max-pages 50
"""
from __future__ import annotations

import argparse
import logging
import sys

from crawler.[已移除] import crawl as crawl_[已移除]
from db import upsert_account
from .common import process_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="螃蟹向前爬虫 (历史数据回填)")
    parser.add_argument("--game-id", required=True, help="游戏ID (如 303=鸣潮)")
    parser.add_argument("--start-page", type=int, required=True, help="起始页码")
    parser.add_argument("--max-pages", type=int, required=True, help="最多翻页数")
    parser.add_argument("--page-size", type=int, default=10, help="每页条数 (默认 10)")
    parser.add_argument("--platform", default="6", help="商品分类ID (默认 6=成品号)")
    args = parser.parse_args()

    game_id = args.game_id
    start_page = args.start_page
    max_pages = args.max_pages
    page_size = args.page_size
    platform = args.platform

    logger.info("开始: game=%s start_page=%d max_pages=%d page_size=%d platform=%s",
                game_id, start_page, max_pages, page_size, platform)

    total_fetched = 0
    total_processed = 0
    pages_done = 0

    for page_offset in range(max_pages):
        current_page = start_page + page_offset
        try:
            # 翻一页 (crawl 内部会翻 max_pages 页, 这里每次传 1 页)
            accounts = crawl_[已移除](game_id, platform=platform, max_pages=1,
                                  page_size=page_size, start_page=current_page)
        except Exception as e:
            logger.error("page=%d 列表 API 失败: %s, 停止", current_page, e)
            break

        if not accounts:
            logger.info("page=%d 无数据 → 结束", current_page)
            break

        processed = 0
        for acc in accounts:
            # 1. 入库 accounts 表 (UNIQUE 去重)
            upsert_account(**acc)
            # 2. 获取详情→解析→特征→入 account_details
            if process_account(acc["source"], acc["product_id"],
                               acc["game_id"], acc["price"], platform=platform):
                processed += 1

        total_fetched += len(accounts)
        total_processed += processed
        pages_done += 1

        logger.info("page=%d fetched=%d processed=%d",
                    current_page, len(accounts), processed)

        # 不足一页 → 最后一页
        if len(accounts) < page_size:
            logger.info("page=%d 不足一页 → 结束", current_page)
            break

    logger.info("完成: pages=%d, total_fetched=%d, total_processed=%d",
                pages_done, total_fetched, total_processed)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 验证 CLI 帮助可正常输出**

运行:
```powershell
uv run python -m backfill.[已移除] --help
```
Expected: 显示 usage 和参数说明（含 `--platform`），无报错

- [ ] **Step 3: 小规模实跑验证（1 页）**

运行:
```powershell
uv run python -m backfill.[已移除] --game-id 303 --start-page 1 --max-pages 1
```
Expected: 浏览器启动 → `page=1 fetched=N processed=M` → `完成: pages=1, total_fetched=N, total_processed=M`，无报错。注意：首次运行需 `patchright install chromium`。

- [ ] **Step 4: 验证 account_details 入库且 value 为 NULL**

运行:
```powershell
uv run python -c "from db import get_db; conn=get_db(); r=conn.execute('SELECT source, COUNT(*) as c, COUNT(value) as v FROM account_details GROUP BY source').fetchall(); [print(f'{row[\"source\"]}: total={row[\"c\"]}, valued={row[\"v\"]}') for row in r]"
```
Expected: `[已移除]` 行的 `valued` 小于 `total`（或为 0）

- [ ] **Step 5: Commit**

```powershell
git add backfill/[已移除].py
git commit -m "feat(backfill): 新增螃蟹向前爬虫 CLI"
```

---

## 实现顺序依赖

```
Task 1 (db.get_account_id) ──┐
Task 2 (pxb7 start_page)  ───┼──→ Task 4 (common.py) ──→ Task 5 (pxb7 CLI)
Task 3 ([已移除] start_page)  ───┘                      └──→ Task 6 ([已移除] CLI)
```

Task 1/2/3 互相独立可并行，Task 4 依赖全部三者，Task 5/6 依赖 Task 4 且互相独立。

## 验证清单

- [ ] `python -m backfill.pxb7 --help` 正常
- [ ] `python -m backfill.[已移除] --help` 正常
- [ ] 螃蟹爬 1 页：accounts + account_details 有数据，value 为 NULL
- [ ] 螃蟹爬 1 页：accounts + account_details 有数据，value 为 NULL
- [ ] main.py 的定时爬虫不受影响（start_page 默认 1）
- [ ] main.py 的 valuer_loop 能捞到向前爬虫写入的未估价账号（`value IS NULL`）

