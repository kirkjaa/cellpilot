import pytest

from app.payback import payback, roi_defaults


def test_defaults_match_contract():
    assert roi_defaults() == {"currency": "SGD", "wage_per_h": 14.0, "shifts": 2, "hours_per_shift": 8,
                              "days_per_year": 250, "robot_price": 40000, "system_multiple": 3.0,
                              "grant_share": 0.5, "grant_cap": 30000}


def test_payback_with_capped_grant():
    p = payback(roi_defaults(), 0.55)
    assert p["annual_labour"] == 14.0 * 2 * 8 * 250 == 56000
    assert p["annual_savings"] == pytest.approx(30800)
    assert p["investment"] == 120000
    assert p["grant"] == 30000  # 0.5 x 120000 = 60000, capped at 30000
    assert p["payback_months"] == pytest.approx((120000 - 30000) / 30800 * 12)  # 35.06


def test_payback_with_grant_under_cap():
    roi = {**roi_defaults(), "robot_price": 10000}
    p = payback(roi, 0.5)
    assert p["investment"] == 30000
    assert p["grant"] == 15000
    assert p["payback_months"] == pytest.approx(15000 / 28000 * 12)


def test_no_savings_gives_null():
    assert payback(roi_defaults(), 0.0)["payback_months"] is None
