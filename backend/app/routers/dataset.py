"""分析数据集接口：维护数据集，覆盖提交校验、启用配置、停用配置、刷新等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dataset import DatasetOutcome, DatasetService
from app.services.pool import pool

router = APIRouter(prefix="/api/dataset", tags=["分析数据集"])

service = DatasetService()

LIST_FIELDS = ["数据集编号", "数据集名称", "来源表名", "字段列表", "行数上限", "刷新频率", "最近刷新", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def _action_result(outcome: DatasetOutcome) -> ActionResult:
    return ActionResult(
        ok=outcome.ok,
        message=outcome.message,
        entry=outcome.entry,
        reason=outcome.reason or None,
        retryable=outcome.retryable,
        pool=outcome.pool,
        extra=outcome.extra or None,
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按数据集编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按数据集编号与状态过滤分析数据集列表；没有数据时返回空页，不报错。

    每条数据集的“刷新可用/刷新边界”都按依赖连接的当前状态实时计算，
    连接停用后这里立即变为不可刷新。
    """
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(
        items=items,
        total=total,
        page=page,
        size=size,
        meta={"pool": pool.snapshot()},
    )


@router.get("/pool", response_model=dict)
def pool_status() -> dict[str, Any]:
    """数据集视角的连接池概览：剩余额度与依赖数据集的放行/拦截计数。

    空态只在真的没有任何数据集时返回，避免把“被连接拦下”误报成空清单。
    """
    snapshot = pool.snapshot()
    rows = service.list_entries(page=1, size=10000)[0]
    return {
        **snapshot,
        "dataset_total": len(rows),
        "dataset_refreshable": sum(1 for row in rows if row.get("刷新可用")),
        "dataset_blocked": sum(
            1 for row in rows
            if not row.get("刷新可用") and str(row.get("status")) != "已停用"
        ),
        "empty_message": "暂无依赖数据集" if not rows else None,
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析数据集清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dataset", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据集明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据集 {entry_id} 不存在或已归档")
    return {**entry, "pool_snapshot": pool.snapshot()}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条数据集，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据集已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据集执行提交校验、启用配置、停用配置、刷新；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    return _action_result(service.run_action(entry_id, action, payload.values))


@router.post("/{entry_id}/refresh", response_model=ActionResult)
def refresh_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """数据集刷新入口：连接停用必须被拦下，异常/超时/中断要说明原因并允许重试。"""
    values = payload.values if payload is not None else {}
    return _action_result(service.refresh_entry(entry_id, values))
