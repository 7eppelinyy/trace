#!/usr/bin/env bash
# systemd service 异常自动恢复验证：杀主进程 -> 等待 RestartSec=30 -> 确认新进程由 systemd 拉起
set -e
PID1=$(systemctl show -p MainPID --value trace-run)
echo "PID_BEFORE=$PID1"
kill -9 "$PID1"
sleep 40
PID2=$(systemctl show -p MainPID --value trace-run)
echo "PID_AFTER=$PID2"
if [ "$PID1" != "$PID2" ] && [ "$PID2" != "0" ]; then echo "AUTO_RESTART_OK"; else echo "AUTO_RESTART_FAILED"; fi
systemctl is-active trace-run
sudo journalctl -u trace-run --since '3 minutes ago' --no-pager | grep -E 'pipeline started|Stopped|Started Trace' | tail -6
