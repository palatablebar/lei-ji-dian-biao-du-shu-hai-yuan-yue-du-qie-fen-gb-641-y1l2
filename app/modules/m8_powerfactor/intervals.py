"""无功区间还原与月度观察窗口。

口径与 M4 保持同构，便于复核人对照：
- 按 (表号, 寄存器, UTC 时间) 排序，相邻两条累计示数之差即该区间无功用量（kvarh）；
- 首条读数只作锚点，不产生区间（没有「上次」就没有区间用量）；
- 区间按覆盖天数切到自然月 [月初, 下月初)，左闭右开、不重不漏；
- 观察窗口 = 该月无功区间实际覆盖的时间范围，覆盖率按天算给复核看。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config
from ..m5_months import weights
from . import evidence as ev

DAYS_QUANT = Decimal("0.000000001")
KVARH_QUANT = Decimal("0.001")


def build_reactive_intervals(rows: list[ev.ReactiveRow]) -> list[dict]:
    """累计无功示数 → 区间无功用量（kvarh）。已按时间排序，倒走在采信层隔离。"""
    series: dict[tuple, list[ev.ReactiveRow]] = {}
    for row in rows:
        series.setdefault((row.meter_id, row.register), []).append(row)
    intervals: list[dict] = []
    for (meter_id, register), items in sorted(series.items()):
        items = sorted(items, key=lambda r: r.read_at)
        seq = 0
        last: ev.ReactiveRow | None = None
        for item in items:
            if last is None:
                last = item
                continue
            seq += 1
            intervals.append({
                "interval_id": f"{meter_id}:{register}:{seq:02d}",
                "meter_id": meter_id, "register": register,
                "start_at": last.read_at, "end_at": item.read_at,
                "prev_reading": last.reading, "cur_reading": item.reading,
                "delta": (item.reading - last.reading).quantize(KVARH_QUANT),
                "usage_kvarh": (item.reading - last.reading).quantize(KVARH_QUANT),
                "days": _days(last.read_at, item.read_at),
                "source_lineno": [last.lineno, item.lineno],
            })
            last = item
    intervals.sort(key=lambda x: (x["meter_id"], x["register"], x["start_at"]))
    return intervals


def _days(start_iso: str, end_iso: str) -> Decimal:
    start = dt.datetime.fromisoformat(start_iso)
    end = dt.datetime.fromisoformat(end_iso)
    return (Decimal((end - start).total_seconds()) / Decimal(86400)).quantize(DAYS_QUANT)


def month_span_days(month: str) -> Decimal:
    """该自然月的总天数（本地时区），覆盖率的分母。"""
    start = weights.month_start(month)
    next_start = weights.month_start(weights.shift_month(month, 1))
    return Decimal((next_start - start).total_seconds()) / Decimal(86400)


def aggregate_month(intervals: list[dict], month: str, meter_id: str) -> dict:
    """把落在某月的无功区间聚成观察窗口，返回覆盖天数与无功用量。"""
    m_start = weights.month_start(month).astimezone(config.UTC)
    m_end = weights.month_start(weights.shift_month(month, 1)).astimezone(config.UTC)
    covered_seconds = Decimal("0")
    reactive_kvarh = Decimal("0")
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    sources: list[str] = []
    for item in intervals:
        if item["meter_id"] != meter_id:
            continue
        start = dt.datetime.fromisoformat(item["start_at"])
        end = dt.datetime.fromisoformat(item["end_at"])
        overlap_start = max(start, m_start)
        overlap_end = min(end, m_end)
        if overlap_end <= overlap_start:
            continue
        covered_seconds += Decimal((overlap_end - overlap_start).total_seconds())
        # 区间无功用量按落在该月的天数比例切分（与 M5 同一套按日权重口径）
        exact, _quant = weights.allocate_interval(start, end, item["usage_kvarh"])
        for slice_month, share in exact:
            if slice_month == month:
                reactive_kvarh += share
        window_start = overlap_start if window_start is None else min(
            window_start, overlap_start)
        window_end = overlap_end if window_end is None else max(window_end, overlap_end)
        sources.append(item["interval_id"])
    covered_days = (covered_seconds / Decimal(86400)).quantize(DAYS_QUANT)
    return {
        "month": month, "meter_id": meter_id,
        "reactive_kvarh": reactive_kvarh.quantize(KVARH_QUANT),
        "covered_days": covered_days,
        "month_days": month_span_days(month).quantize(DAYS_QUANT),
        "coverage": (covered_days / month_span_days(month)).quantize(Decimal("0.0001")),
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "sources": sorted(set(sources)),
    }


def _overlap_usage(start: dt.datetime, end: dt.datetime,
                   win_start: dt.datetime, win_end: dt.datetime,
                   usage: Decimal) -> Decimal:
    """区间用量按与观察窗口的重叠天数比例切到窗口内（按日权重，与 M5 同口径）。"""
    lo = max(start, win_start)
    hi = min(end, win_end)
    if hi <= lo:
        return Decimal("0")
    total_days = Decimal((end - start).total_seconds()) / Decimal(86400)
    overlap_days = Decimal((hi - lo).total_seconds()) / Decimal(86400)
    if total_days <= 0:
        return Decimal("0")
    return usage * overlap_days / total_days


def active_within_window(active_intervals: list[dict], meter_id: str,
                         window_start: str, window_end: str) -> Decimal:
    """取观察窗口 [起, 止) 内的有功电量，保证 cosφ 的 P 与 Q 来自同一观察范围。

    只统计主寄存器（总表优先），避免峰平谷重复计；区间按重叠天数比例切。
    """
    win_start_dt = dt.datetime.fromisoformat(window_start)
    win_end_dt = dt.datetime.fromisoformat(window_end)
    total = Decimal("0")
    for item in active_intervals:
        if item["meter_id"] != meter_id:
            continue
        start = dt.datetime.fromisoformat(item["start_at"])
        end = dt.datetime.fromisoformat(item["end_at"])
        if end <= win_start_dt or start >= win_end_dt:
            continue
        total += _overlap_usage(start, end, win_start_dt, win_end_dt,
                                item["usage_kwh"])
    return total.quantize(KVARH_QUANT)
