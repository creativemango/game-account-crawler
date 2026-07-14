"""LightGBM 分位数回归模型

架构:
  - 3 个独立 LightGBM booster (P10 / P50 / P90)
  - objective='quantile', alpha=0.1/0.5/0.9
  - log 变换价格 (长尾分布稳定)
  - Early Stopping (验证分位数损失不下降 50 轮停止)
  - 80/20 训练验证分割

相比 MLP 的优势:
  - 表格数据 SOTA, 自动捕捉非线性阈值效应 + 特征交互
  - 小样本稳定, 树模型尺度无关无需标准化
  - 原生 quantile loss, 三个分位数解耦训练 (P90 不被 P50 拖偏)
  - 训练快无需 GPU, 可输出特征重要性

预测:
  - 输出 [P10, P50, P90] 价格区间 (exp 还原 log)
  - 单调约束: 逐级 np.maximum 确保 P10 <= P50 <= P90
  - value_ratio = P50 / 实际价格 (>1 = 低于市场价, 划算)
  - score = log(ratio) 经 sigmoid 归一化到 0-100
"""
from __future__ import annotations

import json
import logging
import math

import numpy as np
import lightgbm as lgb

from .features import FEATURE_NAMES, MIN_SAMPLES, features_to_vector

logger = logging.getLogger(__name__)

# 分位数 (P10=低价, P50=中位, P90=高价)
QUANTILES = [0.1, 0.5, 0.9]

# LightGBM 超参 (兼顾小样本与未来 1-10 万规模)
NUM_LEAVES = 31
LEARNING_RATE = 0.05
N_ESTIMATORS = 1000
MIN_CHILD_SAMPLES = 20  # 叶子最小样本数, 调高防小样本过拟合
SUBSAMPLE = 0.8
SUBSAMPLE_FREQ = 1
COLSAMPLE_BYTREE = 0.8
REG_ALPHA = 0.1         # L1 正则
REG_LAMBDA = 1.0        # L2 正则
EARLY_STOPPING_ROUNDS = 50
VAL_RATIO = 0.2
RANDOM_SEED = 42


def _quantile_loss(y_true: np.ndarray, y_pred: np.ndarray, q: float) -> float:
    """分位数损失 (Pinball Loss, 用于评估验证集)"""
    errors = y_true - y_pred
    return float(np.mean(np.where(errors >= 0, q * errors, (q - 1) * errors)))


class ValuerModel:
    """价值评估模型 (3 个 LightGBM 分位数 booster)"""

    def __init__(self):
        self.boosters: list[lgb.Booster] | None = None
        self.is_trained = False
        self.feature_importances: dict[str, float] | None = None

    def train(self, X: np.ndarray, y: np.ndarray) -> dict:
        """训练 3 个分位数模型

        Args:
            X: (n, INPUT_DIM) 特征矩阵
            y: (n,) 原始价格 (内部 log 变换)

        Returns:
            训练信息 dict
        """
        n = len(X)
        if n < MIN_SAMPLES:
            return {"error": f"样本不足: {n} < {MIN_SAMPLES}"}

        # 1. log 变换价格 (长尾分布稳定)
        y_log = np.log(np.maximum(y, 1.0))

        # 2. 80/20 分割 (固定种子可复现)
        rng = np.random.default_rng(RANDOM_SEED)
        idx = rng.permutation(n)
        n_train = int(n * (1 - VAL_RATIO))
        train_idx, val_idx = idx[:n_train], idx[n_train:]
        X_train, y_train = X[train_idx], y_log[train_idx]
        X_val, y_val = X[val_idx], y_log[val_idx]

        # 3. 复用 Dataset (三个分位数共享数据)
        train_data = lgb.Dataset(X_train, label=y_train, feature_name=FEATURE_NAMES)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data, feature_name=FEATURE_NAMES)

        # 4. 训练 3 个独立分位数 booster
        self.boosters = []
        val_losses = []
        for q in QUANTILES:
            params = {
                "objective": "quantile",
                "alpha": q,
                "num_leaves": NUM_LEAVES,
                "learning_rate": LEARNING_RATE,
                "min_child_samples": MIN_CHILD_SAMPLES,
                "subsample": SUBSAMPLE,
                "subsample_freq": SUBSAMPLE_FREQ,
                "colsample_bytree": COLSAMPLE_BYTREE,
                "reg_alpha": REG_ALPHA,
                "reg_lambda": REG_LAMBDA,
                "verbose": -1,
                "force_col_wise": True,
                "seed": RANDOM_SEED,
            }
            booster = lgb.train(
                params,
                train_data,
                num_boost_round=N_ESTIMATORS,
                valid_sets=[val_data],
                callbacks=[
                    lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False),
                    lgb.log_evaluation(0),
                ],
            )
            self.boosters.append(booster)
            val_pred = booster.predict(X_val)
            vloss = _quantile_loss(y_val, val_pred, q)
            val_losses.append(vloss)
            logger.info("q=%.1f: best_iter=%d val_loss=%.4f",
                        q, booster.best_iteration, vloss)

        self.is_trained = True

        # 5. 特征重要性 (取 P50 模型, gain 类型)
        imp = self.boosters[1].feature_importance(importance_type="gain")
        self.feature_importances = dict(zip(FEATURE_NAMES, imp.tolist()))

        avg_val = float(np.mean(val_losses))
        logger.info("训练完成: samples=%d, avg_val_loss=%.4f", n, avg_val)
        return {
            "samples": n,
            "train_samples": n_train,
            "val_samples": n - n_train,
            "val_losses": {f"q{q}": v for q, v in zip(QUANTILES, val_losses)},
            "avg_val_loss": avg_val,
            "feature_importances": self.feature_importances,
        }

    def predict(self, X: np.ndarray) -> np.ndarray:
        """预测价格区间

        Args:
            X: (n, INPUT_DIM) 特征矩阵

        Returns:
            (n, 3) 数组, 每行 [P10, P50, P90] (exp 还原后的原始价格, 满足单调递增)
        """
        if not self.is_trained or not self.boosters:
            raise RuntimeError("模型未训练")

        # 3 个 booster 预测 log 价格 → exp 还原
        preds = np.column_stack([b.predict(X) for b in self.boosters])
        preds = np.exp(preds)

        # 单调约束: 逐级 maximum 确保 P10 <= P50 <= P90
        # (独立训练可能出现交叉, 用 maximum 保留各分位数主体预测只修正违反单调部分)
        p10 = preds[:, 0]
        p50 = np.maximum(preds[:, 1], p10)
        p90 = np.maximum(preds[:, 2], p50)
        return np.column_stack([p10, p50, p90])

    def to_state(self) -> dict:
        """序列化模型状态 (存入数据库, JSON 兼容)"""
        if not self.is_trained or not self.boosters:
            raise RuntimeError("模型未训练")
        return {
            "boosters": [b.model_to_string() for b in self.boosters],
            "quantiles": QUANTILES,
            "feature_names": FEATURE_NAMES,
            "feature_importances": self.feature_importances,
        }

    def from_state(self, state: dict):
        """从状态恢复模型"""
        self.boosters = [lgb.Booster(model_str=s) for s in state["boosters"]]
        self.feature_importances = state.get("feature_importances")
        self.is_trained = True


# ===== 数据库交互 (接口与 MLP 版本一致) =====

def train_and_save(game_id: str, samples: list[dict]) -> dict:
    """训练并保存模型到数据库

    Args:
        game_id: 游戏ID
        samples: [{"features": {...}, "price": float}, ...]

    Returns:
        训练信息
    """
    from db import save_weights

    n = len(samples)
    if n < MIN_SAMPLES:
        logger.info("游戏 %s 样本不足 %d < %d, 跳过训练", game_id, n, MIN_SAMPLES)
        return {"error": "样本不足", "samples": n, "min_required": MIN_SAMPLES}

    X = np.array([features_to_vector(s["features"]) for s in samples])
    y = np.array([float(s["price"]) for s in samples])

    model = ValuerModel()
    info = model.train(X, y)
    if "error" in info:
        return info

    state_json = json.dumps(model.to_state(), ensure_ascii=False)
    save_weights(
        game_id=game_id,
        weights=state_json,
        intercept=0.0,
        feature_names=FEATURE_NAMES,
        sample_count=n,
    )

    logger.info("模型已保存: game=%s, samples=%d", game_id, n)
    return info


def _load_model(game_id: str) -> ValuerModel | None:
    """从数据库加载模型"""
    from db import get_weights

    row = get_weights(game_id)
    if not row:
        return None

    try:
        state = json.loads(row["weights"]) if isinstance(row["weights"], str) else row["weights"]
        model = ValuerModel()
        model.from_state(state)
        return model
    except Exception as e:
        logger.error("加载模型失败: %s", e)
        return None


def predict_value(game_id: str, features: dict) -> dict | None:
    """预测单个账号的价格区间

    Args:
        game_id: 游戏ID
        features: extract_features() 返回的特征 dict

    Returns:
        {"p10": float, "p50": float, "p90": float} 或 None (模型未训练)
    """
    model = _load_model(game_id)
    if not model:
        return None

    vec = features_to_vector(features)
    X = np.array([vec])
    preds = model.predict(X)[0]

    return {
        "p10": float(preds[0]),
        "p50": float(preds[1]),
        "p90": float(preds[2]),
    }


def is_model_ready(game_id: str) -> bool:
    """模型是否已训练"""
    from db import get_weights

    row = get_weights(game_id)
    return row is not None and row["sample_count"] >= MIN_SAMPLES


def compute_score(value_ratio: float) -> float:
    """计算 0-100 评分

    value_ratio = P50 / 实际价格
      ratio > 1: 实际价 < 预测中位价 → 划算 → 高分
      ratio = 1: 正好中位 → 50 分
      ratio < 1: 实际价 > 预测中位价 → 偏贵 → 低分

    使用 log 变换 + sigmoid 归一化, 避免极端值影响。
    """
    if value_ratio <= 0:
        return 0.0
    # log(ratio) 映射到 [-∞, +∞], ratio=1 → 0
    log_ratio = math.log(value_ratio)
    # sigmoid: 0 → 0.5, 正值 → 接近 1, 负值 → 接近 0
    # 乘以 3 放大灵敏度: ratio=1.5 → log=0.4 → sigmoid(1.2)=0.77 → 77 分
    score = 100.0 / (1.0 + math.exp(-3.0 * log_ratio))
    return round(score, 1)
