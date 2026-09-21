-- 0020_immutable_forecast_snapshots: 不可变预测快照与防前视回测评估 (T13 / F19 / F27)
--
-- 解决两大问题：
-- 1. 前视偏差（F19）：基准价格不得锚定在原始 event_time，必须锚定在预测生成时刻
--    （analysis_created_at）可获得的最新行情；修订事件版本生成新快照，禁止覆盖历史快照。
-- 2. 评估指标扩充（F27）：增加相对基准超额收益（SPX / STAR50），分组统计模型、市场、方向维度。

CREATE TABLE IF NOT EXISTS forecast_snapshot (
    snapshot_id             TEXT PRIMARY KEY,
    impact_id               TEXT NOT NULL,
    event_id                TEXT NOT NULL,
    security_id             TEXT NOT NULL,
    event_version           INTEGER NOT NULL DEFAULT 1,
    predicted_direction     TEXT NOT NULL,
    predicted_score         REAL,
    confidence              REAL,
    model_version           TEXT NOT NULL DEFAULT 'v1',
    market                  TEXT NOT NULL DEFAULT 'US',
    analysis_created_at     TEXT NOT NULL,
    published_at            TEXT,
    anchor_price            REAL,
    anchor_ts               TEXT,
    horizon_hours           REAL NOT NULL DEFAULT 24.0,
    due_at                  TEXT NOT NULL,
    benchmark_code          TEXT NOT NULL DEFAULT 'SPX',
    benchmark_anchor_price  REAL,
    status                  TEXT NOT NULL DEFAULT 'pending', -- pending / evaluated / unmeasurable / expired
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_forecast_snapshot_due ON forecast_snapshot(status, due_at);
CREATE INDEX IF NOT EXISTS idx_forecast_snapshot_event ON forecast_snapshot(event_id, event_version);
CREATE INDEX IF NOT EXISTS idx_forecast_snapshot_security ON forecast_snapshot(security_id);
CREATE INDEX IF NOT EXISTS idx_forecast_snapshot_impact ON forecast_snapshot(impact_id);

-- 迁移 forecast_check：将唯一约束从 impact_id 转移到 snapshot_id，增加基准与超额收益列
CREATE TABLE IF NOT EXISTS forecast_check_new (
    check_id             TEXT PRIMARY KEY,
    snapshot_id          TEXT UNIQUE,
    impact_id            TEXT NOT NULL,
    event_id             TEXT NOT NULL,
    security_id          TEXT NOT NULL,
    event_version        INTEGER NOT NULL DEFAULT 1,
    predicted_direction  TEXT NOT NULL,
    predicted_score      REAL,
    confidence           REAL,
    actual_change_pct    REAL,
    actual_direction     TEXT,
    outcome              TEXT NOT NULL,
    horizon_hours        REAL,
    evaluated_at         TEXT NOT NULL,
    anchor_price         REAL,
    anchor_ts            TEXT,
    exit_price           REAL,
    elapsed_hours        REAL,
    note                 TEXT NOT NULL DEFAULT '',
    model_version        TEXT NOT NULL DEFAULT 'v1',
    market               TEXT NOT NULL DEFAULT 'US',
    benchmark_code       TEXT NOT NULL DEFAULT 'SPX',
    benchmark_change_pct REAL,
    excess_return_pct    REAL,
    excluded_reason      TEXT NOT NULL DEFAULT ''
);

INSERT INTO forecast_check_new (
    check_id, snapshot_id, impact_id, event_id, security_id, event_version,
    predicted_direction, predicted_score, confidence, actual_change_pct,
    actual_direction, outcome, horizon_hours, evaluated_at, anchor_price,
    anchor_ts, exit_price, elapsed_hours, note, model_version, market,
    benchmark_code, benchmark_change_pct, excess_return_pct, excluded_reason
)
SELECT
    check_id, COALESCE(check_id, impact_id), impact_id, event_id, security_id, 1,
    predicted_direction, predicted_score, confidence, actual_change_pct,
    actual_direction, outcome, horizon_hours, evaluated_at, anchor_price,
    anchor_ts, exit_price, elapsed_hours, note, 'v1', 'US',
    'SPX', NULL, NULL, ''
FROM forecast_check;

DROP TABLE forecast_check;
ALTER TABLE forecast_check_new RENAME TO forecast_check;

CREATE INDEX IF NOT EXISTS idx_forecast_check_eval ON forecast_check(evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_forecast_check_snapshot ON forecast_check(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_forecast_check_impact ON forecast_check(impact_id);
CREATE INDEX IF NOT EXISTS idx_forecast_check_event ON forecast_check(event_id, event_version);
