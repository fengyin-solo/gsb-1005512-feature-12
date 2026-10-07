"""关口计量接口：维护关口表计，覆盖抄表导入、结算对账导出、中断重取与状态流转。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas import ActionResult, EntryPayload, MeterReadingImportPayload, MeterReadingImportResult, PageResult
from app.services.meter import SETTLEMENT_FIELDS, MeterService

router = APIRouter(prefix="/api/meter", tags=["关口计量"])

service = MeterService()

LIST_FIELDS = ["表计编号", "计量点名称", "表计精度", "正向有功电量", "反向有功电量", "上月示数", "本月示数", "通讯状态"]
STATUSES = ["通讯正常", "数据异常", "通讯中断", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表计编号检索"),
    status: str | None = Query(default=None, description="通讯正常、数据异常、通讯中断、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表计编号与状态过滤关口计量列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出关口计量清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "meter", "total": total, "items": items}


@router.get("/settlement/export")
def export_settlement(
    month: str | None = Query(default=None, description="结算月份 YYYY-MM，缺省按当前列表口径导出"),
) -> Response:
    """导出结算电量对账文件：与关口表计列表读同一份数据，不另出一套结果。"""
    rows = service.settlement_rows(month=month)
    if not rows:
        raise HTTPException(status_code=404, detail="该结算月没有可导出的结算电量，请先导入抄表文件")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SETTLEMENT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    filename = f"meter-settlement-{month or 'current'}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),  # 带 BOM，Excel 打开中文不乱码
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/import", response_model=MeterReadingImportResult)
def import_readings(payload: MeterReadingImportPayload) -> MeterReadingImportResult:
    """整批导入抄表文件：按表计编号匹配入库，匹配不上的行逐条打回并写明原因。"""
    return MeterReadingImportResult(**service.import_readings(payload.batch_id, payload.rows))


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条关口表计明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"关口表计 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条关口表计，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="关口表计已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条关口表计执行确认正常、标记异常、停用表计；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/fetch-reading", response_model=ActionResult)
def fetch_reading(entry_id: int) -> ActionResult:
    """通讯中断时重新采集示数：重取一次仍失败则按累计值顺延。"""
    entry, message = service.fetch_reading(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
