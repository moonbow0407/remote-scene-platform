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
| **Stage 4** 目录与生态映射 | 已完成 | 资源目录树、卫星/传感器、生态参数与显式多对多映射；资产分类外键；检索按目录（含子树）/卫星/传感器/生态映射过滤。A4.1–A4.3 已在 Compose 上通过 |
| **Stage 5** 监测计划与调度 | 主体已落地 | 计划/occurrence/执行/输入快照模型，RRULE 与固定间隔，Scheduler 互斥锁与停机补跑，增量资产选择，不可变输入快照；执行经 Job(MONITORING_RUN)+Outbox→RabbitMQ→Geo Worker 快照审计闭环。真实 Broker/Worker 全链路集成测试通过；A5.1–A5.5 人工验收待 Compose 执行 |
| **Stage 6** 生命周期与可靠性 | 主体已落地 | 软删除/7 天恢复、异步物理清理与共享 blob 保护、MinIO 退避删除；任务租约/取消检查点/软硬时限/临时盘预检；队列、Job、Worker、存储指标与 Grafana 面板。关闭与否以 A6.1–A6.4 为准 |
| **Stage 7** 迁移收口 | 进行中 | 57 项矩阵静态核对、OpenAPI RFC 9457 契约、发布/备份/排障与前端接入文档已落地；A7.2 干净 Compose 全量验收仍是关闭门禁 |

**当前工作：Stage 6 主体实现与 Stage 7 静态收口已落地；正在执行 A1–A6 干净 Compose 验收，未通过前不宣称 Stage 6/7 已完成。**

## 已交付能力（截至 Stage 6 主体与 Stage 7 静态收口）

对外入口：`http://localhost:8080`，业务 API 前缀 `/api/v1`。

| 能力 | 路径 |
| --- | --- |
| 存活 / 就绪 | `GET /api/v1/healthz`、`GET /api/v1/readyz`（db/minio/rabbitmq/titiler） |
| 创建分片上传会话 | `POST /api/v1/uploads/sessions`（预签名直传 MinIO，文件不经 API） |
| 会话详情 / 补签分片 / 完成 / 中止 | `GET/POST /api/v1/uploads/sessions/{id}`… |
| 资产与版本 | `GET /api/v1/assets/{id}`、`…/versions`、`…/versions/{vid}` |
| 空间/目录检索 | `POST /api/v1/assets/search`（EPSG:4326 几何；目录含子树、卫星、传感器、生态映射） |
| 更新资产分类 | `PATCH /api/v1/assets/{id}`（名称与目录/卫星/传感器外键；显式 null 清除） |
| 缺 CRS 续跑 | `POST /api/v1/assets/{id}/versions/{vid}/inputs` |
| 工件下载 | `GET /api/v1/assets/{id}/versions/{vid}/artifacts/{kind}/download-url` |
| Job 轮询 | `GET /api/v1/jobs/{job_id}` |
| Job 取消 | `POST /api/v1/jobs/{job_id}/cancel`（运行中在步骤检查点收敛） |
| 瓦片 URL | `GET /api/v1/assets/{id}/versions/{vid}/tile-url`（经 Nginx `/tiles/`，无令牌拒绝） |
| 追加版本 | `POST /api/v1/uploads/sessions` 带 `asset_id` |
| 要素检索 | `POST /api/v1/assets/{id}/versions/{vid}/features/search` |
| JSON Schema | `GET/PUT /api/v1/assets/property-schemas` |
| 登录与用户 | `POST /api/v1/auth/login`、`/refresh`、`GET /api/v1/auth/me`；管理员 `/api/v1/users` |
| 资源目录 | `GET/POST /api/v1/catalogs/resources`、`…/tree`、`…/{id}` |
| 卫星 / 传感器 | `GET/POST /api/v1/catalogs/satellites`、`…/sensors`、`GET …/satellites/{id}/sensors` |
| 生态参数 | `GET/POST /api/v1/ecology/parameters`、`…/tree`、`…/{id}` |
| 生态↔资源映射 | `GET/POST/PUT /api/v1/ecology/mappings`、`POST …/mappings/batch` |
| 资产删除与恢复 | `DELETE /api/v1/assets/{id}`、`POST …/assets/{id}/restore`（默认 7 天） |
| 监测计划 | `GET/POST /api/v1/monitoring/plans`、`GET/PUT/DELETE …/plans/{id}` |
| 计划暂停/恢复/手动触发 | `POST …/plans/{id}/pause`、`…/resume`、`…/trigger` |
| 监测执行与输入快照 | `GET …/plans/{id}/runs`、`GET /api/v1/monitoring/runs/{id}`、`…/inputs`；执行方状态接缝 `POST …/runs/{id}/start|succeed|fail` |

Prometheus 指标 `GET /api/v1/metrics` 仅 Compose 内网抓取，Nginx 对外返回 404。Stage 6
指标覆盖 Outbox/RabbitMQ 积压、Job 状态/失败/处理时长、Worker 消费者/利用率、存储与
清理积压；`observability` profile 自动加载 Grafana 运维面板。

上传支持栅格 TIFF、矢量（GeoJSON / Shapefile ZIP / GeoPackage）和普通附件；创建会话可同时绑定资源目录、卫星与传感器。监测计划支持固定间隔（ISO 8601 duration 子集）与 RRULE（RFC 5545）调度：到期由独立 Scheduler 扫描派发（多实例经 PostgreSQL advisory lock 互斥，occurrence `(plan_id, scheduled_for)` 数据库唯一），停机只补跑最近一次、其余周期记 `MISSED`；每次执行按增量窗口（上次成功执行之后）选择 READY 资产版本并冻结不可变输入快照。执行派发与 Job(MONITORING_RUN)+Outbox 同事务创建，由 Dispatcher 投递、Geo Worker 中的监测执行任务认领，完成输入快照执行期审计后推进 Run 与 Job 终态。

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

进程内测试与静态检查不需要容器：

```bash
uv sync --all-groups            # 安装全部依赖组（api/worker/dev）
uv run pytest                   # 进程内测试
uv run ruff check .             # 静态检查
uv run pyright                  # 类型检查
```

日常联调（WSL Docker Engine + 本机进程）：不要用 Windows 上的 PostgreSQL 15 / Redis / MySQL。
基础设施用 Compose，API / Dispatcher / Worker 留在 WSL。只需一份 `.env`（`127.0.0.1` 映射口）；
容器内主机名由 `compose.yaml` 覆盖，不必再维护第二份环境文件。

```bash
cp .env.example .env          # 首次
uv sync --all-groups
./scripts/dev.sh              # 起 db/minio/rabbitmq/titiler，并在本机跑 API+Dispatcher+Worker
# ./scripts/dev.sh --all      # 还要 Scheduler / Recovery / Cleanup 时
```

验证：`curl -s http://127.0.0.1:8000/api/v1/readyz`。浏览器访问 MinIO 控制台 `http://127.0.0.1:9001`。
Ctrl-C 结束本机进程，基础设施容器保持运行。PostGIS 映射 `127.0.0.1:55432`，避开 Windows `5432`。
瓦片网关（Nginx `:8080` fail-closed）需要全栈 Compose，日常改 API 不必起 nginx。

干净环境验收仍走全栈（会构建应用镜像）：

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8080/api/v1/readyz
```

依赖组边界：API 镜像只装基础依赖 + `api` 组（无 GDAL/科学栈）；Geo Worker 镜像装 `worker`
组（Celery + rasterio/shapely/pyproj/numpy，GDAL 由基础镜像提供）。
WSL 原生 Worker 依赖本机已 `uv sync --all-groups` 安装的 rasterio 等；不要把 GDAL 装进 API 进程。

## 进程与端口

| 进程 | 说明 | 入口 |
| --- | --- | --- |
| api | FastAPI，Nginx 反代 `/api/`，启动时执行迁移 | `app.api.app:create_app --factory` |
| worker | Celery Geo Worker，队列 `geo`，并发 2 | `app.worker.celery_app:celery` |
| dispatcher | Outbox 投递循环 | `python -m app.dispatcher.main` |
| scheduler | 监测计划调度循环：advisory lock 互斥 + 到期扫描 + occurrence 幂等派发 + 停机补跑 | `python -m app.scheduler.main` |
| recovery | 回收租约过期的 RUNNING Job并经 Outbox 重投 | `python -m app.recovery.main` |
| cleanup | 过期资产物理清理、blob 引用复核与 MinIO 退避删除 | `python -m app.cleanup.main` |
| nginx | 唯一对外入口 `:8080`；`/tiles/` fail-closed，令牌由 API 校验 | — |

基础设施：PostgreSQL/PostGIS 16-3.4（本机 `127.0.0.1:55432`，避开 Windows PostgreSQL 15 的 5432）、MinIO `RELEASE.2025-09-07T16-13-09Z`（`127.0.0.1:9000/9001`）、RabbitMQ 3.13（`127.0.0.1:5672`，Management `15672`）、TiTiler（`127.0.0.1:8081`）、
Prometheus（`:9090`），Grafana 位于 `observability` profile（可选）。
Docker Hub 直连超时时，可 `sudo ./scripts/apply-docker-wsl-network.sh` 让 dockerd 走本机 `127.0.0.1:7897` 代理并启用镜像源。

## 目录结构

```
src/app/
├── core 层职责当前分布在顶层：settings.py（配置）、db.py（会话/基类）、
│   logging.py（结构化日志）、ids.py（UUIDv7）、errors.py（RFC 9457 错误）、
│   pagination.py（分页基元）、checks.py（就绪检查）、context.py（ActorContext）
├── api/        # FastAPI 应用工厂、探针、指标、trace 中间件
├── assets/     # 逻辑资产、不可变版本、检索
├── uploads/    # MinIO Multipart 上传会话
├── catalogs/   # 资源目录、卫星、传感器（Stage 4）
├── ecology/    # 生态参数与资源映射（Stage 4）
├── monitoring/ # 监测计划、occurrence、执行与输入快照（Stage 5）
├── auth/       # JWT 用户鉴权接缝
├── jobs/       # Job 状态机、事件、Outbox
├── processing/ # 栅格/矢量/附件入库流水线与 Celery 任务
├── vector_features/  # PostGIS 要素与空间检索
├── tiles/      # 短期瓦片令牌
├── worker/     # Celery Geo Worker
├── dispatcher/ # Outbox Dispatcher
├── scheduler/  # 独立 Scheduler
├── recovery/   # Job 租约过期恢复
└── cleanup/    # Stage 6 资产/对象异步清理
alembic/        # 0001–0010（0010：软删除、对象清理任务与运维索引）
docker/         # api/worker 镜像与 Nginx 配置
prometheus/     # 抓取配置
grafana/        # 自动配置的 Prometheus 数据源与 Stage 6 运维面板
doc/            # 架构、阶段方案、迁移矩阵、验收基线
tests/          # 进程内测试；集成测试随阶段补充
```

尚未关闭：Stage 2/3/5 的剩余人工场景、A6.1–A6.4 与 A7.2 干净环境全量重放；
2–100 GB 代表性演练受实际硬件容量约束。监测算法类工作负载（生态参数计算）按
AGENTS.md 约束不在首版范围，当前执行语义为输入快照执行期审计。

发布、备份/恢复、排障与限制见 `doc/发布与运维手册.md`；前端只需 OpenAPI
`/api/v1/openapi.json` 与 `doc/前端接入指南.md` 即可完成上传、轮询、下载和瓦片联调。

## 约定摘要

- 核心主键 UUIDv7（`app.ids`）；时间 UTC 持久化、API 响应携带时区。
- 列表响应统一 `{items, total, page, page_size}`；错误统一 RFC 9457 + 业务 `code` + `trace_id`。
- 日志为单行 JSON，`trace_id` 贯穿 API → Job → Worker。
- Worker 任务必须幂等（至少一次投递）；瞬时错误可重试，确定性错误不得盲目重试。
- 缺 CRS 等可补信息进入 `NEEDS_INPUT`，不得假定 EPSG:4326。
