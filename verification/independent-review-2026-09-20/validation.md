# Independent review validation — 2026-09-20

Environment: Windows PowerShell, D:\Trace, existing project .venv. Existing environment was used; this is not a clean-install certification.

## Default Python suite

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Actual completion output:

```text
389 passed in 76.85s (0:01:16)
```

## Mini-program mapping checks

Command:

```powershell
node miniprogram/tests/detail_mapping.test.js
```

Actual result: all seven checks A01–A07 passed (no fabricated quote, no default 7.0 score, honest unconfirmed state, no unrelated fallback, no silent mock default, two-level direct-only layout, evidence/claims/uncertainties mapping).

## Independent boundary/fault probes

Command:

```powershell
.\.venv\Scripts\python.exe verification/independent-review-2026-09-20/reproduce.py
```

Exit code 0 means observations completed. It does not mean the product passed. Raw observations are in `probe-results.json`. The script uses temporary databases, fake model and notification providers, fake HTTP responses, environment credential isolation, and external socket blocking. It makes no production writes or real deliveries.

Thirteen observation groups: production/deployment auth modes; private share and invalid evidence; real budget + Ask integration; macro Ask; cursor round-trip; stable source ID revision; index market timestamp; processing crash; recovered blocked-budget positive control; official denial merge; disable after enqueue; foreign-key-invalid restore; Compose runner CLI.

## Source and artifact checks

- Git HEAD: f27252b93d6328eecafa62f6a23a88b46dd54628. Reviewed implementation also contains substantial uncommitted changes.
- `source-manifest.json` records SHA-256 hashes of the current source/test/client/script and deployment configuration files (no private `.env` contents). Use it to identify the reviewed dirty workspace snapshot; HEAD alone is insufficient.
- `git ls-files data/benchmarks/event_dedup_golden_100.json` produced no tracked file.
- `git check-ignore -v data/benchmarks/event_dedup_golden_100.json` matched `.gitignore:7:/data/`.
- No production cloud, real LLM, Telegram, WeChat device, live calendar, or commercial data-rights verification was performed.
- This review adds its own report and evidence; it does not rewrite historical implementation reports or business code.
