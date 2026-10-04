# 部署进度 — 2026-10-04

状态：GCP HTTPS 接入口已部署并验证；微信线上入口尚未切换，真机离线尚未解决。

## 实测发现

- GCP 项目 `trace-prod-77`，主机 `trace-vm`，区域 `us-central1-a`，IP `34.31.198.232`。
- `trace-api`、`trace-run` 均 active，后端已有数据库与采集记录。
- GCP `/ready`：HTTP 200，31 个迁移，46 个标的。
- GCP `/api/v1/health`：数据库 OK，sources_status OK；巨潮单源降级。
- 微信现有 `/api/v1/health`：HTTP 200，但 14 个启用来源 last_success_at 均 null，sources_status STALE。
- 普通 HTTPS 成功不能证明 `wx.cloud.callContainer` 的环境/服务授权成功；仍需正式版真机检查。

## 已完成

- GCP 安装 nginx，并以 systemd 开机启动；443 端口使用项目原有防火墙规则。
- `/etc/nginx/conf.d/trace-gateway.conf` 转发至原 API `127.0.0.1:8000`。
- 证书与私钥位于 `/etc/trace-gateway/`，私钥留在 VM。
- 本机严格校验证书访问 GCP HTTPS `/ready` 和 `/`，均成功。
- 微信转发规则在 VM 独立临时 nginx 中验证；`/ready` 成功，受保护的自选接口正确返回 HTTP 401。
- 上传构建包：`D:\Trace\.deploy\trace-wechat-gateway.zip`，只含 Dockerfile、nginx 配置、公开证书及 dockerignore。
- 修复事件列表展示权限 SQL 的跨表 OR 扫描：拆成直接证据与关联证据的索引查询，保留保守展示规则。
- 本地分页、来源权限与性能相关测试：28 passed。
- 真实数据库新查询：0.504 秒，9,995 个可展示事件，返回前 6 个。
- 修复已部署至 GCP，API 重启后本地严格 TLS 验证事件接口：HTTP 200，1.475 秒，6 个事件，总数 9,995。
- 旧代码备份：`/opt/trace/.deploy/repositories.before-20261004.py`。回滚可恢复该文件至
  `/opt/trace/trace/db/repositories.py` 后重启 `trace-api`。
- 未修改数据库、采集服务或小程序代码；当前 nginx、API、采集服务均 active。

证书到期：2027-10-04 19:54:45（Asia/Taipei）。

## 待完成

1. 微信控制台读取访问权限、当前版本、持久化设置，以及旧实例真实用户数据；需要备份与迁移时先完成。
2. 发布网关至原环境 `trace-prod-d0g7s6tv2aba0f5cb` 的 `trace-api`，容器端口 8080。
3. 微信入口数据与 GCP 一致性验证、设备访客会话及正式版真机验收。

阻塞：浏览器控制多次返回 `nodeRepl.fetch request failed`，用户已进入控制台，但当前工具仍无法读取或操作该页面。
