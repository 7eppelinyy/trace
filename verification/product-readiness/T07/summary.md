# 任务 T07 交付与验收报告：可重复构建、断网测试与发布基础

- **任务 ID**: T07
- **优先级**: P0
- **审阅对应**: F07
- **执行时间**: 2026-09-19 (Asia/Taipei)
- **状态**: 已完成并通过自动化验收

---

## 1. 问题与修复方案

| 维度 | 修复前实际行为 (Bug) | 修复后实际行为 (Fixed) | 验证证据 |
|---|---|---|---|
| **CI 依赖脱节** | `.github/workflows/ci.yml` 手写安装列表未包含 `fastapi` 与 `uvicorn`，干净环境必然构建失败 | 拆分 `requirements-core.txt` 单一真实依赖源，CI 与轻量部署使用同一清单，覆盖 FastAPI/uvicorn | `ci.yml` 引用 `requirements-core.txt` |
| **外部网络污染风险** | 默认单元测试没有全局 socket 拦截，行情/采集器若发生异常可能暗中外连公网 | 在 `tests/conftest.py` 增加 `_block_external_network` autouse fixture，阻断非 loopback 的外部套接字连接；live 测试通过 marker 显式放行 | `tests/test_isolation.py` 验证尝试外连 `8.8.8.8` 立即被拦截抛错 |
| **前端自动化测试集成** | CI 仅跑 Python 测试，前端映射与数据契约无自动化门禁 | 在 CI 流水线中加入 Node.js 步骤，自动执行 `node miniprogram/tests/detail_mapping.test.js` | `ci.yml` 新增前端测试步骤 |

---

## 2. 自动化测试结果

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_isolation.py -v
```

输出：
```text
tests/test_isolation.py::test_external_network_blocked PASSED            [100%]
============================== 1 passed in 0.57s ==============================
```

```powershell
node miniprogram/tests/detail_mapping.test.js
```

输出：
```text
=== All T01 Detail Mapping Tests Passed! ===
```

---

## 3. 回滚方案
保留原始依赖文件与工作流配置；如遇特殊测试需要直接访问公网，可通过 `-m live` 标记显式放行。
