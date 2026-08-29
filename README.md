# remote-scene-platform

多源遥感数据共享平台后端（Python 迁移版）。模块化单体：FastAPI API、Outbox Dispatcher、
独立 Scheduler、Celery Geo Worker 分进程运行；PostgreSQL/PostGIS 唯一关系与空间库，
MinIO 保存不可变对象，RabbitMQ 仅负责消息传递，TiTiler 由 Nginx 瓦片网关受控访问。

**本文件是阶段看板。** 每个阶段开始、主体落地或验收关闭时，必须在同一批改动中更新
「阶段进度」表和「已交付能力」，不得只改代码或 `doc/` 而让此处停留在上一阶段。
详细规格与门禁见：

- `AGENTS.md`：实现约束
- `doc/总体架构.md`
- `doc/阶段迁移实施方案.md`
- `doc/旧接口迁移矩阵.md`
- `doc/验收基线与测试夹具.md`

旧 Java 业务代码在 `D:\multi-data-remote-scene-share-platfrom`（WSL：`/mnt/d/multi-data-remote-scene-share-platfrom`），只读核对调用链，不是新 API 兼容契约。

## 阶段进度

状态只使用：`未开始` / `进行中` / `主体已落地` / `已完成`。
`已完成` 以对应阶段退出条件和 `doc/验收基线与测试夹具.md` 的人工场景为准，不以“代码已写完”代替。

| 阶段 | 状态 | 交付摘要 |
| --- | --- | --- |
| **Stage 0** 旧接口盘点与验收基线 | 已完成 | 55 个旧端点迁移矩阵、缺陷清单、夹具规格与人工验收脚本，现位于 `doc/` |
| **Stage 1** 可运行骨架 | 已完成 | Python 3.12 / `uv` 依赖分组、API 与 Worker 分镜像、Compose、RFC 9457、分页、探针、结构化日志、Alembic `0001`（PostGIS） |
| **Stage 2** 栅格纵向闭环 | 主体已落地 | 上传 → Outbox → Worker → COG → PostGIS → 瓦片令牌。关闭与否以 A2.1–A2.10 为准 |
| **Stage 3** 矢量与附件 | 主体已落地 | 同一资产生命周期上 GeoJSON/Shapefile/GPKG 导入、附件 READY、要素空间检索、JSON Schema。关闭与否以 A3.1–A3.5 为准 |
| **Stage 4** 目录与生态映射 | 进行中 | 管理类 CRUD 已落地（资源目录树、卫星/传感器、生态参数与显式多对多映射）；资产检索过滤与监测接入待后续 |
| **Stage 5** 监测计划与调度 | 未开始 | 计划、RRULE/间隔、Scheduler 锁、增量选择、不可变输入快照 |
| **Stage 6** 生命周期与可靠性 | 未开始 | 软删除与 7 天恢复、无引用对象清理、超时/取消、运维指标 |
| **Stage 7** 迁移收口 | 未开始 | 矩阵核对、全量验收、OpenAPI 与前端交接 |

**当前工作：关闭 Stage 2 / Stage 3 验收；Stage 4A 目录/生态管理类 CRUD 可并行联调。**

## 已交付能力（截至 Stage 3，含 Stage 4A 管理类）

对外入口：`http://localhost:8080`，业务 API 前缀 `/api/v1`。

| 能力 | 路径 |
| --- | --- |
| 存活 / 就绪 | `GET /api/v1/healthz`、`GET /api/v1/readyz`（db/minio/rabbitmq/titiler） |
| 创建分片上传会话 | `POST /api/v1/uploads/sessions`（预签名直传 MinIO，文件不经 API） |
| 会话详情 / 补签分片 / 完成 / 中止 | `GET/POST /api/v1/uploads/sessions/{id}`… |
| 资产与版本 | `GET /api/v1/assets/{id}`、`…/versions`、`…/versions/{vid}` |
| 空间检索 | `POST /api/v1/assets/search`（EPSG:4326 Polygon/MultiPolygon） |
| 缺 CRS 续跑 | `POST /api/v1/assets/{id}/versions/{vid}/inputs` |
| 工件下载 | `GET /api/v1/assets/{id}/versions/{vid}/artifacts/{kind}/download-url` |
| Job 轮询 | `GET /api/v1/jobs/{job_id}` |
| 瓦片 URL | `GET /api/v1/assets/{id}/versions/{vid}/tile-url`（经 Nginx `/tiles/`，无令牌拒绝） |
| 追加版本 | `POST /api/v1/uploads/sessions` 带 `asset_id` |
| 要素检索 | `POST /api/v1/assets/{id}/versions/{vid}/features/search` |
| JSON Schema | `GET/PUT /api/v1/assets/property-schemas` |
| 登录与用户 | `POST /api/v1/auth/login`、`/refresh`、`GET /api/v1/auth/me`；管理员 `/api/v1/users` |
| 资源目录 | `GET/POST /api/v1/catalogs/resources`、`…/tree`、`…/{id}` |
| 卫星 / 传感器 | `GET/POST /api/v1/catalogs/satellites`、`…/sensors`、`GET …/satellites/{id}/sensors` |
| 生态参数 | `GET/POST /api/v1/ecology/parameters`、`…/tree`、`…/{id}` |
| 生态↔资源映射 | `GET/POST /api/v1/ecology/mappings`、`POST …/mappings/batch` |

Prometheus 指标 `GET /api/v1/metrics` 仅 Compose 内网抓取，Nginx 对外返回 404。

上传支持栅格 TIFF、矢量（GeoJSON / Shapefile ZIP / GeoPackage）和普通附件。监测计划尚未提供 API。Stage 4 资产检索按目录/卫星/传感器/生态映射过滤尚未接入。

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
| api | FastAPI，Nginx 反代 `/api/`，启动时执行迁移 | `app.api.app:create_app --factory` |
| worker | Celery Geo Worker，队列 `geo`，并发 2 | `app.worker.celery_app:celery` |
| dispatcher | Outbox 投递循环 | `python -m app.dispatcher.main` |
| scheduler | 监测计划调度循环（Stage 5 实装） | `python -m app.scheduler.main` |
| nginx | 唯一对外入口 `:8080`；`/tiles/` fail-closed，令牌由 API 校验 | — |

基础设施：PostgreSQL/PostGIS 16-3.4、MinIO（本机 `127.0.0.1:9000`）、RabbitMQ 3.13（Management 仅内网）、TiTiler、
Prometheus（`:9090`），Grafana 位于 `observability` profile（可选）。

## 目录结构

```
src/app/
├── core 层职责当前分布在顶层：settings.py（配置）、db.py（会话/基类）、
│   logging.py（结构化日志）、ids.py（UUIDv7）、errors.py（RFC 9457 错误）、
│   pagination.py（分页基元）、checks.py（就绪检查）、context.py（ActorContext）
├── api/        # FastAPI 应用工厂、探针、指标、trace 中间件
├── assets/     # 逻辑资产、不可变版本、检索
├── uploads/    # MinIO Multipart 上传会话
├── catalogs/   # 资源目录、卫星、传感器（Stage 4A）
├── ecology/    # 生态参数与资源映射（Stage 4A）
├── jobs/       # Job 状态机、事件、Outbox
├── processing/ # 栅格/矢量/附件入库流水线与 Celery 任务
├── vector_features/  # PostGIS 要素与空间检索
├── tiles/      # 短期瓦片令牌
├── worker/     # Celery Geo Worker
├── dispatcher/ # Outbox Dispatcher
└── scheduler/  # 独立 Scheduler
alembic/        # 迁移（0001 PostGIS，0002 栅格，0003 矢量/附件；Stage 4 表待合并）
docker/         # api/worker 镜像与 Nginx 配置
prometheus/     # 抓取配置
doc/            # 架构、阶段方案、迁移矩阵、验收基线
tests/          # 进程内测试；集成测试随阶段补充
```

尚未落地、不要预先建空目录：`catalogs`、`ecology`、`monitoring`、`vector_features`。

## 约定摘要

- 核心主键 UUIDv7（`app.ids`）；时间 UTC 持久化、API 响应携带时区。
- 列表响应统一 `{items, total, page, page_size}`；错误统一 RFC 9457 + 业务 `code` + `trace_id`。
- 日志为单行 JSON，`trace_id` 贯穿 API → Job → Worker。
- Worker 任务必须幂等（至少一次投递）；瞬时错误可重试，确定性错误不得盲目重试。
- 缺 CRS 等可补信息进入 `NEEDS_INPUT`，不得假定 EPSG:4326。
