# AGENTS.md

本文件约束在本仓库中工作的所有自动化代理。目标是按已经确认的架构与阶段方案推进 Python 遥感后端实现，而不是重新设计系统，也不是翻译旧 Java 代码。

## 1. 项目定位与事实来源

本仓库是多源遥感数据共享平台的 **Python 实现仓库**（模块化单体）。旧 Java/若依工程只作业务行为参考，不再存放迁移规格。

开始工作前，按以下顺序获取上下文：

1. 阅读本文件，确认仓库边界、不变量和当前阶段。
2. 阅读 `doc/总体架构.md`，确认目标架构、业务边界和长期不变量。
3. 阅读 `doc/阶段迁移实施方案.md`，确认当前阶段、依赖、退出条件和测试接缝。
4. 涉及旧业务行为或缺陷时，阅读 `doc/旧接口迁移矩阵.md`，再沿 Java 参考仓库的 Controller、Service、Mapper、Mapper XML、Domain 和配置核对完整调用链。
5. 涉及验收场景、夹具规格时，阅读 `doc/验收基线与测试夹具.md`，并对照 `tests/fixtures/`。

若架构文档与阶段方案冲突，以总体架构为准，并明确指出冲突。不要私自修改顶层设计来适配局部实现。

Java 参考仓库（只读，除非任务明确要求改它）：

- Windows：`D:\multi-data-remote-scene-share-platfrom`
- WSL：`/mnt/d/multi-data-remote-scene-share-platfrom`

不要把 `D:\workspace\multi-data-remote-scene-share-platfrom` 当作本仓库的前身或规范来源。那是一份已放弃的早期原型，其中的若依 HTTP 信封、旧 URL 兼容层、同步/异步双轨等约定与当前架构冲突。

## 2. 仓库边界

- 本仓库实现新后端：FastAPI API、Outbox Dispatcher、独立 Scheduler、Celery Geo Worker；Nginx 是唯一对外入口。
- `ruoyi-multidata` 和 `ruoyi-geo` 是需要分析和迁移的主要业务来源，位于 Java 参考仓库，不在本仓库复制。
- `ruoyi-admin`、`ruoyi-system`、`ruoyi-framework`、`ruoyi-common` 仅在理解旧调用链时参考，不能默认视为新系统迁移范围。
- 若依用户、角色、菜单、部门、岗位、验证码、系统配置、字典、通知、监控、日志、Quartz、代码生成器和 Druid 不属于首版迁移范围。
- 旧 Java 接口是业务规则证据，不是新 API 兼容契约。不要机械翻译 Controller、URL、`AjaxResult`、`TableDataInfo` 或权限注解。
- 不要擅自修改、删除或“清理” Java 参考仓库中的旧代码。它在迁移关闭前仍是业务行为参考。
- 不要顺手修改目标前端或其他目录；需要联调时，任务必须明确包含对应路径。

### 2.1 当前代码布局

core 职责目前位于 `src/app/` 顶层文件，尚未单独成包。不要为了对齐文档目录做无行为的搬迁。

```
src/app/
├── settings.py / db.py / errors.py / ids.py / logging.py
│   pagination.py / checks.py / context.py    # core 层
├── api/            # FastAPI 工厂、探针、指标、trace 中间件
├── assets/         # 逻辑资产、不可变版本、栅格扩展、成果、检索
├── uploads/        # MinIO Multipart 上传会话
├── catalogs/       # 资源目录、卫星、传感器
├── ecology/        # 生态参数与资源映射
├── auth/           # JWT 用户鉴权接缝
├── jobs/           # Job 状态机、事件、Outbox
├── processing/     # 入库流水线与 Celery 任务
├── vector_features/# PostGIS 要素与空间检索
├── tiles/          # 短期瓦片令牌签发与 Nginx 校验
├── dispatcher/     # Outbox 投递循环
├── worker/         # Celery Geo Worker 入口
├── scheduler/      # 独立 Scheduler（Stage 5 实装评估循环）
├── recovery/       # Job 执行租约过期回收
└── cleanup/        # Stage 6 过期资产与 MinIO 对象异步清理
alembic/            # 0001–0010（0010 为 Stage 6 生命周期与运维索引）
docker/             # api/worker 镜像与 Nginx
tests/              # 进程内测试；integration 需显式基础设施
tests/fixtures/     # 验收夹具
doc/                # 总体架构、阶段方案、迁移矩阵、验收基线
```

已随 Stage 5 落地的模块：`monitoring`（监测计划、occurrence、执行与输入快照）；其派发链路
已闭环（`monitoring.dispatch.JobRunDispatcher` + `monitoring.execution` 快照审计任务）；剩余 A5.1–A5.5 人工验收。

每个已落地模块内部按 `router / schemas / service / models` 组织。本模块查询可以写在 Service；只有查询复杂到需要独立测试或复用时才抽取 Repository。模块之间通过公开 Service 协作，不跨模块直接访问 ORM 或内部实现。

### 2.2 进程、镜像与命令

| 进程 | 入口 | 说明 |
| --- | --- | --- |
| api | `app.api.app:create_app --factory` | 轻量镜像，不安装 GDAL/科学栈 |
| worker | `app.worker.celery_app:celery`，队列 `geo`，并发 2 | Geo 镜像，含 rasterio/shapely/pyproj/numpy |
| dispatcher | `python -m app.dispatcher.main` | 认领 Outbox，至少一次投递 |
| scheduler | `python -m app.scheduler.main` | Stage 5 前只等待 `monitoring_plan` |
| nginx | `:8080` | `/api/` 反代 API；`/tiles/` fail-closed 校验后到 TiTiler |

常用命令在仓库根目录执行：

```bash
docker compose up -d --build
./scripts/dev.sh                # 日常：只起基础设施，本机跑 API/Dispatcher/Worker
uv sync --all-groups
uv run pytest
uv run pytest -m integration    # 需显式提供真实基础设施
uv run ruff check .
uv run pyright
uv run alembic upgrade head
```

配置来自 `APP_` 前缀环境变量，见 `.env.example`（本机地址）。Compose 全栈在 `compose.yaml` 覆盖容器 DNS。配置错误必须 fail fast，给出可定位的中文诊断。

## 3. 核心工作原则

### 3.1 先理解再修改

- 修改代码或文档前，先阅读相关架构、阶段方案和完整业务调用链。
- 不根据文件名、注释、单个 Controller 或单条 SQL 猜测系统行为。
- 发现旧代码存在重复入口、拼写错误或异常实现时，记录真实行为与目标处理方式，不默认把缺陷复制到新系统。
- 跨模块行为必须说明数据从入口到持久化、对象存储、消息队列和 Worker 的完整路径。

### 3.2 以业务用例迁移，不以文件迁移

- 每个旧业务接口必须在迁移矩阵中标记为 `MIGRATED`、`MERGED`、`REPLACED` 或 `EXCLUDED_WITH_REASON`。
- “全量迁移”指有效业务用例全覆盖，不指 Java 类、表、URL 和返回结构一比一复制。
- 应主动合并旧系统中的重复能力：上传数据与卫星数据归入统一资产模型，`SendTask` 与矿山任务归入监测计划和执行流程。
- 不保留没有明确需求的旧兼容路径、逗号分隔 ID、自定义字符串分隔响应、本地文件路径或同步大文件处理方式。

### 3.3 保持模块与运行时边界

目标业务模块包括：

- `core`：配置、数据库、错误、日志、`ActorContext`（当前为顶层文件）。
- `assets`：逻辑资产、不可变版本、成果和检索。
- `uploads`：MinIO Multipart 上传会话。
- `catalogs`：资源目录、卫星和传感器（Stage 4）。
- `ecology`：生态参数及资源映射（Stage 4）。
- `jobs`：Job 状态、事件和 Transactional Outbox。
- `processing`：入库流水线与类型处理器。
- `monitoring`：监测计划、调度、执行和输入快照（Stage 5）。
- `vector_features`：PostGIS 矢量要素（Stage 3）。
- `tiles`：TiTiler 受控访问。

边界规则：

- Router 只负责 HTTP 适配，不直接写复杂 SQL、调用 Celery 或操作 MinIO。
- Service 负责用例、事务和状态转换。
- 持久化查询留在本模块；跨模块不得直接访问其他模块的 ORM 模型或内部实现。
- FastAPI API、Outbox Dispatcher、Scheduler、Celery Geo Worker 和 TiTiler 是独立进程，不因共享代码而混淆职责。
- API 镜像保持轻量，不安装 GDAL 和完整科学计算栈；地理处理依赖属于 Geo Worker 镜像。Dispatcher 可用纯 Python 的 Celery `send_task` 发布任务，不得因此把科学栈装进 API 镜像。

### 3.4 遵守核心数据不变量

- 核心主键使用 UUIDv7，数据库类型为 PostgreSQL `uuid`。
- 时间统一以 UTC 持久化，API 时间必须携带时区。
- PostgreSQL/PostGIS 是唯一关系与空间数据库，不延续 MySQL/PostgreSQL 双库设计。
- MinIO 保存不可变二进制对象；数据库保存对象键、内容哈希、业务状态和关系。
- 逻辑资产、资产版本、类型扩展和成果必须分离。版本和成果不可覆盖历史输入。
- Job、监测执行和计算结果必须引用具体 `asset_version`，不能只引用逻辑资产。
- 栅格、矢量、附件按物理类型扩展；来源和业务分类使用字段、目录和标签，不为每个来源或业务分类创建资产表。
- 高频检索和 STAC 核心元数据使用明确列；扩展属性使用 JSONB，并通过 JSON Schema 校验（Schema 校验随 Stage 3 落地）。
- 栅格 COG 保留原始 CRS；用于检索和 API 的 footprint 统一为 EPSG:4326。
- 空间请求只接受 EPSG:4326 GeoJSON `Polygon` 或 `MultiPolygon`。
- 矢量原文件保留在 MinIO，要素几何进入 PostGIS，动态属性进入 JSONB。
- 任务与资产关系使用关联表，禁止逗号分隔 ID、名称关联或无约束字符串外键。
- 相同二进制可被多个逻辑资产引用；物理清理前必须确认引用计数为零。

违反这些不变量的请求或数据应尽早失败，不能用默认值或静默修正掩盖问题。

### 3.5 保持 Job、Outbox 和调度一致性

- PostgreSQL 中的 Job 是任务状态权威来源；RabbitMQ 只负责消息传递。
- Job 和 Outbox 事件必须在同一数据库事务中创建。
- Dispatcher 与 Worker 按至少一次投递设计，所有任务步骤必须幂等。
- 瞬时基础设施错误可以按策略指数退避；数据损坏、非法参数和不满足不变量属于确定性错误，不应盲目重试。
- 缺少 CRS、波段映射等可人工补充的信息进入 `NEEDS_INPUT`，不得擅自假定 EPSG:4326 或伪造处理成功。
- 状态转换必须通过明确的领域/Application 服务执行，并记录 Job 事件。禁止任意直接修改状态字段。
- Scheduler 使用数据库锁防止重复调度，并为每个计划周期生成稳定唯一标识。
- 停机恢复只补跑最近一次，其他错过周期标记为 `MISSED`，避免任务风暴。
- 监测执行开始前必须冻结具体资产版本输入快照。

### 3.6 遵守 API 契约

- 新 API 统一使用 `/api/v1`。
- 成功响应直接返回资源，不使用若依式 `{code, msg, data}` 全局信封。
- 分页统一返回 `items`、`total`、`page` 和 `page_size`。
- 错误统一使用 RFC 9457 `application/problem+json`，并包含稳定业务错误码和 `trace_id`。
- 不把业务失败包装成 HTTP 200，不返回伪造成功或空对象掩盖失败。
- MinIO 和 TiTiler 不直接暴露给客户端；下载和瓦片通过短期签名 URL 或受控网关访问。
- 首版通过 Job 查询轮询进度，只保留未来 SSE 所需事件边界，不提前实现 SSE/WebSocket。
- 内部模型对齐 STAC 核心语义，但首版不实现完整 STAC API。

### 3.7 避免无意义兼容、兜底和过度设计

- 不为废弃的若依接口、表结构、返回结构或前端调用保留兼容层。
- 重构完成后删除被替代的重复实现、死代码、无用 import、无效配置和旧路径。
- 不保留注释掉的大段旧代码，不创建没有行为的空抽象、空目录和占位接口。
- 不使用宽泛 `except Exception` 静默恢复。只有在 API、Worker、Dispatcher、Scheduler 等明确系统边界允许统一捕获，并必须完成错误归一化、日志和状态落库。
- 不因为未来可能使用而提前引入微服务、Kubernetes、Kafka、Redis Broker、Airflow、Prefect、Temporal、Dask、Spark 或完整动态 RBAC。
- 架构文档已经明确的长期边界应一次设计正确；当前阶段只实现退出条件所需能力。
- 首版没有仓库中不存在的遥感算法，不创建假算法、空算法插件或伪造结果。

### 3.8 类型、注释和命名

- 公共接口、函数参数、返回值、领域模型、状态、事件和重要数据结构必须提供完整类型注解。
- 避免滥用 `Any`、无结构 `dict` 和字符串常量。边界确实动态时，应使用明确 Schema、Protocol、枚举或受校验 JSONB。
- 简单且能自然推断的局部变量不要求机械补类型。
- 代码注释、业务错误说明、迁移文档和 Prompt 使用中文。
- 核心模型字段应说明业务含义；关键状态转换、幂等键、事务边界、并发控制和空间参考决策必须说明“为什么”。
- 不逐行翻译代码，不为显而易见的语句添加注释。
- 领域命名优先使用总体架构中的术语：资产、资产版本、成果、上传会话、处理任务、监测计划、监测执行、输入快照。

## 4. 阶段执行规则

必须按 `doc/阶段迁移实施方案.md` 的 Stage 0–7 门禁推进。如任务要求跳过阶段门禁，必须先指出被跳过的依赖和风险，不能静默执行。

`README.md` 的「阶段进度」是仓库对外的当前状态。每个阶段的状态发生变化（开始、主体落地、验收关闭）时，必须在同一批改动中更新 README，至少包括：

- 阶段表中的状态列（只使用 `未开始` / `进行中` / `主体已落地` / `已完成`）
- 「当前工作」指向的阶段
- 「已交付能力」中的 API、模块、迁移；以及明确尚未交付的项
- 目录结构若随该阶段新增模块，一并更新

不得只改代码或 `doc/` 而让 README 停留在上一阶段。宣称某阶段 `已完成` 必须以该阶段退出条件和验收脚本的可观察结果为准。

当前进度以 README 阶段表为准；此处不重复维护第二份进度，避免漂移。

后续门禁不变：

- Stage 4 的目录和生态关系必须在 Stage 5 监测计划验收前完成。
- Stage 5 的计划调度依赖已就绪资产检索、空间查询和目录/生态筛选。
- Stage 6 的可靠性工作可以增量实施，但不能用来替代 Stage 2–5 的业务完成度。
- 只有 Stage 7 的迁移矩阵核对和完整验收通过后，首版迁移才算完成。

## 5. 测试与验证原则

### 5.1 测试优先级

1. 可重复的人工端到端场景是首版主要验收方式。
2. PostgreSQL/PostGIS、MinIO、RabbitMQ/Celery、Outbox、TiTiler 和 Scheduler 等高风险边界编写集成测试。
3. 状态机、重试分类、RRULE、增量时间窗、几何校验、渲染推断等复杂纯逻辑编写必要单元测试。
4. 不为简单 CRUD、Pydantic 字段映射、ORM getter、薄封装或框架默认行为机械补单元测试。
5. 不以覆盖率数字或测试数量代替真实风险验证。

### 5.2 最高测试接缝

- 栅格主链路从创建上传会话开始，到资产 `READY`、成果存在且瓦片可读取结束。
- 栅格纠错链路覆盖 `NEEDS_INPUT`、补充元数据和断点恢复。
- 矢量链路覆盖上传、导入、空间查询和原文件下载。
- 监测链路从到期计划开始，到增量选择和不可变输入快照结束。
- 恢复链路覆盖 Broker 不可用、Outbox 积压、恢复投递和一次有效执行。
- 删除链路覆盖软删除、七天恢复期、恢复和无引用对象清理。

测试应断言 API、数据库业务状态、对象和可观察结果，不断言内部方法调用次数等实现细节。

### 5.3 修改后的最低验证要求

- 文档修改至少执行 Markdown 结构检查和 `git diff --check`。
- Python 实现修改运行受影响的 `uv run pytest`；触及类型或导入时再跑 `uv run ruff check .` 与 `uv run pyright`。
- 集成测试使用 `@pytest.mark.integration`，必须显式提供真实基础设施，禁止用 Mock 替代 PostGIS/MinIO/RabbitMQ 后声称链路已通过。
- 数据库设计修改必须验证 Alembic 在空库升级，并按风险验证降级或前向修复策略。
- API 修改必须核对 OpenAPI、RFC 9457 错误和人工调用场景。
- Worker 修改必须验证成功、确定性失败、瞬时重试和重复投递。
- 空间处理修改必须使用包含真实 CRS 和几何的固定样本验证。
- 只有任务明确要求修改 Java 参考代码时，才运行受影响 Maven 模块的针对性测试。

## 6. 鉴权与运行环境约束

- 首版不实现登录、用户管理和动态 RBAC，只允许本地开发环境使用。
- PostgreSQL、RabbitMQ、MinIO 和 TiTiler 不得暴露为公网服务。Nginx 是唯一对外入口，仅绑定本机回环。
- 业务表保留 nullable 的 `owner_id` 和 `created_by`；Service 使用 `ActorContext`，首版返回匿名系统操作者。
- 不得因首版无鉴权而绕过受控下载和瓦片访问边界。
- 后续鉴权阶段采用 PostgreSQL 用户、Argon2、JWT Access Token、`ADMIN/USER`、管理员建号和“共享读、属主写”。首版不得提前实现超出接缝所需的认证逻辑。

## 7. 大文件与资源约束

- 目标文件规模为单文件 2–100 GB，上传必须使用 MinIO Multipart，文件字节不经过 FastAPI。
- GDAL/Rasterio 处理不得默认将完整栅格载入内存。
- 每个 Worker 任务使用独立临时目录，并在开始前检查可用空间。
- 单机临时空间应按最大源文件的 2–3 倍规划。
- Geo Worker 初始并发为 2；只有在观察 CPU、内存、临时磁盘和 MinIO/PostgreSQL I/O 后才能提高。
- 超时、重试和资源不足必须产生明确 Job 状态和诊断，不允许进程无状态崩溃后留下伪运行任务。

## 8. 代码库整洁与改动范围

- 只修改当前任务需要的模块、文档和配置。
- 可以修复实现当前任务过程中发现的直接相关问题，但不大规模重写无关模块。
- 工作区可能包含用户未提交的修改。修改前检查状态，保留并绕开无关变更。
- 不使用破坏性 Git 操作清除用户修改，不擅自恢复、删除或提交用户文件。
- 新能力完成后同步移除直接被替代的重复实现和无效配置，避免同一能力存在两套路径。
- 不提交生成物、临时大文件、上传样本、密钥、数据库数据目录、MinIO 数据目录，以及补丁残留的 `*.orig`。
- 配置错误、缺少依赖和非法状态应 fail fast，并提供可定位的中文诊断。

## 9. 工作完成标准

任务完成时必须说明：

- 修改覆盖了哪个迁移阶段和业务用例。
- 是否遵守总体架构；如有偏差，偏差及原因是什么。
- 数据、API、状态机或运行时边界发生了什么变化。
- 若阶段状态发生变化，README 阶段表与已交付能力是否已同步更新。
- 执行了哪些人工场景、集成测试、单元测试或静态检查。
- 仍有哪些未完成项、风险或受环境限制无法验证的部分。

不得仅以“代码已写完”“测试通过”作为完成说明。完成的判定依据是当前阶段的退出条件和可观察业务结果。
