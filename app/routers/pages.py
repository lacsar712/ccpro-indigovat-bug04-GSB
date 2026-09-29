from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services.dip_lot_mapping import (
    LotMappingError,
    apply_to_lot,
    build_lot,
    find_identical_lot,
    lot_to_dto,
    order_lots,
    parse_dipped_at,
    parse_form,
    recent_dtos,
    recent_window,
    spark_values_from_lots,
)
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    # 入参为近笔窗口；取数仍只走映射模块的电位列，空电位不成点
    vals = spark_values_from_lots(lots)
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    # 顺序与最近窗口只有一个口径（映射模块），近笔列表与折线共用同一窗口
    ordered = order_lots(vat.lots)
    window = recent_window(vat.lots)
    latest = ordered[-1] if ordered else None
    latest_dto = lot_to_dto(latest) if latest else None
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": latest_dto["redoxMv"] if latest_dto else None,
        "lastMeters": latest_dto["clothMeters"] if latest_dto else None,
        "lastDippedAt": latest_dto["dippedAt"] if latest_dto else None,
        "spark": _spark_points(window),
        "recentLots": recent_dtos(vat.lots),
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


def _redirect_to_vat(pk: int, workshop_id: Optional[int]) -> RedirectResponse:
    suffix = f"&workshop={workshop_id}" if workshop_id is not None else ""
    return RedirectResponse(f"/?vat={pk}{suffix}", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return _redirect_to_vat(pk, ws)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(""),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    try:
        # 先在库外完成全部列映射；任何一列非法都在写库前失败，整笔不入库
        dipped_at = parse_dipped_at(dippedAt)
        values = parse_form(clothMeters, redoxMv)
        # 连点提交幂等：同缸同时间且逐字段相同则不新增，杜绝半截/重复残行
        if find_identical_lot(db, pk, dipped_at, values) is None:
            db.add(build_lot(pk, dipped_at, values))
            db.commit()
        return _redirect_to_vat(pk, ws)
    except LotMappingError as exc:
        db.rollback()
        error = f"浸染记录无效：{exc}"
    except ValueError as exc:  # 兜底：时间/数字层面的其余解析错误同样不得落库
        db.rollback()
        error = f"浸染记录无效：{exc}"
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/lots/{lot_id}/update", response_class=HTMLResponse)
async def bay_update_lot(
    lot_id: int,
    request: Request,
    clothMeters: str = Form(""),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    lot = db.get(DipLot, lot_id)
    ws = int(workshop) if workshop.strip() else None
    if not lot:
        return RedirectResponse("/", status_code=303)
    vat_id = lot.vat_id
    try:
        # 更新保存与新建保存同一列口径；非法值在赋值前抛出，既有批次保持不变
        values = parse_form(clothMeters, redoxMv)
        apply_to_lot(lot, values)
        db.commit()
        return _redirect_to_vat(vat_id, ws)
    except LotMappingError as exc:
        db.rollback()
        error = f"更新失败：{exc}"
    except ValueError as exc:
        db.rollback()
        error = f"更新失败：{exc}"
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, vat_id, error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
