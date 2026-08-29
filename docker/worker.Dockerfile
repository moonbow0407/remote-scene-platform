# Geo Worker 镜像：包含 GDAL 运行库与基线地理处理栈
# 说明：rasterio/pyproj 自带部分 GDAL 组件，系统 GDAL 用于命令行工具与 Stage 2 数据源
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.10.3

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# PyPI 镜像源可由 compose build.args 覆盖（国内网络环境注入镜像站）
ARG UV_DEFAULT_INDEX
ENV UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX:-https://pypi.org/simple} \
    UV_HTTP_TIMEOUT=120

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /srv/app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group worker --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --group worker

RUN useradd --create-home appuser \
    && install -d -o appuser -g appuser /data/tmp
USER appuser

CMD [".venv/bin/celery", "-A", "app.worker.celery_app:celery", "worker", "-Q", "geo", "--concurrency", "2", "--loglevel", "INFO"]
