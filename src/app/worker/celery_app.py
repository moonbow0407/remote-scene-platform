"""Celery Geo Worker 进程定义。

运行时约定：
- 队列固定为 `geo`；Geo Worker 初始并发为 2，提升前必须观察 CPU/内存/临时盘与 IO；
- 任务状态权威在 PostgreSQL，不使用 Celery result backend；
- acks_late + reject_on_worker_lost 构成至少一次投递，任务步骤必须幂等（Stage 2 起生效）；
- confirm_publish 确保发布到 Broker 的消息收到代理确认。
"""

from celery import Celery

from app.settings import get_settings


def make_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "geo_worker",
        broker=settings.rabbitmq_url,
        backend=None,
        include=["app.processing.tasks"],
    )
    app.conf.update(
        task_default_queue="geo",
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        broker_transport_options={"confirm_publish": True},
    )
    return app


celery = make_celery_app()
