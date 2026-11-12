"""分析数据源业务规则：状态流转、字段校验、连接池准入与数据集联动都收在这里。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.pool import pool, format_capacity_message
from app.store import store

MODULE = "datasource"
DATASET_MODULE = "dataset"
REQUIRED_FIELDS = ["连接编号", "连接名称", "数据库类型"]
OPTIONAL_FIELDS = [
    "连接地址",
    "认证凭据",
    "默认库名",
    "连接池大小",
    "连接池上限",
    "租用额度",
    "冷却窗口",
    "冷却秒数",
]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {
    "提交校验": "待校验",
    "启用配置": "已启用",
    "停用配置": "已停用",
    "修复异常": "待校验",
}
NEGATIVE_ACTIONS = ["停用配置"]

# 复检失败的稳定分类与说明；数据源和数据集两个入口共用。
RECHECK_FAILURES = {"复检失败", "校验失败", "recheck_failed", "recheck"}
TIMEOUT_FAILURES = {"超时", "timeout"}
INTERRUPTED_FAILURES = {"中断", "中断刷新", "interrupted"}
ABNORMAL_FAILURES = {"异常", "abnormal"}


def normalize_failure(raw: Any) -> str:
    text = str(raw or "").strip()
    if text in RECHECK_FAILURES:
        return "recheck_failed"
    if text in TIMEOUT_FAILURES:
        return "timeout"
    if text in INTERRUPTED_FAILURES:
        return "interrupted"
    if text in ABNORMAL_FAILURES:
        return "abnormal"
    return ""


FAILURE_MESSAGES = {
    "recheck_failed": "数据源复检失败：连接地址或认证凭据已失效，本次刷新未写入任何结果，可修复连接后重试",
    "timeout": "数据源响应超时：本次刷新已中止且未写入任何结果，可稍后重试或释放繁忙的查询租用后重试",
    "interrupted": "刷新过程中断：数据源连接被中断，本次刷新已回滚且未写入任何结果，可重试",
    "abnormal": "数据源状态异常：本次刷新已拦下且未写入任何结果，请先修复异常连接后重试",
    "check_recheck_failed": "数据源复检失败：连接地址或认证凭据无效，本次未启用也未占用连接池额度，可修复后重试",
    "check_timeout": "数据源复检超时：本次未启用也未占用连接池额度，可稍后重试",
    "check_interrupted": "数据源复检中断：本次未启用也未占用连接池额度，可重试",
    "check_abnormal": "数据源状态异常：本次未启用也未占用连接池额度，请先修复异常连接后重试",
    "abnormal_connection": "连接状态异常，启用已终止；本次未占用连接池额度，请先提交校验并修复异常连接后重试",
}


@dataclass
class ActionOutcome:
    """动作结果：entry 为 None 表示失败；失败时 message 说明原因并给出可操作建议。

    - reason: 稳定分类（capacity / cooldown / dependency / abnormal / invalid），
      供前端和自动化按类别处理；
    - retryable: 是否建议原样重试；
    - pool: 发生准入判断时附带的连接池快照。
    """

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


def _pool_limit() -> int:
    """连接池上限：工作区只有一份共享连接池，连接上的“连接池大小”只做展示。

    示例数据里该字段是占位文字，不能反过来把共享池缩小，否则连接池余量会
    与页面展示、其他连接的实际放行口径不一致。
    """
    return int(pool.snapshot()["limit"])


def _lease_slots(entry: dict[str, Any]) -> int:
    for field_name in ("租用额度", "占用额度", "租用数"):
        if entry.get(field_name) not in (None, ""):
            try:
                return max(1, int(str(entry.get(field_name)).strip()))
            except (TypeError, ValueError):
                return 1
    return 1


def entry_ref(entry: dict[str, Any] | None) -> int | None:
    if entry is None:
        return None
    return int(entry.get("id", 0))


def dependency_entry_id(row: dict[str, Any]) -> int | None:
    """数据集绑定连接的两种写法：数据源连接 id 或连接编号。"""
    for field_name in ("数据源连接", "连接id", "连接ID", "数据源"):
        value = row.get(field_name)
        if value in (None, ""):
            continue
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            continue
    for field_name in ("数据源连接编号", "连接编号"):
        value = row.get(field_name)
        if value in (None, ""):
            continue
        key = str(value).strip()
        for datasource in store.rows(MODULE):
            if str(datasource.get("连接编号")) == key:
                return entry_ref(datasource)
    return None


def bind_dataset(dataset_entry: dict[str, Any], datasource_entry: dict[str, Any]) -> None:
    """把数据集挂到具体连接上，连接池状态就能跨模块反映到数据集清单。"""
    dataset_entry["数据源连接"] = entry_ref(datasource_entry)
    dataset_entry["数据源连接编号"] = datasource_entry.get("连接编号")
    dataset_entry["数据源连接状态"] = datasource_entry.get("status")
    dataset_entry["数据源连接名称"] = datasource_entry.get("连接名称")


def dependent_datasets(entry_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(DATASET_MODULE)
        if dependency_entry_id(row) == int(entry_id)
    ]


class DatasourceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("连接编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 可选配置原样保留，启用准入才能按连接自身的上限/冷却窗口计算。
        for field_name in OPTIONAL_FIELDS:
            if values.get(field_name) not in (None, ""):
                entry[field_name] = values.get(field_name)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------ 状态流转
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return ActionOutcome(None, f"数据源连接 {entry_id} 不存在或已归档", "missing")
        if action not in ACTION_RULES:
            return ActionOutcome(
                None, f"动作「{action}」不属于分析数据源可执行范围", "invalid"
            )
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return ActionOutcome(
                None, f"目标状态「{target}」不在允许的状态序列里", "invalid"
            )

        if action == "启用配置":
            return self._enable_entry(entry)
        if action == "停用配置":
            return self._disable_entry(entry)
        if action == "提交校验":
            return self._recheck_entry(entry, values)
        if action == "修复异常":
            return self._clear_abnormal(entry)

        # 其余动作沿用原有流转，失败时说明原因且不留任何连接池占位。
        with pool.locked():
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return ActionOutcome(entry, f"数据源连接已{action}")

    def _recheck_entry(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> ActionOutcome:
        """提交校验：复检失败/超时/中断都要说明原因、保持可重试，且不留下半启用结果。"""
        failure = normalize_failure(
            values.get("模拟失败")
            or values.get("故障")
            or values.get("failure")
            or values.get("simulate")
            or entry.get("connection_error")
        )
        with pool.locked():
            previous_status = self._status_of(entry)
            if failure:
                # 只记录复检结论，不占用额度、不切换到已启用，状态保持原值。
                entry["status"] = "待校验"
                entry["pending"] = True
                entry["abnormal"] = False
                entry["connection_error"] = failure
                entry["last_recheck"] = _now_text()
                return ActionOutcome(
                    None,
                    FAILURE_MESSAGES.get(
                        f"check_{failure}", FAILURE_MESSAGES["check_interrupted"]
                    ),
                    failure,
                    retryable=True,
                    extra={"previous_status": previous_status},
                )
            entry["status"] = ACTION_RULES["提交校验"]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["connection_error"] = None
            entry["last_recheck"] = _now_text()
        return ActionOutcome(entry, "数据源连接已提交校验，复检通过，可申请启用")

    def _clear_abnormal(self, entry: dict[str, Any]) -> ActionOutcome:
        """修复异常连接：只清掉复检结论，不占用额度、不直接算启用成功。"""
        with pool.locked():
            entry["abnormal"] = False
            entry["connection_error"] = None
            entry["status"] = "待校验"
            entry["pending"] = True
            entry["last_recheck"] = _now_text()
        return ActionOutcome(entry, "连接异常已修复，请重新提交校验后再申请启用")

    @staticmethod
    def _status_of(entry: dict[str, Any]) -> str:
        return str(entry.get("status") or "")

    def _enable_entry(self, entry: dict[str, Any]) -> ActionOutcome:
        """启用准入：异常连接一律拒绝；冷却、重复与额度在同一把锁内原子判断。"""
        # 先做无副作用的检查，失败时连额度都不会被占用。
        with pool.locked():
            entry_id = entry_ref(entry) or 0
            limit = _pool_limit()
            slots = _lease_slots(entry)
            snapshot = pool.snapshot()
            failure = normalize_failure(
                entry.get("connection_error") or entry.get("refresh_error")
            )
            # 先按连接池余量判定：达到上限时优先给出“剩余额度不足”，
            # 让并发申请同一份最后额度的请求得到可操作的原因。
            if entry.get("status") != "已启用" and pool.reserved_slots(entry_id) == 0:
                if snapshot["available"] < slots:
                    return ActionOutcome(
                        None,
                        format_capacity_message(snapshot, slots),
                        "capacity",
                        retryable=True,
                        pool=snapshot,
                    )
            if entry.get("abnormal") or failure:
                return ActionOutcome(
                    None,
                    FAILURE_MESSAGES["abnormal_connection"],
                    "abnormal",
                    retryable=True,
                    pool=snapshot,
                )
            # 原子占位：并发申请同一份最后额度时只有一条能成功。
            decision = pool.try_reserve(entry_id, slots)
            if not decision.ok:
                return ActionOutcome(
                    None,
                    decision.message,
                    decision.reason,
                    retryable=decision.reason == "capacity",
                    pool=decision.snapshot,
                )

            previous_status = self._status_of(entry)
            previous = {
                "status": previous_status,
                "pending": entry.get("pending"),
                "abnormal": entry.get("abnormal"),
                "enabled_at": entry.get("enabled_at"),
            }
            try:
                entry["status"] = "已启用"
                entry["pending"] = False
                entry["abnormal"] = False
                entry["enabled_at"] = _now_text()
                entry["pool_reserved"] = slots
                entry["pool_limit"] = limit
                self.sync_dependents(entry)
            except Exception as exc:  # pragma: no cover - 防御性回滚
                entry.update(previous)
                pool.release_connection(entry_id)
                return ActionOutcome(
                    None,
                    f"启用过程中断（{exc}），已回滚连接状态与连接池额度，可重试",
                    "interrupted",
                    retryable=True,
                    pool=pool.snapshot(),
                )
            entry["pool_snapshot"] = pool.snapshot()
            return ActionOutcome(
                entry,
                f"数据源连接已启用，占用连接池租用 {slots} 份，剩余额度 "
                f"{entry['pool_snapshot']['available']} 份",
                "granted",
                pool=entry["pool_snapshot"],
            )

    def _disable_entry(self, entry: dict[str, Any]) -> ActionOutcome:
        with pool.locked():
            entry_id = entry_ref(entry) or 0
            released = pool.release_connection(entry_id)
            cooldown = pool.cooldown_seconds_for(entry)
            pool.set_cooldown(entry_id, cooldown)
            entry["status"] = "已停用"
            entry["pending"] = True
            entry["abnormal"] = False
            entry["pool_reserved"] = 0
            entry["disabled_at"] = _now_text()
            self.sync_dependents(entry)
            snapshot = pool.snapshot()
            entry["pool_snapshot"] = snapshot
            message = "数据源连接已停用"
            if released:
                message += f"，已归还连接池租用 {released} 份"
            if cooldown > 0:
                message += f"，{math.ceil(cooldown)} 秒冷却窗口内不可重新启用"
            return ActionOutcome(
                entry,
                message,
                "disabled",
                retryable=False,
                pool=snapshot,
            )

    # ------------------------------------------------------------ 跨模块同步
    def sync_dependents(self, entry: dict[str, Any]) -> None:
        """连接池状态变化同步到依赖数据集：清单状态、刷新放行边界一起更新。"""
        from app.services.dataset import DatasetService

        summary = DatasetService().datasource_summary(entry)
        for dataset in summary["items"]:
            dataset["刷新可用"] = bool(dataset.get("刷新可用"))

    def available_for_entry(self, entry: dict[str, Any]) -> int:
        return int(pool.snapshot()["available"])

    def pool_status(self) -> dict[str, Any]:
        """给数据源页面用的连接池概览：阈值和数据集清单计数统一从这里出。"""
        from app.services.dataset import DatasetService

        snapshot = pool.snapshot()
        datasets = DatasetService().list_entries(page=1, size=10000, snapshot=snapshot)[0]
        return {
            **snapshot,
            "datasource_total": len(store.rows(MODULE)),
            "enabled_datasources": sum(
                1 for row in store.rows(MODULE) if row.get("status") == "已启用"
            ),
            "dataset_total": len(datasets),
            "dataset_refreshable": sum(
                1 for row in datasets if row.get("刷新可用")
            ),
            "dataset_blocked": sum(
                1 for row in datasets
                if not row.get("刷新可用")
                and str(row.get("status")) in ("已启用", "待校验")
            ),
        }
