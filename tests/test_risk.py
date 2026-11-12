from datetime import datetime, timezone

from bot.risk import DailyGuard, calc_lot, check_new_trade, currency_exposure


def test_lot_example_eurusd(eurusd_meta):
    # Equity 10.000, risiko 1% = 100, SL 30 pip = 0.0030 -> 300/lot -> 0.33 lot
    m = eurusd_meta
    lot = calc_lot(100, 0.0030, m["tick_size"], m["tick_value"], m["volume_step"], m["volume_min"], m["volume_max"])
    assert lot == 0.33


def test_lot_rounds_down_never_exceeds_risk(eurusd_meta):
    m = eurusd_meta
    lot = calc_lot(100, 0.0029, m["tick_size"], m["tick_value"], m["volume_step"], m["volume_min"], m["volume_max"])
    assert lot * 0.0029 / m["tick_size"] * m["tick_value"] <= 100


def test_lot_below_minimum_is_skipped(eurusd_meta):
    m = eurusd_meta
    assert calc_lot(1, 0.0050, m["tick_size"], m["tick_value"], m["volume_step"], m["volume_min"], m["volume_max"]) == 0


def test_lot_invalid_inputs():
    assert calc_lot(100, 0, 0.00001, 1, 0.01, 0.01, 100) == 0
    assert calc_lot(100, 0.003, 0.00001, 0, 0.01, 0.01, 100) == 0


def test_currency_exposure():
    exp = currency_exposure([("EURUSD", 1), ("GBPUSD", 1), ("USDJPY", -1)])
    assert exp == {"EUR": 1, "USD": -3, "GBP": 1, "JPY": 1}


def test_check_new_trade_rules():
    assert check_new_trade("EURUSD", 1, [], 3, 2) is None
    assert "maks" in check_new_trade("EURUSD", 1, [("A", 1)] * 3, 3, 2)
    assert "sudah ada" in check_new_trade("EURUSD", -1, [("EURUSD", 1)], 3, 2)
    # sudah 2x short USD, buy AUDUSD akan jadi short USD ke-3
    assert "USD" in check_new_trade("AUDUSD", 1, [("EURUSD", 1), ("GBPUSD", 1)], 3, 2)
    # arah berlawanan justru mengurangi eksposur
    assert check_new_trade("USDJPY", 1, [("EURUSD", 1), ("GBPUSD", 1)], 3, 2) is None


def test_daily_guard():
    g = DailyGuard(0.03)
    assert g.update(datetime(2026, 1, 5, 1, tzinfo=timezone.utc), 10_000)
    assert not g.halted(9_750)
    assert g.halted(9_700)
    assert not g.update(datetime(2026, 1, 5, 20, tzinfo=timezone.utc), 9_700)
    assert g.update(datetime(2026, 1, 6, 0, tzinfo=timezone.utc), 9_700)
    assert not g.halted(9_700)


def test_lot_with_min_small_account(eurusd_meta):
    from bot.risk import lot_with_min

    # $100: risiko 0.5% = $0.50; SL 8 pip = $0.80 di 0.01 lot -> lot minimum dipakai (0.8% <= 1%)
    assert lot_with_min(100, 0.005, 0.01, 0.0008, eurusd_meta) == eurusd_meta["volume_min"]
    # SL 15 pip = $1.50 di 0.01 lot (1.5%) -> dilewati
    assert lot_with_min(100, 0.005, 0.01, 0.0015, eurusd_meta) == 0
    # akun besar: lot normal dari calc_lot
    assert lot_with_min(10_000, 0.01, 0.01, 0.0030, eurusd_meta) == 0.33
