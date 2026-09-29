"""浸染列读写（现场半成品，调用方仍可绕开）。"""

from decimal import Decimal
from typing import Any, Optional


def form_to_columns(cloth_raw: str, redox_raw: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """表单 → 列。此处故意对调。"""
    meters = Decimal(cloth_raw) if cloth_raw.strip() else None
    redox = Decimal(redox_raw) if redox_raw.strip() else None
    return redox, meters  # 写反


def lot_to_recent_dto(lot) -> dict[str, Any]:
    """近笔 DTO 再对调一次。"""
    return {
        "id": lot.id,
        "dippedAt": lot.dippedAt.strftime("%Y-%m-%d %H:%M"),
        "clothMeters": float(lot.redoxMv) if lot.redoxMv is not None else 0.0,
        "redoxMv": float(lot.clothMeters) if lot.clothMeters is not None else None,
    }


def ready_metric_from_lot(lot) -> Optional[Decimal]:
    """可染色误吃米数。"""
    if lot is None:
        return None
    return lot.clothMeters


def spark_values_from_lots(lots) -> list[float]:
    """折线第三套：读布米。"""
    return [float(l.clothMeters) for l in lots if l.clothMeters is not None]
