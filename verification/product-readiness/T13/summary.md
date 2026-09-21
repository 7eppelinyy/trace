# 任务交付总结：T13（P1 · 不可变预测快照与可解释效果评估）

## 1. 任务背景与核心缺陷
- **关联缺陷**：
  - **F19（前视偏差）**：原回测账本直接基于 `event_impact` 与原始 `event_time` 锚定行情。当分析发生滞后（如事件发生于开盘前或数小时前，而分析流水线延后运行）时，评估基准若取 `event_time` 会把分析产生前已经发生的涨跌幅计入模型收益，产生虚假胜率；且事件版本修订时直接覆盖更新破坏了可重现性。
  - **F27（回测指标单一）**：原 `/accuracy` 仅输出单一总体胜率，缺乏大盘基准收益对比（Beta vs Alpha）、缺乏美股/A股与看多/看空多维分组表现，且未披露无法核对与中性样本占比，存在报喜不报忧。

## 2. 核心改动范围

### 2.1 数据库迁移 (`0020_immutable_forecast_snapshots.sql`)
- **`forecast_snapshot` 表**：
  - 记录 `snapshot_id, impact_id, event_id, security_id, event_version, predicted_direction, predicted_score, confidence, model_version, market, analysis_created_at, published_at, anchor_price, anchor_ts, horizon_hours, due_at, benchmark_code, benchmark_anchor_price, status, created_at`。
  - 建立 `(status, due_at)`、`(event_id, event_version)` 等高性能复合索引。
- **`forecast_check` 约束与字段升级**：
  - 将唯一键从单一的 `impact_id UNIQUE` 平滑迁移至 `snapshot_id UNIQUE`，支持同事件不同版本修订独立落账并存。
  - 增加字段：`snapshot_id`, `model_version`, `market`, `benchmark_code`, `benchmark_change_pct`, `excess_return_pct`, `excluded_reason`。

### 2.2 领域模型与仓储层
- **`trace/domain/models.py`**：新增 `ForecastSnapshot` 数据类，扩充 `ForecastCheck` 审计与基准收益字段。
- **`trace/db/repositories.py`**：
  - 新增 `ForecastSnapshotRepo`：提供不可变快照的追加写入、版本检索、到期未核对查询 (`list_pending_due`) 及状态流转。
  - 扩充 `ForecastCheckRepo.summary()`：
    - 样本全景透明度：输出 `total_snapshots`、`total`（实测有效样本）、`measurement_rate`（实测率）、`unmeasurable_rate`（失效占比）、`neutral_rate`（中性样本占比）。
    - 收益增强：计算有效预测样本的平均超额收益 `avg_excess_return`。
    - 多维分组：按 `by_market` (US/CN)、`by_direction` (bullish/bearish)、`by_model` (v1/...)、`by_horizon` (24h/...) 结构化聚合统计。

### 2.3 防前视偏差与回测评估闭环 (`trace/feedback/ledger.py`)
- **消除前视偏差 (F19)**：
  - 预测基准价格**严格锚定在预测生成时点**（`analysis_created_at`）可获得的最新行情，彻底阻断在事件发生与分析生成之间的历史价格涨幅被非法揽入模型战绩。
  - 真实间隔生命周期以 `analysis_created_at` 为起点，到期核对时点为 `due_at = analysis_created_at + horizon_hours`。
- **多市场基准与超额收益测算 (F27)**：
  - 美股默认对标 **S&P 500 (`SPX`)**，A股默认对标 **科创 50 (`STAR50`)**。
  - 锁定预测生成时刻的基准指数价格，核对时获取最新基准退出价，计算基准收益率与预测超额收益率：
    $$\text{Excess Return} = \Delta \text{Security}_{\%} - \Delta \text{Benchmark}_{\%}$$
- **多版本修订隔离**：
  - 事件版本升级产生新快照，历史快照与核对记录永远保持不可变。

### 2.4 流水线注入与 Telegram 交互展示
- **`trace/ai/pipeline.py`**：Stage B 生成影响预测时，即刻定格对应点位的不可变 `ForecastSnapshot`。
- **`trace/feedback/ledger.py` (`render_summary`)**：
  - Telegram `/accuracy` 命令全面呈现：
    - 【📊 样本全景】：预测总量、实测有效样本、实测率、待核对与无法核对数；
    - 【🎯 整体表现】：命中、未命中、中性数量、胜率与相对基准平均超额收益；
    - 【🌐 市场分组】：美股 (SPX) 与 A股 (STAR50) 分组命中率与超额；
    - 【⚖️ 方向分组】：看多 (Bullish) 与 看空 (Bearish) 对比；
    - 【口径与披露】：明确注明预测生成时点基准行情与防前视原则，禁止报喜不报忧。

---

## 3. 测试与验收证据

### 3.1 单元测试矩阵 (`tests/test_immutable_forecast_snapshots.py`)
1. `test_immutable_snapshots_on_revision`:
   - 验证事件修订升级为 `version=2` 时，新增 v2 预测快照，历史 v1 快照与 v1 核对记录完全不受影响，历史核对无篡改。
2. `test_no_lookahead_bias_when_analysis_delayed`:
   - 验证事件发生 6 小时内暴涨 +8.0%，分析滞后启动预测看多，后续区间仅上涨 +0.46%。
   - 旧逻辑错误判定为 +8.5% 假命中；新逻辑准确判定为 +0.46% (neutral)，无前视增益。
3. `test_benchmark_excess_return_us_and_cn`:
   - 美股样本对标 SPX 超额收益率计算；
   - A股样本对标 STAR50 超额收益率计算。
4. `test_grouped_summary_and_sample_transparency`:
   - 验证实测率、中性样本率、美股/A股分组表、看多/看空分组表均准确输出。
5. `test_telegram_accuracy_render_output`:
   - 验证 Telegram `/accuracy` 文案具备完整结构化分组与披露。

### 3.2 自动化测试执行结果
```bash
$ .\.venv\Scripts\python.exe -m pytest tests/test_forecast_ledger.py tests/test_immutable_forecast_snapshots.py -v
============================= 18 passed in 3.62s ==============================

$ .\.venv\Scripts\python.exe -m pytest -q
........................................................................ [ 19%]
........................................................................ [ 39%]
........................................................................ [ 58%]
........................................................................ [ 78%]
........................................................................ [ 97%]
........                                                                 [100%]
368 passed in 50.54s
```

## 4. 交付物清单
1. `trace/db/migrations/0020_immutable_forecast_snapshots.sql`
2. `trace/common/ids.py` (`forecast_snapshot_id`)
3. `trace/domain/models.py` (`ForecastSnapshot`, updated `ForecastCheck`)
4. `trace/domain/__init__.py`
5. `trace/db/repositories.py` (`ForecastSnapshotRepo`, upgraded `ForecastCheckRepo`)
6. `trace/feedback/ledger.py` (不可变快照回测调度、消除前视偏差、基准超额收益、分组输出)
7. `trace/ai/pipeline.py` (Stage B 预测快照定格落库)
8. `tests/test_immutable_forecast_snapshots.py` (全新验收套件 5 项)
9. `verification/product-readiness/T13/summary.md` (本交付报告)
