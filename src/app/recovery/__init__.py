"""独立 Job 恢复器进程入口。

职责（Job 基础设施，与监测计划等业务无关）：周期扫描租约过期的 RUNNING Job，
回收重投或置 FAILED，使 Worker 崩溃后任务不依赖"下一条 Broker 消息"即可恢复。
"""
