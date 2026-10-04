# 微信云托管接入已有 Google Cloud 后端

小程序 → 微信云托管 `trace-api` → 验证证书的 HTTPS → GCP nginx → 现有 FastAPI 与 SQLite。

此目录只部署接入网关，不另建 SQLite 或采集进程。Google Cloud 的
`trace-api` / `trace-run` 保持原服务和数据。现有小程序的环境 ID、服务名与路径保持一致。

GitHub 发布方式：选分支 `codex/wechat-gcp-gateway`，构建目录为仓库根目录，
Dockerfile 路径为 `Dockerfile.wechat-gateway`，容器监听端口为 `8080`。
代码包发布方式则使用下述目录内 Dockerfile；两种构建方式内容相同。

1. 在 `trace-prod-77/us-central1-a/trace-vm` 执行 `setup-gcp-gateway.sh`。
2. 将 VM `/etc/trace-gateway/backend.crt` 的**公开证书**保存为此目录的 `backend.crt`。
   私钥 `backend.key` 必须留在 VM，不能打包或上传微信。
3. 将此目录作为微信云托管 `trace-api` 的构建目录，Dockerfile 为 `Dockerfile`，端口为 `8080`。
4. 确认该服务允许当前小程序通过 `wx.cloud.callContainer` 调用。记录旧版本号后再发布。
5. 验证微信入口 `/api/v1/health`、`/ready`、访客会话与事件读取，再用正式版小程序实测。
   微信入口与 GCP 的来源采集时间应一致；HTTP 200 不能单独作为业务验收结论。

TLS 证书仅在此网关中作为指定上游的信任证书，不安装到客户端或系统信任库。
证书有效期一年；更新证书后需同时更新微信网关镜像中的公开证书。
此方案不提供小程序直接访问 GCP 的公开可信域名。

回滚：微信服务切回发布前版本。GCP 可执行
`sudo rm /etc/nginx/conf.d/trace-gateway.conf && sudo nginx -t && sudo systemctl reload nginx`，
不删除数据库、不停止采集服务。原微信容器数据若有真实用户记录，需先备份并确认迁移，
再切换入口，避免用户记录留在旧实例。
