# Research Copilot

> 面向科研语料的多智能体研究助手：**PDF → bge-m3 双向量 → Milvus 混合检索 + RRF → LangGraph 三范式编排 → 带句级溯源的流式回答**。

技术栈：Nuxt 3 + FastAPI + LangGraph + Milvus 2.4 + PostgreSQL 16 + MinerU，外部工具统一封装为 MCP Server。
长任务（PDF 解析入库、引文边重建）**在 backend 进程内**的后台线程执行，不依赖 Redis / Celery / 独立 worker 容器。

---

## 快速开始

```bash
cp .env.example .env          # 或 make init
# 至少填两项：LLM_API_KEY、SECRET_KEY（openssl rand -hex 32）
make up                       # = docker compose up -d --build
```

| 入口 | 地址 |
|:---|:---|
| 前端 | http://localhost:3000 |
| API 文档（Swagger） | http://localhost:8000/docs |
| API 健康检查 | http://localhost:8000/health |
| Milvus 指标 | http://localhost:9091/healthz |

首次启动会拉取 **bge-m3（约 2GB）** 到 `models_cache` 卷，后续重建镜像不会重复下载。全部服务 `healthy` 约需 2–5 分钟（Milvus 最慢）。

```bash
make ps      # 看健康状态
make logs    # 跟踪日志；make logs S=backend 只看一个
make down    # 停止（保留数据）
```

## 目录结构

```
research-copilot/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口 + 生命周期
│   │   ├── core/                # 配置、日志、错误、安全
│   │   ├── api/v1/              # papers / qa / graph / writing / tools / chat(SSE) / tasks
│   │   ├── models/ schemas/     # SQLAlchemy 2.0 模型 / Pydantic v2 模型
│   │   ├── agents/              # ★ LangGraph：state / graph / checkpointer / 11 个节点 / prompts
│   │   │   └── mcp/             # MultiServerMCPClient 封装 + 工具注册表
│   │   ├── mcp_servers/         # ★ 5 个独立 FastMCP Server
│   │   ├── rag/                 # ★ 混合检索 + RRF / CRAG 降级 / 溯源引擎
│   │   ├── llm/                 # OpenAI SDK 统一封装（多端点 + 流式 + 结构化）
│   │   ├── embeddings/          # bge-m3 Dense + Sparse 双向量
│   │   ├── parsers/             # ★ MinerU 为主 + PyMuPDF/pdfplumber/pypdf 降级链
│   │   ├── graph_analysis/      # NetworkX 引文/实体图
│   │   ├── indexing/            # ★ 进程内后台任务（pipeline / runner），替代原 workers/
│   │   └── db/                  # PostgreSQL / Milvus 连接
│   ├── alembic/                 # 迁移
│   └── tests/                   # 183 个离线用例
├── frontend/                    # Nuxt 3：pages / components / stores / composables / assets/css（设计 token）
├── docs/                        # 技术文档 + 同类项目调研报告
├── docker-compose.yml           # 6 个服务
└── .env.example
```

## 服务与端口

| 服务 | 镜像 | 端口 | 说明 |
|:---|:---|:---|:---|
| postgres | postgres:16-alpine | 5432 | 会话、论文元数据、任务 |
| etcd | quay.io/coreos/etcd:v3.5.16 | — | Milvus 元数据 |
| minio | minio/minio | — | Milvus 对象存储 |
| milvus | milvusdb/milvus:v2.4.15 | 19530 / 9091 | 向量库（Dense + Sparse） |
| backend | ./backend | 8000 | FastAPI（长任务在进程内跑） |
| frontend | ./frontend | 3000 | Nuxt 3 |

> Milvus standalone（etcd + minio + milvus）单机建议预留 **≥4GB 内存**。

## 验收自检

```bash
make up
docker compose ps                                   # 验收标准第 1 条：6 个服务
curl -s localhost:8000/docs -o /dev/null -w '%{http_code}\n'   # 期望 200
curl -s localhost:8000/health | python -m json.tool            # postgres / milvus / mineru / llm
curl -s localhost:3000 -o /dev/null -w '%{http_code}\n'        # 期望 200
```

## 文档解析（MinerU）

MinerU 默认**不装进后端镜像**（torch + 版面模型权重好几个 GB）。推荐装在独立 venv，
再用 `MINERU_CMD` 指过去：

```bash
python -m venv C:/tmp/mineru-venv
C:/tmp/mineru-venv/Scripts/python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
C:/tmp/mineru-venv/Scripts/python.exe -m pip install "mineru[core]"
# .env: MINERU_CMD=C:/tmp/mineru-venv/Scripts/mineru.exe
```

没装也能跑：解析会自动降级 `pymupdf → pdfplumber → pypdf`，实际生效的解析器记在
`papers.parser` 并在文献库列表里显示。`/health` 的 `mineru` 项会告诉你它到底找没找到。

## 开发

```bash
make sh-backend     # 进后端容器
make migrate        # 应用迁移
make revision M="add xxx"
make test           # 容器内跑 pytest
make lint fmt       # ruff
```

后端不依赖 LLM/网络即可跑单测：RAG 融合、溯源校验、意图映射等纯逻辑都有离线用例。

## 文档

- `docs/技术文档.md` —— 架构、Agent 图、RAG 链路、MCP 协议约定、数据模型、验收自检结果
- `../docs/reference-projects.md` —— 同类开源项目实测调研（含"模型名不是包名"的方法论修正）

## 许可

MIT
