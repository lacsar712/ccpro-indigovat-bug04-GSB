"""浸染列映射：表单、入库、近笔/折线展示三条链路共用同一套列义。

唯一列义（与 ``app.models.DipLot`` 对齐）：
- ``clothMeters`` —— 布料米数（必填，非负）
- ``redoxMv``    —— 氧化还原电位 mV，可空

本模块之外不得再各自解释/对调这两列；新增、更新、近笔 DTO、
可染色判定、折线取数全部从这里走。
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.models import DipLot

# 近笔展示与折线取数共用的窗口大小
RECENT_WINDOW = 8


def _to_decimal(raw: str, *, field: str, required: bool) -> Optional[Decimal]:
    text = (raw or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field}不能为空")
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field}必须是数字")


def form_to_columns(cloth_raw: str, redox_raw: str) -> tuple[Decimal, Optional[Decimal]]:
    """表单 → (clothMeters, redoxMv)，列义固定，不再对调。

    米数必填；电位可空。两个值都在返回前解析完成，调用方拿到结果后
    再落库，可保证“解析失败整笔不入库”。
    """
    meters = _to_decimal(cloth_raw, field="布料米数", required=True)
    redox = _to_decimal(redox_raw, field="氧化还原电位", required=False)
    if meters is not None and meters < 0:
        raise ValueError("布料米数不能为负")
    return meters, redox


def parse_dipped_at(raw: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise ValueError("浸染时间不能为空")
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError("浸染时间格式无效")


def make_lot(*, vat_id: int, dipped_at_raw: str, cloth_raw: str, redox_raw: str) -> DipLot:
    """新建路径：先完整解析表单，再构造（尚未持久化的）DipLot。"""
    dipped_at = parse_dipped_at(dipped_at_raw)
    meters, redox = form_to_columns(cloth_raw, redox_raw)
    return DipLot(vat_id=vat_id, dippedAt=dipped_at, clothMeters=meters, redoxMv=redox)


def apply_form_to_lot(lot: DipLot, *, cloth_raw: str, redox_raw: str) -> DipLot:
    """更新路径：先完整解析，再一次性写回同一对象的正确列。

    解析发生在任何赋值之前，因此非法输入不会留下“只改了一半”的残行。
    """
    meters, redox = form_to_columns(cloth_raw, redox_raw)
    lot.clothMeters = meters
    lot.redoxMv = redox
    return lot


def lots_chronological(lots) -> list[DipLot]:
    """库内批次的唯一排序：按浸染时间、再按 id 升序。

    近笔顺序、折线取数都以此为准，避免与库内顺序/按米数排序打架。
    """
    return sorted(lots, key=lambda x: (x.dippedAt, x.id))


def recent_lots(lots) -> list[DipLot]:
    """近笔：时间升序的末尾 RECENT_WINDOW 笔，再反转为最新在前。"""
    chronological = lots_chronological(lots)
    return list(reversed(chronological[-RECENT_WINDOW:]))


def lot_to_recent_dto(lot: DipLot) -> dict[str, Any]:
    """库内行 → 近笔展示 DTO，逐字段直读，不再交叉对调。"""
    return {
        "id": lot.id,
        "dippedAt": lot.dippedAt.strftime("%Y-%m-%d %H:%M"),
        "clothMeters": float(lot.clothMeters) if lot.clothMeters is not None else None,
        "redoxMv": float(lot.redoxMv) if lot.redoxMv is not None else None,
    }


def ready_metric_from_lot(lot: Optional[DipLot]) -> Optional[Decimal]:
    """可染色判定只吃电位列（redoxMv），绝不拿布米去比 -500。"""
    if lot is None:
        return None
    return lot.redoxMv


def spark_values_from_lots(lots) -> list[float]:
    """折线取数：与近笔同一批次集合、同一时间顺序，只取电位列。

    只保留已填电位的批次，因此折线点数 == 带电位的近笔条数。
    """
    return [
        float(l.redoxMv)
        for l in lots_chronological(lots)
        if l.redoxMv is not None
    ]
