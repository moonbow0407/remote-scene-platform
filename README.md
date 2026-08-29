# remote-scene-platform

多源遥感数据共享平台后端（Python 迁移版）。模块化单体：FastAPI API、Outbox Dispatcher、
独立 Scheduler、Celery Geo Worker 分进程运行；PostgreSQL/PostGIS 唯一关系与空间库，
MinIO 保存不可变对象，RabbitMQ 仅负责消息传递，TiTiler 由 Nginx 瓦片网关受控访问。

迁移依据与阶段方案见旧参考仓库 `multi-data-remote-scene-share-platfrom` 的
`doc/总体架构.md`、`doc/阶段迁移实施方案.md`、`doc/旧接口迁移矩阵.md`、`doc/验收基线与测试夹具.md`。

## 当前阶段

- **Stage 0 已完成**：旧接口矩阵、缺陷清单、验收基线与测试夹具定义（在旧仓库 doc/ 下）。
- **Stage 1 已完成**：可运行骨架——依赖分组、镜像、Compose、配置、结构化日志、trace_id、
  RFC 9457、分页基元、健康/就绪探针、Alembic 初始迁移（PostGIS）。
- 下一阶段（Stage 2）：栅格纵向闭环（上传 → Outbox → Worker → COG → PostGIS → TiTiler）。

## 快速启动

```bash
cp .env.example .env          # 按需修改口令
docker compose up -d --build  # 首次构建镜像
docker compose ps             # 全部服务 healthy/up
```

验证：

```bash
curl -s http://localhost:8080/api/v1/readyz | jq   # db/minio/rabbitmq/titiler 全部 ok
curl -s http://localhost:9090/-/healthy            # Prometheus
```

业务 API 与运维探针统一挂在 `/api/v1`；指标端点只允许 Compose 内网中的 Prometheus 抓取。

## 本地开发

```bash
uv sync --all-groups            # 安装全部依赖组（api/worker/dev）
uv run pytest                   # 进程内测试
uv run ruff check .             # 静态检查
uv run pyright                  # 类型检查
uv run alembic upgrade head     # 迁移（需可达 PostgreSQL）
```

依赖组边界：API 镜像只装基础依赖 + `api` 组（无 GDAL/科学栈）；Geo Worker 镜像装 `worker`
组（Celery + rasterio/shapely/pyproj/numpy，GDAL 由基础镜像提供）。

## 进程与端口

| 进程 | 说明 | 入口 |
| --- | --- | --- |
| api | FastAPI，Nginx 反代 `/api/`，迁移随启动执行 | `app.api.app:create_app --factory` |
| worker | Celery Geo Worker，队列 `geo`，并发 2 | `app.worker.celery_app:celery` |
| dispatcher | Outbox 投递循环（Stage 2 实装认领/发布） | `python -m app.dispatcher.main` |
| scheduler | 监测计划调度循环（Stage 5 实装） | `python -m app.scheduler.main` |
| nginx | 唯一对外入口 `:8080`；`/tiles/` fail-closed，瓦片令牌校验端点 Stage 2 实装 | — |

基础设施：PostgreSQL/PostGIS 16-3.4、MinIO、RabbitMQ 3.13（Management 仅内网）、TiTiler、
Prometheus（`:9090`），Grafana 位于 `observability` profile（可选）。

## 目录结构

```
src/app/
├── core 层职责当前分布在顶层：settings.py（配置）、db.py（会话/基类）、
│   logging.py（结构化日志）、ids.py（UUIDv7）、errors.py（RFC 9457 错误）、
│   pagination.py（分页基元）、checks.py（就绪检查）
├── api/        # FastAPI 应用工厂、探针、指标、trace 中间件
├── worker/     # Celery Geo Worker
├── dispatcher/ # Outbox Dispatcher
└── scheduler/  # 独立 Scheduler
alembic/        # 迁移（0001：启用 PostGIS 扩展）
docker/         # api/worker 镜像与 Nginx 配置
prometheus/     # 抓取配置
tests/          # 进程内测试；集成测试随阶段补充
```

## 约定摘要

- 核心主键 UUIDv7（`app.ids`）；时间 UTC 持久化、API 响应携带时区。
- 列表响应统一 `{items, total, page, page_size}`；错误统一 RFC 9457 + 业务 `code` + `trace_id`。
- 日志为单行 JSON，`trace_id` 贯穿 API → Job → Worker（Stage 2 起任务日志继承该约定）。
- Worker 任务必须幂等（至少一次投递）；瞬时错误可重试，确定性错误不得盲目重试。
