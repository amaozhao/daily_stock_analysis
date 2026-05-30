# 全市场盘后选股推荐规格

## 目标

本规格定义“盘后全市场选股推荐”能力。该能力不是对 `STOCK_LIST` 自选股做排序，而是在每个交易日收盘后从全市场股票池中筛选值得普通用户关注的候选股票，并输出可解释、可配置、可回测的推荐关注名单。

首版目标聚焦 A 股盘后推荐：

- 从全量 A 股股票池筛选，不依赖用户自选股。
- 使用规则引擎完成主筛选，LLM 只做最终解释、风险复核和摘要生成。
- 保存每日行情快照、候选评分、最终推荐和运行元数据，便于复盘、调参和回测。
- 输出“重点关注 / 观察确认 / 只看不追”等关注等级，不把全市场筛选结果直接表述为确定买入建议。

非目标：

- 首版不做港股、美股全市场选股。
- 首版不承诺自动交易或确定性收益。
- 首版不对 5000+ 股票逐只调用 LLM。
- 首版不把推荐规则写死在代码里。

## 总体流程

```text
股票池加载
  -> 全市场盘后行情快照
  -> 硬过滤
  -> 快速预筛
  -> 历史技术特征计算
  -> 分策略评分
  -> 分散约束和排序
  -> Top N 深度分析 / LLM 复核
  -> 推荐 CSV + 元数据 + 通知 / Web 展示
```

分层原则：

- `Gate`：硬过滤，命中直接剔除。
- `Score`：加分项，决定排序。
- `Penalty`：扣分项，降低推荐等级但不一定剔除。
- `Explain`：保存可解释原因，供 Web、通知、调参和回测使用。

## 当前代码基础与差异

可复用能力：

- `stocks.index.json` 已提供股票自动补全索引，可作为全市场股票池入口。
- `data_provider/efinance_fetcher.py` 与 `data_provider/akshare_fetcher.py` 已具备全量实时行情缓存能力。
- `UnifiedRealtimeQuote` 已统一单股实时行情字段。
- `StockTrendAnalyzer` 已具备均线、量能、MACD、RSI 等技术分析逻辑。
- `src/scheduler.py` 已支持每日定时任务。
- `AnalysisResult`、通知服务、历史报告、回测服务可用于 Top 候选深度分析和后续验证。

需要补齐的差异：

- 当前 `STOCK_LIST` 是用户自选股，不是全市场选股池。推荐功能必须新增独立股票池逻辑。
- `stocks.index.json` 是补全索引，不包含完整选股基础数据，如上市日期、行业、停牌状态、涨跌停价、精确板块归属。它只能作为股票池入口，不能承担全部筛选。
- 当前实时行情封装主要是 `get_realtime_quote(stock_code)`，对外缺少明确的全市场快照接口。推荐功能需要新增 `get_realtime_quotes_snapshot(market="cn")` 或等价服务层接口。
- 当前历史 K 线加载适合单股分析，不适合对 5000+ 股票逐只拉取。必须先用全市场行情快照预筛，再只对候选池拉历史。
- 当前 `SCHEDULE_TIME` 是主分析任务时间。推荐任务需要独立配置 `RECOMMENDATION_SCHEDULE_TIME`，或扩展 scheduler 支持多个 daily job。
- 当前 `AnalysisResult` 是个股分析报告结构，不适合承载全市场筛选过程。应新增筛选专用结构。

## 股票池

首版股票池来源：

- 本地或远程缓存后的 `stocks.index.json`。
- 只取 `market = CN`、`asset_type = stock`、`active = true`。
- 排除指数、ETF、基金、债券等非普通股票。
- 用户 `STOCK_LIST` 不参与股票池选择，只可作为后续“用户偏好加权”或“已关注提醒”。

股票池字段最小化结构：

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| `code` | `stocks.index.json` | 统一股票代码 |
| `display_code` | `stocks.index.json` | 展示与数据源调用兼容 |
| `name` | `stocks.index.json` / 快照行情 | 展示、ST/退市风险初筛 |
| `market` | `stocks.index.json` | 市场过滤 |
| `asset_type` | `stocks.index.json` | 类型过滤 |
| `active` | `stocks.index.json` | 活跃状态过滤 |
| `board` | 代码规则推断或数据源补充 | 涨跌停制度、风险分层 |
| `listing_date` | 数据源补充，首版可为空 | 新股过滤 |
| `industry` / `sector` | 数据源补充，首版可为空 | 分散约束、板块加权 |

板块推断初版可按代码前缀近似：

- `60` / `00`：主板，默认涨跌停约 `10%`。
- `30` / `68`：创业板 / 科创板，默认涨跌停约 `20%`。
- `8` / `4` / `92`：北交所，默认涨跌停约 `30%`，可配置为默认排除。
- 名称含 `ST` / `*ST`：默认排除；若不排除，涨跌停近似按 `5%`。
- 名称含 `退` 或明显退市风险标记：默认排除。

注意：涨跌停制度存在新股、复牌、特殊处理等例外。首版用近似规则识别“疑似涨跌停 / 一字板”，不能用于精确交易撮合。

## 数据文件

每日文件采用 CSV + JSON + TOML 组合：

```text
data/recommendations/
  snapshots/cn/2026-05-29.market.csv
  runs/cn/2026-05-29/153520-beginner_cn-a1b2c3.candidates.csv
  runs/cn/2026-05-29/153520-beginner_cn-a1b2c3.recommendations.csv
  runs/cn/2026-05-29/153520-beginner_cn-a1b2c3.meta.json
  profiles/cn/2026-05-29/153520-beginner_cn-a1b2c3.toml
```

职责：

- `market.csv`：每日全市场行情快照，二维表，适合 Excel、pandas、回测和人工排查。
- `candidates.csv`：通过硬过滤并参与评分的候选池，包含特征、评分、风险标签和过滤原因。
- `recommendations.csv`：最终展示给用户的推荐列表。
- `meta.json`：运行元数据、统计、数据源状态、文件路径、profile hash、异常摘要。
- `profile.toml`：当天实际使用的规则快照，保证后续回测能复现。

运行 ID：

- 每次筛选运行生成独立 `run_id`，格式建议为 `{market}-{trade_date}-{HHMMSS}-{profile}-{profile_hash_prefix}`。
- 同一交易日多次手动重跑、切换 profile 或更换数据源时，不覆盖旧运行结果。
- `snapshots/{market}/{trade_date}.market.csv` 是交易日行情快照的默认路径；若用户强制刷新快照，应在 `meta.json` 中记录 `snapshot_refreshed=true` 与数据源状态。
- 所有文件使用 UTF-8 编码、逗号分隔、标准 CSV quote 规则；写入时先写临时文件，再原子替换目标文件，避免 Web 或回测读取半文件。
- `trade_date` 使用目标市场交易日；A 股默认时区为 `Asia/Shanghai`。

### `market.csv` 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `trade_date` | 是 | 交易日 |
| `code` | 是 | 股票代码 |
| `name` | 是 | 股票名称 |
| `price` | 是 | 收盘后最新价 |
| `change_pct` | 否 | 当日涨跌幅 |
| `change_amount` | 否 | 当日涨跌额 |
| `volume` | 否 | 成交量，统一为股口径 |
| `amount` | 否 | 成交额，统一为元口径 |
| `volume_ratio` | 否 | 量比 |
| `turnover_rate` | 否 | 换手率 |
| `amplitude` | 否 | 振幅 |
| `open` | 否 | 开盘价 |
| `high` | 否 | 最高价 |
| `low` | 否 | 最低价 |
| `pre_close` | 否 | 昨收价 |
| `pe_ratio` | 否 | 动态市盈率 |
| `pb_ratio` | 否 | 市净率 |
| `total_mv` | 否 | 总市值，元 |
| `circ_mv` | 否 | 流通市值，元 |
| `change_60d` | 否 | 60 日涨跌幅，数据源支持时写入 |
| `high_52w` | 否 | 52 周高点，数据源支持时写入 |
| `low_52w` | 否 | 52 周低点，数据源支持时写入 |
| `source` | 是 | 快照数据源 |
| `data_quality` | 是 | `ok` / `partial` / `unavailable` |

### `candidates.csv` 字段

| 字段 | 说明 |
| --- | --- |
| `trade_date` | 交易日 |
| `code` / `name` | 股票标识 |
| `strategy` | 最高分策略，如 `trend_pullback` |
| `passed_hard_filters` | 是否通过硬过滤 |
| `filtered_by` | 未通过时的过滤原因，分号分隔 |
| `selection_score` | 最终分 |
| `beginner_safety_score` | 小白安全分 |
| `entry_quality_score` | 买点质量分 |
| `liquidity_score` | 流动性分 |
| `trend_score` | 趋势分 |
| `volume_price_score` | 量价分 |
| `sector_score` | 板块分 |
| `stability_score` | 稳定性分 |
| `risk_penalty` | 风险扣分 |
| `risk_tags` | 风险标签，分号分隔 |
| `positive_reasons` | 主要加分原因，分号分隔 |
| `negative_reasons` | 主要扣分原因，分号分隔 |
| `watch_price` | 关注价 |
| `stop_loss` | 止损参考 |
| `take_profit_reference` | 止盈或压力参考 |
| `rank` | 候选排序 |

### `recommendations.csv` 字段

在 `candidates.csv` 基础上增加：

| 字段 | 说明 |
| --- | --- |
| `recommendation_level` | `focus` / `confirm` / `watch_only` |
| `recommendation_label` | 中文展示：重点关注 / 观察确认 / 只看不追 |
| `beginner_action` | 小白可执行动作 |
| `no_position_action` | 未持仓建议 |
| `has_position_action` | 已持仓建议 |
| `llm_review_status` | `not_run` / `passed` / `downgraded` / `rejected` |
| `analysis_query_id` | 若进行了深度分析，对应分析 query id |

### `meta.json` 字段

```json
{
  "schema_version": 1,
  "run_id": "cn-2026-05-29-153520-beginner_cn-a1b2c3",
  "market": "cn",
  "trade_date": "2026-05-29",
  "generated_at": "2026-05-29T15:35:20+08:00",
  "timezone": "Asia/Shanghai",
  "profile": "beginner_cn",
  "profile_hash": "sha256:...",
  "snapshot_file": "snapshots/cn/2026-05-29.market.csv",
  "snapshot_refreshed": false,
  "candidates_file": "runs/cn/2026-05-29/153520-beginner_cn-a1b2c3.candidates.csv",
  "recommendations_file": "runs/cn/2026-05-29/153520-beginner_cn-a1b2c3.recommendations.csv",
  "profile_snapshot_file": "profiles/cn/2026-05-29/153520-beginner_cn-a1b2c3.toml",
  "summary": {
    "universe_count": 5200,
    "snapshot_count": 5100,
    "passed_hard_filters": 880,
    "scored_count": 300,
    "deep_analyzed_count": 30,
    "recommended_count": 10,
    "filter_breakdown": {
      "amount_too_low": 2100,
      "limit_like": 35,
      "history_unavailable": 120
    }
  },
  "data_sources": [
    {"name": "efinance", "status": "ok", "latency_ms": 4300}
  ],
  "warnings": []
}
```

若 `snapshot_count / universe_count` 低于 `min_snapshot_coverage_ratio`，默认中止推荐生成，只保存 `meta.json` 和告警，不输出低可信推荐。

## 配置 Profile

规则使用 TOML profile 表达。`.env` 只保留开关和 profile 路径：

```env
RECOMMENDATION_ENABLED=false
RECOMMENDATION_SCHEDULE_TIME=15:30
RECOMMENDATION_MARKET=cn
RECOMMENDATION_PROFILE=beginner_cn
RECOMMENDATION_PROFILE_PATH=config/recommendation_profiles/beginner_cn.toml
RECOMMENDATION_OUTPUT_DIR=data/recommendations
RECOMMENDATION_SNAPSHOT_RETENTION_DAYS=90
```

TOML 使用 Python 标准库 `tomllib` 读取，不新增运行时依赖。

Profile 示例：

```toml
[universe]
market = "cn"
asset_types = ["stock"]
exclude_st = true
exclude_delisting_risk = true
exclude_bse = true
exclude_new_stock_days = 60

[snapshot]
preferred_sources = ["efinance", "akshare_em"]
min_snapshot_coverage_ratio = 0.85

[hard_filters.price]
min_price = 3.0
max_price = 300.0

[hard_filters.liquidity]
min_amount = 100000000
min_avg_amount_5d = 80000000
min_avg_amount_20d = 60000000
min_circ_mv = 3000000000

[hard_filters.intraday]
max_abs_change_pct = 7.0
max_amplitude = 12.0
max_volume_ratio = 5.0
max_turnover_rate = 20.0
exclude_limit_like = true
exclude_one_word_board = true

[features]
ma_windows = [5, 10, 20, 60]
return_windows = [3, 5, 20, 60]
volume_windows = [5, 20]
atr_window = 14
history_prefilter_limit = 500

[scoring.weights]
liquidity = 15
trend = 25
entry_quality = 25
volume_price = 15
sector_strength = 10
stability = 10

[output]
candidate_limit = 300
deep_analysis_limit = 30
recommend_limit = 10
max_per_sector = 2
min_final_score = 70
```

配置校验要求：

- 所有权重应为非负数。
- `recommend_limit <= deep_analysis_limit <= candidate_limit <= history_prefilter_limit`。
- 金额、市值、价格阈值必须非负。
- 时间格式必须是 `HH:MM`。
- 未支持的策略、字段或方向应在启动或任务运行前给出明确错误。

## 特征计算

### 快照特征

从全市场快照直接得到：

- `price`
- `change_pct`
- `amount`
- `volume_ratio`
- `turnover_rate`
- `amplitude`
- `pe_ratio`
- `pb_ratio`
- `total_mv`
- `circ_mv`
- `change_60d`
- `high_52w`
- `low_52w`

这些字段用于硬过滤和快速预筛。

### 历史技术特征

只对预筛后的候选计算，避免全市场逐只拉历史。至少计算：

- `ma5` / `ma10` / `ma20` / `ma60`
- `bias_ma5` / `bias_ma10` / `bias_ma20`
- `ma20_slope_5d`
- `ma60_slope_10d`
- `return_3d` / `return_5d` / `return_20d` / `return_60d`
- `volume_ratio_5d`
- `avg_amount_5d` / `avg_amount_20d`
- `max_drawdown_10d` / `max_drawdown_20d`
- `atr_pct_14d`
- `up_day_ratio_20d`
- `consecutive_up_days`
- `consecutive_down_days`
- `distance_to_20d_high_pct`
- `distance_to_60d_high_pct`
- `distance_to_20d_low_pct`
- `macd_status`
- `rsi_6` / `rsi_12` / `rsi_24`

可复用 `StockTrendAnalyzer` 的计算逻辑，但推荐引擎应输出独立的 `ScreeningFeatures`，避免把筛选过程塞进 `AnalysisResult`。

## 硬过滤规则

硬过滤默认保守，宁可少推荐，不硬凑数量。

### 基础过滤

直接剔除：

- 非 A 股普通股票。
- 名称含 `ST`、`*ST`、`退` 等风险标记。
- 北交所股票，除非 profile 明确允许。
- 上市不足配置天数的股票；若无法获得上市日期，默认加风险标签 `listing_age_unknown`，是否剔除由 profile 决定。
- 停牌或无有效价格：`price <= 0`、`volume <= 0`、`amount <= 0`。

### 流动性过滤

直接剔除：

- 当日成交额低于 `min_amount`。
- 5 日或 20 日均成交额低于配置阈值。
- 流通市值低于 `min_circ_mv`。

目的：避免小白进入流动性不足、滑点大、容易被单日资金操纵的标的。

### 盘面异常过滤

直接剔除或降级：

- 疑似涨停 / 跌停：`abs(change_pct)` 接近对应板块涨跌停幅度。
- 疑似一字板：`open == high == low == price` 且涨跌幅接近涨跌停。
- 当日涨跌幅绝对值超过配置阈值。
- 振幅超过配置阈值。
- 量比超过配置阈值。
- 换手率超过配置阈值。

涨跌停识别使用板块近似规则，必须保存 `limit_rule_source`，例如 `code_prefix` / `name_st` / `unknown`。

## 策略分组

最终推荐不能只按总分取前 N，需要分策略输出，避免全是追涨股。

### 趋势回踩型 `trend_pullback`

优先级最高，适合小白。

硬条件：

- `ma5 > ma10 > ma20`。
- `ma20_slope_5d > 0`。
- `price >= ma20`。
- `bias_ma5` 在配置范围内，默认 `-3%` 到 `+4%`。
- `change_pct` 不极端，默认 `-3%` 到 `+3%`。
- `volume_ratio_5d` 或快照 `volume_ratio` 不失控，默认 `0.6` 到 `1.3` 更优。
- `max_drawdown_20d` 在可控范围内。

加分项：

- 缩量回踩到 MA5 / MA10 附近。
- MA20 和 MA60 斜率为正。
- 成交额稳定。
- 止损位清晰且止损距离合理。
- 所属板块不弱。

扣分项：

- 距离前高太近。
- 近 3 日连续上涨后才回踩不足。
- 止损距离过大。
- RSI 过热。

### 放量启动型 `volume_breakout`

适合加入观察，不鼓励当天追高。

硬条件：

- `price` 站上 MA20 或近 20 日平台。
- `change_pct` 默认在 `2%` 到 `6%`。
- 量比默认在 `1.5` 到 `3.5`。
- 成交额达到更高门槛。
- 过去 10 日没有连续暴涨。

加分项：

- MA5 接近上穿 MA10。
- MACD 转强。
- 放量但换手率不过热。
- 距离 60 日高点仍有空间。

扣分项：

- 当日长阳后距离 MA5 太远。
- 上方压力位太近。
- 量比过大，有冲高回落风险。

### 强势整理型 `strong_consolidation`

寻找强股休整后的二次机会。

硬条件：

- 60 日收益为正但不过热。
- 价格仍在 MA20 上方。
- 近 5 日横盘或温和回调。
- 成交量萎缩。
- 未跌破关键均线。

加分项：

- 回撤期间成交额下降。
- 20 日上涨天数占比健康。
- 行业或题材仍有相对强度。

扣分项：

- 60 日涨幅过大。
- 整理时间太短。
- 近期最大单日跌幅过大。

### 低位反转型 `low_reversal`

首版低权重，默认最多推荐 1 只。

硬条件：

- 长期跌势收敛。
- 站回 MA20。
- MACD 或均线开始修复。
- 放量但不过热。

扣分项更严格：

- MA60 仍下行。
- 20 日趋势不稳定。
- 基本面或消息面风险未知。

## 评分模型

最终分：

```text
selection_score =
  liquidity_score * w_liquidity
+ trend_score * w_trend
+ entry_quality_score * w_entry_quality
+ volume_price_score * w_volume_price
+ sector_score * w_sector_strength
+ stability_score * w_stability
- risk_penalty
```

各子分标准化为 `0` 到 `1` 后乘权重，最终可换算为 `0` 到 `100`。

### 流动性分

考虑：

- 当日成交额。
- 5 日 / 20 日均成交额。
- 流通市值。
- 成交额稳定性。

### 趋势分

考虑：

- 均线多头排列。
- MA20 / MA60 斜率。
- 20 日 / 60 日收益。
- 20 日上涨天数占比。
- 价格是否保持在关键均线之上。

### 买点质量分

这是小白入口的核心分，不应低于趋势分权重。

考虑：

- 距 MA5 / MA10 / MA20 的偏离。
- 是否靠近可解释支撑位。
- 止损距离是否合理，默认止损距离过大要扣分。
- 当日是否已经涨太多。
- 是否距离前高压力过近。
- 是否适合“明天等待回踩”，而不是“现在追”。

### 量价分

按策略不同解释：

- 趋势回踩型：缩量回踩加分，放量下跌扣分。
- 放量启动型：温和放量加分，异常放量扣分。
- 强势整理型：整理缩量加分。
- 低位反转型：放量修复加分，但过热扣分。

### 板块分

首版若缺少可靠行业/板块数据，可将该分置为中性并记录 `sector_data_unavailable`。

可用时考虑：

- 所属行业当日表现。
- 所属行业 5 日表现。
- 板块内个股同步强度。
- 最终推荐每行业最多 N 只。

### 稳定性分

考虑：

- ATR 百分比。
- 近 10 / 20 日最大回撤。
- 单日最大跌幅。
- 连续上涨或连续下跌天数。
- 价格和成交额是否有异常跳变。

### 风险扣分

风险标签与扣分必须结构化保存：

| 标签 | 含义 |
| --- | --- |
| `overextended` | 距均线过远或短线涨幅过大 |
| `liquidity_low` | 流动性偏低 |
| `limit_up_like` | 疑似涨停或接近涨停 |
| `limit_down_like` | 疑似跌停或接近跌停 |
| `one_word_board` | 疑似一字板 |
| `high_turnover` | 换手过高 |
| `volume_spike` | 量比异常 |
| `sector_weak` | 板块偏弱 |
| `near_resistance` | 接近压力位 |
| `stop_loss_too_wide` | 止损距离过大 |
| `recent_spike` | 近期连续拉升 |
| `trend_broken` | 趋势破坏 |
| `listing_age_unknown` | 上市时间未知 |
| `sector_data_unavailable` | 板块数据不可用 |

## 推荐排序与分散约束

默认最终结构：

- 趋势回踩型：最多 4 只。
- 放量启动型：最多 3 只。
- 强势整理型：最多 2 只。
- 低位反转型：最多 1 只。
- 单行业 / 单板块最多 2 只。
- 若达不到 `min_final_score`，允许推荐少于 `recommend_limit`，不硬凑数量。

推荐等级：

| 等级 | 展示 | 规则 |
| --- | --- | --- |
| `focus` | 重点关注 | 分数高，买点质量好，风险标签少 |
| `confirm` | 观察确认 | 有机会但需要次日确认 |
| `watch_only` | 只看不追 | 强但位置高或风险偏多 |

输出语言必须避免“确定买入”。默认表达为“关注、等待回踩、观察确认、不要追高、跌破止损放弃”。

## LLM 复核

LLM 不决定全市场入选，只参与 Top 候选复核。

允许 LLM 做：

- 把规则结果翻译成小白可读解释。
- 补充新闻、公告、基本面风险。
- 将候选降级或拒绝，理由必须保存。

不允许 LLM 做：

- 覆盖硬过滤。
- 推荐 ST、停牌、流动性不足、疑似一字板等硬过滤标的。
- 凭空改变规则分数。

## API 与 Web

建议新增 API：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/recommendations/run` | 手动触发一次盘后推荐 |
| `GET` | `/api/v1/recommendations/latest` | 获取最新推荐 |
| `GET` | `/api/v1/recommendations/runs` | 获取历史运行列表 |
| `GET` | `/api/v1/recommendations/runs/{run_id}` | 获取某次运行详情 |
| `GET` | `/api/v1/recommendations/runs/{run_id}/files/{kind}` | 下载 CSV / meta |
| `POST` | `/api/v1/recommendations/runs/{run_id}/backtest` | 基于已落盘推荐结果生成文件型回测 CSV |

`POST /run` 支持 `run_deep_analysis=true/false` 查询参数；未显式传入时遵循 `RECOMMENDATION_LLM_REVIEW_ENABLED`。LLM 复核只处理 Top 候选并写回 `llm_review_status`、`analysis_query_id` 和风险原因，不覆盖硬过滤与规则分数。

Web 首页建议新增“今日盘后推荐”作为小白第一入口，展示：

- 推荐等级。
- 推荐类型。
- 关注价 / 止损价。
- 小白动作。
- 入选原因。
- 风险标签。
- 查看详情与历史推荐入口。

Web 推荐历史页用于：

- 分页检索历史推荐运行。
- 查看某次运行的推荐明细、LLM 复核状态和风险原因。
- 下载 `market/candidates/recommendations/meta/profile/backtest` 文件。
- 手动触发某次推荐运行的文件型回测。

API 数据读取策略：

- MVP 可以直接扫描 `runs/{market}/{trade_date}/*.meta.json` 建立只读列表，避免过早引入迁移。
- 若 Web 需要分页、搜索、回测聚合或跨设备同步，应新增 DB 索引表；DB 只保存摘要、路径和关键字段，CSV / meta 仍作为完整审计记录。
- `RECOMMENDATION_DB_INDEX_ENABLED=true` 时，推荐运行会同步 `recommendation_run_index` 表，`GET /runs` 优先从 DB 返回分页结果，DB 不可用时回退扫描文件。
- 推荐 API 返回的详情必须来自 `recommendations.csv` 与 `meta.json`，不能重新运行规则生成，以保证历史可复现。

## 调度

新增配置：

```env
RECOMMENDATION_ENABLED=false
RECOMMENDATION_SCHEDULE_TIME=15:30
RECOMMENDATION_RUN_IMMEDIATELY=false
```

调度要求：

- 只在目标市场交易日执行，复用 `trading_calendar`。
- 默认收盘后执行，A 股建议 `15:10` 到 `15:30`；若 `RECOMMENDATION_RUN_IMMEDIATELY=true` 但市场仍处于盘前/盘中/收盘集合竞价阶段，应跳过而不是使用未完成日线。
- 推荐任务与现有每日分析任务相互独立。
- 若同时启用 `--serve --schedule`，scheduler 需要支持多个 daily job 或注册独立后台任务。
- 任务运行失败不得影响 Web 服务和普通自选股分析。

## 数据保留与回测

保留策略：

- 默认保留最近 90 天文件。
- 允许配置 `RECOMMENDATION_SNAPSHOT_RETENTION_DAYS`。
- 删除历史文件前应保留 `meta.json` 或 DB 索引中的摘要，避免 Web 历史列表完全丢失。

回测要求：

- 每次推荐必须记录 `trade_date`、`code`、`rank`、`strategy`、`selection_score`、`watch_price`、`stop_loss`。
- 回测记录必须引用 `run_id` 和 `profile_hash`，避免不同规则版本的推荐混在一起。
- 后续可计算推荐后 `3 / 5 / 10 / 20` 个交易日最大收益、最大回撤、是否触及止损、是否跑赢指数、不同策略的命中率和收益分布。
- 回测不可使用推荐日之后才可得的数据生成推荐特征，避免未来函数。

## 实现阶段

### Phase 0：只读规格与样例

- 增加本规格。
- 增加 profile 示例。
- 增加 CSV / meta 样例。

### Phase 1：全市场快照

- 新增股票池服务。
- 新增全市场行情快照接口。
- 写入 `market.csv` 与 `meta.json`。
- 验证数据源覆盖率与字段口径。

### Phase 2：规则筛选与评分

- 新增 profile parser 和校验。
- 新增 `ScreeningFeatures`、`CandidateScore`、`RecommendationItem`。
- 实现硬过滤、快速预筛、历史特征、分策略评分。
- 写入 `candidates.csv` 与 `recommendations.csv`。

### Phase 3：Web / API / 通知

- 新增 Recommendation API。
- Web 首页展示最新推荐。
- 通知推送极简推荐摘要。

### Phase 4：LLM 复核与回测

- Top 候选接入现有深度分析。
- LLM 只做解释与风险复核。
- 推荐结果接入回测统计。

## 验收标准

功能验收：

- 不配置 `STOCK_LIST` 也能执行全市场推荐。
- 任务只对全市场快照预筛后的候选拉历史数据，不对 5000+ 股票逐只深度分析。
- `market.csv`、`candidates.csv`、`recommendations.csv`、`meta.json`、profile 快照均能生成。
- 每只推荐都有策略、分数、加分原因、扣分原因、风险标签和小白动作。
- 推荐数量允许少于上限，不硬凑低质量股票。

质量验收：

- 同一快照和同一 profile 下，筛选结果可复现。
- profile 改动后能通过 `profile_hash` 区分。
- 数据源字段缺失时降级，不静默产生误导性高分。
- 规则命中和过滤原因可解释。
- 文档明确当前数据源和市场制度近似的边界。

## 自查结论

已检查并规避的错误：

- 未把 `STOCK_LIST` 当作全市场股票池。
- 未设计成全量股票逐只调用 LLM。
- 未把 JSON 作为大规模表格行情的主存储；行情和候选使用 CSV，元数据使用 JSON。
- 未把筛选字段塞进 `AnalysisResult`，避免职责混乱。
- 未假设 `stocks.index.json` 拥有上市日期、行业、停牌和涨跌停价等完整选股字段。
- 未复用单一 `SCHEDULE_TIME` 承担推荐任务，避免与现有自选股分析调度冲突。
- 未使用粗糙统一涨跌停阈值，明确按板块近似并保存来源。
- 未让同一天多次运行覆盖彼此，已定义独立 `run_id` 与运行目录。

仍需实现时重点验证的风险：

- `efinance` / `akshare_em` 全市场快照字段名、单位和覆盖率需要实测固定。
- 上市日期、行业板块、停牌状态可能需要补充数据源；首版必须显式降级。
- 北交所、科创板、创业板、ST、新股的涨跌停规则只能近似判断，不能宣称精确。
- 历史 K 线拉取必须有候选数量上限和缓存，否则会触发限流或运行过慢。
- 板块分如果数据不可用，应置中性并记录标签，不能凭空加分。
