#!/usr/bin/env bash
# Run on the existing Trace VM. Private key stays on the VM.
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y nginx openssl
sudo install -d -m 700 /etc/trace-gateway
if ! sudo test -f /etc/trace-gateway/backend.key; then
  sudo openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 365 \
    -keyout /etc/trace-gateway/backend.key \
    -out /etc/trace-gateway/backend.crt \
    -subj '/CN=trace-backend.internal' \
    -addext 'subjectAltName=DNS:trace-backend.internal,IP:34.31.198.232' \
    -addext 'basicConstraints=critical,CA:TRUE'
  sudo chmod 600 /etc/trace-gateway/backend.key
fi
sudo tee /etc/nginx/conf.d/trace-gateway.conf >/dev/null <<'NGINX'
server {
    listen 443 ssl;
    server_name trace-backend.internal;
    ssl_certificate /etc/trace-gateway/backend.crt;
    ssl_certificate_key /etc/trace-gateway/backend.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 1m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_connect_timeout 10s;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }
}
NGINX
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx
# Export only the public certificate, never backend.key.
sudo cp /etc/trace-gateway/backend.crt /tmp/trace-backend-public.crt
sudo chmod 644 /tmp/trace-backend-public.crt
curl --fail --silent --show-error --max-time 30 \
  --cacert /tmp/trace-backend-public.crt \
  --resolve trace-backend.internal:443:127.0.0.1 \
  https://trace-backend.internal/ready
