"""关口计量接口：表计档案维护、抄表文件导入、通讯补抄与结算对账导出。"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
import io

from app.schemas import ActionResult, EntryPayload, MeterImportPayload, MeterImportResult, PageResult
from app.services.meter import MeterService

router = APIRouter(prefix="/api/meter", tags=["关口计量"])

service = MeterService()

LIST_FIELDS = ["表计编号", "计量点名称", "表计精度", "正向有功电量", "反向有功电量", "上月示数", "本月示数", "通讯状态"]
STATUSES = ["通讯正常", "数据异常", "通讯中断", "已停用"]
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_IMPORT_RETRIES = 1  # 导入失败（文件整体解析失败/网络错误）允许重试一次


def _check_month(month: str | None) -> str | None:
    if month is not None and not MONTH_PATTERN.match(month):
        raise HTTPException(status_code=400, detail=f"月份「{month}」格式不正确，应为 YYYY-MM")
    return month


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表计编号检索"),
    status: str | None = Query(default=None, description="通讯正常、数据异常、通讯中断、已停用"),
    month: str | None = Query(default=None, description="抄表月份 YYYY-MM；不传取每块表最后一次导入值"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表计编号、状态与月份读取关口计量列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, month=_check_month(month), page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats")
def meter_stats(month: str | None = None) -> dict:
    """看板卡片：与导出/列表同一份口径直接求和。"""
    return service.settle_stats(_check_month(month))


@router.get("/import-template")
def import_template() -> StreamingResponse:
    """抄表文件导入模板（CSV，带 BOM，Excel 可直接打开）。"""
    content = service.import_template()
    return StreamingResponse(
        io.BytesIO("﻿".encode("utf-8") + content.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=meter_import_template.csv"},
    )


@router.post("/imports", response_model=MeterImportResult)
def import_readings(payload: MeterImportPayload, retry: int = Query(default=0, ge=0, le=MAX_IMPORT_RETRIES)) -> MeterImportResult:
    """一次性导入抄表文件。

    按表计编号匹配：匹配不上的行整笔打回并写明原因，其余照常入库；
    同一计量点同一个月重复导入以最后一次为准（整笔覆盖，不叠加）。
    文件整体解析失败时允许重试一次（``retry=1``），重试结果会带 ``retried=true``。
    """
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="抄表文件内容为空，请先选择文件")
    _check_month(payload.month)
    result = service.import_readings(payload.content, default_month=payload.month)
    result["message"] = (
        f"导入完成：成功 {result['imported_count']} 条，打回 {result['rejected_count']} 条"
        if result["ok"]
        else result.get("fatal")
    )
    if retry:
        result["retried"] = True
    return MeterImportResult(**result)


@router.get("/export")
def export_entries(
    month: str | None = Query(default=None, description="对账月份 YYYY-MM；不传取最后一次导入值"),
    format: str = Query(default="json", pattern="^(json|csv)$"),
):
    """导出结算电量对账文件：与关口表计列表、明细视图完全同一份口径。"""
    month = _check_month(month)
    if format == "csv":
        content = "﻿" + service.export_csv(month)
        suffix = month or "latest"
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=meter_settlement_{suffix}.csv"},
        )
    items = service.export_rows(month)
    return {
        "module": "meter",
        "month": month or "latest",
        "total": len(items),
        "stats": service.settle_stats(month),
        "items": items,
    }


@router.post("/{entry_id}/refetch", response_model=ActionResult)
def refetch_reading(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """通讯中断重新取数：重新取一次，仍取不到按累计值顺延入库（按月覆盖）。"""
    month = None
    if payload is not None:
        month = _check_month(str(payload.values.get("month") or "").strip() or None)
    entry, message = service.refetch_reading(entry_id, month=month)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条关口表计明细（含历次抄表示数）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"关口表计 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条关口表计，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或编号冲突：{'、'.join(missing)}")
    return ActionResult(ok=True, message="关口表计已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条关口表计执行确认正常、标记异常、停用表计；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
