from bot.journal import Journal


def test_signal_shadow_and_trade_flow(tmp_path):
    j = Journal(tmp_path / "j.db")
    a = j.log_signal("EURUSD", 1, "2026-01-05 10:00:00", 1.1, 0.003, 0.006, {"adx": 25})
    assert j.signal_exists("EURUSD", "2026-01-05 10:00:00")
    j.update_signal(a, ai_decision="reject", ai_multiplier=0.0, ai_reasons=["resisten dekat"])
    j.open_shadow(a, 1.1, 1.097, 1.106)
    j.resolve_shadow(a, "tp", 2.0)

    b = j.log_signal("GBPUSD", -1, "2026-01-05 11:00:00", 1.3, 0.004, 0.008, {})
    j.update_signal(b, ai_decision="approve", ai_multiplier=1.0, executed=1)
    j.log_trade_open(b, 555, "GBPUSD", -1, 0.25, 1.3, 1.304, 1.292, 100.0)
    assert len(j.open_trades()) == 1
    tr = j.close_trade(555, 1.304, "2026-01-05T15:00:00+00:00", -100.0)
    assert tr["r_multiple"] == -1.0

    s = j.ai_vs_rules()
    assert s["ditolak_ai"]["total_R"] == 2.0
    assert s["diloloskan"]["total_R"] == -1.0
    assert s["semua_sinyal_aturan_saja"]["n"] == 2

    paths = j.export_csv(tmp_path / "out")
    assert all(p.exists() for p in paths)


def test_ai_vs_rules_counts_signal_exits_and_news_blocks(tmp_path):
    j = Journal(tmp_path / "j.db")
    a = j.log_signal("EURUSD", 1, "2026-01-05 08:00:00", 1.1, 0.003, 0.0, {})
    j.update_signal(a, ai_decision="approve")
    j.open_shadow(a, 1.1, 1.097, None)
    j.resolve_shadow(a, "signal", 1.5)       # exit lewat sinyal Ichimoku
    b = j.log_signal("GBPUSD", 1, "2026-01-05 08:00:00", 1.3, 0.004, 0.0, {})
    j.update_signal(b, ai_decision="news_block")
    j.open_shadow(b, 1.3, 1.296, None)
    j.resolve_shadow(b, "sl", -1.0)
    s = j.ai_vs_rules()
    assert s["diloloskan"]["total_R"] == 1.5
    assert s["diblok_berita"]["total_R"] == -1.0
