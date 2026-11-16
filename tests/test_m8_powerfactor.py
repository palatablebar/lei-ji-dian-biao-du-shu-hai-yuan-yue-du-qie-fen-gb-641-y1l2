"""M8 力率调整电费：缺证月份裁决、观察窗口、幂等、证据保全、原子发布、追补。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import config, errors
from app.modules.m6_batches import service as batch_service
from app.modules.m8_powerfactor import billing, intervals as iv
from app.modules.m8_powerfactor import release as release_mod
from app.modules.m8_powerfactor import service as pf_service
from app.modules.m8_powerfactor import table as table_mod

SAMPLE = config.REACTIVE_FILE
FINAL_SAMPLE = config.REACTIVE_FINAL_FILE


@pytest.fixture()
def batch_id(settings, store) -> str:
    return batch_service.run(settings, store, sample="readings_dirty.csv"
                             )["batch"]["batch_id"]


@pytest.fixture()
def reactive_bytes(sample_dir) -> bytes:
    return (sample_dir / SAMPLE).read_bytes()


@pytest.fixture()
def evaluated(settings, store, batch_id, reactive_bytes):
    return pf_service.evaluate(
        settings, store, batch_id=batch_id, filename=SAMPLE,
        raw_bytes=reactive_bytes)


# ---------------- 分档表纯函数 ----------------
@pytest.mark.parametrize("cos,rate", [
    ("0.90", "0.00"), ("0.91", "-0.15"), ("0.95", "-0.75"), ("1.00", "-0.75"),
    ("0.89", "0.50"), ("0.70", "10.00"), ("0.69", "11.00"), ("0.60", "25.00"),
])
def test_table_090_rates(cos, rate):
    assert table_mod.adjustment_rate(Decimal(cos), Decimal("0.90")) == Decimal(rate)


def test_table_below_floor_uses_last_tier():
    assert table_mod.adjustment_rate(Decimal("0.40"), Decimal("0.90")) == Decimal("25.00")


def test_table_085_and_080_standards():
    assert table_mod.adjustment_rate(Decimal("0.85"), Decimal("0.85")) == Decimal("0.00")
    assert table_mod.adjustment_rate(Decimal("0.94"), Decimal("0.85")) == Decimal("-1.10")
    assert table_mod.adjustment_rate(Decimal("0.80"), Decimal("0.80")) == Decimal("0.00")
    assert table_mod.adjustment_rate(Decimal("0.92"), Decimal("0.80")) == Decimal("-1.30")


def test_unsupported_standard_rejected():
    with pytest.raises(errors.BadRequest):
        table_mod.adjustment_rate(Decimal("0.90"), Decimal("0.70"))


def test_cos_phi_formula():
    # P=300, Q=400 → 300/500 = 0.60
    assert billing.cos_phi(Decimal("300"), Decimal("400")) == Decimal("0.60")
    assert billing.cos_phi(Decimal("0"), Decimal("10")) is None


# ---------------- 缺证月份裁决口径 ----------------
def _bill(bills, month, meter):
    return next(b for b in bills if b["month"] == month and b["meter_id"] == meter)


def test_full_month_uses_observed_pf_and_is_final(evaluated):
    b = _bill(evaluated["bills"], "2026-03", "M0001")
    assert b["pf_basis"] == "observed"
    assert b["evidence_grade"] == "OBSERVED"
    assert b["status"] == billing.FINAL
    # P=408.065, Q=250 → cosφ≈0.85
    assert b["observed_pf"] == "0.85"
    assert b["applied_pf"] == "0.85"
    assert b["adjust_rate_pct"] == "2.50"
    assert b["coverage"] == Decimal("1.0000")


def test_low_coverage_falls_back_to_contract_but_keeps_observation(evaluated):
    # M0002 一月只覆盖 12/31 天，低于 0.60 门槛 → 合同兜底，且保持暂估
    b = _bill(evaluated["bills"], "2026-01", "M0002")
    assert b["evidence_grade"] == "LOW_COVERAGE"
    assert b["pf_basis"] == "contract"
    assert b["applied_pf"] == "0.90"
    assert b["status"] == billing.PROVISIONAL
    # 实测值与观察范围仍然保留给复核，不被合同值抹掉
    assert b["observed_pf"] is not None
    assert b["window_start"] and b["window_end"]
    assert b["coverage"] < config.PF_MIN_COVERAGE
    assert "K_PF_LOW_COVERAGE" in b["marks"]


def test_no_evidence_month_uses_contract_and_is_provisional(evaluated):
    b = _bill(evaluated["bills"], "2026-02", "M0003")
    assert b["evidence_grade"] == "NO_EVIDENCE"
    assert b["pf_basis"] == "contract"
    assert b["applied_pf"] == "0.90"
    assert b["observed_pf"] is None
    assert b["reactive_kvarh"] is None
    assert b["status"] == billing.PROVISIONAL
    assert "K_PF_NO_EVIDENCE" in b["marks"]


def test_observed_p_and_q_come_from_same_window(evaluated):
    # 低覆盖月窗口内 P、Q 同源，不会拿整月 P 配窗口 Q 得到虚高 cosφ
    b = _bill(evaluated["bills"], "2026-01", "M0002")
    assert b["window_active_kwh"] is not None
    assert b["window_active_kwh"] < b["active_kwh"]


def test_coverage_at_threshold_uses_observed():
    # 覆盖率恰好等于门槛（含等号）→ 允许实测
    grade, marks, use_observed = billing.classify(
        coverage=config.PF_MIN_COVERAGE, has_evidence=True,
        window_covers_full_month=False, min_coverage=config.PF_MIN_COVERAGE)
    assert grade == "OBSERVED" and use_observed is True
    # 差一点点 → 兜底
    grade2, _, use2 = billing.classify(
        coverage=config.PF_MIN_COVERAGE - Decimal("0.0001"), has_evidence=True,
        window_covers_full_month=False, min_coverage=config.PF_MIN_COVERAGE)
    assert grade2 == "LOW_COVERAGE" and use2 is False


def test_adjustment_amount_money_quantized(evaluated):
    b = _bill(evaluated["bills"], "2026-03", "M0001")
    # 310.33 × 2.5% = 7.75825 → 7.76
    assert b["adjustment_yuan"] == Decimal("7.76")
    assert b["total_yuan"] == b["base_amount"] + b["adjustment_yuan"]


def test_quarantine_keeps_bad_rows_with_lineno(evaluated):
    codes = {q["code"] for q in evaluated["quarantine"]}
    assert {"Q_PF_REVERSE_READING", "Q_PF_UNKNOWN_METER",
            "Q_MISSING_READING", "Q_BAD_READING"} <= codes
    assert all(q["stage"] == "pf_adjust" for q in evaluated["quarantine"])


# ---------------- 幂等：同抄表包重跑不新增电费 ----------------
def test_same_package_rerun_is_idempotent(settings, store, batch_id, reactive_bytes):
    first = pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                                raw_bytes=reactive_bytes)
    again = pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                                raw_bytes=reactive_bytes)
    assert again["idempotent_replay"] is True
    assert again["package_id"] == first["package_id"]
    bills_before = store.load_pf_bills(first["package_id"])
    pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                        raw_bytes=reactive_bytes)
    assert store.load_pf_bills(first["package_id"]) == bills_before


def test_different_bytes_make_different_package(settings, store, batch_id,
                                                reactive_bytes, sample_dir):
    first = pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                                raw_bytes=reactive_bytes)
    final_bytes = (sample_dir / FINAL_SAMPLE).read_bytes()
    second = pf_service.evaluate(settings, store, batch_id=batch_id,
                                 filename=FINAL_SAMPLE, raw_bytes=final_bytes)
    assert second["package_id"] != first["package_id"]


def test_force_rerun_does_not_duplicate_quarantine(settings, store, batch_id,
                                                   reactive_bytes):
    first = pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                                raw_bytes=reactive_bytes)
    n_before = len(store.load_pf_quarantine(first["package_id"]))
    assert n_before > 0
    pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                        raw_bytes=reactive_bytes, force=True)
    assert len(store.load_pf_quarantine(first["package_id"])) == n_before


# ---------------- 原始证据不可缺失 ----------------
def test_raw_evidence_stored_worm(settings, store, batch_id, reactive_bytes, evaluated):
    from pathlib import Path
    evidence = Path(evaluated["evidence_path"])
    assert evidence.is_file()
    assert evidence.read_bytes() == reactive_bytes
    # 同 package 同名证据内容一致时不报错；证据目录按 package 隔离
    assert evaluated["package_id"] in str(evidence)


def test_evidence_conflict_refuses_overwrite(settings, store, batch_id,
                                             reactive_bytes, evaluated):
    from pathlib import Path
    target = Path(evaluated["evidence_path"])
    target.write_bytes(b"tampered")  # 模拟同路径被外部改写
    with pytest.raises(errors.Conflict):
        pf_service.evaluate(settings, store, batch_id=batch_id, filename=SAMPLE,
                            raw_bytes=reactive_bytes, force=True)


def test_empty_package_raises_and_keeps_no_bills(settings, store, batch_id):
    with pytest.raises(errors.UnprocessableInput) as exc:
        pf_service.evaluate(settings, store, batch_id=batch_id, filename="empty.csv",
                            raw_bytes=b"   \n")
    assert exc.value.code == "E_PF_PACKAGE_EMPTY"


# ---------------- 原子发布：中断保留旧版，同版本三件套 ----------------
def test_publish_writes_atomic_versioned_dir(settings, store, evaluated):
    row = release_mod.publish(settings, store, package_id=evaluated["package_id"])
    from pathlib import Path
    vdir = Path(row["release_dir"])
    assert vdir.is_dir()
    for name in ("pf_bills.csv", "pf_windows.csv", "pf_reconcile.json",
                 "pf_manifest.json"):
        assert (vdir / name).is_file()
    # current 软链指向生效版本
    link = Path(settings.data_dir) / config.PF_RELEASE_DIRNAME / \
        f"{evaluated['package_id']}-current"
    assert link.is_symlink()
    assert link.resolve() == vdir.resolve()


def test_publish_manifest_exports_reconcile_same_version(settings, store, evaluated):
    row = release_mod.publish(settings, store, package_id=evaluated["package_id"])
    manifest_json = row["manifest_json"]
    # 清单里的调整合计 = 对账文件 = 账单 CSV 逐笔之和，三者同版本号
    recon = json.loads((__import__("pathlib").Path(row["release_dir"]) /
                        "pf_reconcile.json").read_text(encoding="utf-8"))
    assert recon["all_balanced"] is True
    bills = store.load_pf_bills(evaluated["package_id"])
    total = sum((b["adjustment_yuan"] for b in bills), Decimal("0"))
    assert Decimal(manifest_json["stats"]["adjustment_total"]) == total
    assert manifest_json["version_seq"] == row["version_seq"]
    csv_text = release_mod.read_release_file(row, "pf_bills.csv")[0]
    assert manifest_json["version"] in csv_text


def test_interrupted_publish_keeps_previous_version(settings, store, evaluated):
    from pathlib import Path
    first = release_mod.publish(settings, store, package_id=evaluated["package_id"])
    root = Path(settings.data_dir) / config.PF_RELEASE_DIRNAME
    # 模拟第二版发布在原子 rename 之前崩溃：残留临时目录，current 仍指第一版
    staging = root / ".tmp-crash"
    staging.mkdir()
    (staging / "pf_manifest.json").write_text("{}", encoding="utf-8")
    link = root / f"{evaluated['package_id']}-current"
    assert link.resolve() == Path(first["release_dir"]).resolve()
    # 正常再发布一版，临时残留不影响
    second = release_mod.publish(settings, store, package_id=evaluated["package_id"])
    assert second["version_seq"] == first["version_seq"] + 1
    assert link.resolve() == Path(second["release_dir"]).resolve()
    assert Path(first["release_dir"]).is_dir()  # 旧版本目录保留不删


def test_release_file_whitelist(settings, store, evaluated):
    row = release_mod.publish(settings, store, package_id=evaluated["package_id"])
    with pytest.raises(errors.NotFound):
        release_mod.read_release_file(row, "../../etc/passwd")


# ---------------- 定案追补 ----------------
def test_finalize_true_up_only_nonzero_provisional(settings, store, sample_dir,
                                                   evaluated):
    release_mod.publish(settings, store, package_id=evaluated["package_id"])
    fin = pf_service.finalize(
        settings, store, provisional_package_id=evaluated["package_id"],
        final_filename=FINAL_SAMPLE,
        final_bytes=(sample_dir / FINAL_SAMPLE).read_bytes())
    # 三笔非零追补：M0002 / M0003 / M0005 的 2026-02
    keys = sorted((t["month"], t["meter_id"]) for t in fin["true_ups"])
    assert keys == [("2026-02", "M0002"), ("2026-02", "M0003"),
                    ("2026-02", "M0005")]
    for t in fin["true_ups"]:
        assert t["true_up_delta_yuan"] == (
            t["adjustment_yuan"] - t["prior_adjustment_yuan"])
        assert t["true_up_of"].endswith(":" + t["month"] + ":" + t["meter_id"])
        assert t["status"] == billing.FINAL


def test_finalize_keeps_evidence_lacking_months_provisional(settings, store, sample_dir,
                                                            evaluated):
    fin = pf_service.finalize(
        settings, store, provisional_package_id=evaluated["package_id"],
        final_filename=FINAL_SAMPLE,
        final_bytes=(sample_dir / FINAL_SAMPLE).read_bytes())
    # 补齐包仍无无功的表（007/M0004/M0006/M0008 等）继续暂估，不被强行定案
    provisional = [(b["month"], b["meter_id"]) for b in fin["bills"]
                   if b["status"] == billing.PROVISIONAL]
    assert ("2026-02", "007") in provisional
    assert ("2026-01", "M0004") in provisional


def test_provisional_version_remains_unchanged_after_finalize(settings, store,
                                                              sample_dir, evaluated):
    before = {b["bill_id"]: str(b["adjustment_yuan"])
              for b in store.load_pf_bills(evaluated["package_id"])}
    pf_service.finalize(
        settings, store, provisional_package_id=evaluated["package_id"],
        final_filename=FINAL_SAMPLE,
        final_bytes=(sample_dir / FINAL_SAMPLE).read_bytes())
    after = {b["bill_id"]: str(b["adjustment_yuan"])
             for b in store.load_pf_bills(evaluated["package_id"])}
    assert before == after  # 旧暂估版一字不改


# ---------------- 窗口聚合纯函数 ----------------
def test_active_within_window_proportional():
    active = [{"meter_id": "X", "start_at": "2026-01-01T00:00:00+00:00",
               "end_at": "2026-02-01T00:00:00+00:00",
               "usage_kwh": Decimal("310")}]
    # 只取 1 月前 15 天
    p = iv.active_within_window(
        active, "X", "2026-01-01T00:00:00+00:00", "2026-01-16T00:00:00+00:00")
    assert p == Decimal("150.000")


# ---------------- HTTP 端到端 ----------------
def test_http_full_flow(client: TestClient, batch_id):
    ev = client.post("/api/powerfactor/evaluate",
                     json={"batch_id": batch_id, "sample": SAMPLE})
    assert ev.status_code == 200
    pid = ev.json()["package_id"]
    pub = client.post("/api/powerfactor/publish", json={"package_id": pid})
    assert pub.status_code == 200
    rid = pub.json()["release_id"]
    for name in ("pf_bills.csv", "pf_windows.csv", "pf_reconcile.json",
                 "pf_manifest.json"):
        resp = client.get(f"/api/powerfactor/releases/{rid}/{name}")
        assert resp.status_code == 200
    page = client.get("/powerfactor")
    assert page.status_code == 200 and pid in page.text
    fz = client.post("/api/powerfactor/finalize",
                     json={"provisional_package_id": pid, "sample": FINAL_SAMPLE})
    assert fz.status_code == 200 and len(fz.json()["true_ups"]) == 3


def test_http_evaluate_unknown_batch_404(client: TestClient):
    resp = client.post("/api/powerfactor/evaluate",
                       json={"batch_id": "mb-nope", "sample": SAMPLE})
    assert resp.status_code == 404


def test_http_rerun_does_not_add_bills(client: TestClient, store, batch_id):
    client.post("/api/powerfactor/evaluate",
                json={"batch_id": batch_id, "sample": SAMPLE})
    packages_before = len(store.list_pf_packages())
    client.post("/api/powerfactor/evaluate",
                json={"batch_id": batch_id, "sample": SAMPLE})
    assert len(store.list_pf_packages()) == packages_before
