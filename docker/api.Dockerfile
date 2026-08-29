# 轻量 API 镜像：不安装 GDAL 与科学计算栈（架构边界，见 doc/总体架构.md）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# PyPI 镜像源可由 compose build.args 覆盖（国内网络环境注入镜像站）
ARG UV_DEFAULT_INDEX
ENV UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX:-https://pypi.org/simple} \
    UV_HTTP_TIMEOUT=120

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /srv/app

# 先只复制依赖清单以利用层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group api --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev --group api

RUN useradd --create-home appuser
USER appuser

# 迁移与应用由 compose command 执行；此处仅为默认值
CMD [".venv/bin/uvicorn", "app.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
