# ---- 基础镜像 ----
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 模型缓存落在挂载卷上，重建镜像不会重下 bge-m3
    HF_HOME=/models/hf \
    MODELSCOPE_CACHE=/models

WORKDIR /app

# libpq 用于 psycopg；curl 便于进容器排查
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl libpq5 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ---- 依赖层（先装依赖，代码改动不触发重装）----
# 先复制 pyproject + app 目录：hatchling 打包需要包体存在
COPY pyproject.toml README.md ./
COPY app ./app

# MinerU 默认不装：torch + 版面模型权重会让镜像多出好几个 GB。
# 要一体化部署时：docker compose build --build-arg INSTALL_MINERU=true
ARG INSTALL_MINERU=false

RUN pip install --upgrade pip setuptools wheel \
    && pip install -e ".[dev]" \
    && if [ "$INSTALL_MINERU" = "true" ]; then pip install -e ".[mineru]"; fi

# ---- 代码层 ----
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
