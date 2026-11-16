"""M8 入口：无功抄表包评估、定案追补、版本发布与同版导出。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ... import config, errors, reasons
from ...web import Ctx, get_ctx, render
from ..m1_ingest import service as m1_service
from . import release as release_mod
from . import service

router = APIRouter(tags=["M8 力率调整"])


class EvaluateBody(BaseModel):
    batch_id: str
    filename: str | None = None
    sample: str | None = None
    standard: str = "0.90"
    contract_pf: str = "0.90"
    min_coverage: str = "0.6000"
    force: bool = False


class FinalizeBody(BaseModel):
    provisional_package_id: str
    sample: str
    filename: str | None = None
    standard: str = "0.90"
    contract_pf: str = "0.90"
    min_coverage: str = "0.6000"


class PublishBody(BaseModel):
    package_id: str
    supersedes_release_id: str | None = None


def _load_payload(ctx: Ctx, body) -> tuple[str, bytes]:
    if body.sample:
        if body.sample not in config.SAMPLES and body.sample not in (
                config.REACTIVE_FILE, config.REACTIVE_FINAL_FILE):
            raise errors.BadRequest(f"样例 {body.sample!r} 不在白名单内",
                                    code="E_SAMPLE_UNKNOWN")
        path = m1_service.sample_path(ctx.settings, body.sample)
        return body.filename or body.sample, path.read_bytes()
    raise errors.BadRequest("必须提供 sample（演示环境按白名单样例取抄表包）",
                            code="E_BAD_REQUEST")


@router.post("/api/powerfactor/evaluate")
def evaluate(body: EvaluateBody, request: Request, ctx: Ctx = Depends(get_ctx)):
    filename, raw = _load_payload(ctx, body)
    result = service.evaluate(
        ctx.settings, ctx.store, batch_id=body.batch_id, filename=filename,
        raw_bytes=raw, standard=Decimal(body.standard),
        contract_pf=Decimal(body.contract_pf),
        min_coverage=Decimal(body.min_coverage), force=body.force)
    return _jsonable(result)


@router.post("/api/powerfactor/finalize")
def finalize(body: FinalizeBody, ctx: Ctx = Depends(get_ctx)):
    filename, raw = _load_payload(ctx, body)
    result = service.finalize(
        ctx.settings, ctx.store,
        provisional_package_id=body.provisional_package_id,
        final_filename=body.filename or body.sample, final_bytes=raw,
        standard=Decimal(body.standard), contract_pf=Decimal(body.contract_pf),
        min_coverage=Decimal(body.min_coverage))
    return _jsonable(result)


@router.post("/api/powerfactor/publish")
def publish(body: PublishBody, ctx: Ctx = Depends(get_ctx)):
    row = release_mod.publish(ctx.settings, ctx.store, package_id=body.package_id,
                              supersedes_release_id=body.supersedes_release_id)
    return _jsonable(row)


@router.get("/api/powerfactor/packages")
def list_packages(ctx: Ctx = Depends(get_ctx)):
    return {"packages": [_jsonable(p) for p in ctx.store.list_pf_packages()]}


@router.get("/api/powerfactor/packages/{package_id}/bills")
def package_bills(package_id: str, ctx: Ctx = Depends(get_ctx)):
    record = ctx.store.get_pf_package(package_id)
    if record is None:
        raise errors.NotFound(f"抄表包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")
    return {"package": _jsonable(record),
            "bills": [_jsonable(b) for b in ctx.store.load_pf_bills(package_id)],
            "quarantine": ctx.store.load_pf_quarantine(package_id)}


@router.get("/api/powerfactor/releases")
def list_releases(package_id: str | None = None, ctx: Ctx = Depends(get_ctx)):
    rows = ctx.store.list_pf_releases(package_id)
    return {"releases": [_jsonable(r) for r in rows]}


@router.get("/api/powerfactor/releases/{release_id}/{name}")
def release_file(release_id: str, name: str, ctx: Ctx = Depends(get_ctx)):
    row = ctx.store.get_pf_release(release_id)
    if row is None:
        raise errors.NotFound(f"发布版本 {release_id} 不存在", code="E_BATCH_NOT_FOUND")
    body, media = release_mod.read_release_file(row, name)
    download_name = f"{release_id}-{name}"
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition":
                             f'attachment; filename="{download_name}"'})


@router.get("/powerfactor")
def page_powerfactor(request: Request, package_id: str | None = None,
                     ctx: Ctx = Depends(get_ctx)):
    packages = ctx.store.list_pf_packages()
    selected = None
    bills: list[dict] = []
    quarantine: list[dict] = []
    releases: list[dict] = []
    if package_id is None and packages:
        package_id = packages[0]["package_id"]
    if package_id:
        selected = ctx.store.get_pf_package(package_id)
        if selected is not None:
            bills = ctx.store.load_pf_bills(package_id)
            quarantine = ctx.store.load_pf_quarantine(package_id)
            releases = ctx.store.list_pf_releases(package_id)
    grade_labels = {"OBSERVED": "实测", "LOW_COVERAGE": "覆盖不足兜底",
                    "NO_EVIDENCE": "无证据兜底"}
    context = {
        "packages": packages, "selected": selected, "bills": bills,
        "quarantine": quarantine, "releases": releases,
        "grade_labels": grade_labels,
        "reason_codes": reasons.catalog(),
        "min_coverage": f"{config.PF_MIN_COVERAGE:.2f}",
        "contract_pf": f"{config.PF_CONTRACT_FALLBACK:.2f}",
        "reactive_samples": [config.REACTIVE_FILE, config.REACTIVE_FINAL_FILE],
    }
    return render(request, "powerfactor.html", page="powerfactor",
                  page_title="力率调整电费", **context)


def _jsonable(value):
    """把 Decimal/dataclass 统一转成 JSON 友好结构。"""
    from decimal import Decimal
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(value.__dict__)
    return value
