-- 0012_llm_budget: LLM 每日调用预算
--
-- 此前全仓没有任何 budget/cap：成本只有事后计数（run 摘要里的
-- llm_stage_a_calls 等），没有事前护栏。某个来源结构一变导致 canonical_url
-- 失效、Level 1 去重全线穿透时，一夜之间可以把配额打爆而没有任何东西拦得住。
--
-- 计数必须落库而不是放进程内存：长驻服务会重启，run-once 每次都是新进程，
-- 内存计数器等于没有护栏。

CREATE TABLE IF NOT EXISTS llm_usage (
    date_str   TEXT PRIMARY KEY,          -- UTC YYYY-MM-DD
    calls      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
