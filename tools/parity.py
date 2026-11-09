"""Phase-0 gate: the bot's signal code must reproduce the backtest's trades exactly.

Usage: python tools/parity.py <dir with 1H CSVs: ts,open,high,low,close,vol> [ticks.json]

For every asset it runs the research backtest (tools/reference_backtest.py, with the BTC gate built in)
and replays the bot's decide() day by day on the same UTC daily candles, then compares:
  - every entry (day + side)  -> must match 100%
  - every channel exit day     -> must match 100% (stop exits happen intraday and are not daily signals)
"""
import json
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from masar.config import ASSETS, SHORTABLE, STRATEGY
from masar.signals import features, btc_regime, decide
import reference_backtest as ref

DAY = 86_400_000


def daily_from_hourly(df):
    day = (df["ts"] // DAY).astype(int)
    return df.groupby(day).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))


def main(data_dir, ticks_path=None, start="2020-07-01"):
    ticks = json.load(open(ticks_path)) if ticks_path else {}
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    raw = {a: pd.read_csv(f"{data_dir}/{a}.csv") for a in ASSETS if os.path.exists(f"{data_dir}/{a}.csv")}
    btc_d = daily_from_hourly(raw["BTC"])
    gate = btc_regime(btc_d, STRATEGY.btc_entry_n, STRATEGY.btc_exit_n).to_dict()
    total = dict(entries=0, entries_ok=0, exits=0, exits_ok=0, extra=0)
    for a, df in raw.items():
        tick = ticks.get(a, 1e-8)
        tp = ref.TP(entryN=STRATEGY.entry_n, exitN=STRATEGY.exit_n, atrN=STRATEGY.atr_n, stopATR=STRATEGY.stop_atr,
                    allowShort=a in SHORTABLE)
        trades = ref.run_trend(df, a, tp, tick, start_ts, gate=None if a == "BTC" else gate)
        ref_entries = {(t["entry_ts"] // DAY, t["dir"]) for t in trades}
        ref_ch_exits = set()
        open_days = {}
        for t in trades:
            ed, xd = t["entry_ts"] // DAY, t["exit_ts"] // DAY
            for d in range(ed, xd):
                open_days[d] = t["dir"]
            if t.get("reason") == "channel":
                ref_ch_exits.add(xd)
        f = features(daily_from_hourly(df), STRATEGY.entry_n, STRATEGY.exit_n, STRATEGY.atr_n)
        bot_entries, bot_exits = set(), set()
        first_trade_day = start_ts // DAY
        for k in range(2, len(f)):
            day = int(f.index[k])
            if day + 1 < first_trade_day:
                continue
            open_side = open_days.get(day, 0)
            dec, _ = decide(a, f.iloc[:k + 1], open_side, int(gate.get(day, 0)), True, SHORTABLE, STRATEGY.stop_atr, tick)
            if dec and dec.action == "enter":
                bot_entries.add((day + 1, dec.side))
            elif dec and dec.action == "exit":
                bot_exits.add(day + 1)
        # channel exits the bot sees for trades that were not stopped earlier
        bot_exits &= {d for d in bot_exits if open_days.get(d - 1)}
        # the last open trade in the data has no exit yet in the backtest
        last_day = int(f.index[-1]) + 1
        ok_e = len(ref_entries & bot_entries)
        ok_x = len(ref_ch_exits & bot_exits)
        extra = len(bot_entries - ref_entries) + len((bot_exits - ref_ch_exits) - {last_day})
        total["entries"] += len(ref_entries); total["entries_ok"] += ok_e
        total["exits"] += len(ref_ch_exits); total["exits_ok"] += ok_x; total["extra"] += extra
        flag = "OK " if ok_e == len(ref_entries) and ok_x == len(ref_ch_exits) and extra == 0 else "BAD"
        print(f"{flag} {a:5} entries {ok_e}/{len(ref_entries)}  channel exits {ok_x}/{len(ref_ch_exits)}  extra {extra}")
        if flag == "BAD":
            print("   missing entries:", sorted(ref_entries - bot_entries)[:5], " extra entries:", sorted(bot_entries - ref_entries)[:5])
            print("   missing exits:", sorted(ref_ch_exits - bot_exits)[:5], " extra exits:", sorted(bot_exits - ref_ch_exits)[:5])
    print(f"\nTOTAL entries {total['entries_ok']}/{total['entries']}  channel exits {total['exits_ok']}/{total['exits']}  extra {total['extra']}")
    return total["entries_ok"] == total["entries"] and total["exits_ok"] == total["exits"] and total["extra"] == 0


if __name__ == "__main__":
    ok = main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    sys.exit(0 if ok else 1)
