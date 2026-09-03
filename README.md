# remote-scene-platform

多源遥感数据共享平台后端（Python 版）。卫星与无人机影像分表管理：
MinIO Multipart 分片直传 GeoTIFF，后台异步生成 COG 与缩略图，PostGIS 提供空间检索，
TiTiler 提供地图瓦片；支持按范围、生态细项和精度自动挑选数据的监测计划。

架构是模块化单体，分进程运行：FastAPI API、Outbox Dispatcher、独立 Scheduler、
Celery Geo Worker；PostgreSQL/PostGIS 是唯一关系与空间库（也是任务状态的权威来源），
MinIO 保存不可变二进制对象，RabbitMQ 只负责消息传递，TiTiler 由 Nginx 瓦片网关受控访问。
本项目由旧 Java/若依系统迁移而来（业务参考仓库另行保存），面向新前端提供 `/api/v1` 契约。

```
Web 前端
   │
   ▼
Nginx :8080（唯一对外入口）
   ├── FastAPI /api/v1
   └── 瓦片网关 ──签名校验──> TiTiler
          │
          ▼
PostgreSQL + PostGIS
   ├── Job + Outbox（同一事务）──> Dispatcher ──> RabbitMQ ──> Celery Geo Worker
   └── 独立 Scheduler（监测计划到期派发）
MinIO（原件 / COG / 缩略图 / 附件）
```

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

静态检查不需要容器；`pytest` 只收集高风险集成测试，未提供基础设施环境变量时全部跳过：

```bash
uv sync --all-groups            # 安装全部依赖组（api/worker/dev）
uv run ruff check .             # 静态检查
# 集成测试需 APP_INTEGRATION_DATABASE_URL（以及 MinIO/RabbitMQ 相关变量）
uv run pytest -m integration
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

**跑集成测试时必须独占 RabbitMQ**：测试自起的 Worker 会和 `dev.sh` 的 Worker 抢同一个
`geo` 队列导致假失败。先停掉本机 Worker，或让 `APP_INTEGRATION_RABBITMQ_URL` 指向独立实例。

依赖组边界：API 镜像只装基础依赖 + `api` 组（无 GDAL/科学栈）；Geo Worker 镜像装 `worker`
组（Celery + rasterio/shapely/pyproj/numpy，GDAL 由基础镜像提供）。
WSL 原生 Worker 依赖本机已 `uv sync --all-groups` 安装的 rasterio 等；不要把 GDAL 装进 API 进程。

## 接口

- **对接文档：[doc/接口说明.md](doc/接口说明.md)**（约定、上传到展示的核心流程、坑点）。
- **交互式 API 文档：`/api/v1/docs`**；机器可读契约：`/api/v1/openapi.json`。
- 对外入口：全栈部署 `http://localhost:8080`；本机开发 `http://127.0.0.1:8000`。

## 进程与端口

| 进程 | 说明 | 入口 |
| --- | --- | --- |
| api | FastAPI，Nginx 反代 `/api/`，启动时执行迁移 | `app.api.app:create_app --factory` |
| worker | Celery Geo Worker，队列 `geo`，并发 2 | `app.worker.celery_app:celery` |
| dispatcher | Outbox 投递循环 | `python -m app.dispatcher.main` |
| scheduler | 监测计划调度循环：advisory lock 互斥 + 到期扫描 + occurrence 幂等派发 + 停机补跑 | `python -m app.scheduler.main` |
| recovery | 回收租约过期的 RUNNING Job 并经 Outbox 重投 | `python -m app.recovery.main` |
| cleanup | 过期影像物理清理、blob 引用复核与 MinIO 退避删除 | `python -m app.cleanup.main` |
| nginx | 唯一对外入口 `:8080`；`/tiles/` fail-closed，令牌由 API 校验 | - |

基础设施：PostgreSQL/PostGIS 16-3.4（本机 `127.0.0.1:55432`，避开 Windows PostgreSQL 15 的 5432）、
MinIO（`127.0.0.1:9000`，控制台 `9001`）、RabbitMQ 3.13（`127.0.0.1:5672`，Management `15672`）、
TiTiler（`127.0.0.1:8081`）、Prometheus（`:9090`），Grafana 位于 `observability` profile（可选）。
Docker Hub 直连超时时，可 `sudo ./scripts/apply-docker-wsl-network.sh` 让 dockerd 走本机 `127.0.0.1:7897`
代理并启用镜像源。

## 目录结构

```
src/app/
├── core 层职责当前分布在顶层：settings.py（配置）、db.py（会话/基类）、
│   logging.py（结构化日志）、ids.py（trace/令牌用 UUID）、errors.py（RFC 9457 错误）、
│   pagination.py（分页基元）、checks.py（就绪检查）、context.py（ActorContext）
├── api/          # FastAPI 应用工厂、探针、指标、trace 中间件
├── data_sources/ # 产品型号字典（0001xx 卫星 / 0002xx 无人机）
├── imagery/      # 卫星/无人机分表、检索、软删除
├── uploads/      # MinIO Multipart 上传会话
├── ecology/      # 生态细项与数据源关系
├── monitoring/   # 监测计划、occurrence、执行与输入快照
├── auth/         # JWT 用户鉴权（业务默认拒绝匿名）
├── jobs/         # Job 状态机、事件、Outbox
├── processing/   # 栅格入库流水线与 Celery 任务
├── tiles/        # 短期瓦片令牌
├── worker/       # Celery Geo Worker
├── dispatcher/   # Outbox Dispatcher
├── scheduler/    # 独立 Scheduler
├── recovery/     # Job 租约过期恢复
└── cleanup/      # 影像对象异步清理
alembic/        # 0001–0014（0014：废弃资产，卫星/无人机分表）
docker/         # api/worker 镜像与 Nginx 配置
prometheus/     # 抓取配置
grafana/        # 自动配置的 Prometheus 数据源与运维面板
doc/            # 接口说明（前端对接）
tests/integration/  # 高风险边界集成测试（需显式基础设施）
tests/fixtures/     # 测试夹具（含真实 CRS/波段的栅格与矢量小样本）
```

## 约定摘要

- 核心主键整数自增；时间 UTC 持久化、API 响应携带时区。
- 列表响应统一 `{items, total, page, page_size}`；错误统一 RFC 9457 + 业务 `code` + `trace_id`。
- 日志为单行 JSON，`trace_id` 贯穿 API -> Job -> Worker。
- Worker 任务必须幂等（至少一次投递）；瞬时错误可重试，确定性错误不得盲目重试。
- 缺 CRS 等可补信息进入 `NEEDS_INPUT`，不得假定 EPSG:4326。
- 2–100 GB 大文件上传必须走 MinIO Multipart 直传；单机临时磁盘按最大文件 2–3 倍规划，
  Geo Worker 并发从 2 起，观察 CPU/内存/磁盘 IO 后再调。
