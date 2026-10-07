"""关口计量业务规则：状态流转、字段校验与筛选口径都收在这里。

抄表导入、结算对账、通讯中断顺延的规则也收在本模块：
- 抄表文件按表计编号匹配，匹配不上的行单独打回并写明原因，其余照常入库；
- 同一计量点同一结算月份重复导入以最后一次为准，覆盖而不是两笔叠加；
- 整批导入失败允许凭批次号重试一次；
- 结算对账与关口表计列表读同一份数据，不另算一套结果；
- 通讯中断取不到示数时重取一次，仍取不到按累计值顺延；
- 老数据按抄表日期回填，结算精度沿用表计自身的表计精度。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store

MODULE = "meter"
READING_MODULE = "meter_reading"
BATCH_MODULE = "meter_import_batch"
REQUIRED_FIELDS = ["表计编号", "计量点名称", "表计精度"]
STATUS_ORDER = ["通讯正常", "数据异常", "通讯中断", "已停用"]
ACTION_RULES = {"确认正常": "通讯正常", "标记异常": "数据异常", "停用表计": "已停用"}
NEGATIVE_ACTIONS = ["停用表计"]

MAX_IMPORT_ATTEMPTS = 2  # 首次导入 + 失败后重试一次
SETTLEMENT_FIELDS = ["表计编号", "计量点名称", "表计精度", "结算月份", "上月示数", "本月示数", "正向有功电量", "反向有功电量", "结算电量", "数据来源"]


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _month_of(raw: Any) -> str | None:
    """抄表日期支持 YYYY-MM-DD 或 YYYY-MM，统一归到结算月份。"""
    text = str(raw or "").strip().replace("/", "-")
    parts = text.split("-")
    if len(parts) < 2:
        return None
    year, month = parts[0], parts[1]
    if not (year.isdigit() and month.isdigit() and 1 <= int(month) <= 12):
        return None
    return f"{int(year):04d}-{int(month):02d}"


def _precision_of(meter: dict[str, Any]) -> int:
    """结算精度沿用表计自身的表计精度；既有记录里精度不是数字时按两位小数。"""
    text = str(meter.get("表计精度") or "").strip()
    try:
        float(text)
    except ValueError:
        return 2
    return len(text.partition(".")[2]) if "." in text else 0


class MeterService:
    # ---- 列表、明细与状态流转（既有口径，导入与导出都复用这份数据） ----

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
            rows = [row for row in rows if keyword in str(row.get("表计编号", ""))]
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
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"关口表计 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于关口计量可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"关口表计已{action}"

    # ---- 抄表文件导入 ----

    def import_readings(self, batch_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """整批导入抄表文件：按表计编号匹配入库，匹配不上的行单独打回。"""
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return {"ok": False, "message": "缺少导入批次号，无法支持失败重试", "imported": 0, "rejected": [], "retryable": False}
        batch = self._find_batch(batch_id)
        if batch and batch.get("status") == "已导入":
            return {"ok": False, "message": f"批次 {batch_id} 已导入成功，请勿重复提交", "imported": 0, "rejected": [], "retryable": False}
        attempt = int(batch.get("attempts", 0)) + 1 if batch else 1
        if attempt > MAX_IMPORT_ATTEMPTS:
            return {"ok": False, "message": f"批次 {batch_id} 已重试过一次仍未成功，请修正抄表文件后换新批次导入", "imported": 0, "rejected": [], "retryable": False}
        if not rows:
            self._save_batch(batch_id, attempt, "失败")
            retryable = attempt < MAX_IMPORT_ATTEMPTS
            suffix = "，可重试一次" if retryable else "，重试次数已用完"
            return {"ok": False, "message": f"抄表文件为空或没有可解析的数据行，本次导入失败{suffix}", "imported": 0, "rejected": [], "retryable": retryable}

        meters = {str(meter.get("表计编号") or "").strip(): meter for meter in store.rows(MODULE)}
        current_month = date.today().strftime("%Y-%m")
        imported = 0
        rejected: list[dict[str, Any]] = []
        touched: set[str] = set()
        for index, raw in enumerate(rows, start=1):
            raw = raw or {}
            code = str(raw.get("表计编号") or "").strip()
            meter = meters.get(code)
            if not code or meter is None:
                rejected.append({"row": index, "表计编号": code or "空", "reason": f"表计编号 {code or '空'} 未在关口表计中登记"})
                continue
            month = _month_of(raw.get("抄表日期"))
            if month is None:
                rejected.append({"row": index, "表计编号": code, "reason": f"抄表日期「{raw.get('抄表日期') or '空'}」无法识别，应为 YYYY-MM-DD"})
                continue
            current = _to_float(raw.get("本月示数"))
            if current is None:
                rejected.append({"row": index, "表计编号": code, "reason": "本月示数缺失或不是数字"})
                continue
            previous = _to_float(raw.get("上月示数"))
            if previous is None:
                previous = self._last_cumulative(code, month)
            forward = _to_float(raw.get("正向有功电量"))
            if forward is None:
                if previous is None:
                    rejected.append({"row": index, "表计编号": code, "reason": "缺少上月示数且未给出正向有功电量，无法结算"})
                    continue
                forward = round(current - previous, 6)
            reverse = _to_float(raw.get("反向有功电量"))
            if reverse is None:
                reverse = 0.0
            self._upsert_reading({
                "表计编号": code,
                "结算月份": month,
                "抄表日期": str(raw.get("抄表日期") or "").strip(),
                "上月示数": previous,
                "本月示数": current,
                "正向有功电量": forward,
                "反向有功电量": reverse,
                "数据来源": "回填" if month < current_month else "导入",
                "导入批次": batch_id,
            })
            touched.add(code)
            imported += 1
        for code in touched:
            self._refresh_meter_display(meters[code])
        self._save_batch(batch_id, attempt, "已导入")
        message = f"导入完成：入库 {imported} 条"
        if rejected:
            message += f"，打回 {len(rejected)} 条（原因见打回明细）"
        return {"ok": True, "message": message, "imported": imported, "rejected": rejected, "retryable": False}

    # ---- 结算对账 ----

    def settlement_rows(self, month: str | None = None) -> list[dict[str, Any]]:
        """结算电量对账：与关口表计列表读同一份数据，不另算一套结果。"""
        meters, _ = self.list_entries(page=1, size=10000)
        items: list[dict[str, Any]] = []
        for meter in meters:
            code = str(meter.get("表计编号") or "")
            reading = self._latest_reading(code)
            if month:
                reading = next((row for row in store.rows(READING_MODULE) if row.get("表计编号") == code and row.get("结算月份") == month), None)
                if reading is None:
                    continue  # 该结算月没有抄表记录的计量点不进对账文件
            source = reading if month else meter
            precision = _precision_of(meter)
            previous = _to_float(source.get("上月示数"))
            current = _to_float(source.get("本月示数"))
            forward = _to_float(source.get("正向有功电量"))
            reverse = _to_float(source.get("反向有功电量"))
            items.append({
                "表计编号": code,
                "计量点名称": meter.get("计量点名称"),
                "表计精度": meter.get("表计精度"),
                "结算月份": month or (reading or {}).get("结算月份") or "",
                "上月示数": previous if previous is not None else source.get("上月示数"),
                "本月示数": current if current is not None else source.get("本月示数"),
                "正向有功电量": round(forward, precision) if forward is not None else source.get("正向有功电量"),
                "反向有功电量": round(reverse, precision) if reverse is not None else source.get("反向有功电量"),
                "结算电量": round(current - previous, precision) if current is not None and previous is not None else "",
                "数据来源": (reading or {}).get("数据来源") or "既有记录",
            })
        return items

    # ---- 通讯中断重取示数 ----

    def fetch_reading(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """远方采集示数：通讯中断时重新取一次，仍取不到就按累计值顺延。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"关口表计 {entry_id} 不存在或已归档"
        if entry.get("status") == "已停用":
            return None, f"关口表计 {entry_id} 已停用，不再采集示数"
        code = str(entry.get("表计编号") or "")
        value = self._read_device(entry)
        if value is not None:
            return entry, f"已采集到示数：{value}"
        value = self._read_device(entry)  # 取不到时重新取一次
        if value is not None:
            return entry, f"首次采集失败，重取成功：{value}"
        cumulative = self._last_cumulative(code)
        if cumulative is None:
            return None, "通讯中断，重取一次仍无示数，且没有可顺延的累计值，请先导入抄表文件"
        month = date.today().strftime("%Y-%m")
        previous = self._last_cumulative(code, month)
        self._upsert_reading({
            "表计编号": code,
            "结算月份": month,
            "抄表日期": date.today().isoformat(),
            "上月示数": previous if previous is not None else cumulative,
            "本月示数": cumulative,
            "正向有功电量": 0.0,
            "反向有功电量": 0.0,
            "数据来源": "顺延",
            "导入批次": "",
        })
        self._refresh_meter_display(entry)
        return entry, f"通讯中断取不到示数，重新取一次仍失败，已按累计值顺延：本月示数沿用 {cumulative}"

    # ---- 内部工具 ----

    @staticmethod
    def _read_device(entry: dict[str, Any]) -> float | None:
        """模拟一次远方采集：仅通讯正常时能取到示数，其余状态视为取不到。"""
        if entry.get("status") != "通讯正常":
            return None
        return _to_float(entry.get("本月示数"))

    @staticmethod
    def _find_batch(batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("batch_id") == batch_id:
                return row
        return None

    @staticmethod
    def _save_batch(batch_id: str, attempts: int, status: str) -> None:
        batch = MeterService._find_batch(batch_id)
        if batch is None:
            store.rows(BATCH_MODULE).append({"batch_id": batch_id, "attempts": attempts, "status": status})
        else:
            batch["attempts"] = attempts
            batch["status"] = status

    @staticmethod
    def _upsert_reading(values: dict[str, Any]) -> None:
        """同一计量点同一结算月份只留一条：重复导入以最后一次为准，覆盖而不是叠加。"""
        rows = store.rows(READING_MODULE)
        rows[:] = [row for row in rows if not (row.get("表计编号") == values["表计编号"] and row.get("结算月份") == values["结算月份"])]
        values["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append(values)

    def _latest_reading(self, code: str) -> dict[str, Any] | None:
        readings = [row for row in store.rows(READING_MODULE) if row.get("表计编号") == code]
        if not readings:
            return None
        return max(readings, key=lambda row: (str(row.get("结算月份") or ""), int(row.get("id", 0))))

    def _last_cumulative(self, code: str, before_month: str | None = None) -> float | None:
        """表计在指定月份之前的累计示数；没有抄表记录时兼容既有表计上的本月示数。"""
        readings = [
            row for row in store.rows(READING_MODULE)
            if row.get("表计编号") == code and (before_month is None or str(row.get("结算月份") or "") < before_month)
        ]
        if readings:
            latest = max(readings, key=lambda row: (str(row.get("结算月份") or ""), int(row.get("id", 0))))
            return _to_float(latest.get("本月示数"))
        meter = next((row for row in store.rows(MODULE) if str(row.get("表计编号") or "") == code), None)
        return _to_float(meter.get("本月示数")) if meter else None

    def _refresh_meter_display(self, meter: dict[str, Any]) -> None:
        """列表与明细看到的示数始终是最后一次导入的值。"""
        latest = self._latest_reading(str(meter.get("表计编号") or ""))
        if latest is None:
            return
        for field in ("上月示数", "本月示数", "正向有功电量", "反向有功电量"):
            meter[field] = latest.get(field)
