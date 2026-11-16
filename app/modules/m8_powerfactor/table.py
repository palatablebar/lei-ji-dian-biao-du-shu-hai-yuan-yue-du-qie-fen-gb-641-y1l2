"""国标《功率因数调整电费办法》调整率分档表（三档考核标准）。

只放规则，不读文件、不碰数据库，便于逐档单测与离线复算。

口径（与办法分档表一致，本文件即规则的唯一代码来源）：
- 入参 cosφ 先量化到 0.01（见 config.PF_QUANT），再按档查表；
- 高于考核标准时给减收（负百分比），低于时给加收（正百分比）；
- 三档分别对应考核标准 0.90 / 0.85 / 0.80；
- 调整率单位是「百分数」，例如 +0.5 表示月电费加收 0.5%，-0.30 表示减收 0.30%。

规则版本名见 config.PF_RULE_NAME；改表必须升版本并补测试，
否则同一份无功证据会算出两版电费。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, errors

# cosφ 分档（量化到 0.01 后的字符串）→ 调整率（%）。
# 0.90 考核档：高于 0.90 每升 0.01 减 0.15%，0.96 及以上封顶 -0.75%；
# 低于 0.90 每降 0.01 加 0.5%，降到 0.70 后每 0.01 加 1.0%。
TABLE_090: dict[str, Decimal] = {
    # 减收段
    "0.90": Decimal("0.00"), "0.91": Decimal("-0.15"), "0.92": Decimal("-0.30"),
    "0.93": Decimal("-0.45"), "0.94": Decimal("-0.60"),
    "0.95": Decimal("-0.75"), "0.96": Decimal("-0.75"),
    "0.97": Decimal("-0.75"), "0.98": Decimal("-0.75"),
    "0.99": Decimal("-0.75"), "1.00": Decimal("-0.75"),
    # 加收段（每 0.01 加 0.5%）
    "0.89": Decimal("0.50"), "0.88": Decimal("1.00"), "0.87": Decimal("1.50"),
    "0.86": Decimal("2.00"), "0.85": Decimal("2.50"), "0.84": Decimal("3.00"),
    "0.83": Decimal("3.50"), "0.82": Decimal("4.00"), "0.81": Decimal("4.50"),
    "0.80": Decimal("5.00"), "0.79": Decimal("5.50"), "0.78": Decimal("6.00"),
    "0.77": Decimal("6.50"), "0.76": Decimal("7.00"), "0.75": Decimal("7.50"),
    "0.74": Decimal("8.00"), "0.73": Decimal("8.50"), "0.72": Decimal("9.00"),
    "0.71": Decimal("9.50"), "0.70": Decimal("10.00"),
    # 深加收段（每 0.01 加 1.0%）
    "0.69": Decimal("11.00"), "0.68": Decimal("12.00"), "0.67": Decimal("13.00"),
    "0.66": Decimal("14.00"), "0.65": Decimal("15.00"), "0.64": Decimal("17.00"),
    "0.63": Decimal("19.00"), "0.62": Decimal("21.00"), "0.61": Decimal("23.00"),
    "0.60": Decimal("25.00"),
}

# 0.85 考核档：高于 0.85 每升 0.01 减 0.1%，0.94 及以上封顶 -1.10%；
# 低于 0.85 每降 0.01 加 0.5%，降到 0.65 后每 0.01 加 1.0%。
TABLE_085: dict[str, Decimal] = {
    "0.85": Decimal("0.00"), "0.86": Decimal("-0.10"), "0.87": Decimal("-0.20"),
    "0.88": Decimal("-0.30"), "0.89": Decimal("-0.40"), "0.90": Decimal("-0.50"),
    "0.91": Decimal("-0.60"), "0.92": Decimal("-0.70"), "0.93": Decimal("-0.80"),
    "0.94": Decimal("-1.10"), "0.95": Decimal("-1.10"), "0.96": Decimal("-1.10"),
    "0.97": Decimal("-1.10"), "0.98": Decimal("-1.10"), "0.99": Decimal("-1.10"),
    "1.00": Decimal("-1.10"),
    "0.84": Decimal("0.50"), "0.83": Decimal("1.00"), "0.82": Decimal("1.50"),
    "0.81": Decimal("2.00"), "0.80": Decimal("2.50"), "0.79": Decimal("3.00"),
    "0.78": Decimal("3.50"), "0.77": Decimal("4.00"), "0.76": Decimal("4.50"),
    "0.75": Decimal("5.00"), "0.74": Decimal("5.50"), "0.73": Decimal("6.00"),
    "0.72": Decimal("6.50"), "0.71": Decimal("7.00"), "0.70": Decimal("7.50"),
    "0.69": Decimal("8.00"), "0.68": Decimal("8.50"), "0.67": Decimal("9.00"),
    "0.66": Decimal("9.50"), "0.65": Decimal("10.00"),
    "0.64": Decimal("11.00"), "0.63": Decimal("12.00"), "0.62": Decimal("13.00"),
    "0.61": Decimal("14.00"), "0.60": Decimal("15.00"),
}

# 0.80 考核档：高于 0.80 每升 0.01 减 0.1%，0.90 及以上封顶 -1.30%；
# 低于 0.80 每降 0.01 加 0.5%，降到 0.60 后每 0.01 加 1.0%。
TABLE_080: dict[str, Decimal] = {
    "0.80": Decimal("0.00"), "0.81": Decimal("-0.10"), "0.82": Decimal("-0.20"),
    "0.83": Decimal("-0.30"), "0.84": Decimal("-0.40"), "0.85": Decimal("-0.50"),
    "0.86": Decimal("-0.60"), "0.87": Decimal("-0.70"), "0.88": Decimal("-0.80"),
    "0.89": Decimal("-0.90"), "0.90": Decimal("-1.00"),
    "0.91": Decimal("-1.15"), "0.92": Decimal("-1.30"), "0.93": Decimal("-1.30"),
    "0.94": Decimal("-1.30"), "0.95": Decimal("-1.30"), "0.96": Decimal("-1.30"),
    "0.97": Decimal("-1.30"), "0.98": Decimal("-1.30"), "0.99": Decimal("-1.30"),
    "1.00": Decimal("-1.30"),
    "0.79": Decimal("0.50"), "0.78": Decimal("1.00"), "0.77": Decimal("1.50"),
    "0.76": Decimal("2.00"), "0.75": Decimal("2.50"), "0.74": Decimal("3.00"),
    "0.73": Decimal("3.50"), "0.72": Decimal("4.00"), "0.71": Decimal("4.50"),
    "0.70": Decimal("5.00"), "0.69": Decimal("5.50"), "0.68": Decimal("6.00"),
    "0.67": Decimal("6.50"), "0.66": Decimal("7.00"), "0.65": Decimal("7.50"),
    "0.64": Decimal("8.00"), "0.63": Decimal("8.50"), "0.62": Decimal("9.00"),
    "0.61": Decimal("9.50"), "0.60": Decimal("10.00"),
    "0.59": Decimal("11.00"), "0.58": Decimal("12.00"), "0.57": Decimal("13.00"),
    "0.56": Decimal("14.00"), "0.55": Decimal("15.00"),
}

_TABLES = {
    Decimal("0.90"): TABLE_090,
    Decimal("0.85"): TABLE_085,
    Decimal("0.80"): TABLE_080,
}

# 各档表的最低可查 cosφ；再低按该档最低档调整率封顶（办法明确「低于表尾按表尾」）。
_FLOOR = {Decimal("0.90"): Decimal("0.60"), Decimal("0.85"): Decimal("0.60"),
          Decimal("0.80"): Decimal("0.55")}


def supported(standard: Decimal) -> bool:
    return Decimal(str(standard)) in _TABLES


def quantize_pf(value: Decimal) -> Decimal:
    """cosφ 量化到 0.01，半进位（与办法按两位小数分档一致）。"""
    return Decimal(value).quantize(config.PF_QUANT)


def adjustment_rate(cos_phi: Decimal, standard: Decimal) -> Decimal:
    """按考核标准返回调整率百分数。cosφ 超界按封顶档处理，不抛异常。"""
    standard = Decimal(str(standard))
    table = _TABLES.get(standard)
    if table is None:
        raise errors.BadRequest(
            f"不支持的考核力率档位：{standard}（仅支持 0.90 / 0.85 / 0.80）",
            code="E_PF_STANDARD_UNSUPPORTED")
    pf = quantize_pf(cos_phi)
    if pf >= Decimal("1.00"):
        pf = Decimal("1.00")
    floor = _FLOOR[standard]
    if pf <= floor:
        pf = floor
    return table[f"{pf:.2f}"]


def rate_table(standard: Decimal) -> dict[str, str]:
    """导出整档表（字符串键值），供证据页/导出与人工复核逐档对照。"""
    standard = Decimal(str(standard))
    table = _TABLES.get(standard)
    if table is None:
        raise errors.BadRequest(
            f"不支持的考核力率档位：{standard}", code="E_PF_STANDARD_UNSUPPORTED")
    return {key: f"{value:.2f}" for key, value in sorted(table.items(), reverse=True)}
