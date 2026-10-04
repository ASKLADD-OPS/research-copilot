# Research Copilot 前端（Nuxt 3）
# 单阶段构建：产物跑在 node 上，镜像大一点但 compose 里是内网服务，换来的
# 是"COPY 完整源码 + nuxt build"这条最短路径，不会踩错 .output 的拷贝目标。
FROM node:22-alpine

WORKDIR /app

# devDependencies 里的 @playwright/test 带 postinstall 钩子，默认会下 ~300MB 浏览器。
# 生产镜像跑的是 .output，不需要浏览器 —— 这一行把下载关掉，否则 build 又慢又臃肿。
# 要跑 E2E 请在本机（或 CI 的独立 job）里 `npx playwright install chromium`。
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# 先只拷 package.json：依赖没变时这层直接命中缓存
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY . .

# nuxt build 需要 devDependencies，所以不能在构建前设 NODE_ENV=production
RUN npm run build

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000

EXPOSE 3000

CMD ["node", ".output/server/index.mjs"]
