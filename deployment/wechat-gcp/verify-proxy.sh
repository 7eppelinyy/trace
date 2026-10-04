#!/usr/bin/env bash
# Verify the same upstream/TLS rules using the VM's installed nginx.
set -euo pipefail
CHECK_DIR=$(mktemp -d /tmp/trace-gateway-check.XXXXXX)
trap 'if test -f "$CHECK_DIR/nginx.pid"; then kill "$(cat "$CHECK_DIR/nginx.pid")"; fi; rm -rf "$CHECK_DIR"' EXIT
sed -e 's/listen 8080;/listen 127.0.0.1:18080;/' \
    -e 's@/etc/nginx/trace-backend.crt@/tmp/trace-backend-public.crt@' \
    /tmp/trace-wechat-proxy.conf > "$CHECK_DIR/server.conf"
cat > "$CHECK_DIR/nginx.conf" <<EOF
pid $CHECK_DIR/nginx.pid;
error_log $CHECK_DIR/error.log;
events { worker_connections 128; }
http {
  access_log off;
  client_body_temp_path $CHECK_DIR/client_temp;
  proxy_temp_path $CHECK_DIR/proxy_temp;
  include $CHECK_DIR/server.conf;
}
EOF
nginx -p "$CHECK_DIR/" -c "$CHECK_DIR/nginx.conf" -t
nginx -p "$CHECK_DIR/" -c "$CHECK_DIR/nginx.conf"
curl --fail --silent --show-error --max-time 30 http://127.0.0.1:18080/ready
CODE=$(curl --silent --show-error --max-time 30 -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/api/v1/watchlist)
test "$CODE" = 401
echo ' PROXY_READY_OK AUTH_GUARD_401_OK'
