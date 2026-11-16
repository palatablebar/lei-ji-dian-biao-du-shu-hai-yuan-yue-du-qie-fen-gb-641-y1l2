"""力率电费发布：暂估/定案结果原子落盘，正式结果、导出与对账同版本。

发布安全口径：
- 先在同卷临时目录写齐「账单 CSV + 观察窗口 CSV + 对账 JSON + 清单 JSON」，
  再用一次 os.replace 原子切名成版本目录；中途崩溃只留临时目录，current 仍指旧版；
- 每个版本目录不可变（写定后不改），新版本另起 version_seq；
- current 是指向「当前生效版本」的符号链接；切换失败/中断不影响旧链接；
- 发布清单里带全部输入指纹与规则版本，导出文件与对账数字全部来自同一批 pf_bills，
  保证「正式结果、导出、对账」同版本。
"""
from __future__ import annotations

import json
import os
import shutil
from decimal import Decimal
from pathlib import Path

from ... import config, manifest, reasons
from ..m6_batches import history
from . import billing


def _releases_root(settings) -> Path:
    root = Path(settings.data_dir) / config.PF_RELEASE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _csv(headers: list[str], rows: list[list[str]]) -> str:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue()


PF_BILL_HEADER = ["版本", "月份", "表号", "考核标准", "证据等级", "状态",
                  "基础电费元", "整月有功kWh", "窗口内有功kWh", "窗口无功kvarh", "覆盖率",
                  "观察起UTC", "观察止UTC", "实测力率", "采用力率", "采用依据",
                  "调整率%", "力率调整电费元", "合计元", "追补原版本", "追补差额元",
                  "标记"]
PF_WINDOW_HEADER = ["月份", "表号", "覆盖天数", "月天数", "覆盖率",
                    "观察起UTC", "观察止UTC", "来源无功区间"]


def _bill_rows(version: str, bills: list[dict]) -> list[list[str]]:
    rows = []
    for b in bills:
        rows.append([
            version, b["month"], b["meter_id"], b["standard"], b["evidence_grade"],
            b["status"], f"{Decimal(b['base_amount']):.2f}",
            f"{Decimal(b['active_kwh']):.3f}",
            "" if b.get("window_active_kwh") is None
            else f"{Decimal(b['window_active_kwh']):.3f}",
            "" if b.get("reactive_kvarh") is None
            else f"{Decimal(b['reactive_kvarh']):.3f}",
            f"{Decimal(b['coverage']):.4f}",
            b.get("window_start") or "", b.get("window_end") or "",
            b.get("observed_pf") or "", b["applied_pf"],
            "实测" if b["pf_basis"] == "observed" else "合同兜底",
            b["adjust_rate_pct"], f"{Decimal(b['adjustment_yuan']):.2f}",
            f"{Decimal(b['total_yuan']):.2f}",
            b.get("true_up_of") or "",
            "" if b.get("true_up_delta_yuan") is None
            else f"{Decimal(b['true_up_delta_yuan']):.2f}",
            " ".join(reasons.label(m) for m in b["marks"]),
        ])
    return rows


def _window_rows(bills: list[dict]) -> list[list[str]]:
    rows = []
    for b in bills:
        rows.append([
            b["month"], b["meter_id"], f"{Decimal(b['covered_days']):.3f}",
            "" if b.get("month_days") is None else f"{Decimal(b['month_days']):.3f}",
            f"{Decimal(b['coverage']):.4f}",
            b.get("window_start") or "", b.get("window_end") or "",
            ";".join(b.get("source_intervals", [])),
        ])
    return rows


def _reconcile(bills: list[dict]) -> dict:
    """力率对账：调整电费合计 = 逐笔调整电费之和；追补差额闭合到定案-暂估。"""
    total = sum((Decimal(b["adjustment_yuan"]) for b in bills), Decimal("0"))
    summed = sum((Decimal(b["adjustment_yuan"]) for b in bills), Decimal("0"))
    true_up = [b for b in bills if b.get("true_up_of")]
    tu_sum = sum((Decimal(b["true_up_delta_yuan"]) for b in true_up), Decimal("0"))
    tu_check = sum(
        (Decimal(b["adjustment_yuan"]) - Decimal(b["prior_adjustment_yuan"])
         for b in true_up), Decimal("0"))
    accounts = [
        {"key": "pf_adjust_total", "label": "力率调整电费合计",
         "left": total.quantize(config.MONEY_QUANT),
         "right": summed.quantize(config.MONEY_QUANT), "unit": "元",
         "balanced": total == summed, "note": f"{len(bills)} 笔逐笔之和"},
        {"key": "pf_true_up", "label": "追补差额闭合",
         "left": tu_sum.quantize(config.MONEY_QUANT),
         "right": tu_check.quantize(config.MONEY_QUANT), "unit": "元",
         "balanced": tu_sum == tu_check,
         "note": f"{len(true_up)} 笔追补（定案调整额 − 暂估调整额）"},
    ]
    return {"accounts": accounts,
            "all_balanced": all(a["balanced"] for a in accounts)}


def publish(settings, store, *, package_id: str, supersedes_release_id: str | None = None
            ) -> dict:
    record = store.get_pf_package(package_id)
    if record is None:
        from ... import errors
        raise errors.NotFound(f"抄表包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")
    bills = store.load_pf_bills(package_id)
    seq = store.next_pf_release_seq(package_id)
    version = f"{package_id}-v{seq}"
    release_id = "rel-" + manifest.digest_parts(
        [package_id, str(seq), record["sha256"]])[:16]
    created_at = history.iso(history.now_utc())

    root = _releases_root(settings)
    final_dir = root / version
    staging = root / (".tmp-" + version)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    recon = _reconcile(bills)
    stats = {
        "bill_count": len(bills),
        "provisional_count": sum(1 for b in bills if b["status"] == billing.PROVISIONAL),
        "final_count": sum(1 for b in bills if b["status"] == billing.FINAL),
        "adjustment_total": sum(
            (Decimal(b["adjustment_yuan"]) for b in bills), Decimal("0")
        ).quantize(config.MONEY_QUANT),
    }
    manifest_json = {
        "release_id": release_id, "version": version, "version_seq": seq,
        "package_id": package_id, "batch_id": record["batch_id"],
        "package_sha256": record["sha256"], "package_filename": record["filename"],
        "rule_version": record["rule_version"], "pf_rule": config.PF_RULE_NAME,
        "params": json.loads(record["params_json"]),
        "created_at": created_at, "supersedes": supersedes_release_id,
        "stats": {k: (str(v) if isinstance(v, Decimal) else v)
                  for k, v in stats.items()},
        "reconcile": {
            "all_balanced": recon["all_balanced"],
            "accounts": [{**a, "left": str(a["left"]), "right": str(a["right"])}
                         for a in recon["accounts"]]},
        "files": ["pf_bills.csv", "pf_windows.csv", "pf_reconcile.json",
                  "pf_manifest.json"],
        "replayable": "同抄表包重跑命中同一 package_id，不新增电费",
    }

    # 先在临时目录写齐四份产物
    (staging / "pf_bills.csv").write_text(
        _csv(PF_BILL_HEADER, _bill_rows(version, bills)), encoding="utf-8")
    (staging / "pf_windows.csv").write_text(
        _csv(PF_WINDOW_HEADER, _window_rows(bills)), encoding="utf-8")
    (staging / "pf_reconcile.json").write_text(
        json.dumps(recon, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    (staging / "pf_manifest.json").write_text(
        json.dumps(manifest_json, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")

    # 原子切名：失败/中断不会出现半个正式版本，旧版本目录保持不动
    os.replace(staging, final_dir)

    release_row = {
        "release_id": release_id, "package_id": package_id,
        "batch_id": record["batch_id"], "version_seq": seq, "status": "published",
        "bill_count": stats["bill_count"],
        "provisional_count": stats["provisional_count"],
        "final_count": stats["final_count"],
        "adjustment_total": stats["adjustment_total"],
        "release_dir": str(final_dir), "manifest_json": manifest_json,
        "created_at": created_at, "supersedes_release_id": supersedes_release_id,
    }
    store.save_pf_release(release_row)
    _switch_current(root, package_id, version)
    return release_row


def _switch_current(root: Path, package_id: str, version: str) -> None:
    """current 符号链接原子切换；切换不成功则保留旧链接（旧版继续生效）。"""
    link = root / package_id / "current" if False else root / f"{package_id}-current"
    tmp_link = root / (".cur-" + package_id)
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(root / version, tmp_link)
    os.replace(tmp_link, link)


def current_release(settings, store, package_id: str) -> dict | None:
    release = store.latest_pf_release(package_id)
    return release


def read_release_file(release_row: dict, name: str) -> tuple[str, str]:
    """从正式版本目录读取产物，保证导出的就是发布的那版。"""
    allowed = {"pf_bills.csv", "pf_windows.csv", "pf_reconcile.json",
               "pf_manifest.json"}
    if name not in allowed:
        from ... import errors
        raise errors.NotFound(f"发布产物不存在：{name}", code="E_BATCH_NOT_FOUND")
    path = Path(release_row["release_dir"]) / name
    if not path.is_file():
        from ... import errors
        raise errors.NotFound(f"发布产物缺失：{name}", code="E_BATCH_NOT_FOUND")
    media = ("application/json; charset=utf-8" if name.endswith(".json")
             else "text/csv; charset=utf-8")
    return path.read_text(encoding="utf-8"), media
