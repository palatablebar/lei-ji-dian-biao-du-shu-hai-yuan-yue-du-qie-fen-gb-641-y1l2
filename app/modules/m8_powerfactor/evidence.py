"""无功抄表包采信：字节 → 规范化无功读数行，原始字节整体留证。

与 M1 刻意分开：
- 无功寄存器不属于 M1–M7 的有功流水线，M1 仍把它当未知寄存器隔离；
- 这里只解析无功表（REACTIVE / kvarh），复用同一套文本归一与哨兵口径；
- 每行保留原始行号、原值、抄表包内部分段，坏行进 M8 隔离区而不是静默丢弃；
- 原始字节 sha256 即「抄表包指纹」，同一指纹重跑必须命中同一份结果。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from ... import config, errors, reasons
from ..m1_ingest import decode, reader, sniff
from ..m2_contract import coerce

REQUIRED = ("meter_id", "register", "read_at", "reading")


@dataclass
class ReactiveRow:
    lineno: int
    segment: int
    meter_id: str
    register: str
    read_at: str          # ISO UTC
    reading: Decimal      # 已归一到 kvarh 的累计无功示数
    reading_text: str
    unit_raw: str
    read_type: str
    raw: str


@dataclass
class ReactivePackage:
    filename: str
    raw_bytes: bytes
    sha256: str
    encoding: str
    rows: list[ReactiveRow] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)
    data_lines: int = 0
    blank_lines: int = 0
    segments: int = 1


def _q(lineno: int, segment: int, code: str, field_name: str, raw_value: str,
       meter_id: str, detail: str) -> dict:
    return {"lineno": lineno, "segment": segment, "code": code, "stage": "pf_adjust",
            "level": reasons.level(code), "field": field_name, "raw_value": raw_value,
            "meter_id": meter_id, "detail": detail}


def _parse_register(raw: str) -> str:
    text = coerce.normalize_text(raw)
    if not text:
        raise coerce.CoerceError("Q_PF_BAD_REGISTER", "无功寄存器为空", raw or "")
    alias = config.REACTIVE_REGISTER_ALIASES.get(text.lower())
    if alias is None:
        raise coerce.CoerceError(
            "Q_PF_BAD_REGISTER", f"无功寄存器无法识别：{raw!r}", raw or "")
    return alias


def _to_kvarh(value: Decimal, unit_raw: str) -> Decimal:
    """把读数折成 kvarh。单位缺失按 kvarh；未知单位直接抛错由调用方隔离。"""
    text = coerce.normalize_text(unit_raw).lower()
    if text in ("", "kwh", "度", "千瓦时"):
        return value
    factor = config.UNIT_TO_KVARH.get(text)
    if factor is None:
        raise coerce.CoerceError("Q_PF_BAD_UNIT" if "Q_PF_BAD_UNIT" in reasons.REASONS
                                 else "Q_PF_BAD_READING",
                                 f"无功单位无法识别：{unit_raw!r}", unit_raw)
    return value * factor


def parse_package(filename: str, raw_bytes: bytes) -> ReactivePackage:
    if not raw_bytes or not raw_bytes.strip():
        raise errors.UnprocessableInput("无功抄表包是 0 字节",
                                        code="E_PF_PACKAGE_EMPTY")
    try:
        decoded = decode.decode_bytes(raw_bytes)
    except decode.DecodeError as exc:
        raise errors.UnprocessableInput(str(exc), code="E_DECODE") from exc
    text = decoded["text"]
    records = reader.read_records(text)
    if not records:
        raise errors.UnprocessableInput("无功抄表包没有任何记录",
                                        code="E_PF_PACKAGE_EMPTY")
    header = records[0].fields
    header_map = sniff.build_header_map(header)
    missing = [name for name in REQUIRED if name not in header_map]
    if missing:
        raise errors.UnprocessableInput(
            "无功包表头缺少必需列：" + "、".join(missing), code="E_PF_HEADER_INVALID")

    package = ReactivePackage(
        filename=filename, raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        encoding=decoded["encoding"], segments=1)

    for record in records[1:]:
        fields_ = record.fields
        if sniff.is_repeated_header(fields_, header):
            package.segments += 1
            continue
        if sniff.is_blank_row(fields_):
            package.blank_lines += 1
            continue
        package.data_lines += 1
        if len(fields_) != len(header):
            package.quarantine.append(_q(
                record.lineno, package.segments, "Q_COLUMN_COUNT", "列数", record.raw,
                sniff.row_value(fields_, header_map, "meter_id"),
                f"期望 {len(header)} 列，实际 {len(fields_)} 列"))
            continue
        cells = {name: sniff.row_value(fields_, header_map, name)
                 for name in header_map}
        meter_id = coerce.normalize_text(cells.get("meter_id"))
        try:
            register = _parse_register(cells.get("register"))
            instant = coerce.parse_instant(cells.get("read_at"))
            reading = coerce.parse_decimal(cells.get("reading"))
        except coerce.CoerceError as exc:
            code = exc.code
            if code not in reasons.REASONS:
                code = "Q_PF_BAD_READING"
            package.quarantine.append(_q(
                record.lineno, package.segments, code, "reading",
                cells.get("reading") or cells.get("read_at") or "", meter_id,
                exc.detail))
            continue
        if reading is None:
            package.quarantine.append(_q(
                record.lineno, package.segments, "Q_MISSING_READING", "reading",
                cells.get("reading") or "", meter_id, "无功读数为空值哨兵"))
            continue
        try:
            reading_kvarh = _to_kvarh(reading, cells.get("unit") or "")
        except coerce.CoerceError as exc:
            package.quarantine.append(_q(
                record.lineno, package.segments, "Q_PF_BAD_READING", "unit",
                exc.raw, meter_id, exc.detail))
            continue
        read_type = "N"
        try:
            read_type = coerce.parse_read_type(cells.get("read_type"))
        except coerce.CoerceError:
            read_type = "N"
        package.rows.append(ReactiveRow(
            lineno=record.lineno, segment=package.segments, meter_id=meter_id,
            register=register, read_at=coerce.iso_utc(instant),
            reading=reading_kvarh, reading_text=coerce.normalize_text(cells.get("reading")),
            unit_raw=coerce.normalize_text(cells.get("unit")), read_type=read_type,
            raw=record.raw))
    if not package.rows and package.quarantine:
        raise errors.UnprocessableInput(
            "无功抄表包没有可用读数，全部进隔离区", code="E_PF_PACKAGE_EMPTY")
    return package


def validate_against_contracts(package: ReactivePackage, contracts: dict) -> tuple[
        list[ReactiveRow], list[dict]]:
    """按表计契约做未知表 / 倒走 / 同键冲突校验，返回（有效行，新增隔离行）。"""
    extra_q: list[dict] = []
    valid: list[ReactiveRow] = []
    seen: dict[tuple, ReactiveRow] = {}
    for row in package.rows:
        if row.meter_id not in contracts:
            extra_q.append(_q(row.lineno, row.segment, "Q_PF_UNKNOWN_METER", "meter_id",
                              row.meter_id, row.meter_id, "无功表号不在契约"))
            continue
        key = (row.meter_id, row.register, row.read_at)
        if key in seen:
            if seen[key].reading != row.reading:
                for dup in (seen[key], row):
                    extra_q.append(_q(
                        dup.lineno, dup.segment, "Q_PF_DUPLICATE_CONFLICT", "reading",
                        dup.reading_text, dup.meter_id,
                        f"同一 (表号,寄存器,时间) 无功读数不一致：{row.read_at}"))
                continue
            # 完全相同的重复读数：保留首条，不重复计费
            continue
        prior = [r for r in valid
                 if r.meter_id == row.meter_id and r.register == row.register
                 and r.read_at < row.read_at]
        if prior and row.reading < max(r.reading for r in prior):
            extra_q.append(_q(row.lineno, row.segment, "Q_PF_REVERSE_READING",
                              "reading", row.reading_text, row.meter_id,
                              "无功累计示数倒走"))
            continue
        seen[key] = row
        valid.append(row)
    valid.sort(key=lambda r: (r.meter_id, r.register, r.read_at))
    return valid, extra_q


__all__ = ["ReactiveRow", "ReactivePackage", "parse_package",
           "validate_against_contracts"]
