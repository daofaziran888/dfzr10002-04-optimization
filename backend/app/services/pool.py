"""连接池额度：启用准入、并发租用与冷却窗口都收在这里。

模型说明
--------
工作区里只有一份共享连接池，分析数据源“启用配置”表示长期占用一份租用，
分析数据集“刷新”则临时借用一份租用，外部分析查询也会占用租用。

可放行的阈值：

* 上限 ``limit``：连接池总容量，默认 20；
* 安全预留 ``safety_reserve``：始终留给已有分析数据集查询的余量，默认 1，
  因此有效容量是 ``limit - safety_reserve``；
* 当前租用 ``leased``：已启用连接持有的租用 + 外部查询租用 + 临时刷新租用；
* 剩余额度 ``available = max(0, effective_limit - leased)``。

启用一条连接需要 ``租用额度``（默认 1）份剩余额度，并且该连接必须已越过
停用后的冷却窗口。检查与占位在同一把可重入锁内完成，因此多个线程同时申请
同一份最后额度时只有一条能占位成功。

所有对外数值同时提供中文别名，方便前端和接口调用方直接读取。
"""
from __future__ import annotations

import contextlib
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import settings


@dataclass(frozen=True)
class PoolDecision:
    """一次额度申请的结论。reason 是稳定分类，message 是可直接展示的原因。"""

    ok: bool
    reason: str
    message: str
    granted: int = 0
    snapshot: dict[str, Any] = field(default_factory=dict)


def _as_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return max(parsed, minimum)


def _as_non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return max(parsed, 0)


def format_capacity_message(snapshot: dict[str, Any], need: int) -> str:
    """生成可操作的额度不足说明，调用方不需要再拼阈值。"""
    return (
        "连接池剩余额度不足："
        f"上限 {snapshot['limit']}、安全预留 {snapshot['safety_reserve']}、"
        f"当前租用 {snapshot['leased']}、本次需要 {need}、"
        f"剩余可用 {snapshot['available']}。"
        "请释放空闲查询租用或停用其他连接后重试"
    )


class ConnectionPool:
    """共享连接池的额度账本；所有读写都在同一把可重入锁内完成。"""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._limit = _as_positive_int(settings.datasource_pool_default_limit, 20)
        self._safety_reserve = _as_non_negative_int(
            settings.datasource_pool_safety_reserve, 1
        )
        if self._safety_reserve >= self._limit:
            self._safety_reserve = max(0, self._limit - 1)
        self._cooldown_seconds = max(0.0, float(settings.datasource_pool_cooldown_seconds))
        self._base_query_leases = max(
            0, int(settings.datasource_pool_seed_query_leases)
        )
        self._query_leases = self._base_query_leases
        self._transient_leases = 0
        self._reservations: dict[int, int] = {}
        self._cooldowns: dict[int, float] = {}
        self._bootstrapped = False

    # ------------------------------------------------------------------ 锁
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """给业务层组合“校验 + 占位 + 改状态”使用，锁可重入。"""
        with self._lock:
            self._ensure_bootstrap()
            yield

    def _ensure_bootstrap(self) -> None:
        """首次使用时把示例数据里已启用的连接登记成长期租用。"""
        if self._bootstrapped:
            return
        self._bootstrapped = True
        try:
            from app.store import store
        except Exception:  # pragma: no cover - 只在极端导入顺序下发生
            return
        for row in store.rows("datasource"):
            if row.get("status") == "已启用":
                entry_id = int(row.get("id", 0))
                self._reservations[entry_id] = self._slots_for_row(row)

    @staticmethod
    def _slots_for_row(row: dict[str, Any]) -> int:
        for field_name in ("租用额度", "占用额度", "租用数"):
            if row.get(field_name) not in (None, ""):
                return _as_positive_int(row.get(field_name), 1)
        return 1

    # ------------------------------------------------------------ 额度快照
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_bootstrap()
            now = self._clock()
            reserved = sum(self._reservations.values())
            leased = reserved + self._query_leases + self._transient_leases
            effective = max(0, self._limit - self._safety_reserve)
            available = max(0, effective - leased)
            cooldowns = []
            for entry_id, until in sorted(self._cooldowns.items()):
                remaining = until - now
                if remaining > 0:
                    cooldowns.append({
                        "entry_id": entry_id,
                        "remaining_seconds": round(remaining, 1),
                        "cooldown_until": round(until, 3),
                    })
            return {
                "limit": self._limit,
                "safety_reserve": self._safety_reserve,
                "effective_limit": effective,
                "leased": leased,
                "reserved": reserved,
                "query_leases": self._query_leases,
                "transient_leases": self._transient_leases,
                "available": available,
                "cooldown_seconds": self._cooldown_seconds,
                "cooldowns": cooldowns,
                "reservation_details": [
                    {"entry_id": entry_id, "slots": slots}
                    for entry_id, slots in sorted(self._reservations.items())
                ],
                # 中文别名，前端直接展示；英文键留给接口调用方。
                "连接池上限": self._limit,
                "有效上限": effective,
                "安全预留": self._safety_reserve,
                "当前租用": leased,
                "剩余额度": available,
                "冷却秒数": self._cooldown_seconds,
            }

    def reserved_slots(self, entry_id: int) -> int:
        with self._lock:
            self._ensure_bootstrap()
            return self._reservations.get(int(entry_id), 0)

    def cooldown_remaining(self, entry_id: int) -> float:
        with self._lock:
            self._ensure_bootstrap()
            until = self._cooldowns.get(int(entry_id), 0.0)
            return max(0.0, until - self._clock())

    def cooldown_seconds_for(self, entry: dict[str, Any]) -> float:
        """停用某条连接时使用的冷却秒数：连接自身配置优先，否则用全局阈值。"""
        for field_name in ("冷却窗口", "冷却秒数"):
            value = entry.get(field_name)
            if value in (None, ""):
                continue
            try:
                return max(0.0, float(str(value).strip()))
            except (TypeError, ValueError):
                continue
        return self._cooldown_seconds

    def _cooldown_message(self, remaining: float) -> str:
        return (
            "连接处于停用冷却窗口，"
            f"剩余 {max(1, math.ceil(remaining))} 秒，暂不可启用；"
            "冷却窗口不能被绕过，结束后可重新申请"
        )

    # ------------------------------------------------------------ 启用占位
    def try_reserve(self, entry_id: int, slots: int = 1) -> PoolDecision:
        """原子地检查冷却、重复占位与剩余额度，并登记长期租用。"""
        entry_id = int(entry_id)
        slots = max(1, int(slots))
        with self._lock:
            self._ensure_bootstrap()
            snapshot = self.snapshot()
            if entry_id in self._reservations:
                return PoolDecision(
                    ok=False,
                    reason="duplicate",
                    message="该连接已启用，本次申请未重复占用连接池额度",
                    snapshot=snapshot,
                )
            remaining = max(0.0, self._cooldowns.get(entry_id, 0.0) - self._clock())
            if remaining > 0:
                return PoolDecision(
                    ok=False,
                    reason="cooldown",
                    message=self._cooldown_message(remaining),
                    snapshot=snapshot,
                )
            if entry_id in self._cooldowns:
                # 冷却窗口已结束：清理登记，之后恢复为“可申请启用”。
                self._cooldowns.pop(entry_id, None)
            if snapshot["available"] < slots:
                return PoolDecision(
                    ok=False,
                    reason="capacity",
                    message=format_capacity_message(snapshot, slots),
                    snapshot=snapshot,
                )
            self._reservations[entry_id] = slots
            return PoolDecision(
                ok=True,
                reason="granted",
                message=f"已占用连接池租用 {slots} 份",
                granted=slots,
                snapshot=self.snapshot(),
            )

    def release_connection(self, entry_id: int) -> int:
        """停用连接时归还长期租用；是否进入冷却由业务层另行登记。"""
        with self._lock:
            self._ensure_bootstrap()
            return int(self._reservations.pop(int(entry_id), 0))

    def set_cooldown(self, entry_id: int, seconds: float | None = None) -> float:
        """登记停用冷却窗口，返回本次窗口的总秒数。"""
        with self._lock:
            self._ensure_bootstrap()
            total = self._cooldown_seconds if seconds is None else max(0.0, float(seconds))
            if total > 0:
                self._cooldowns[int(entry_id)] = self._clock() + total
            else:
                self._cooldowns.pop(int(entry_id), None)
            return total

    # ------------------------------------------------------------ 临时租用
    def acquire_transient(self, count: int = 1) -> PoolDecision:
        """数据集刷新临时借用额度，成功时需要调用 release_transient 归还。"""
        count = max(1, int(count))
        with self._lock:
            self._ensure_bootstrap()
            snapshot = self.snapshot()
            if snapshot["available"] < count:
                return PoolDecision(
                    ok=False,
                    reason="capacity",
                    message=format_capacity_message(snapshot, count),
                    snapshot=snapshot,
                )
            self._transient_leases += count
            return PoolDecision(
                ok=True,
                reason="granted",
                message=f"已临时占用连接池租用 {count} 份",
                granted=count,
                snapshot=self.snapshot(),
            )

    def release_transient(self, count: int = 1) -> None:
        with self._lock:
            self._transient_leases = max(0, self._transient_leases - max(1, int(count)))

    def query_lease_status(self) -> dict[str, Any]:
        return self.snapshot()

    # ------------------------------------------------------------ 运维调整
    def configure(self, values: dict[str, Any]) -> tuple[bool, str]:
        """调整上限/预留/冷却和外部查询租用，便于本地演示与压测。"""
        with self._lock:
            self._ensure_bootstrap()
            limit = self._limit
            safety = self._safety_reserve
            cooldown = self._cooldown_seconds
            cooldown_changed = False
            query_leases = self._query_leases
            changed = False

            for key in ("limit", "连接池上限"):
                if values.get(key) not in (None, ""):
                    limit = _as_positive_int(values[key], limit)
                    changed = True
            for key in ("safety_reserve", "安全预留"):
                if values.get(key) not in (None, ""):
                    safety = _as_non_negative_int(values[key], safety)
                    changed = True
            for key in ("cooldown_seconds", "冷却秒数"):
                if values.get(key) not in (None, ""):
                    try:
                        cooldown = max(0.0, float(values[key]))
                    except (TypeError, ValueError):
                        return False, "冷却窗口必须是大于等于 0 的秒数"
                    changed = True
                    cooldown_changed = True
            for key in ("clear_cooldowns", "清除冷却"):
                if values.get(key) not in (None, "", False):
                    self._cooldowns.clear()
                    changed = True
                    cooldown_changed = True
            for key in ("query_leases", "外部查询租用"):
                if values.get(key) not in (None, ""):
                    query_leases = _as_non_negative_int(values[key], query_leases)
                    changed = True
            # 允许直接给定期望的“当前租用”总数，扣掉已登记的长期/临时租用。
            for key in ("leased", "current_leases", "当前租用", "set_leased"):
                if values.get(key) not in (None, ""):
                    desired = _as_non_negative_int(values[key], 0)
                    held = sum(self._reservations.values()) + self._transient_leases
                    query_leases = max(0, desired - held)
                    changed = True

            if not changed:
                return False, "没有需要更新的连接池参数"
            if safety >= limit:
                return False, "安全预留必须小于连接池上限"
            self._limit = limit
            self._safety_reserve = safety
            self._cooldown_seconds = cooldown
            if cooldown_changed and cooldown > 0:
                # 调整冷却阈值后，已有记录的剩余时间按新阈值重新起算；
                # 设为 0 时直接清除，窗口内连接才能恢复可启用。
                now = self._clock()
                self._cooldowns = {
                    entry_id: now + cooldown for entry_id in self._cooldowns
                }
            elif cooldown_changed:
                self._cooldowns.clear()
            self._base_query_leases = query_leases
            self._query_leases = query_leases
            return True, "连接池参数已更新"

    def reset(self) -> None:
        """恢复初始阈值与示例负载，并按当前已启用连接重建长期租用。"""
        with self._lock:
            self._limit = _as_positive_int(settings.datasource_pool_default_limit, 20)
            self._safety_reserve = _as_non_negative_int(
                settings.datasource_pool_safety_reserve, 1
            )
            if self._safety_reserve >= self._limit:
                self._safety_reserve = max(0, self._limit - 1)
            self._cooldown_seconds = max(
                0.0, float(settings.datasource_pool_cooldown_seconds)
            )
            self._base_query_leases = max(
                0, int(settings.datasource_pool_seed_query_leases)
            )
            self._query_leases = self._base_query_leases
            self._transient_leases = 0
            self._cooldowns.clear()
            self._reservations.clear()
            self._bootstrapped = False
            self._ensure_bootstrap()


pool = ConnectionPool()
