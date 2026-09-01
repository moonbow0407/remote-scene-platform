"""鉴权核心：用户、Argon2 密码、JWT、ActorContext 适配。

API 默认拒绝匿名；业务 Service 经 ActorContext 读取当前操作者。
"""
