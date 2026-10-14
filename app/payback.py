"""Payback arithmetic, identical to the page's JavaScript (docs/contract.md, Payback)."""
from __future__ import annotations

from typing import Optional

from .schemas import RoiDefaults


def roi_defaults() -> dict:
    return RoiDefaults().model_dump()


def payback(roi: dict, automated_share: float) -> dict:
    annual_labour = roi["wage_per_h"] * roi["shifts"] * roi["hours_per_shift"] * roi["days_per_year"]
    annual_savings = annual_labour * automated_share
    investment = roi["robot_price"] * roi["system_multiple"]
    grant = min(roi["grant_share"] * investment, roi["grant_cap"])
    months: Optional[float] = None
    if annual_savings:
        months = (investment - grant) / annual_savings * 12
    return {"annual_labour": annual_labour, "annual_savings": annual_savings,
            "investment": investment, "grant": grant, "payback_months": months}
