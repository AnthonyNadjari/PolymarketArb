"""Look-ahead check: value-betting PnL vs Binance price lag, and market/Binance lead-lag.
Usage: python lag.py <datadir> [...]
"""
import sys, numpy as np, pandas as pd
from analyze import load, FEE_RATE
from model import load_klines, realized_sigma, model_prob, logloss, TWAP

m, t = load(sys.argv[1:])
t = t[(t.tau >= 0) & (t.tau <= 300)].copy()
days = sorted(set(pd.to_datetime(t.t, unit="s").dt.strftime("%Y-%m-%d")))
close = load_klines(days)
t = t[(t.t >= close.index.min() + 3600) & (t.t <= close.index.max())].copy()
m = m[m.ts.isin(t.ts.unique())].copy()
cs = close.cumsum()
m["K"] = (cs.reindex(m.ts.values).values - cs.reindex(m.ts.values - TWAP).values) / TWAP
t = t.merge(m[["ts", "K"]], on="ts")
sig_all = realized_sigma(close, 3600)
y = t.up.values; pm = t.p_up.values
print(f"markets {len(m)}, trades {len(t)}; market log-loss {logloss(pm, y):.4f}")

print("\n=== value betting PnL (per share, first signal per market, no slippage) vs Binance lag L ===")
rows = []
for L in [0, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45]:
    tt = t.t.values - L                      # what Binance showed L seconds before the trade timestamp
    S = close.reindex(tt).values
    end = t.ts.values + 300
    n_known = np.clip(TWAP - (end - tt), 0, TWAP)    # closing-window seconds already observed at tt
    known_sum = cs.reindex(tt).values - cs.reindex(end - TWAP).values
    known_avg = np.where(n_known > 0, known_sum / np.maximum(n_known, 1), S)
    tau_eff = end - tt                         # time remaining as seen at tt
    sig = sig_all.reindex(tt).values
    p = model_prob(S, t.K.values, sig, tau_eff, known_avg)
    row = dict(L=L, model_logloss=logloss(p, y), blend25_logloss=logloss(0.25 * p + 0.75 * pm, y))
    for theta in [0.05, 0.10, 0.20]:
        bu = (p - pm) > theta; bd = (pm - p) > theta; sel = bu | bd
        price = np.where(bu, pm, 1 - pm); win = np.where(bu, y, 1 - y)
        pnl = win - price - FEE_RATE * price * (1 - price)
        d = pd.DataFrame({"pnl": pnl, "ts": t.ts.values})[sel].groupby("ts").first()
        row[f"pnl_th{theta}"] = d.pnl.mean(); row[f"n_th{theta}"] = len(d)
    rows.append(row)
print(pd.DataFrame(rows).round(4).to_string(index=False))

print("\n=== lead-lag: corr(d market price(t), d Binance(t - lag)), pooled 1s series ===")
# per market 1s last-trade Up price, ffilled, vs Binance close
dpm, dS = {}, {}
for lag in range(-15, 31):
    dpm[lag] = []; dS[lag] = []
agg = []
for ts, g in t.groupby("ts"):
    s = g.groupby("t").p_up.last().reindex(range(ts, ts + 301)).ffill()
    if s.notna().sum() < 200: continue
    dp = s.diff().values
    b = close.reindex(range(ts - 60, ts + 301)).values
    db = np.diff(np.log(b))  # db[i] = change from (ts-60+i) to (ts-60+i+1)
    for lag in range(-15, 31):
        # market change over (t-1,t] vs binance change over (t-1-lag, t-lag]
        idx = np.arange(1, 301)                      # t = ts + idx
        bi = idx + 60 - 1 - lag                      # index into db for interval ending at t-lag
        ok = (bi >= 0) & (bi < len(db))
        x = dp[idx[ok]]; z = db[bi[ok]]
        good = ~np.isnan(x) & ~np.isnan(z)
        agg.append((lag, x[good], z[good]))
res = {}
for lag, x, z in agg:
    res.setdefault(lag, [[], []]); res[lag][0].append(x); res[lag][1].append(z)
out = []
for lag in sorted(res):
    x = np.concatenate(res[lag][0]); z = np.concatenate(res[lag][1])
    out.append(dict(lag=lag, corr=np.corrcoef(x, z)[0, 1], n=len(x)))
o = pd.DataFrame(out); print(o.round(4).to_string(index=False))
print("peak lag:", int(o.loc[o["corr"].idxmax(), "lag"]), "s  (positive = market timestamps lag Binance)")
