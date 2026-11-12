"""Unduh data historis (default H1/H4/D1) dari MT5 ke data/<SYMBOL>_<TF>.csv (+ spesifikasi simbol di <SYMBOL>_meta.json).

Contoh: python scripts/download_data.py --from 2019-01-01
        python scripts/download_data.py --from 2023-01-01 --tf M1 M5 M15 M30 --symbols EURUSD XAUUSD
"""
import argparse
import json
from datetime import datetime, timezone

from _common import setup_logging

from bot.config import load_settings
from bot.mt5_client import MT5Client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2019-01-01")
    ap.add_argument("--to", dest="end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--tf", nargs="*", default=["H1", "H4", "D1"])
    args = ap.parse_args()
    setup_logging()

    s = load_settings()
    names = args.symbols or [x.name for x in s.symbols]
    start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    data_dir = s.path(s.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    mt5c = MT5Client(s.mt5)
    mt5c.connect()
    try:
        offset = s.session.server_utc_offset_hours
        if offset is None:
            offset = mt5c.server_utc_offset(s.broker_symbol(names[0]))
        for name in names:
            bsym = s.broker_symbol(name)
            meta = mt5c.symbol_meta(bsym)
            meta["server_utc_offset"] = offset
            for tf in args.tf:
                df = mt5c.rates_range(bsym, tf, start, end)
                if df.empty:   # rentang melebihi 'Max bars in chart': ambil bar terbanyak yang tersedia
                    df = mt5c.rates(bsym, tf, 99_000)
                    df = df[df["time"] >= start]
                df.to_csv(data_dir / f"{name}_{tf}.csv", index=False)
                if df.empty:
                    print(f"{name} {tf}: TIDAK ADA DATA")
                    continue
                print(f"{name} {tf}: {len(df)} bar, {df['time'].iloc[0]} s/d {df['time'].iloc[-1]}")
                if (df["time"].iloc[0] - start).days > 7:
                    print(f"  ! Data mulai lebih lambat dari {args.start}. Naikkan 'Max bars in chart' di "
                          f"MT5 (Tools > Options > Charts) lalu jalankan ulang.")
            (data_dir / f"{name}_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"Offset server: {offset if offset is not None else 'tidak terdeteksi (pasar tutup)'}")
    finally:
        mt5c.shutdown()


if __name__ == "__main__":
    main()
