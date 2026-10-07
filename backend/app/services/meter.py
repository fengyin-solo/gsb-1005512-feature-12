"""关口计量业务规则：表计档案、抄表示数导入、通讯补抄与结算对账口径。

数据模型（内存仓库，真实项目换数据库时保持同样的键约束）：

- ``meter``：表计档案（表计编号、计量点名称、表计精度、通讯状态……），只登记表计本身。
- ``meter_reading``：月度抄表示数，以 (表计编号, 月份) 为业务键：
    表计编号 / 月份(YYYY-MM) / 抄表日期 / 上月示数 / 本月示数 /
    正向有功电量 / 反向有功电量 / 数据来源 / 备注 / version
  同一计量点同一个月重复导入按最后一次整笔覆盖（upsert），不叠加。

列表、明细、导出、看板卡片都走 :meth:`MeterService.latest_views` 这一份视图，
任何对外读数不允许另出一套结果。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

from app.store import store

MODULE = "meter"
READING_MODULE = "meter_reading"

REQUIRED_FIELDS = ["表计编号", "计量点名称", "表计精度"]
STATUS_ORDER = ["通讯正常", "数据异常", "通讯中断", "已停用"]
ACTION_RULES = {"确认正常": "通讯正常", "标记异常": "数据异常", "停用表计": "已停用"}
NEGATIVE_ACTIONS = ["停用表计"]

# 导入文件里的列：表头按这些名字匹配，顺序无所谓，多出来的列忽略。
IMPORT_REQUIRED_COLUMNS = ["表计编号", "抄表日期", "本月示数"]
IMPORT_OPTIONAL_COLUMNS = ["月份", "上月示数", "正向有功电量", "反向有功电量", "备注"]
EXPORT_COLUMNS = [
    "表计编号", "计量点名称", "表计精度",
    "正向有功电量", "反向有功电量", "上月示数", "本月示数",
    "月份", "抄表日期", "通讯状态", "数据来源",
]

# 关口表精度等级 -> 电量/示数保留的小数位；老数据沿用导入时已落库的精度，不重新舍入。
PRECISION_PLACES = {"0.2S": 4, "0.5S": 3, "0.2": 4, "0.5": 3, "1.0": 2, "2.0": 1}

ReadingFetcher = Callable[[str], "Decimal | None"]


def _to_decimal(raw: Any) -> Decimal | None:
    """把文件里的数字解析成 Decimal；空串/None 视为缺数，无法解析抛 ValueError。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"「{raw}」不是合法数字") from exc


def _round(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _reading_decimal(row: dict[str, Any], key: str) -> Decimal | None:
    """读示数时兼容 Decimal（导入落库）与字符串（老数据/seed）两种形态。"""
    value = row.get(key)
    if isinstance(value, Decimal):
        return value
    return _to_decimal(value)


def _format_number(value: Decimal | None) -> str:
    """统一展示/导出口径：去掉尾随 0，None 留空（导出空单元格，页面显示 —）。"""
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _normalize_month(raw: Any) -> str | None:
    """接受 2026-09、2026/09、202609；非法返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    compact = text.replace("/", "-").replace(".", "-")
    parts = compact.split("-")
    if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) in (1, 2):
        compact = f"{parts[0]}-{int(parts[1]):02d}"
    elif len(compact) == 6 and compact.isdigit():
        compact = f"{compact[:4]}-{compact[4:]}"
    year_ok = len(compact) == 7 and compact[:4].isdigit() and compact[4] == "-" and compact[5:].isdigit()
    if not year_ok:
        return None
    month = int(compact[5:])
    return compact if 1 <= month <= 12 else None


def _normalize_date(raw: Any) -> str | None:
    """接受 2026-09-30、2026/9/30；非法返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


class MeterService:
    # ------------------------------------------------------------------ 档案
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        month: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = self.latest_views(month=month)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("表计编号", ""))]
        if status:
            rows = [row for row in rows if row.get("通讯状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        master = store.find(MODULE, entry_id)
        if master is None:
            return None
        views = {str(v["表计编号"]): v for v in self.latest_views(month=None)}
        view = views.get(str(master.get("表计编号")))
        readings = [
            dict(r)
            for r in store.rows(READING_MODULE)
            if str(r.get("表计编号")) == str(master.get("表计编号"))
        ]
        readings.sort(key=lambda r: str(r.get("月份")), reverse=True)
        detail = dict(view or master)
        detail["示数明细"] = readings
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        code = str(values.get("表计编号")).strip()
        if any(str(row.get("表计编号")) == code for row in rows):
            return None, [f"表计编号 {code} 已存在"]
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

    # ------------------------------------------------------------ 示数视图
    def _meter_index(self) -> dict[str, dict[str, Any]]:
        return {str(row.get("表计编号")): row for row in store.rows(MODULE)}

    def _reading_index(self, month: str | None) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in store.rows(READING_MODULE):
            if month is not None and str(row.get("月份")) != month:
                continue
            result[str(row.get("表计编号"))] = row
        return result

    def latest_views(self, month: str | None = None) -> list[dict[str, Any]]:
        """档案 + 指定月份示数的统一视图；列表/导出/看板都读这一份，保证口径一致。

        month 为 None 时取每块表最近一次导入的示数（重新进入页面看到的就是最后一次导入值）。
        老表计档案在该月没有示数时，示数列留空、沿用电量档位列展示兼容记录。
        """
        masters = store.rows(MODULE)
        if month is None:
            chosen: dict[str, dict[str, Any]] = {}
            for row in store.rows(READING_MODULE):
                code = str(row.get("表计编号"))
                if code not in chosen or str(row.get("月份")) > str(chosen[code].get("月份")):
                    chosen[code] = row
        else:
            chosen = self._reading_index(month)

        views: list[dict[str, Any]] = []
        for master in masters:
            code = str(master.get("表计编号"))
            reading = chosen.get(code)
            view = {
                "id": master.get("id"),
                "表计编号": code,
                "计量点名称": master.get("计量点名称"),
                "表计精度": master.get("表计精度"),
                "通讯状态": master.get("status"),
                "正向有功电量": "",
                "反向有功电量": "",
                "上月示数": "",
                "本月示数": "",
                "月份": "",
                "抄表日期": "",
                "数据来源": "",
            }
            if reading:
                view.update({
                    "正向有功电量": _format_number(_reading_decimal(reading, "正向有功电量")),
                    "反向有功电量": _format_number(_reading_decimal(reading, "反向有功电量")),
                    "上月示数": _format_number(_reading_decimal(reading, "上月示数")),
                    "本月示数": _format_number(_reading_decimal(reading, "本月示数")),
                    "月份": reading.get("月份"),
                    "抄表日期": reading.get("抄表日期"),
                    "数据来源": reading.get("数据来源"),
                })
            else:
                # 兼容既有表计记录：没有月度示数的老档案，档案里写了什么就展示什么
                for legacy in ("正向有功电量", "反向有功电量", "上月示数", "本月示数", "通讯状态"):
                    value = master.get(legacy)
                    if value not in (None, ""):
                        view[legacy] = str(value)
            views.append(view)
        return views

    def settle_stats(self, month: str | None = None) -> dict[str, Any]:
        """看板卡片与导出同源：直接对统一视图求和，不另算一遍。"""
        views = self.latest_views(month=month)

        def total(column: str) -> Decimal:
            amount = Decimal("0")
            for view in views:
                value = _to_decimal(view.get(column))
                if value is not None:
                    amount += value
            return amount

        abnormal = sum(1 for v in views if v.get("通讯状态") in ("数据异常", "通讯中断"))
        return {
            "月份": month or "最近一次导入",
            "正向有功电量合计": _format_number(total("正向有功电量")),
            "反向有功电量合计": _format_number(total("反向有功电量")),
            "异常表计数": abnormal,
        }

    # ------------------------------------------------------------ 导入
    def import_readings(
        self,
        content: str,
        *,
        default_month: str | None = None,
        fetcher: ReadingFetcher | None = None,
        source: str = "文件导入",
    ) -> dict[str, Any]:
        """一次性导入抄表文件。

        - 按表计编号匹配表计档案，匹配不上的整行打回并写明原因，其余照常入库；
        - 同一(表计编号, 月份)整笔覆盖，重复导入以最后一次为准，不叠加；
        - 表计通讯中断且文件未给示数时，先重新取一次，仍取不到按累计值顺延。
        """
        try:
            reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
        except Exception as exc:  # pragma: no cover - DictReader 基本不会炸
            return self._import_result([], [], total=0, fatal=f"文件无法按 CSV 解析：{exc}")

        headers = reader.fieldnames or []
        headers = [h.strip() for h in headers]
        missing_columns = [col for col in IMPORT_REQUIRED_COLUMNS if col not in headers]
        if missing_columns:
            return self._import_result(
                [], [], total=0,
                fatal=f"缺少必填列：{'、'.join(missing_columns)}（应为：{'、'.join(IMPORT_REQUIRED_COLUMNS)}）",
            )

        meter_index = self._meter_index()
        imported: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        line_no = 1  # 表头占第 1 行
        for raw in reader:
            line_no += 1
            record = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
            if not any(str(v or "").strip() for v in record.values()):
                continue  # 跳过空行
            reading, reason = self._build_reading(record, meter_index, default_month, fetcher, source)
            if reading is None:
                rejected.append({"行号": line_no, "表计编号": record.get("表计编号", ""), "原因": reason})
                continue
            overwritten = self._upsert_reading(reading)
            imported.append({"行号": line_no, "表计编号": reading["表计编号"], "月份": reading["月份"], "覆盖": overwritten})

        return self._import_result(imported, rejected, total=len(imported) + len(rejected))

    def _build_reading(
        self,
        record: dict[str, Any],
        meter_index: dict[str, dict[str, Any]],
        default_month: str | None,
        fetcher: ReadingFetcher | None,
        source: str,
    ) -> tuple[dict[str, Any] | None, str]:
        code = str(record.get("表计编号") or "").strip()
        if not code:
            return None, "表计编号为空"
        master = meter_index.get(code)
        if master is None:
            return None, f"表计编号 {code} 在关口表计列表中匹配不上"

        read_date = _normalize_date(record.get("抄表日期"))
        if read_date is None:
            return None, f"抄表日期「{record.get('抄表日期')}」格式不正确，应为 YYYY-MM-DD"
        month = _normalize_month(record.get("月份")) or read_date[:7]
        if default_month and _normalize_month(record.get("月份")) is None and read_date[:7] != default_month:
            return None, f"抄表日期 {read_date} 不属于本次导入月份 {default_month}"

        try:
            current = _to_decimal(record.get("本月示数"))
        except ValueError as exc:
            return None, f"本月示数{exc}"
        previous = None
        try:
            previous = _to_decimal(record.get("上月示数"))
        except ValueError as exc:
            return None, f"上月示数{exc}"
        energy_forward = energy_reverse = None
        for label, name in (("正向有功电量", "energy_forward"), ("反向有功电量", "energy_reverse")):
            try:
                value = _to_decimal(record.get(label))
            except ValueError as exc:
                return None, f"{label}{exc}"
            if name == "energy_forward":
                energy_forward = value
            else:
                energy_reverse = value

        note = str(record.get("备注") or "").strip()
        places = PRECISION_PLACES.get(str(master.get("表计精度") or "").strip(), 3)

        # 通讯中断取不到示数：重新取一次，仍取不到按累计值顺延。
        if current is None and master.get("status") == "通讯中断":
            fetched = self._fetch_with_retry(code, fetcher)
            if fetched is not None:
                current = fetched
                note = (note + "；" if note else "") + "通讯中断，示数为重新取数获得"
            else:
                carried = self._carry_forward(code, month, energy_forward)
                if carried is None:
                    return None, "通讯中断取不到示数，且无历史累计值可顺延"
                current, energy_forward, note = carried[0], carried[1], (note + "；" if note else "") + carried[2]
        if current is None:
            return None, "本月示数为空"

        # 上月示数缺省时按最近一次累计示数顺延（含跨月）。
        if previous is None:
            last = self._previous_reading(code, month)
            previous = _reading_decimal(last, "本月示数") if last else Decimal("0")

        for value, label in ((current, "本月示数"), (previous, "上月示数")):
            if value < 0:
                return None, f"{label}不允许为负数"
        if current < previous:
            return None, f"本月示数 {current} 小于上月示数 {previous}，疑似表计翻转或录入错误"

        if energy_forward is None:
            energy_forward = current - previous
        elif energy_forward < 0:
            return None, "正向有功电量不允许为负数"
        if energy_reverse is None:
            energy_reverse = Decimal("0")
        elif energy_reverse < 0:
            return None, "反向有功电量不允许为负数"

        return {
            "表计编号": code,
            "月份": month,
            "抄表日期": read_date,
            "上月示数": _round(previous, places),
            "本月示数": _round(current, places),
            "正向有功电量": _round(energy_forward, places),
            "反向有功电量": _round(energy_reverse, places),
            "数据来源": source,
            "备注": note,
        }, ""

    def _fetch_with_retry(self, code: str, fetcher: ReadingFetcher | None) -> Decimal | None:
        """通讯中断补抄：失败后重新取一次；没有接入真实采集通道时默认取不到。"""
        if fetcher is None:
            return None
        for _ in range(2):
            try:
                value = fetcher(code)
            except Exception:
                value = None
            if value is not None:
                return value
        return None

    def _previous_reading(self, code: str, month: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows(READING_MODULE)
            if str(row.get("表计编号")) == code and str(row.get("月份")) < month
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: str(r.get("月份")))

    def _carry_forward(
        self, code: str, month: str, supplied_energy: Decimal | None
    ) -> tuple[Decimal, Decimal, str] | None:
        """仍取不到示数时按累计值顺延：示数沿用上次累计，电量顺延一笔。"""
        last = self._previous_reading(code, month)
        if last is None:
            return None
        cumulative = _reading_decimal(last, "本月示数")
        if cumulative is None:
            return None
        if supplied_energy is not None:
            energy = supplied_energy
        else:
            energy = _reading_decimal(last, "正向有功电量") or Decimal("0")
        note = "通讯中断取不到示数，按累计值顺延"
        return cumulative, energy, note

    def _upsert_reading(self, reading: dict[str, Any]) -> bool:
        """按 (表计编号, 月份) 整笔覆盖；返回是否覆盖了旧值。"""
        rows = store.rows(READING_MODULE)
        for row in rows:
            if str(row.get("表计编号")) == reading["表计编号"] and str(row.get("月份")) == reading["月份"]:
                version = int(row.get("version", 1))
                row.clear()
                row.update(reading)
                row["version"] = version + 1
                return True
        reading = dict(reading)
        reading["version"] = 1
        rows.append(reading)
        return False

    @staticmethod
    def _import_result(
        imported: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        *,
        total: int,
        fatal: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": fatal is None,
            "total": total,
            "imported_count": len(imported),
            "rejected_count": len(rejected),
            "imported": imported,
            "rejected": rejected,
            "fatal": fatal,
        }

    # ------------------------------------------------------------ 通讯补抄
    def refetch_reading(self, entry_id: int, month: str | None = None, fetcher: ReadingFetcher | None = None) -> tuple[dict[str, Any] | None, str]:
        """通讯中断手动补抄：重新取一次，仍取不到按累计值顺延；整笔按月覆盖入库。"""
        master = store.find(MODULE, entry_id)
        if master is None:
            return None, f"关口表计 {entry_id} 不存在或已归档"
        code = str(master.get("表计编号"))
        if month is None:
            latest = self._previous_reading(code, "9999")
            month = latest["月份"] if latest else date.today().strftime("%Y-%m")
        else:
            month = _normalize_month(month) or month

        fetched = self._fetch_with_retry(code, fetcher)
        record: dict[str, Any]
        if fetched is not None:
            record = {
                "表计编号": code,
                "抄表日期": date.today().isoformat(),
                "月份": month,
                "本月示数": format(fetched, "f"),
                "备注": "重新取数成功",
            }
            message = "重新取数成功"
        else:
            carried = self._carry_forward(code, month, None)
            if carried is None:
                return None, "重新取数仍失败，且没有历史累计值可顺延"
            record = {
                "表计编号": code,
                "抄表日期": date.today().isoformat(),
                "月份": month,
                "本月示数": format(carried[0], "f"),
                "正向有功电量": format(carried[1], "f"),
                "备注": carried[2],
            }
            message = "取不到示数，已按累计值顺延"
        reading, reason = self._build_reading(record, self._meter_index(), month, fetcher, "通讯补抄")
        if reading is None:
            return None, reason
        self._upsert_reading(reading)
        return reading, message

    # ------------------------------------------------------------ 导出
    def export_rows(self, month: str | None = None) -> list[dict[str, Any]]:
        """结算对账数据：与列表/明细完全同一份 latest_views，打包不另出一套结果。"""
        return [
            {column: view.get(column, "") for column in EXPORT_COLUMNS}
            for view in self.latest_views(month=month)
        ]

    def export_csv(self, month: str | None = None) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in self.export_rows(month=month):
            writer.writerow(row)
        return buffer.getvalue()

    def import_template(self) -> str:
        columns = IMPORT_REQUIRED_COLUMNS + IMPORT_OPTIONAL_COLUMNS
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerow(["METE-0001", "2026-09-30", "10234.5600", "2026-09", "9876.5400", "358.0200", "0", ""])
        return buffer.getvalue()
