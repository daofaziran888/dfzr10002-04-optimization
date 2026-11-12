"""运行配置：端口、跨域、运行环境与连接池放行阈值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = "数据分析工作台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200

    # 连接池默认上限：数据源没有显式配置“连接池大小/连接池上限”时使用。
    datasource_pool_default_limit: int = field(
        default_factory=lambda: _env_int("DATASOURCE_POOL_DEFAULT_LIMIT", 20)
    )
    # 启用准入安全预留：启用一条连接会新增一份持续租用，不能把池子用到最后一格，
    # 否则现有分析数据集查询没有余量、容易被新任务挤到超时。
    datasource_pool_safety_reserve: int = field(
        default_factory=lambda: _env_int("DATASOURCE_POOL_SAFETY_RESERVE", 1)
    )
    # 停用后的冷却窗口，避免连接刚释放就被同一条配置立刻重新占用。
    # 单条连接可以用“冷却窗口/冷却秒数”字段覆盖这个默认值。
    datasource_pool_cooldown_seconds: float = field(
        default_factory=lambda: _env_float("DATASOURCE_POOL_COOLDOWN_SECONDS", 3.0)
    )
    # 启动时模拟的“外部分析查询正在租用”数量。默认 17 加上示例里已启用的
    # 1 条连接，正好复现“上限 20、当前租用 18、剩余 1”的准入场景。
    datasource_pool_seed_query_leases: int = field(
        default_factory=lambda: _env_int("DATASOURCE_POOL_SEED_QUERY_LEASES", 17)
    )


settings = Settings()
