"""Penjaga config/settings.yaml yang dipakai bot live: keputusan pemilik 2026-11-16 (Tahap 5 audit) dan parameter
yang tidak boleh berubah tanpa persetujuan (strategi Ichimoku H4, rugi harian, risiko per trade)."""
from bot.config import load_settings


def test_partial_usd_disabled_h5():
    m = load_settings().manage
    assert m.partial_at_usd == 0          # H5: parsial $10 dimatikan, belum ada pengganti berbasis R
    assert m.partial_at_r == 1.0 and m.be_at_r == 1.0 and m.lock_at_r == 1.5


def test_eurusd_ichimoku_shadow_xauusd_live_h6():
    s = load_settings()
    assert s.symbol_cfg("EURUSD").rule_mode == "shadow" and s.symbol_cfg("XAUUSD").rule_mode == "live"


def test_open_risk_cap_one_percent_m4():
    assert load_settings().risk.max_open_risk == 0.01


def test_ai_scoring_and_prompt_frozen_until_calibration_h9():
    """H9 opsi B: skor dan prompt analisis AI tidak diubah sampai >= 30 hasil forward valid. Ubah sidik jari ini hanya
    setelah pemilik menyetujui perubahan skor / prompt berdasarkan data kalibrasi."""
    import hashlib

    from bot.ai.analysis import MIN_RR, MODERATE_SCORE, SETUP_SCORE_WEIGHTS, STRONG_SCORE
    from bot.ai.prompts import ANALYSIS_SYSTEM
    assert SETUP_SCORE_WEIGHTS == {"context": 25, "structure": 20, "liquidity": 15, "price_action": 20,
                                   "location": 15, "rr": 5}
    assert (MIN_RR, MODERATE_SCORE, STRONG_SCORE) == (1.3, 60, 80)
    assert hashlib.sha256(ANALYSIS_SYSTEM.encode()).hexdigest()[:16] == "ce0b3c53e7ce07bb"


def test_protected_strategy_and_risk_parameters_unchanged():
    s = load_settings()
    p, r, c = s.strategy, s.risk, s.confirm
    assert (p.entry_type, p.entry_tf, p.sl_atr_mult, p.exit_rule, p.ma_filter) == ("ichimoku", "H4", 2.5, "signal", True)
    assert r.max_daily_loss == 0.015 and r.risk_per_trade == 0.005
    assert (c.risk_strong, c.risk_moderate, c.risk_options) == (0.005, 0.0025, [0.0025, 0.005, 0.01])
