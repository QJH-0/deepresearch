# ──────────────────────────────────────────────
# DeepResearch 后端 Dockerfile
# 多阶段构建：builder 安装依赖 → runner 精简运行镜像
# ──────────────────────────────────────────────

# ---- Stage 1: builder ----
FROM python:3.11-slim AS builder

WORKDIR /build

# 系统依赖（编译 psycopg 等 C 扩展所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .

# 安装依赖到独立 prefix 目录（便于多阶段拷贝）
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Stage 2: runner ----
FROM python:3.11-slim AS runner

WORKDIR /app

# 运行时系统依赖（psycopg2-binary 需要 libpq，pymilvus 不需要额外系统库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 拷贝已安装的 Python 包
COPY --from=builder /install /usr/local

# 拷贝应用代码
COPY app/ app/
COPY config.json config.json
COPY scripts/ scripts/

# .env 通过 compose environment 注入，不打进镜像
# 确保日志输出目录存在
RUN mkdir -p /app/output/logs/research

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import httpx; r=httpx.get('http://127.0.0.1:8000/health/live'); exit(0 if r.status_code==200 else 1)" \
    || exit 1

# 启动命令
CMD ["uvicorn", "app.app_main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
