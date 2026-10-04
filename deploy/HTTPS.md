# HTTPS 配置说明

生产入口是 `deploy/nginx/research-copilot.conf` 里的 Nginx。默认只监听 **80**（HTTP），
要上 HTTPS 按下面三步走，全部改动都在配置里、不动代码。

> 为什么默认不是 HTTPS：评测环境的域名/证书不确定，写死 `ssl_certificate` 会让 Nginx
> 在缺证书时直接起不来，反而违背「在线演示 24h 可用」。所以 HTTPS 做成显式三行开关。

---

## 一、证书从哪来

| 场景 | 做法 | 有效期 |
|---|---|---|
| 有公网域名 | Let's Encrypt + certbot（下方 webroot 方式） | 90 天，自动续 |
| 内网 / 无域名 | 自签证书（浏览器会警告，可点继续；仅演示够用） | 自定义 |
| 云厂商托管 | 直接下载证书文件放进 `deploy/certs/` | 厂商决定 |

证书文件统一放 **`deploy/certs/`**（已 gitignore，**不要提交私钥**）：

```
deploy/certs/fullchain.pem    # 证书链
deploy/certs/privkey.pem      # 私钥
```

---

## 二、Let's Encrypt（有域名，推荐）

```bash
# 1. 域名 A 记录先指向本机公网 IP，并放行 80/443
# 2. 起一次 HTTP 形态（nginx 的 /.well-known/acme-challenge/ 已指向 /var/www/certbot）
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d nginx

# 3. 用 certbot（宿主 docker 方式，无需装到机器上）
docker run --rm \
  -v "$PWD/deploy/certs:/etc/letsencrypt" \
  -v "$PWD/deploy/certbot-webroot:/var/www/certbot" \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d research-copilot.example.com \
  --email you@example.com --agree-tos --no-eff-email

# 4. certbot 生成的是
#    deploy/certs/live/research-copilot.example.com/{fullchain.pem,privkey.pem}
#    把它软链/复制成 deploy/certs/{fullchain.pem,privkey.pem}
```

**续期**（每 90 天）：

```bash
docker run --rm \
  -v "$PWD/deploy/certs:/etc/letsencrypt" \
  -v "$PWD/deploy/certbot-webroot:/var/www/certbot" \
  certbot/certbot renew --webroot -w /var/www/certbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec nginx nginx -s reload
```

---

## 三、自签证书（无域名，最省事）

```bash
mkdir -p deploy/certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout deploy/certs/privkey.pem \
  -out deploy/certs/fullchain.pem \
  -subj "/CN=research-copilot.local"
```

---

## 四、打开 HTTPS（三处改动）

**1. Nginx 配置** — 打开 `deploy/nginx/research-copilot.conf` 底部被注释的 `server { listen 443 ssl; ... }`，
把 `server_name` 换成你的域名；同时把 80 端口那个 server 里的 `location` 全部删掉，只留一行跳转：

```nginx
server {
    listen 80;
    server_name research-copilot.example.com;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
```

**2. compose 放行 443 + 挂证书** — `docker-compose.prod.yml` 的 nginx 服务：

```yaml
    ports:
      - "${HTTP_PORT:-80}:80"
      - "${HTTPS_PORT:-443}:443"        # ← 取消注释
    volumes:
      # certs 与 certbot-webroot 已经挂好，无需再加
```

**3. 让后端认 https 协议** — 反代已经带了 `X-Forwarded-Proto`，后端 uvicorn 也已加
`--proxy-headers --forwarded-allow-ips='*'`（见 prod 覆盖层的 command）。
如果你要做登录跳转 / 绝对 URL，`.env` 里再补：

```
BACKEND_CORS_ORIGINS=https://research-copilot.example.com
NUXT_PUBLIC_API_BASE=/api/v1
```

> 保持 `NUXT_PUBLIC_API_BASE` 是**相对路径** `/api/v1` 是刻意的：同源走 Nginx，
> 前后端之间不存在跨域，CORS 那一整类问题直接消失。

---

## 五、上线后自检

```bash
# 1. 证书链与有效期
openssl s_client -connect research-copilot.example.com:443 -servername research-copilot.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -dates

# 2. Nginx 存活
curl -s https://research-copilot.example.com/healthz          # → ok

# 3. 后端健康（经反代）
curl -s https://research-copilot.example.com/health | head -c 300

# 4. 安全头是否落地
curl -sI https://research-copilot.example.com/ | grep -i "strict-transport\|x-frame\|x-content-type"

# 5. SSE 是否真的不缓冲（应逐行、有间隔地吐，不是一次性全出）
curl -N -X POST https://research-copilot.example.com/api/v1/qa/stream \
  -H 'Content-Type: application/json' -d '{"question":"测试流式"}'
```

第 4 步没输出 = 443 那个 server 块的 `add_header` 没生效，检查是否真走了 https 而不是被 301 到 http。
第 5 步如果「唰」一下全出来，检查 `/api/` 的 `proxy_buffering off` 是否被覆盖。

---

## 六、常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| Nginx 起不来，日志 `cannot load certificate` | 证书没放进 `deploy/certs/` 就打开了 443 块 | 先放证书，或先注释回 443 块 |
| 502 Bad Gateway | backend 未 healthy | `docker compose ps` 看 backend 健康态；反代 `depends_on: service_healthy` 会挡在前面 |
| 上传大 PDF 报 413 | 传输层上限 | 已设 `client_max_body_size 200m`；若还报，检查是否有外层云 LB 的独立上限 |
| 问答页面看不到流式轨迹 | SSE 被缓冲 | `/api/` 必须 `proxy_buffering off`（已设）；云厂商 CDN/LB 也可能缓冲，需关其压缩/缓冲 |
| 续期后浏览器仍报旧证书 | Nginx 没 reload | 跑上面的 `nginx -s reload` |
