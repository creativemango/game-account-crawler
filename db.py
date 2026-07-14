import sqlite3
import json
from datetime import datetime, timedelta

DB_PATH = "accounts.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            game_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            title TEXT,
            price REAL,
            raw_data TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            last_detail_check TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            first_seen_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source, product_id)
        );
        CREATE INDEX IF NOT EXISTS idx_source_game ON accounts(source, game_id);
        CREATE INDEX IF NOT EXISTS idx_price ON accounts(price);
        CREATE INDEX IF NOT EXISTS idx_created ON accounts(created_at);
        CREATE INDEX IF NOT EXISTS idx_active_check ON accounts(is_active, last_detail_check);
        CREATE VIRTUAL TABLE IF NOT EXISTS accounts_fts USING fts5(
            title, content='accounts', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS accounts_ai AFTER INSERT ON accounts BEGIN
            INSERT INTO accounts_fts(rowid, title) VALUES (new.id, new.title);
        END;
        CREATE TRIGGER IF NOT EXISTS accounts_ad AFTER DELETE ON accounts BEGIN
            INSERT INTO accounts_fts(accounts_fts, rowid, title) VALUES('delete', old.id, old.title);
        END;
        CREATE TRIGGER IF NOT EXISTS accounts_au AFTER UPDATE ON accounts BEGIN
            INSERT INTO accounts_fts(accounts_fts, rowid, title) VALUES('delete', old.id, old.title);
            INSERT INTO accounts_fts(rowid, title) VALUES (new.id, new.title);
        END;
        -- 账号详情 + 价值评估
        CREATE TABLE IF NOT EXISTS account_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            game_id TEXT NOT NULL,
            source TEXT NOT NULL,
            parsed_data TEXT NOT NULL,
            features TEXT NOT NULL,
            value REAL,
            score REAL,
            value_ratio REAL,
            computed_at TEXT DEFAULT (datetime('now')),
            UNIQUE(account_id),
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_details_game ON account_details(game_id);
        CREATE INDEX IF NOT EXISTS idx_details_value_ratio ON account_details(value_ratio);
        CREATE INDEX IF NOT EXISTS idx_details_value ON account_details(value);
        -- 价值模型权重（按游戏训练）
        CREATE TABLE IF NOT EXISTS valuer_weights (
            game_id TEXT PRIMARY KEY,
            weights TEXT NOT NULL,
            intercept REAL NOT NULL,
            feature_names TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            trained_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def upsert_account(source: str, game_id: str, product_id: str,
                   title: str, price: float, raw_data: dict) -> bool:
    """返回 True 表示新插入，False 表示已存在"""
    conn = get_db()
    raw_json = json.dumps(raw_data, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = conn.execute(
        "SELECT id FROM accounts WHERE source=? AND product_id=?",
        (source, product_id)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE accounts SET title=?, price=?, raw_data=?, is_active=1
               WHERE source=? AND product_id=?""",
            (title, price, raw_json, source, product_id)
        )
        conn.commit()
        conn.close()
        return False
    else:
        conn.execute(
            """INSERT INTO accounts (source, game_id, product_id, title, price, raw_data, is_active, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (source, game_id, product_id, title, price, raw_json, now)
        )
        conn.commit()
        conn.close()
        return True


def get_active_for_check(interval_minutes: int = 10) -> list[dict]:
    """获取需要详情检查的在售商品"""
    conn = get_db()
    threshold = (datetime.now() - timedelta(minutes=interval_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """SELECT * FROM accounts
           WHERE is_active=1
             AND (last_detail_check IS NULL OR last_detail_check < ?)
           ORDER BY last_detail_check ASC LIMIT 100""",
        (threshold,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_sold(product_id: str, source: str):
    """标记商品为已售出"""
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE accounts SET is_active=0, last_detail_check=? WHERE product_id=? AND source=?",
        (now, product_id, source)
    )
    conn.commit()
    conn.close()


def mark_active(product_id: str, source: str):
    """标记商品仍在售（刷新检查时间）"""
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE accounts SET is_active=1, last_detail_check=? WHERE product_id=? AND source=?",
        (now, product_id, source)
    )
    conn.commit()
    conn.close()


def search_accounts(source: str = None, game_id: str = None,
                    game_ids: list = None, keyword: str = None,
                    min_price: float = None, max_price: float = None,
                    is_active: bool = True, sort: str = None,
                    page: int = 1, size: int = 20):
    conn = get_db()
    conditions = []
    params = []

    if is_active is not None:
        conditions.append("a.is_active=1" if is_active else "a.is_active=0")
    if source:
        conditions.append("a.source=?")
        params.append(source)
    if game_id:
        conditions.append("a.game_id=?")
        params.append(game_id)
    if game_ids:
        # game_ids: [(source, game_id), ...] 多来源多游戏
        placeholders = []
        for src, gid in game_ids:
            placeholders.append("(a.source=? AND a.game_id=?)")
            params.extend([src, gid])
        conditions.append("(" + " OR ".join(placeholders) + ")")
    if min_price is not None:
        conditions.append("a.price>=?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("a.price<=?")
        params.append(max_price)
    if keyword:
        conditions.append("a.id IN (SELECT rowid FROM accounts_fts WHERE title MATCH ?)")
        params.append(keyword)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # 排序
    order_by = "a.created_at DESC"
    join_detail = ""
    if sort in ("value_ratio_desc", "value_desc", "score_desc"):
        join_detail = "LEFT JOIN account_details d ON a.id = d.account_id"
        # SQLite 不支持 NULLS LAST，用 CASE 模拟（非 NULL 排前面）
        if sort == "value_ratio_desc":
            order_by = "CASE WHEN d.value_ratio IS NULL THEN 1 ELSE 0 END, d.value_ratio DESC"
        elif sort == "value_desc":
            order_by = "CASE WHEN d.value IS NULL THEN 1 ELSE 0 END, d.value DESC"
        elif sort == "score_desc":
            order_by = "CASE WHEN d.score IS NULL THEN 1 ELSE 0 END, d.score DESC"

    offset = (page - 1) * size

    total = conn.execute(
        f"SELECT COUNT(*) FROM accounts a {join_detail} {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT a.*, d.value, d.score, d.value_ratio, d.parsed_data
            FROM accounts a
            {join_detail}
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?""",
        params + [size, offset]
    ).fetchall()

    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        # parsed_data 可能很大，列表页只返回摘要
        if d.get("parsed_data"):
            d["parsed_data"] = None  # 列表不返回完整解析数据，用 /api/accounts/{id} 查看
        items.append(d)
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": items
    }


def get_account(account_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_account_id(source: str, product_id: str) -> int | None:
    """按 source + product_id 查询账号 id

    用于 upsert_account 之后获取 account_id，以便调用 upsert_detail。
    accounts 表有 UNIQUE(source, product_id) 约束，查询唯一。
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM accounts WHERE source=? AND product_id=?",
        (source, product_id),
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def get_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM accounts WHERE is_active=1").fetchone()[0]
    by_source = [dict(r) for r in conn.execute(
        "SELECT source, COUNT(*) as count FROM accounts WHERE is_active=1 GROUP BY source"
    ).fetchall()]
    by_game = [dict(r) for r in conn.execute(
        "SELECT source, game_id, COUNT(*) as count FROM accounts WHERE is_active=1 GROUP BY source, game_id"
    ).fetchall()]
    sold = conn.execute("SELECT COUNT(*) FROM accounts WHERE is_active=0").fetchone()[0]
    conn.close()
    return {"total": total, "sold": sold, "by_source": by_source, "by_game": by_game}


# ===== 账号详情 =====

def upsert_detail(account_id: int, game_id: str, source: str,
                  parsed_data: dict, features: dict,
                  value: float | None = None, score: float | None = None,
                  value_ratio: float | None = None):
    """插入/更新账号详情和价值评估"""
    conn = get_db()
    conn.execute("""
        INSERT INTO account_details
            (account_id, game_id, source, parsed_data, features, value, score, value_ratio, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(account_id) DO UPDATE SET
            parsed_data=excluded.parsed_data,
            features=excluded.features,
            value=excluded.value,
            score=excluded.score,
            value_ratio=excluded.value_ratio,
            computed_at=datetime('now')
    """, (
        account_id, game_id, source,
        json.dumps(parsed_data, ensure_ascii=False),
        json.dumps(features, ensure_ascii=False),
        value, score, value_ratio,
    ))
    conn.commit()
    conn.close()


def get_detail(account_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM account_details WHERE account_id=?", (account_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_unvalued_accounts(game_id: str = None, limit: int = 100) -> list[dict]:
    """获取有 price 但未计算价值的账号（用于异步计算）"""
    conn = get_db()
    sql = """
        SELECT a.id, a.source, a.game_id, a.product_id, a.price, a.raw_data
        FROM accounts a
        LEFT JOIN account_details d ON a.id = d.account_id
        WHERE a.is_active=1 AND a.price IS NOT NULL AND a.price > 0
          AND d.id IS NULL
    """
    params = []
    if game_id:
        sql += " AND a.game_id=?"
        params.append(game_id)
    sql += " LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_training_data(game_id: str) -> list[dict]:
    """获取某游戏的训练数据（已计算价值且有价格的）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT a.price, d.features, d.parsed_data
        FROM account_details d
        JOIN accounts a ON a.id = d.account_id
        WHERE d.game_id=? AND a.price IS NOT NULL AND a.price > 0
    """, (game_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===== 价值模型权重 =====

def save_weights(game_id: str, weights: str | list, intercept: float,
                 feature_names: list[str], sample_count: int):
    """保存模型权重

    Args:
        weights: 权重 JSON 字符串（NN 完整 state）或权重列表（线性回归）
        intercept: 截距（NN 时为 0，权重在 state 内）
        feature_names: 特征名列表
        sample_count: 训练样本数
    """
    conn = get_db()
    weights_json = weights if isinstance(weights, str) else json.dumps(weights)
    conn.execute("""
        INSERT INTO valuer_weights (game_id, weights, intercept, feature_names, sample_count, trained_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(game_id) DO UPDATE SET
            weights=excluded.weights,
            intercept=excluded.intercept,
            feature_names=excluded.feature_names,
            sample_count=excluded.sample_count,
            trained_at=datetime('now')
    """, (
        game_id,
        weights_json,
        intercept,
        json.dumps(feature_names, ensure_ascii=False),
        sample_count,
    ))
    conn.commit()
    conn.close()


def get_weights(game_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM valuer_weights WHERE game_id=?", (game_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["weights"] = json.loads(d["weights"])
    d["feature_names"] = json.loads(d["feature_names"])
    return d


def get_all_weights() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM valuer_weights").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["weights"] = json.loads(d["weights"])
        d["feature_names"] = json.loads(d["feature_names"])
        result.append(d)
    return result
