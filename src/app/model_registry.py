"""全量模型注册表：一次导入即完成 Base.metadata 的完整表注册。

为什么需要：跨模块外键以表名字符串声明（job.asset_version_id → asset_version、
monitoring_run_input → data_asset/asset_version 等），SQLAlchemy 在首次 ORM
查询触发 mapper 配置时才解析目标 Table。API 与 Worker 进程的业务导入链天然
覆盖全部模型模块；而 Dispatcher、Scheduler、Recovery 等独立进程只导入自身
所需模块，若不先注册外键目标表，进程会在运行期 ConfigurationError 崩溃并
无限重试（Dispatcher 实跑 E2E 已暴露该缺陷）。

独立进程入口与 Alembic env.py 一律导入本模块，模型清单只在此维护一份。
"""

from app.assets import models as _assets_models  # noqa: F401
from app.auth import models as _auth_models  # noqa: F401
from app.catalogs import models as _catalogs_models  # noqa: F401
from app.ecology import models as _ecology_models  # noqa: F401
from app.jobs import models as _jobs_models  # noqa: F401
from app.monitoring import models as _monitoring_models  # noqa: F401
from app.uploads import models as _uploads_models  # noqa: F401
from app.vector_features import models as _vector_models  # noqa: F401
