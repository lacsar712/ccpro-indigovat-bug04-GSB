"""浸染列映射：表单 / 入库 / 近笔 / 折线 / 可染色判定的唯一字段口径。

任何读写 DipLot 的 clothMeters（布料米数）与 redoxMv（氧化还原电位 mV）
的链路都必须经过本模块，禁止在调用方各写一套字段对应。

列义固定：
- ``clothMeters`` —— 布料米数，必填，正数；
- ``redoxMv``    —— 氧化还原电位 mV，可空。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.models import DipLot

# ---- 列义常量：全程序只允许这两个名字，不再出现第二套/第三套取数字段 ----
CLOTH_COLUMN = "clothMeters"
REDOX_COLUMN = "redoxMv"

RECENT_LIMIT = 8


class LotMappingError(ValueError):
    """表单值无法映射到浸染列。"""


@dataclass(frozen=True)
class LotColumns:
    """已解析、列义固定的浸染列值。"""

    meters: Decimal
    redox: Optional[Decimal]

    def as_kwargs(self) -> dict[str, Any]:
        return {CLOTH_COLUMN: self.meters, REDOX_COLUMN: self.redox}


def _to_decimal(raw: str, field_label: str, *, allow_empty: bool) -> Optional[Decimal]:
    text = (raw or "").strip()
    if not text:
        if allow_empty:
            return None
        raise LotMappingError(f"{field_label}不能为空")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise LotMappingError(f"{field_label}必须是数字")
    if not value.is_finite():
        raise LotMappingError(f"{field_label}必须是有限数字")
    return value


def parse_dipped_at(raw: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise LotMappingError("浸染时间不能为空")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise LotMappingError("浸染时间格式无效")


def parse_form(cloth_raw: str, redox_raw: str) -> LotColumns:
    """表单两个输入 → 列义固定的 LotColumns（米数列 / 电位列，不允许对调）。"""
    meters = _to_decimal(cloth_raw, "布料米数", allow_empty=False)
    redox = _to_decimal(redox_raw, "氧化还原电位", allow_empty=True)
    if meters <= 0:
        raise LotMappingError("布料米数必须大于 0")
    return LotColumns(meters=meters, redox=redox)


def build_lot(vat_id: int, dipped_at: datetime, values: LotColumns) -> DipLot:
    """新建：按固定列义构造浸染批次（尚未入库，由调用方在同一事务提交）。"""
    return DipLot(
        vat_id=vat_id,
        dippedAt=dipped_at,
        clothMeters=values.meters,
        redoxMv=values.redox,
    )


def apply_to_lot(lot: DipLot, values: LotColumns) -> DipLot:
    """更新：按固定列义就地改写既有批次，返回同一实例。"""
    lot.clothMeters = values.meters
    lot.redoxMv = values.redox
    return lot


def find_identical_lot(
    db, vat_id: int, dipped_at: datetime, values: LotColumns
) -> Optional[DipLot]:
    """同缸同时间且米数/电位逐字段相同的已存批次（用于连点提交幂等去重）。"""
    candidates = (
        db.query(DipLot)
        .filter(DipLot.vat_id == vat_id, DipLot.dippedAt == dipped_at)
        .all()
    )
    for lot in candidates:
        if lot.clothMeters is not None and lot.clothMeters == values.meters:
            if (lot.redoxMv is None and values.redox is None) or (
                lot.redoxMv is not None and lot.redoxMv == values.redox
            ):
                return lot
    return None


def order_lots(lots: list[DipLot]) -> list[DipLot]:
    """库内顺序的唯一口径：按 (浸染时间, id) 升序。近笔/折线均由此派生。"""
    return sorted(lots, key=lambda x: (x.dippedAt, x.id))


def recent_window(lots: list[DipLot], limit: int = RECENT_LIMIT) -> list[DipLot]:
    """最近 N 笔（时间升序），近笔展示与折线取数共用同一窗口。"""
    ordered = order_lots(lots)
    return ordered[-limit:] if limit else ordered


def lot_to_dto(lot: DipLot) -> dict[str, Any]:
    """库内批次 → 展示 DTO。逐字段严格对列：米数来自米数列，电位来自电位列。"""
    return {
        "id": lot.id,
        "dippedAt": lot.dippedAt.strftime("%Y-%m-%d %H:%M"),
        CLOTH_COLUMN: float(lot.clothMeters) if lot.clothMeters is not None else None,
        REDOX_COLUMN: float(lot.redoxMv) if lot.redoxMv is not None else None,
    }


# 旧调用名保留为同一实现的别名，禁止再出现第二套近笔组装。
lot_to_recent_dto = lot_to_dto


def recent_dtos(lots: list[DipLot], limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """近笔展示：最近窗口倒序（最新在前），字段逐列对库。"""
    return [lot_to_dto(lot) for lot in reversed(recent_window(lots, limit))]


def ready_metric_from_lot(lot: Optional[DipLot]) -> Optional[Decimal]:
    """可染色判定只吃电位列 redoxMv，绝不允许拿布米去比 -500。"""
    if lot is None:
        return None
    return lot.redoxMv


def spark_values_from_lots(lots: list[DipLot]) -> list[float]:
    """折线取数：只取电位列；与近笔共用同一最近窗口，空电位不成点。"""
    return [
        float(lot.redoxMv)
        for lot in recent_window(lots)
        if lot.redoxMv is not None
    ]
