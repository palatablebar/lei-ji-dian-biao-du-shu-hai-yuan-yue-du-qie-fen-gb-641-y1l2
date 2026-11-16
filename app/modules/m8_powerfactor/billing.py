"""力率调整电费核心计算（纯函数，不碰 IO 与数据库）。

裁决口径（缺证月份如何形成可发布电费）：
1. 当月无功证据覆盖率 ≥ config.PF_MIN_COVERAGE：用观察窗口内的实测有功 P / 无功 Q
   算 cosφ，标记 K_PF_OBSERVED；
2. 覆盖率 > 0 但不足门槛：实测样本不足以代表整月，退回合同力率兜底，
   标记 K_PF_LOW_COVERAGE，并在结果里保留实测值与观察范围供复核；
3. 当月完全没有无功证据：按合同力率兜底，标记 K_PF_NO_EVIDENCE；
4. 凡证据不完整（2、3 或无功未到月底）一律 status=PROVISIONAL（暂估可发布、待追补），
   无功补齐且覆盖整月后 status=FINAL；
5. 调整电费 = 当月基础电费（不含力率调整）× 调整率%，金额量化到 0.01 元。

不用「统一合同力率」覆盖有实测数据的月份，避免考核电费偏离实际；
也不卡住出账——暂估版本先发布，补齐后再出追补，两版都留痕。
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

from ... import config
from . import table as table_mod

# 账单状态
FINAL = "FINAL"              # 证据齐备，定案
PROVISIONAL = "PROVISIONAL"  # 暂估，可发布，待无功补齐追补
SUPERSEDED = "SUPERSEDED"    # 被后续版本取代（保留，不再作为发布版）


def cos_phi(active_kwh: Decimal, reactive_kvarh: Decimal) -> Decimal:
    """cosφ = P / sqrt(P² + Q²)。P≤0 时无定义，返回 None 由调用方兜底。"""
    p = Decimal(active_kwh)
    q = Decimal(reactive_kvarh)
    if p <= 0:
        return None
    value = p / Decimal(math.sqrt(float(p * p + q * q)))
    return value.quantize(config.PF_QUANT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(config.MONEY_QUANT, rounding=ROUND_HALF_UP)


def classify(coverage: Decimal, has_evidence: bool, window_covers_full_month: bool,
             min_coverage: Decimal) -> tuple[str, list[str], bool]:
    """返回 (证据等级, 标记列表, 是否允许用实测力率)。"""
    if not has_evidence:
        return "NO_EVIDENCE", ["K_PF_NO_EVIDENCE", "K_PF_PROVISIONAL"], False
    if coverage < min_coverage:
        return "LOW_COVERAGE", ["K_PF_LOW_COVERAGE", "K_PF_PROVISIONAL"], False
    marks = ["K_PF_OBSERVED"]
    if not window_covers_full_month:
        marks.append("K_PF_PROVISIONAL")
    return "OBSERVED", marks, True


def build_bill(*, month: str, meter_id: str, base_amount: Decimal,
               active_kwh: Decimal, window_active_kwh: Decimal | None,
               observation: dict | None,
               standard: Decimal, contract_pf: Decimal,
               min_coverage: Decimal = config.PF_MIN_COVERAGE) -> dict:
    """形成单月单表的力率调整电费结果（暂估或定案）。

    active_kwh：整月有功（展示/对账用）；
    window_active_kwh：观察窗口内的有功，与窗口内无功同源，才算实测 cosφ。
    """
    has_evidence = bool(observation and observation["reactive_kvarh"] is not None
                        and observation["covered_days"] > 0)
    coverage = observation["coverage"] if observation else Decimal("0")
    full_month = bool(observation and observation["window_covers_full_month"])
    grade, marks, use_observed = classify(
        coverage, has_evidence, full_month, min_coverage)

    observed_pf = None
    if has_evidence and window_active_kwh is not None:
        observed_pf = cos_phi(window_active_kwh, observation["reactive_kvarh"])

    if use_observed and observed_pf is not None:
        applied_pf = observed_pf
        pf_basis = "observed"
    else:
        applied_pf = Decimal(contract_pf)
        pf_basis = "contract"

    rate = table_mod.adjustment_rate(applied_pf, standard)
    adjustment = _money(base_amount * rate / Decimal("100"))
    status = FINAL if (grade == "OBSERVED" and full_month) else PROVISIONAL

    return {
        "month": month,
        "meter_id": meter_id,
        "standard": f"{Decimal(standard):.2f}",
        "base_amount": _money(base_amount),
        "active_kwh": Decimal(active_kwh).quantize(config.USAGE_QUANT),
        "window_active_kwh": (None if window_active_kwh is None
                              else Decimal(window_active_kwh).quantize(config.USAGE_QUANT)),
        "reactive_kvarh": (observation["reactive_kvarh"].quantize(config.USAGE_QUANT)
                           if observation else None),
        "coverage": coverage,
        "covered_days": observation["covered_days"] if observation else Decimal("0"),
        "month_days": observation["month_days"] if observation else None,
        "window_start": observation["window_start"] if observation else None,
        "window_end": observation["window_end"] if observation else None,
        "evidence_grade": grade,
        "pf_basis": pf_basis,
        "observed_pf": (f"{observed_pf:.2f}" if observed_pf is not None else None),
        "applied_pf": f"{applied_pf:.2f}",
        "contract_pf": f"{Decimal(contract_pf):.2f}",
        "adjust_rate_pct": f"{rate:.2f}",
        "adjustment_yuan": adjustment,
        "total_yuan": _money(_money(base_amount) + adjustment),
        "status": status,
        "marks": marks,
        "source_intervals": observation["sources"] if observation else [],
        "true_up_of": None,
        "prior_adjustment_yuan": None,
        "true_up_delta_yuan": None,
    }


def build_true_up(*, prior: dict, final_bill: dict) -> dict:
    """无功补齐后的追补：在定案账单上挂与原暂估版本的差额，不改写旧版。"""
    delta = _money(final_bill["adjustment_yuan"] - Decimal(prior["adjustment_yuan"]))
    true_up = dict(final_bill)
    true_up["status"] = FINAL
    true_up["true_up_of"] = prior["bill_id"]
    true_up["prior_adjustment_yuan"] = _money(Decimal(prior["adjustment_yuan"]))
    true_up["true_up_delta_yuan"] = delta
    if "K_PF_TRUE_UP" not in true_up["marks"]:
        true_up["marks"] = list(true_up["marks"]) + ["K_PF_TRUE_UP"]
    return true_up
