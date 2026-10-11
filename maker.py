"""Maker-side economics from historical trades: the maker is the counterparty of each taker trade.
Maker gross PnL per share to expiry = -(taker PnL) = price_taker_paid - taker_win ... computed per trade.
Usage: python maker.py <datadir> [...]
"""
import sys, numpy as np, pandas as pd
from analyze import load, FEE_RATE

m, t = load(sys.argv[1:])
t = t[(t.tau >= 0) & (t.tau <= 300)].copy()
# taker bought outcome `outcomeIndex` if side==BUY, else sold it. Taker long Up iff (BUY and Up) or (SELL and Down).
taker_long_up = ((t.side == "BUY") == (t.outcomeIndex == 0)).values
p_side = np.where(taker_long_up, t.p_up, 1 - t.p_up).astype("float32")   # price taker paid for the side he is long
win = np.where(taker_long_up, t.up, 1 - t.up).astype("int8")
t["taker_pnl"] = (win - p_side).astype("float32")
t["maker_pnl"] = -t.taker_pnl
t["fee"] = (FEE_RATE * p_side * (1 - p_side)).astype("float32")  # paid by taker; rebate pool for makers
t["usd"] = (t["size"] * p_side).astype("float32")
rng = np.random.default_rng(0)
ALL = t.ts.unique(); M = len(ALL); mi = {x: i for i, x in enumerate(ALL)}
def cl(d, col, w="size"):
    g = d.assign(wx=d[col] * d[w]).groupby("ts").agg(s=("wx", "sum"), n=(w, "sum"))
    s = np.zeros(M); n = np.zeros(M); idx = g.index.map(mi).values; s[idx] = g.s; n[idx] = g.n
    W = rng.multinomial(M, np.ones(M) / M, size=300); b = (W @ s) / np.maximum(W @ n, 1e-9)
    return np.average(d[col], weights=d[w]), b.std()
pd.set_option("display.width", 200)
print(f"markets {len(m)}, trades {len(t)}, shares {t['size'].sum():,.0f}, notional ${t.usd.sum():,.0f}")
mp, se = cl(t, "maker_pnl"); fee = np.average(t.fee, weights=t["size"])
print(f"\nALL: maker gross PnL per share (vol-weighted) {mp*100:+.2f}c (se {se*100:.2f}c); taker fee per share {fee*100:.2f}c "
      f"-> maker net with rebate = {mp*100+0.2*fee*100:+.2f}c (20% rebate) .. {mp*100+fee*100:+.2f}c (100% rebate)")
print(f"     taker net per share: {-mp*100-fee*100:+.2f}c")

def table(keys, label):
    rows = []
    for k, g in t.groupby(keys, observed=True):
        mp, se = cl(g, "maker_pnl"); fee = np.average(g.fee, weights=g["size"])
        rows.append({label: str(k), "n": len(g), "shares": g["size"].sum(), "maker_gross_c": mp * 100, "se_c": se * 100,
                     "fee_c": fee * 100, "maker_net_20pct_c": mp * 100 + 0.2 * fee * 100, "maker_net_100pct_c": mp * 100 + fee * 100})
    print(pd.DataFrame(rows).round(2).to_string(index=False))

print("\n=== by time remaining ==="); t["tb"] = pd.cut(t.tau, [0, 30, 60, 120, 180, 240, 301], right=False); table("tb", "tau")
print("\n=== by taker trade size (shares) ==="); t["sb"] = pd.cut(t["size"], [0, 20, 100, 500, 2000, 1e9]); table("sb", "size")
print("\n=== by price of the side the taker bought ==="); t["pb"] = pd.cut(pd.Series(p_side), [0, .1, .3, .5, .7, .9, 1.0001]).values; table("pb", "taker_price")
print("\n=== by taker side ==="); t["dir"] = np.where(t.side.values == "BUY", "taker BUY", "taker SELL"); table("dir", "side")
print("\n=== size x time (last 60s vs before) ===")
t["late"] = np.where(t.tau < 60, "tau<60", "tau>=60"); table(["late", "sb"], "late_size")
