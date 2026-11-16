"""M8 力率调整电费编排：无功抄表包 → 观察窗口 → 暂估/定案力率电费。

关键不变量：
- 抄表包指纹（原始字节 sha256）相同 → package_id 相同 → 重跑直接回放，不新增电费行；
- 原始字节先整体落证（WORM 目录），再解析；解析失败也保留证据；
- 只从已落库的有功批次账单取基础电费与月有功电量，不重算 M1–M7；
- 暂估（PROVISIONAL）与定案（FINAL）分版本留存，追补只挂差额不改旧版。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ... import config, errors, manifest
from ..m2_contract import contract as contract_mod
from ..m6_batches import history
from . import billing, evidence as ev, intervals as iv
from . import table as table_mod


def _now_iso() -> str:
    return history.iso(history.now_utc())


def package_id_for(sha256: str, batch_id: str) -> str:
    """同抄表包 + 同有功批次 → 同一 package_id，重跑命中不新增。"""
    return "pf-" + manifest.digest_parts([batch_id, sha256])[:16]


def bill_id_for(package_id: str, month: str, meter_id: str) -> str:
    return package_id + ":" + month + ":" + meter_id


def _evidence_dir(settings) -> Path:
    path = Path(settings.data_dir) / config.PF_EVIDENCE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _store_raw_evidence(settings, package_id: str, filename: str,
                        raw_bytes: bytes) -> Path:
    """原始抄表包 WORM 落证：按 package_id 分目录，只写不覆盖。"""
    folder = _evidence_dir(settings) / package_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    if target.exists():
        # 已存在则校验内容一致；不一致说明同名校验失败，拒绝覆盖原始证据
        if target.read_bytes() != raw_bytes:
            raise errors.Conflict(
                f"原始证据 {filename} 已存在且内容不一致，拒绝覆盖",
                code="E_PF_EVIDENCE_CONFLICT")
        return target
    target.write_bytes(raw_bytes)
    return target


def _month_active(bills: list[dict], allocations: list[dict], contracts: dict) -> dict:
    """从已落库有功数据取 (月, 表) 的基础电费与月有功电量（主寄存器，避免峰平谷重复）。"""
    primary = {m: contracts[m].primary_register for m in contracts}
    active: dict[tuple, Decimal] = {}
    for a in allocations:
        if primary.get(a["meter_id"]) == a["register"]:
            active[(a["month"], a["meter_id"])] = active.get(
                (a["month"], a["meter_id"]), Decimal("0")) + a["usage_kwh"]
    base: dict[tuple, Decimal] = {}
    for b in bills:
        base[(b["month"], b["meter_id"])] = b["amount_yuan"]
    return {"active": active, "base": base}


def evaluate(settings, store, *, batch_id: str, filename: str, raw_bytes: bytes,
             contract_pf: Decimal = config.PF_CONTRACT_FALLBACK,
             standard: Decimal = Decimal("0.90"),
             min_coverage: Decimal = config.PF_MIN_COVERAGE,
             force: bool = False) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"有功批次 {batch_id} 不存在", code="E_BATCH_NOT_FOUND")
    standard = Decimal(str(standard))
    if not table_mod.supported(standard):
        raise errors.BadRequest(
            f"不支持的考核力率档位：{standard}", code="E_PF_STANDARD_UNSUPPORTED")

    # 先算指纹并查重：同一抄表包重放走幂等回放，绝不新增电费
    digest = manifest.digest_bytes(raw_bytes)
    package_id = package_id_for(digest, batch_id)
    if not force:
        existing = store.get_pf_package(package_id)
        if existing is not None:
            return _replay(store, package_id)

    # 原始证据先落盘（即便后续解析失败，证据也不丢）
    evidence_path = _store_raw_evidence(settings, package_id, filename, raw_bytes)

    package = ev.parse_package(filename, raw_bytes)
    contracts = contract_mod.load(settings)
    valid_rows, contract_q = ev.validate_against_contracts(package, contracts)
    quarantine = package.quarantine + contract_q
    reactive_intervals = iv.build_reactive_intervals(valid_rows)

    bills = store.load_bills(batch_id)
    allocations = store.load_allocations(batch_id)
    if not bills:
        raise errors.UnprocessableInput(
            "该有功批次没有月度账单，无法形成力率调整电费", code="E_PF_NO_BILLS")
    monthly = _month_active(bills, allocations, contracts)
    # 只取主寄存器有功区间（总表优先），用于在无功观察窗口内同源取 P
    primary_regs = {m: contracts[m].primary_register for m in contracts}
    active_intervals = [
        item for item in store.load_intervals(batch_id)
        if primary_regs.get(item["meter_id"]) == item["register"]
    ]

    # 每个 (月, 表) 形成一条力率调整电费
    pf_bills: list[dict] = []
    created_at = _now_iso()
    months = sorted(monthly["base"].keys())
    for month, meter_id in months:
        agg = iv.aggregate_month(reactive_intervals, month, meter_id)
        observation = _observation(agg)
        active = monthly["active"].get((month, meter_id), Decimal("0"))
        window_active = None
        if observation is not None:
            window_active = iv.active_within_window(
                active_intervals, meter_id,
                observation["window_start"], observation["window_end"])
        base_amount = monthly["base"][(month, meter_id)]
        bill = billing.build_bill(
            month=month, meter_id=meter_id, base_amount=base_amount,
            active_kwh=active, window_active_kwh=window_active,
            observation=observation, standard=standard,
            contract_pf=contract_pf, min_coverage=Decimal(str(min_coverage)))
        bill["bill_id"] = bill_id_for(package_id, month, meter_id)
        bill["package_id"] = package_id
        bill["batch_id"] = batch_id
        bill["created_at"] = created_at
        pf_bills.append(bill)

    store_record = {
        "package_id": package_id, "batch_id": batch_id, "filename": filename,
        "sha256": digest, "encoding": package.encoding,
        "data_lines": package.data_lines, "quarantine_count": len(quarantine),
        "created_at": created_at, "rule_version": settings.rule_version,
        "params": {"standard": f"{standard:.2f}", "contract_pf": f"{contract_pf:.2f}",
                   "min_coverage": f"{Decimal(str(min_coverage)):.4f}",
                   "pf_rule": config.PF_RULE_NAME},
    }
    reading_rows = [{
        "lineno": r.lineno, "segment": r.segment, "meter_id": r.meter_id,
        "register": r.register, "read_at": r.read_at,
        "reading_kvarh": r.reading, "reading_text": r.reading_text,
        "unit_raw": r.unit_raw, "read_type": r.read_type, "raw": r.raw,
    } for r in valid_rows]
    store.save_pf_package(package=store_record, readings=reading_rows,
                          intervals=reactive_intervals, quarantine=quarantine)
    store.save_pf_bills(package_id, pf_bills)

    return {
        "status": "done", "idempotent_replay": False, "package_id": package_id,
        "batch_id": batch_id, "sha256": digest, "filename": filename,
        "evidence_path": str(evidence_path),
        "encoding": package.encoding, "data_lines": package.data_lines,
        "valid_readings": len(valid_rows), "interval_count": len(reactive_intervals),
        "quarantine": quarantine, "bills": pf_bills,
        "stats": _stats(pf_bills),
    }


def _observation(agg: dict) -> dict | None:
    if agg["covered_days"] <= 0:
        return None
    # 观察窗口是否覆盖整个自然月（容差 1 秒，处理月末抄表恰好落在边界）
    full = agg["covered_days"] >= (agg["month_days"] - Decimal("0.00001157"))
    return {**agg, "window_covers_full_month": bool(full)}


def _stats(bills: list[dict]) -> dict:
    adj = sum((b["adjustment_yuan"] for b in bills), Decimal("0"))
    return {
        "bill_count": len(bills),
        "provisional": sum(1 for b in bills if b["status"] == billing.PROVISIONAL),
        "final": sum(1 for b in bills if b["status"] == billing.FINAL),
        "observed": sum(1 for b in bills if b["pf_basis"] == "observed"),
        "contract_fallback": sum(1 for b in bills if b["pf_basis"] == "contract"),
        "adjustment_total": adj.quantize(config.MONEY_QUANT),
    }


def _replay(store, package_id: str) -> dict:
    record = store.get_pf_package(package_id)
    bills = store.load_pf_bills(package_id)
    for b in bills:
        if "K_PF_REPLAY" not in b["marks"]:
            b["marks"] = list(b["marks"]) + ["K_PF_REPLAY"]
    return {
        "status": record and "done", "idempotent_replay": True,
        "replay_of": package_id, "package_id": package_id,
        "batch_id": record["batch_id"], "sha256": record["sha256"],
        "filename": record["filename"], "data_lines": record["data_lines"],
        "quarantine": store.load_pf_quarantine(package_id), "bills": bills,
        "stats": _stats(bills),
    }


def finalize(settings, store, *, provisional_package_id: str, final_filename: str,
             final_bytes: str | bytes, **kwargs) -> dict:
    """无功补齐后的定案：先按完整包独立算出 FINAL 账单，再对暂估月逐笔挂追补差额。"""
    prior = store.get_pf_package(provisional_package_id)
    if prior is None:
        raise errors.NotFound(f"暂估抄表包 {provisional_package_id} 不存在",
                              code="E_BATCH_NOT_FOUND")
    if isinstance(final_bytes, str):
        final_bytes = final_bytes.encode("utf-8")
    result = evaluate(settings, store, batch_id=prior["batch_id"],
                      filename=final_filename, raw_bytes=final_bytes, **kwargs)
    final_pid = result["package_id"]
    prior_bills = {(b["month"], b["meter_id"]): b
                   for b in store.load_pf_bills(provisional_package_id)}
    final_bills = store.load_pf_bills(final_pid)
    by_id = {b["bill_id"]: b for b in final_bills}
    trued = []
    for bill in final_bills:
        key = (bill["month"], bill["meter_id"])
        old = prior_bills.get(key)
        # 只追补「原来暂估、现在已能按实测定案」的月；仍缺证据的月保持暂估不动
        if old is None or old["status"] != billing.PROVISIONAL:
            continue
        if bill["status"] != billing.FINAL or bill["pf_basis"] != "observed":
            continue
        delta = billing._money(
            bill["adjustment_yuan"] - Decimal(old["adjustment_yuan"]))
        if delta == 0:
            continue  # 实测后调整额没变，不产生追补，只留定案版本
        trued.append(billing.build_true_up(prior=old, final_bill=bill))
    if trued:
        for t in trued:
            by_id[t["bill_id"]] = t
        store.save_pf_bills(final_pid, list(by_id.values()))
        result["bills"] = store.load_pf_bills(final_pid)
        result["stats"] = _stats(result["bills"])
    result["true_ups"] = trued
    result["provisional_package_id"] = provisional_package_id
    return result
