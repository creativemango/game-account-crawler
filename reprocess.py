"""离线重处理：用改进后的解析器基于标题补齐已存账号的解析结果与特征

用途：修复早期解析器漏掉的原石/纠缠之缘/角色等数据后，无需重新抓取详情，
直接从 accounts.raw_data 中的标题文本补齐解析与特征，并更新 account_details。

用法:
  python reprocess.py [--limit N]
"""
import argparse
import json
import sqlite3

from parser.genshin import parse_pxb7
from valuer import extract_features

DB = "accounts.db"
GAME = "10026"


def main() -> None:
    parser = argparse.ArgumentParser(description="离线重处理账号解析与特征")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（默认全部）")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT d.account_id, d.parsed_data, a.price, a.title, a.raw_data
           FROM account_details d JOIN accounts a ON a.id = d.account_id
           WHERE d.game_id = ? ORDER BY d.account_id""",
        (GAME,),
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    updated = 0
    for row in rows:
        rd = json.loads(row["raw_data"]) if row["raw_data"] else {}
        product_name = rd.get("productName") or row["title"] or ""
        old = json.loads(row["parsed_data"]) if row["parsed_data"] else {}

        # 用改进后的解析器从标题提取（角色/数值兜底）
        pa = parse_pxb7({"productName": product_name})
        nd = pa.to_dict()

        # 合并：保留旧结构化数据（真实详情），标题只补缺失字段
        merged = dict(old)
        for key in ("yellow", "level", "star_sounds", "fuujin_waves",
                    "zhuchao_waves", "yubo_coral", "artifact_count"):
            if not merged.get(key):
                merged[key] = nd.get(key, 0)
        if not merged.get("five_star_chars"):
            merged["five_star_chars"] = nd.get("five_star_chars", [])
        if not merged.get("four_star_chars"):
            merged["four_star_chars"] = nd.get("four_star_chars", [])
        if not merged.get("five_star_weapons"):
            merged["five_star_weapons"] = nd.get("five_star_weapons", [])
        if not merged.get("skins"):
            merged["skins"] = nd.get("skins", [])
        merged["total_pulls"] = (
            float(merged.get("star_sounds") or 0) / 160
            + float(merged.get("fuujin_waves") or 0)
            + float(merged.get("zhuchao_waves") or 0)
        )

        features = extract_features(merged, "pxb7", row["price"], game_id=GAME)
        conn.execute(
            "UPDATE account_details SET parsed_data=?, features=? WHERE account_id=?",
            (
                json.dumps(merged, ensure_ascii=False),
                json.dumps(features, ensure_ascii=False),
                row["account_id"],
            ),
        )
        updated += 1

    conn.commit()
    conn.close()
    print(f"updated {updated} rows")


if __name__ == "__main__":
    main()
