# ──────────────────────────────────────────────
# DeepResearch 前端 Dockerfile
# 多阶段构建：node build → nginx 托管
# ──────────────────────────────────────────────

# ---- Stage 1: build ----
FROM node:20-alpine AS builder

WORKDIR /build

# 先复制依赖文件，利用层缓存
COPY package.json package-lock.json ./

# 安装依赖
RUN npm ci

# 拷贝源码并构建
COPY . .
RUN npm run build-only

# ---- Stage 2: nginx ----
FROM nginx:alpine AS runner

# 拷贝自定义 nginx 配置（SSE 透传）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 拷贝构建产物
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
