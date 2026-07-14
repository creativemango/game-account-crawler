"""账号价值评估模块

基于 LightGBM 分位数回归预测账号合理价格区间。

流程:
  1. parser 解析商品 → ParsedAccount
  2. valuer.features 提取特征向量
  3. valuer.model 训练 3 个 LightGBM 分位数模型 (按游戏, P10/P50/P90)
  4. valuer.predict 预测 P10/P50/P90 价格区间
  5. value_ratio = P50 / 实际价格 (>1 = 低于市场价, 划算)
"""
from .features import extract_features, FEATURE_NAMES, MIN_SAMPLES
from .model import (
    ValuerModel, train_and_save, predict_value, is_model_ready,
    compute_score,
)

__all__ = [
    "extract_features", "FEATURE_NAMES", "MIN_SAMPLES",
    "ValuerModel", "train_and_save", "predict_value", "is_model_ready",
    "compute_score",
]
