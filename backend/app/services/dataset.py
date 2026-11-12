"""分析数据集业务规则：状态流转、数据源依赖门禁与刷新入口都收在这里。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.datasource import (
    FAILURE_MESSAGES,
    MODULE as DATASOURCE_MODULE,
    bind_dataset,
    dependency_entry_id,
    entry_ref,
    normalize_failure,
)
from app.services.pool import pool, format_capacity_message
from app.store import store

MODULE = "dataset"
REQUIRED_FIELDS = ["数据集编号", "数据集名称", "来源表名"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]
REFRESH_ACTIONS = {"刷新数据集", "刷新", "立即刷新", "refresh"}


@dataclass
class DatasetOutcome:
    entry: dict[str, Any] | None
    message: str
    reason: str = ""
    retryable: bool = False
    pool: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.entry is not None


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _datasource_of(dataset: dict[str, Any]) -> dict[str, Any] | None:
    datasource_id = dependency_entry_id(dataset)
    if datasource_id is None:
        return None
    return store.find(DATASOURCE_MODULE, datasource_id)


def _refresh_slots(dataset: dict[str, Any]) -> int:
    for field_name in ("刷新租用额度", "租用额度"):
        if dataset.get(field_name) not in (None, ""):
            try:
                return max(1, int(str(dataset.get(field_name)).strip()))
            except (TypeError, ValueError):
                return 1
    return 1


class DatasetService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("数据集编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 列表读取时按连接当前状态与连接池余量重新计算刷新边界，
        # 跨模块状态变化立即反映到清单。
        pool_snapshot = snapshot or pool.snapshot()
        for row in rows:
            self._decorate(row, pool_snapshot)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._decorate(entry)
        return entry

    def _decorate(
        self,
        dataset: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
        *,
        retry_ready: bool = False,
    ) -> None:
        datasource = _datasource_of(dataset)
        if datasource is None:
            dataset["数据源连接状态"] = "未绑定"
            dataset["刷新可用"] = False
            dataset.setdefault("刷新边界", "数据集未绑定数据源连接")
            return
        bind_dataset(dataset, datasource)
        enabled = datasource.get("status") == "已启用"
        pool_snapshot = snapshot or pool.snapshot()
        slots = _refresh_slots(dataset)
        if not enabled:
            dataset["刷新可用"] = False
            dataset["刷新边界"] = f"数据源连接{datasource.get('status')}，刷新已停用"
            return
        if dataset.get("刷新状态") == "刷新失败" and not retry_ready:
            dataset["刷新可用"] = False
            dataset["刷新边界"] = "最近一次刷新失败，可通过「刷新数据集」重试"
            return
        if pool_snapshot["available"] < slots:
            dataset["刷新可用"] = False
            dataset["刷新边界"] = format_capacity_message(pool_snapshot, slots)
            return
        dataset["刷新可用"] = True
        dataset["刷新边界"] = "连接已启用，可按连接池余量刷新"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        datasource = self._resolve_datasource(values)
        if datasource is not None:
            bind_dataset(entry, datasource)
            self._decorate(entry)
        else:
            entry["数据源连接状态"] = "未绑定"
            entry["刷新可用"] = False
        rows.append(entry)
        return entry, []

    @staticmethod
    def _resolve_datasource(values: dict[str, Any]) -> dict[str, Any] | None:
        for key in ("数据源连接", "连接id", "连接ID", "数据源"):
            if values.get(key) not in (None, ""):
                try:
                    entry_id = int(str(values.get(key)).strip())
                except (TypeError, ValueError):
                    continue
                found = store.find(DATASOURCE_MODULE, entry_id)
                if found is not None:
                    return found
        for key in ("数据源连接编号", "连接编号"):
            value = values.get(key)
            if value in (None, ""):
                continue
            for row in store.rows(DATASOURCE_MODULE):
                if str(row.get("连接编号")) == str(value).strip():
                    return row
        return None

    # ------------------------------------------------------------ 动作入口
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> DatasetOutcome:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return DatasetOutcome(None, f"数据集 {entry_id} 不存在或已归档", "missing")
        if action in REFRESH_ACTIONS:
            return self.refresh_entry(entry_id, values)
        if action not in ACTION_RULES:
            return DatasetOutcome(
                None, f"动作「{action}」不属于分析数据集可执行范围", "invalid"
            )
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return DatasetOutcome(
                None, f"目标状态「{target}」不在允许的状态序列里", "invalid"
            )
        if action == "启用配置":
            return self._enable_entry(entry)
        if action == "停用配置":
            return self._disable_entry(entry)

        with pool.locked():
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            self._decorate(entry)
        return DatasetOutcome(entry, f"数据集已{action}")

    def _enable_entry(self, entry: dict[str, Any]) -> DatasetOutcome:
        datasource = _datasource_of(entry)
        if datasource is None:
            return DatasetOutcome(
                None,
                "数据集未绑定数据源连接，启用已拦下；请先绑定一条已启用的连接后重试",
                "unbound",
                retryable=True,
            )
        if datasource.get("status") != "已启用":
            return DatasetOutcome(
                None,
                f"数据源连接 {datasource.get('连接编号')} 当前为"
                f"{datasource.get('status')}，依赖它的数据集不能启用；请先启用该连接后重试",
                "datasource_disabled",
                retryable=True,
                extra={"datasource_id": entry_ref(datasource)},
            )
        with pool.locked():
            entry["status"] = "已启用"
            entry["pending"] = False
            entry["abnormal"] = False
            self._decorate(entry)
        return DatasetOutcome(entry, "数据集已启用")

    def _disable_entry(self, entry: dict[str, Any]) -> DatasetOutcome:
        with pool.locked():
            entry["status"] = "已停用"
            entry["pending"] = True
            entry["abnormal"] = False
            self._decorate(entry)
        return DatasetOutcome(entry, "数据集已停用")

    # ------------------------------------------------------------ 刷新入口
    def refresh_entry(self, entry_id: int, values: dict[str, Any] | None = None) -> DatasetOutcome:
        """刷新入口：连接停用即拒绝；连接可用时按连接池余量临时租用。"""
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return DatasetOutcome(None, f"数据集 {entry_id} 不存在或已归档", "missing")
        if str(entry.get("status")) == "已停用":
            return DatasetOutcome(
                None,
                "数据集自身已停用，刷新已拦下；请先启用数据集后重试",
                "dataset_disabled",
                retryable=True,
            )

        datasource = _datasource_of(entry)
        if datasource is None:
            return DatasetOutcome(
                None,
                "数据集未绑定数据源连接，刷新已拦下；请先绑定一条已启用的连接后重试",
                "unbound",
                retryable=True,
            )
        # 数据源仍然可用时，本次刷新尝试本身就作为重试入口，不受上次失败结论阻挡。
        self._decorate(entry, retry_ready=True)
        if datasource.get("status") != "已启用":
            return DatasetOutcome(
                None,
                f"数据源连接 {datasource.get('连接编号')} 当前为"
                f"{datasource.get('status')}，依赖它的数据集刷新已拦下；请先启用该连接后重试",
                "datasource_disabled",
                retryable=True,
                extra={"datasource_id": entry_ref(datasource)},
            )

        failure_reason = normalize_failure(
            values.get("模拟失败")
            or values.get("故障")
            or values.get("failure")
            or values.get("simulate")
        ) or normalize_failure(entry.get("connection_error"))
        with pool.locked():
            if failure_reason:
                # 复检失败/超时/中断都要明确说明原因，并保持可重试且不留半成功结果。
                entry["refresh_error"] = failure_reason
                entry["刷新状态"] = "刷新失败"
                entry["刷新可用"] = False
                entry["刷新边界"] = "最近一次刷新失败，可通过「刷新数据集」重试"
                return DatasetOutcome(
                    None,
                    FAILURE_MESSAGES.get(failure_reason, FAILURE_MESSAGES["interrupted"]),
                    failure_reason,
                    retryable=True,
                    pool=pool.snapshot(),
                    extra={"refresh_error": failure_reason},
                )

            slots = _refresh_slots(entry)
            decision = pool.acquire_transient(slots)
            if not decision.ok:
                return DatasetOutcome(
                    None,
                    decision.message,
                    "capacity",
                    retryable=True,
                    pool=decision.snapshot,
                )
            try:
                previous = {
                    key: entry.get(key)
                    for key in ("最近刷新", "刷新状态", "refresh_error", "刷新可用")
                }
                entry["最近刷新"] = _now_text()
                entry["刷新状态"] = "刷新成功"
                entry["refresh_error"] = None
                entry["刷新可用"] = True
                entry["刷新边界"] = "最近一次刷新成功，可按连接池余量继续刷新"
            except Exception as exc:  # pragma: no cover - 防御性回滚
                entry.update(previous)
                pool.release_transient(slots)
                return DatasetOutcome(
                    None,
                    f"刷新过程中断（{exc}），已回滚且未写入任何结果，可重试",
                    "interrupted",
                    retryable=True,
                    pool=pool.snapshot(),
                )
            pool.release_transient(slots)
            snapshot = pool.snapshot()
        return DatasetOutcome(
            entry,
            f"数据集刷新完成，临时占用连接池租用 {slots} 份后已归还，"
            f"剩余额度 {snapshot['available']} 份",
            "refreshed",
            pool=snapshot,
        )

    # ------------------------------------------------------------ 联动查询
    def datasource_summary(self, datasource_entry: dict[str, Any]) -> dict[str, Any]:
        """给数据源详情用：依赖数据集清单 + 空态说明与刷新边界。"""
        items = [
            row for row in store.rows(MODULE)
            if dependency_entry_id(row) == entry_ref(datasource_entry)
        ]
        for row in items:
            self._decorate(row)
        enabled_connection = datasource_entry.get("status") == "已启用"
        return {
            "items": items,
            "total": len(items),
            "empty_message": "暂无依赖数据集",
            "refresh_available": enabled_connection,
            "refresh_hint": (
                "连接已启用，可按连接池余量刷新"
                if enabled_connection
                else f"数据源连接{datasource_entry.get('status')}，数据集刷新已停用"
            ),
        }
