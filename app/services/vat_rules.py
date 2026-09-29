"""染缸状态业务规则。"""

from decimal import Decimal
from typing import Optional

from app.models import DipLot, Vat
from app.services.dip_lot_mapping import ready_metric_from_lot


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    metric = ready_metric_from_lot(latest)
    if metric is None or Decimal(metric) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_vat_status_change(vat: Vat, new_status: str, latest: Optional[DipLot]) -> None:
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
