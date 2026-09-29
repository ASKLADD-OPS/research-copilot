# 后端 API —— Research Copilot

FastAPI + SQLAlchemy 2.0 + LangGraph + Milvus，外部工具统一走 MCP。

```bash
# 容器内（推荐）
make sh-backend
alembic upgrade head
pytest

# 本机直跑（需先装依赖：pip install -e ".[dev]"）
python run.py --port 8000        # Windows 必须用它，见下
```

入口：`app/main.py` · 端点定义：`app/api/v1/` · Agent 图：`app/agents/graph.py`

> **Windows 上不要用 `uvicorn app.main:app`。** psycopg 的异步驱动不接受默认的
> `ProactorEventLoop`，而 uvicorn 0.36+ 会在 `--no-reload` 下强制用它。
> `run.py` 会先切到 SelectorEventLoop 并传 `loop="none"`，绕开这件事。详见 `docs/技术文档.md` §9.1。

> 文档解析默认走 MinerU。它不在这个镜像里（torch + 版面模型的权重好几个 GB），
> 建议装在独立 venv 并用 `MINERU_CMD` 指过去；没装也会自动降级到 PyMuPDF → pdfplumber → pypdf。
> 见 `docs/技术文档.md` §3.5。

> 环境变量统一由仓库根的 `.env` 提供（compose 注入）。`backend/.env.example` 仅列出**脱离 Docker 本机直跑**时需要额外补的项。
