#!/usr/bin/env bash
# 安装 systemd service（trace-run）：开机自启 + 异常自动恢复 + journal 日志。
set -e
sudo cp /tmp/trace-run.service /etc/systemd/system/trace-run.service
sudo systemctl daemon-reload
sudo systemctl enable trace-run.service
sudo systemctl restart trace-run.service
sleep 5
sudo systemctl is-active trace-run.service
sudo systemctl is-enabled trace-run.service
echo SERVICE_INSTALLED
