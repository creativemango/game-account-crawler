# game-account-crawler

游戏账号交易爬虫 + 价值评估系统。抓取螃蟹（pxb7）交易平台的在售账号，解析为结构化资产数据，并使用 LightGBM 分位数回归模型预测合理价格区间，支持按性价比排序。

## 功能

- **爬取**：螃蟹 HTTP API 直连
- **结构化解析**：统一解析螃蟹数据为 `ParsedAccount`（黄数/等级/星声/浮金波纹/余波珊瑚/角色命座/武器精炼/队伍/服饰）
- **价值评估**：3 个 LightGBM 分位数模型（P10/P50/P90）预测价格区间，`value_ratio = P50 / 实际价格` 量化性价比
- **自动训练**：每日定时训练或独立 CLI 触发；冷启动时留空待回填（样本不足 `MIN_SAMPLES=200` 不训练）
- **售出检测**：定时轮询详情接口，标记下架商品
- **历史回填**：独立 backfill CLI 脚本，向前翻页爬取历史数据（仅提取特征，不计算价值）
- **Web API**：FastAPI 提供列表查询（含性价比排序）、详情、统计、训练触发等接口

## 项目结构

```
.
├── crawler/              # 爬虫
│   ├── pxb7.py           # 螃蟹: httpx 直连 + detailPost 接口
├── parser/
│   └── wuwa.py           # 鸣潮解析: 统一 ParsedAccount 数据结构
├── valuer/
│   ├── features.py       # 26 维特征提取
│   └── model.py          # LightGBM 分位数回归模型
├── backfill/             # 历史数据回填爬虫
│   ├── common.py         # 共享: process_account / process_account
│   ├── pxb7.py           # 螃蟹回填 CLI
├── db.py                 # SQLite (accounts + account_details + valuer_weights)
├── main.py               # FastAPI + 后台 worker (crawl/detail/valuer/train)
├── train.py              # 独立模型训练 CLI
├── config.yaml           # 数据源 + 爬取间隔 + worker 开关 + API 配置
└── static/index.html     # 前端查询页
```

## 数据流

```
crawl_loop ──→ upsert_account ──→ accounts 表
                                       │
valuer_loop ──→ fetch_detail ──→ parse ──→ extract_features ──→ predict_value
                                       │                           │
                                       ▼                           ▼
                              account_details 表 (parsed_data, features, value, score)
                                       │
train_loop (每日) ──→ get_training_data ──→ train_and_save ──→ valuer_weights 表

backfill CLI ──→ crawl(历史页) ──→ upsert_account ──→ process_account ──→ account_details (value 留空)
```

## 安装

```powershell
uv sync
```

依赖（见 [pyproject.toml](pyproject.toml)）：httpx, fastapi, uvicorn, pyyaml, numpy, lightgbm

## 配置

编辑 [config.yaml](config.yaml)：

```yaml
sources:
  pxb7:
    enabled: true
    name: "螃蟹"
    games: ["10302"]      # 鸣潮
crawl:
  interval_seconds: 300        # 爬取间隔
  max_pages: 3                 # 每次最多翻页数
  detail_interval_seconds: 1.5 # 详情请求间隔（避免触发风控）
  # proxy: "host:port"         # 可选代理（IP 被封时使用）

# 后台 worker 开关（默认只开爬虫和详情轮询）
workers:
  crawl: true            # 定时爬取最新账号
  detail_check: true     # 定时轮询详情检测售出
  valuer: false          # 价值评估（需要模型已训练）
  train: false           # 每日自动训练

game_names:
  "鸣潮":
    pxb7: "10302"\n
api:
  host: "0.0.0.0"
  port: 8000
```

## 运行

### 启动服务

```powershell
uv run python main.py
# 或
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

启动后根据 `workers` 配置运行后台 worker：

| Worker | 作用 | 间隔 | 默认 |
|---|---|---|---|
| `run_crawl_loop` | 爬取商品列表入库 | `crawl.interval_seconds`（300s） | 开 |
| `run_detail_check_loop` | 轮询详情检测售出 | 600s | 开 |
| `run_valuer_loop` | 解析未估价账号 + 预测价值 | 300s | 关 |
| `run_train_loop` | 重新训练模型 | 86400s（每日） | 关 |

### 训练模型

独立训练命令（不启动 FastAPI）：

```powershell
uv run python train.py                 # 训练所有已配置游戏
uv run python train.py --game-id 303   # 仅训练指定游戏
```

### 历史数据回填

向前翻页爬取历史在售账号，仅提取特征不计算价值（交 `run_valuer_loop` 补全）：

```powershell
# 螃蟹回填（从第 1 页向前爬 50 页）
uv run python -m backfill.pxb7 --game-id 10302 --start-page 1 --max-pages 50 --interval 0.5

```

回填 CLI 参数：

| 参数 | 说明 | 默认 |
|---|---|---|
| `--game-id` | 游戏 ID（螃蟹 10302） | 必填 |
| `--start-page` | 起始页码 | 必填 |
| `--max-pages` | 最多翻页数 | 必填 |
| `--page-size` | 每页条数 | 螃蟹 16 |

| `--interval` | 详情请求间隔秒数 | 螃蟹 0.5 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/accounts` | 商品列表，支持 `sort=value_ratio_desc` / `value_desc` / `score_desc` |
| GET | `/api/accounts/{id}` | 商品详情（含 `parsed_data` / `value` / `score`） |
| GET | `/api/stats` | 统计：在售/已售/按源/按游戏 |
| GET | `/api/mappings` | 数据源名称 + 游戏 ID 映射 |
| GET | `/api/valuer/status` | 各游戏模型状态（样本数/是否就绪/训练时间） |
| GET | `/api/valuer/weights` | 所有游戏权重信息 |
| POST | `/api/valuer/train?game_id=` | 手动触发训练 |

### 查询示例

```powershell
# 鸣潮在售账号，按性价比降序
curl "http://localhost:8000/api/accounts?game_name=鸣潮&sort=value_ratio_desc&size=20"

# 手动训练鸣潮模型
curl -X POST "http://localhost:8000/api/valuer/train?game_id=10302"
```

## 价值模型

**LightGBM 分位数回归**，按游戏分别训练。3 个独立 booster（`objective='quantile'`, `alpha=0.1/0.5/0.9`），输出 P10/P50/P90 价格区间，预测时逐级 `np.maximum` 保证单调递增。

### 特征（26 维，见 [valuer/features.py](valuer/features.py)）

| 类别 | 特征 |
|---|---|
| 基础数值 (7) | yellow, level, star_sounds, fuujin_waves, zhuchao_waves, yubo_coral, total_pulls |
| 命座分布 (8) | c0~c6 数量 + 四星满命数 |
| 精炼分布 (6) | r1~r5 数量 + high_refine_count（精3+） |
| 稀有度 (4) | team_count, hot_char_count, skin_count, five_star_char_count |
| 来源 (1) | source_pzds（保留用于多源扩展, 当前恒为0） |

### 评分

```
value_ratio = P50 / 实际价格
score = 100 / (1 + exp(-3 * log(value_ratio)))   # sigmoid 归一化到 0-100
```

- `ratio > 1`：实际价低于预测中位 → 划算 → 高分
- `ratio = 1`：正好中位 → 50 分
- `ratio < 1`：实际价高于预测中位 → 偏贵 → 低分

### 训练流程

1. `train_loop` 每日触发，或 `train.py` CLI 触发，或 `POST /api/valuer/train` 手动触发
2. 从 `account_details` 取已估价的样本（features + price）
3. log 变换价格（长尾分布稳定）+ 80/20 分割 + early stopping（patience=50）
4. 序列化为 booster 字符串存入 `valuer_weights` 表
5. 冷启动：样本 `< 200` 时不训练，`account_details.value` 留空，等数据积累后回填

## 数据来源说明

### 螃蟹（pxb7）

- 列表：`POST /api/search/product/v2/selectSearchPageList`（公开接口，`trust_env=False` 直连）
- 详情：`POST /api/product/web/product/detailPost`（公开接口，含 `reportTabInfo.groupList` 角色武器绑定）
- 售出检测：`detailPost` 返回 `status != 1` 即已售

## 数据库

SQLite（`accounts.db`），3 张表：

- `accounts`：商品基本信息（source/game_id/product_id/title/price/raw_data/is_active）
- `account_details`：解析后的结构化数据 + 价值评估（parsed_data/features/value/score/value_ratio）
- `valuer_weights`：模型权重（按 game_id，booster 字符串 + 特征名 + 样本数 + 训练时间）

`accounts_fts` 为 title 的 FTS5 全文索引，支持关键词搜索。

## 注意事项\r\n\r\n- 螃蟹 `httpx.Client` 使用 `trust_env=False` 关闭代理直连
- `MIN_SAMPLES=200`：样本不足不训练，价值字段留空





