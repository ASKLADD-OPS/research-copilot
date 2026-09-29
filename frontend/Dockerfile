# Research Copilot 前端（Nuxt 3）
# 单阶段构建：产物跑在 node 上，镜像大一点但 compose 里是内网服务，换来的
# 是"COPY 完整源码 + nuxt build"这条最短路径，不会踩错 .output 的拷贝目标。
FROM node:22-alpine

WORKDIR /app

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
