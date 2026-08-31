#!/usr/bin/env bash
# Trace GCP 部署脚本（幂等）：解压代码、注入 .env、创建 venv、安装依赖。
# sentence-transformers 除外：本地生产环境亦未安装（自动降级为 hash embedder，
# 153 tests 与两轮真实 run 均在该状态通过），e2-micro 1GB RAM 避免引入大体积依赖。
set -e
cd /opt/trace

echo "=== [1/5] unzip code ==="
sudo apt-get install -y unzip >/dev/null 2>&1 || true
unzip -oq /tmp/trace-deploy.zip
rm -f /tmp/trace-deploy.zip
echo "files: $(ls | tr '\n' ' ')"

echo "=== [2/5] place .env (600) ==="
if [ -f /tmp/.env.server ]; then
  mv /tmp/.env.server /opt/trace/.env
fi
chmod 600 /opt/trace/.env
echo "env lines: $(wc -l < /opt/trace/.env)"

echo "=== [3/5] venv ==="
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip

echo "=== [4/5] install deps ==="
grep -v '^sentence-transformers' requirements.txt > requirements-server.txt
.venv/bin/pip install -q -r requirements-server.txt
echo "deps installed"

echo "=== [5/5] import check ==="
.venv/bin/python -c 'import httpx, feedparser, bs4, lxml, openai, telegram, yaml, dotenv, pytest; print("DEPS_OK")'
.venv/bin/python -c 'import trace.main; print("MODULE_OK")'
echo "SETUP_DONE"
