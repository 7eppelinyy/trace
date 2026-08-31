# DeepSeek Real Call Chain — Smoke Test

## Purpose
Prove the LLM Stage A (Extractor) / Stage B (Impact Analyzer) / Same-event Verifier
call the real DeepSeek OpenAI-compatible endpoint from the GCP VM (direct connect, no proxy),
and return schema-valid, non-fabricated structured output.

## Configuration (server .env, values masked)
```
LLM_PROVIDER=openai            # OpenAI-compatible provider
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-chat
OPENAI_API_KEY=<masked>
```

## Evidence — real calls during run-once #1 (from run-once-1.log)
Endpoint: `POST https://api.deepseek.com/chat/completions`
Every call returned `HTTP/1.1 200 OK`.

Call count (run summary):
```
llm_stage_a_calls   = 222   # Stage A Extractor (one per new raw item)
llm_stage_b_calls   = 160   # Stage B Impact Analyzer
llm_verifier_calls  = 26    # Same-event Verifier (merge decisions)
total               = 408 real DeepSeek calls
```

Representative log lines:
```
httpx: HTTP Request: POST https://api.deepseek.com/chat/completions "HTTP/1.1 200 OK"
trace.event_engine.engine: merged into EVT-20260826-c124b63cc6ef (reason=official_source_appeared, official=True)
trace.ai.pipeline: event EVT-20260826-a99a131e2eb4 analyzed: 12 impacts
```

## Idempotency (run-once #2)
```
llm_stage_a_calls = 0
llm_stage_b_calls = 0
llm_verifier_calls = 0
```
Duplicate items consume zero DeepSeek calls.

## Conclusion
DeepSeek real call chain VERIFIED on the GCP VM via direct connection.
No mock output; structured schema output passed validation (human_review=0).
