import pytest

from app.core.trading_config import (
    LIVE_CONFIRMATION_PHRASE,
    REQUIRED_BEFORE_LIVE,
    LiveGateError,
    TradingSettings,
    check_live_gate,
)


def settings(**kw):
    return TradingSettings(_env_file=None, **kw)


def full_live_values():
    return dict(
        target_notional_per_leg_usd=500, target_leverage=2, rv_window_minutes=30, basis_vol_window_minutes=30,
        vol_percentile_lookback_days=1, vol_min_observations=300, min_expected_net_edge_bps=12,
        max_allowed_slippage_bps=4, min_required_book_depth_multiplier=4, max_market_data_age_ms=2000,
        max_delta_imbalance_usd=40, max_delta_imbalance_ratio="0.04", delta_hedge_timeout_ms=8000,
        min_liquidation_distance_pct=35, max_margin_utilization_pct=50, max_concurrent_trades=1,
        max_total_gross_notional_usd=2000, max_notional_per_venue_usd=1000, max_notional_per_asset_usd=1000,
        normal_exit_policy="edge_decay",
    )


def test_defaults_are_paper_and_live_off():
    s = settings()
    assert s.run_mode == "paper" and s.live_trading_enabled is False
    check_live_gate(s)  # paper always passes


def test_paper_resolves_with_labelled_defaults():
    p = settings().resolve()
    assert p.provenance["target_leverage"] == "paper_default"
    assert p.provenance["max_allowed_leverage"] == "locked"
    assert p.target_leverage <= p.max_allowed_leverage == 4
    assert p.low_vol_max_percentile == 60
    assert p.funding_screening_horizon_minutes == 480


def test_explicit_values_are_marked_explicit():
    p = settings(target_leverage=3).resolve()
    assert p.provenance["target_leverage"] == "explicit" and p.target_leverage == 3


def test_target_leverage_cannot_exceed_locked_cap():
    with pytest.raises(ValueError):
        settings(target_leverage=5).resolve()
    with pytest.raises(ValueError):
        settings(max_allowed_leverage=5)


@pytest.mark.parametrize("field,value", [
    ("twap_entry_duration_minutes", 120), ("controlled_exit_equity_ratio", "0.5"),
    ("emergency_exit_equity_ratio", "0.2"), ("controlled_exit_duration_minutes", 60),
    ("emergency_exit_target_seconds", 30), ("funding_screening_horizon_minutes", 60),
    ("funding_screening_horizon_minutes", 120),
    ("twap_max_child_notional_usd", 150), ("low_vol_max_percentile", 61),
])
def test_locked_values_cannot_be_loosened(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


def test_child_size_and_vol_percentile_may_be_tightened():
    s = settings(twap_max_child_notional_usd=50, low_vol_max_percentile=30)
    assert s.twap_max_child_notional_usd == 50


def test_live_needs_flag_phrase_and_every_required_value():
    with pytest.raises(LiveGateError) as e:
        check_live_gate(settings(run_mode="live"))
    msg = str(e.value)
    assert "LIVE_TRADING_ENABLED" in msg and "CONFIRMATION" in msg and "TARGET_LEVERAGE" in msg
    with pytest.raises(LiveGateError):
        settings(run_mode="live").resolve()  # never fills defaults in live mode


def test_live_passes_only_when_everything_explicit():
    s = settings(run_mode="live", live_trading_enabled=True, live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
                 **full_live_values())
    check_live_gate(s)
    assert all(v == "explicit" for k, v in s.resolve().provenance.items() if k in REQUIRED_BEFORE_LIVE)


def test_live_fails_with_wrong_phrase():
    with pytest.raises(LiveGateError):
        check_live_gate(settings(run_mode="live", live_trading_enabled=True,
                                 live_trading_confirmation="yes", **full_live_values()))


def test_api_keys_do_not_enable_live(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "x")
    monkeypatch.setenv("BINANCE_API_SECRET", "y")
    s = TradingSettings(_env_file=None)
    assert s.run_mode == "paper" and not s.live_trading_enabled


def test_scan_assets_comma_separated(monkeypatch):
    monkeypatch.setenv("SCAN_ASSETS", "btc, eth")
    assert TradingSettings(_env_file=None).scan_assets == ["BTC", "ETH"]


def test_an_entry_time_limit_shorter_than_the_locked_twap_is_refused():
    from app.core.trading_config import TradingSettings

    assert TradingSettings(_env_file=None).resolve().max_entry_hours == 12
    with pytest.raises(ValueError, match="MAX_ENTRY_HOURS"):
        TradingSettings(_env_file=None, max_entry_hours=5).resolve()
    assert TradingSettings(_env_file=None, max_entry_hours=6).resolve().max_entry_hours == 6


def test_exclude_venues_is_comma_separated_and_checked(monkeypatch):
    monkeypatch.setenv("EXCLUDE_VENUES", "BloFin, Bitget")
    assert TradingSettings(_env_file=None).exclude_venues == ["BloFin", "Bitget"]
    monkeypatch.setenv("EXCLUDE_VENUES", "bolfin")
    with pytest.raises(ValueError, match="not a supported venue"):
        TradingSettings(_env_file=None)
