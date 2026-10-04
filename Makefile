# Research Copilot · 常用命令
# 说明：所有目标都幂等。init 只在 .env 不存在时创建，绝不覆盖已有配置。

SHELL := /bin/sh
COMPOSE := docker compose

.PHONY: help init up down restart logs ps build sh-backend sh-db migrate revision \
        test test-backend test-cov e2e up-prod up-gpu lint fmt clean reset

help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## 从 .env.example 创建 .env（已存在则跳过，不覆盖）
	@if [ -f .env ]; then \
		echo "[skip] .env 已存在，未覆盖"; \
	else \
		cp .env.example .env; \
		echo "[ok] 已生成 .env —— 请填写 LLM_API_KEY 与 SECRET_KEY"; \
	fi

up: init ## 启动全部服务（后台）
	$(COMPOSE) up -d --build
	@echo "[ok] 前端 http://localhost:$${FRONTEND_PORT:-3000}   后端 http://localhost:$${BACKEND_PORT:-8000}/docs"

down: ## 停止全部服务（保留数据卷）
	$(COMPOSE) down

restart: ## 重启 backend（改完 Python 代码常用）
	$(COMPOSE) restart backend

logs: ## 跟踪全部日志（make logs S=backend 只看一个服务）
	$(COMPOSE) logs -f $(S)

ps: ## 查看服务状态与健康
	$(COMPOSE) ps

build: ## 重新构建镜像
	$(COMPOSE) build

sh-backend: ## 进后端容器
	$(COMPOSE) exec backend bash

sh-db: ## 进 PostgreSQL
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-copilot} -d $${POSTGRES_DB:-research_copilot}

migrate: ## 应用数据库迁移
	$(COMPOSE) exec backend alembic upgrade head

revision: ## 生成迁移（make revision M="add xxx"）
	$(COMPOSE) exec backend alembic revision --autogenerate -m "$(M)"

test: ## 跑后端全部测试
	$(COMPOSE) exec backend python -m pytest

test-backend: ## 本机直接跑测试（需已装依赖，不需要 docker）
	cd backend && pytest

test-cov: ## 跑后端测试并校验覆盖率门槛（≥70%）
	cd backend && pytest --cov=app --cov-report=term-missing --cov-fail-under=70

e2e: ## 跑前端 Playwright E2E（自动拉起 dev server）
	cd frontend && npm run e2e

up-prod: init ## 生产形态启动（含 Nginx 反向代理）
	$(COMPOSE) -f docker-compose.yml -f docker-compose.prod.yml up -d --build
	@echo "[ok] 入口 http://localhost:$${HTTP_PORT:-80}"

up-gpu: init ## 生产 + GPU 形态启动（需 nvidia-container-toolkit）
	$(COMPOSE) -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build

lint: ## ruff 检查
	cd backend && ruff check .

fmt: ## ruff 自动修复 + 格式化
	cd backend && ruff check --fix . && ruff format .

clean: ## 删除镜像与孤儿容器（! 不动数据卷）
	$(COMPOSE) down --rmi local --remove-orphans

reset: ## ⚠️ 危险：连数据卷一起删，数据库与向量库全部清空
	$(COMPOSE) down -v --remove-orphans
