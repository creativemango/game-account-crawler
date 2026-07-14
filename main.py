"""爬虫 + API + 详情轮询 + 价值评估 一体化入口"""
import json
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import yaml
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from db import (
    init_db, upsert_account, get_active_for_check,
    mark_sold, mark_active, search_accounts, get_account, get_stats,
    upsert_detail, get_detail, get_unvalued_accounts,
    get_training_data, get_weights, get_all_weights,
)
from crawler.pxb7 import crawl as crawl_pxb7, check_detail as check_pxb7, fetch_detail as fetch_pxb7_detail
from crawler.pzds import crawl as crawl_pzds, check_detail as check_pzds
from crawler.base import CrawlerError
from parser import parse_pxb7, parse_pzds
from valuer import extract_features, train_and_save, predict_value, compute_score, is_model_ready, MIN_SAMPLES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="游戏账号交易爬虫")
app.mount("/static", StaticFiles(directory="static"), name="static")
DETAIL_CHECK_INTERVAL = 600  # 10 分钟
DETAIL_CHECK_WORKERS = 5


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_crawl_loop(config: dict):
    interval = config["crawl"]["interval_seconds"]
    max_pages = config["crawl"].get("max_pages", 3)
    while True:
        new_count = 0
        update_count = 0

        if config["sources"].get("pxb7", {}).get("enabled"):
            for game_id in config["sources"]["pxb7"]["games"]:
                try:
                    accounts = crawl_pxb7(game_id, max_pages=max_pages)
                    for a in accounts:
                        if upsert_account(**a):
                            new_count += 1
                        else:
                            update_count += 1
                    print(f"[pxb7] game={game_id} fetched={len(accounts)}")
                except CrawlerError as e:
                    print(f"[pxb7] game={game_id} error: {e}")

        if config["sources"].get("pzds", {}).get("enabled"):
            platform = config["sources"]["pzds"].get("platform", "6")
            for game_id in config["sources"]["pzds"]["games"]:
                try:
                    accounts = crawl_pzds(game_id, platform, max_pages=max_pages)
                    for a in accounts:
                        if upsert_account(**a):
                            new_count += 1
                        else:
                            update_count += 1
                    print(f"[pzds] game={game_id} fetched={len(accounts)}")
                except CrawlerError as e:
                    print(f"[pzds] game={game_id} error: {e}")

        print(f"Done: new={new_count} updated={update_count}")
        time.sleep(interval)


def run_detail_check_loop():
    """定时轮询详情接口，检测已售出商品"""
    while True:
        products = get_active_for_check(DETAIL_CHECK_INTERVAL // 60)
        if not products:
            time.sleep(DETAIL_CHECK_INTERVAL)
            continue

        sold_count = 0
        with ThreadPoolExecutor(max_workers=DETAIL_CHECK_WORKERS) as pool:
            futures = {}
            for p in products:
                if p["source"] == "pxb7":
                    f = pool.submit(check_pxb7, p["product_id"])
                else:
                    f = pool.submit(check_pzds, p["product_id"])
                futures[f] = p

            for f in as_completed(futures):
                p = futures[f]
                try:
                    is_active = f.result()
                except Exception:
                    is_active = True  # 异常时保留原状态

                if is_active:
                    mark_active(p["product_id"], p["source"])
                else:
                    mark_sold(p["product_id"], p["source"])
                    sold_count += 1

        print(f"[detail] checked={len(products)} sold={sold_count}")
        time.sleep(DETAIL_CHECK_INTERVAL)


# ===== 价值评估 =====
VALUER_INTERVAL = 300  # 价值计算间隔（秒）
VALUER_BATCH = 20      # 每轮处理条数
TRAIN_INTERVAL = 86400  # 每日训练（秒）


def _fetch_and_parse(source: str, product_id: str, game_id: str) -> tuple[dict, dict] | None:
    """获取详情并解析为 (parsed_data, features)"""
    try:
        if source == "pxb7":
            detail = fetch_pxb7_detail(product_id)
            if not detail:
                return None
            parsed = parse_pxb7(detail).to_dict()
        elif source == "pzds":
            # 盼之详情需要浏览器，用异步 client（在单独线程跑 event loop）
            import asyncio
            from crawler.pzds import _get_client, _get_loop
            loop = _get_loop()
            client = asyncio.run_coroutine_threadsafe(
                _get_client(game_id, "6"), loop
            ).result(timeout=60)
            detail = asyncio.run_coroutine_threadsafe(
                client.fetch_goods_detail(product_id), loop
            ).result(timeout=60)
            parsed = parse_pzds(detail).to_dict()
        else:
            return None
        return parsed
    except Exception as e:
        logger.error("解析失败 %s/%s: %s", source, product_id, e)
        return None


def run_valuer_loop():
    """异步价值计算 worker：定期处理未估价的账号"""
    while True:
        try:
            pending = get_unvalued_accounts(limit=VALUER_BATCH)
            if not pending:
                time.sleep(VALUER_INTERVAL)
                continue

            computed = 0
            for acc in pending:
                parsed = _fetch_and_parse(acc["source"], acc["product_id"], acc["game_id"])
                if not parsed:
                    continue

                features = extract_features(parsed, acc["source"], acc["price"])

                # 预测价值（模型未训练则为 None）
                value = None
                score = None
                ratio = None
                if is_model_ready(acc["game_id"]):
                    pred = predict_value(acc["game_id"], features)
                    if pred:
                        value = pred["p50"]
                        ratio = value / acc["price"] if acc["price"] > 0 else None
                        score = compute_score(ratio) if ratio else None

                upsert_detail(
                    account_id=acc["id"],
                    game_id=acc["game_id"],
                    source=acc["source"],
                    parsed_data=parsed,
                    features=features,
                    value=value,
                    score=score,
                    value_ratio=ratio,
                )
                computed += 1

            logger.info("[valuer] computed=%d/%d", computed, len(pending))
        except Exception as e:
            logger.error("[valuer] error: %s", e)

        time.sleep(10)  # 短间隔继续处理剩余


def run_train_loop():
    """每日训练价值模型"""
    while True:
        time.sleep(TRAIN_INTERVAL)
        try:
            config = load_config()
            # 收集所有游戏的训练数据
            seen_games = set()
            for src_key, src_val in config["sources"].items():
                if not src_val.get("enabled"):
                    continue
                for gid in src_val.get("games", []):
                    seen_games.add(gid)

            for game_id in seen_games:
                samples_raw = get_training_data(game_id)
                if len(samples_raw) < MIN_SAMPLES:
                    logger.info("[train] %s 样本不足 %d < %d", game_id, len(samples_raw), MIN_SAMPLES)
                    continue
                samples = [
                    {"features": json.loads(s["features"]), "price": s["price"]}
                    for s in samples_raw
                ]
                info = train_and_save(game_id, samples)
                logger.info("[train] %s: %s", game_id, info)
        except Exception as e:
            logger.error("[train] error: %s", e)


@app.on_event("startup")
def start_background_tasks():
    init_db()
    config = load_config()

    crawl_thread = threading.Thread(target=run_crawl_loop, args=(config,), daemon=True)
    crawl_thread.start()
    print(f"Crawler started, interval={config['crawl']['interval_seconds']}s")

    detail_thread = threading.Thread(target=run_detail_check_loop, daemon=True)
    detail_thread.start()
    print(f"Detail checker started, interval={DETAIL_CHECK_INTERVAL}s, workers={DETAIL_CHECK_WORKERS}")

    valuer_thread = threading.Thread(target=run_valuer_loop, daemon=True)
    valuer_thread.start()
    print(f"Valuer started, interval={VALUER_INTERVAL}s, batch={VALUER_BATCH}")

    train_thread = threading.Thread(target=run_train_loop, daemon=True)
    train_thread.start()
    print(f"Trainer started, interval={TRAIN_INTERVAL}s")


@app.get("/")
def index():
    return FileResponse("static/index.html")
@app.get("/api/mappings")
def mappings():
    config = load_config()
    source_names = {}
    for key, val in config["sources"].items():
        source_names[key] = val.get("name", key)
    return {
        "sources": source_names,
        "games": config.get("game_names", {}),
    }


@app.get("/api/accounts")
def list_accounts(
    source: str = Query(None),
    game_id: str = Query(None),
    game_name: str = Query(None),
    keyword: str = Query(None),
    min_price: float = Query(None),
    max_price: float = Query(None),
    include_sold: bool = Query(False),
    sort: str = Query(None, description="排序: value_ratio_desc=性价比降序, value_desc=价值降序, score_desc=评分降序"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    # 按游戏名搜索时，解析为多个 (source, game_id) 对
    game_ids = []
    if game_name:
        config = load_config()
        name_map = config.get("game_names", {}).get(game_name, {})
        game_ids = list(name_map.items())  # [(source, id), ...]
    return search_accounts(
        source=source, game_id=game_id, game_ids=game_ids or None,
        keyword=keyword, min_price=min_price, max_price=max_price,
        is_active=not include_sold, sort=sort, page=page, size=size
    )


@app.get("/api/accounts/{account_id}")
def get_account_detail(account_id: int):
    acc = get_account(account_id)
    if not acc:
        return {"error": "not found"}
    # 附带详情和估价
    detail = get_detail(account_id)
    if detail:
        acc["parsed_data"] = json.loads(detail["parsed_data"]) if detail.get("parsed_data") else None
        acc["features"] = json.loads(detail["features"]) if detail.get("features") else None
        acc["value"] = detail.get("value")
        acc["score"] = detail.get("score")
        acc["value_ratio"] = detail.get("value_ratio")
    return acc


@app.get("/api/stats")
def stats():
    return get_stats()


# ===== 价值评估 API =====

@app.post("/api/valuer/train")
def trigger_train(game_id: str = Query(None)):
    """手动触发模型训练"""
    config = load_config()
    games_to_train = []
    if game_id:
        games_to_train = [game_id]
    else:
        seen = set()
        for src_val in config["sources"].values():
            if src_val.get("enabled"):
                seen.update(src_val.get("games", []))
        games_to_train = list(seen)

    results = {}
    for gid in games_to_train:
        samples_raw = get_training_data(gid)
        if len(samples_raw) < MIN_SAMPLES:
            results[gid] = {"error": "样本不足", "samples": len(samples_raw), "min_required": MIN_SAMPLES}
            continue
        samples = [
            {"features": json.loads(s["features"]), "price": s["price"]}
            for s in samples_raw
        ]
        results[gid] = train_and_save(gid, samples)
    return {"results": results}


@app.get("/api/valuer/weights")
def list_weights():
    """查看所有游戏的模型权重信息"""
    return {"weights": get_all_weights()}


@app.get("/api/valuer/status")
def valuer_status():
    """价值评估状态"""
    config = load_config()
    seen_games = set()
    for src_val in config["sources"].values():
        if src_val.get("enabled"):
            seen_games.update(src_val.get("games", []))

    status = {}
    for gid in seen_games:
        w = get_weights(gid)
        status[gid] = {
            "ready": w is not None and w["sample_count"] >= MIN_SAMPLES,
            "sample_count": w["sample_count"] if w else 0,
            "trained_at": w["trained_at"] if w else None,
            "min_required": MIN_SAMPLES,
        }
    return status
