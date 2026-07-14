"""价值模型训练 CLI

独立训练命令, 不依赖 FastAPI 启动, 从数据库读取已采集样本训练 LightGBM 分位回归模型。

用法:
  uv run python train.py                    # 训练所有已配置游戏
  uv run python train.py --game-id 303      # 仅训练指定游戏
"""
import argparse
import json
import logging
import sys

import yaml

from db import get_training_data
from valuer import train_and_save, MIN_SAMPLES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def train_game(game_id: str) -> dict:
    """训练单个游戏的价值模型, 返回训练信息或错误"""
    samples_raw = get_training_data(game_id)
    if len(samples_raw) < MIN_SAMPLES:
        return {
            "error": "样本不足",
            "samples": len(samples_raw),
            "min_required": MIN_SAMPLES,
        }
    samples = [
        {"features": json.loads(s["features"]), "price": s["price"]}
        for s in samples_raw
    ]
    return train_and_save(game_id, samples)


def main():
    parser = argparse.ArgumentParser(description="训练价值模型 (LightGBM 分位回归)")
    parser.add_argument("--game-id", default=None,
                        help="指定游戏ID (如 303=鸣潮); 不指定则训练所有已配置游戏")
    args = parser.parse_args()

    config = load_config()

    if args.game_id:
        games = [args.game_id]
    else:
        games = set()
        for src_val in config["sources"].values():
            if src_val.get("enabled"):
                games.update(src_val.get("games", []))
        games = sorted(games)

    logger.info("待训练游戏: %s", games)

    results = {}
    for gid in games:
        logger.info("开始训练: %s", gid)
        results[gid] = train_game(gid)
        info = results[gid]
        if "error" in info:
            logger.warning("[train] %s: %s (samples=%s)",
                           gid, info["error"], info.get("samples"))
        else:
            logger.info("[train] %s: %s", gid, info)

    print("\n===== 训练结果 =====")
    for gid, info in results.items():
        print(f"{gid}: {info}")

    # 任意成功则返回 0, 全部失败返回 1
    any_success = any("error" not in v for v in results.values())
    return 0 if any_success else 1


if __name__ == "__main__":
    sys.exit(main())
