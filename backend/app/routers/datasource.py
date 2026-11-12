"""分析数据源接口：维护数据源连接，覆盖提交校验、启用配置、停用配置等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dataset import DatasetService
from app.services.datasource import DatasourceService
from app.services.pool import pool

router = APIRouter(prefix="/api/datasource", tags=["分析数据源"])

service = DatasourceService()
dataset_service = DatasetService()

LIST_FIELDS = ["连接编号", "连接名称", "数据库类型", "连接地址", "认证凭据", "默认库名", "连接池大小", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def _action_result(outcome: Any) -> ActionResult:
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
    keyword: str | None = Query(default=None, description="按连接编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按连接编号与状态过滤分析数据源列表；没有数据时返回空页，不报错。

    响应 meta 里的连接池快照与启用准入使用同一份阈值，页面展示的剩余额度
    不会和实际放行条件打架。
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
    """连接池概览：阈值、当前租用、剩余额度、冷却窗口与数据集放行计数。"""
    return service.pool_status()


@router.post("/pool", response_model=ActionResult)
def configure_pool(payload: EntryPayload) -> ActionResult:
    """调整连接池上限/安全预留/冷却窗口/外部查询租用，用于本地演示与压测。"""
    ok, message = pool.configure(payload.values)
    return ActionResult(ok=ok, message=message, pool=pool.snapshot())


@router.post("/pool/reset", response_model=ActionResult)
def reset_pool() -> ActionResult:
    """恢复初始阈值与示例负载；用于本地演示结束后清理状态。"""
    pool.reset()
    return ActionResult(ok=True, message="连接池已恢复初始阈值", pool=pool.snapshot())


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析数据源清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datasource", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据源连接明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据源连接 {entry_id} 不存在或已归档")
    return {**entry, "pool_snapshot": pool.snapshot(), "依赖数据集": dataset_service.datasource_summary(entry)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条数据源连接，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据源连接已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据源连接执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    return _action_result(service.run_action(entry_id, action, payload.values))

