"""End-of-window favorite test: buy the side priced > 0.5 at the trade price in the last N seconds.
Market-clustered bootstrap se, fee V2, optional 1c slippage. Usage: python endgame.py <datadir> [...]
"""
import sys, numpy as np, pandas as pd
from analyze import load, FEE_RATE

m, t = load(sys.argv[1:])
t = t[(t.tau >= 0) & (t.tau <= 300)].copy()
t["fav_price"] = np.where(t.p_up >= 0.5, t.p_up, 1 - t.p_up).astype("float32")
t["fav_win"] = np.where(t.p_up >= 0.5, t.up, 1 - t.up).astype("int8")
t["pnl"] = (t.fav_win - t.fav_price - FEE_RATE * t.fav_price * (1 - t.fav_price)).astype("float32")
rng = np.random.default_rng(0)
ALL = t.ts.unique(); M = len(ALL); mi = {x: i for i, x in enumerate(ALL)}

def cl_mean(d, col):
    """market-clustered bootstrap of mean(col) over trades (equal-weight per trade)."""
    g = d.groupby("ts")[col].agg(["sum", "size"])
    s = np.zeros(M); n = np.zeros(M); idx = g.index.map(mi).values
    s[idx] = g["sum"]; n[idx] = g["size"]
    W = rng.multinomial(M, np.ones(M) / M, size=300)
    b = (W @ s) / np.maximum(W @ n, 1)
    return d[col].mean(), b.std()

pd.set_option("display.width", 200)
for tmax in [15, 30, 60]:
    d = t[t.tau < tmax]
    d = d.assign(fb=pd.cut(d.fav_price, [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0001], right=False))
    rows = []
    for fb, g in d.groupby("fb", observed=True):
        wr, se_wr = cl_mean(g, "fav_win"); pnl, se_pnl = cl_mean(g, "pnl")
        rows.append(dict(tau_lt=tmax, fav_bucket=str(fb), n_mkts=g.ts.nunique(), n=len(g), price=g.fav_price.mean(),
                         win=wr, se_win=se_wr, mis=wr - g.fav_price.mean(), fee=(FEE_RATE * g.fav_price * (1 - g.fav_price)).mean(),
                         pnl_net=pnl, se_pnl=se_pnl, pnl_net_1c_slip=pnl - 0.01, z=pnl / max(se_pnl, 1e-9)))
    print(pd.DataFrame(rows).round(4).to_string(index=False)); print()

# one bet per market: first trade with tau<30 and favorite in [0.75,0.92]
d = t[(t.tau < 30) & (t.fav_price >= 0.75) & (t.fav_price <= 0.92)].sort_values("t").groupby("ts").first()
print(f"one bet/market, tau<30, fav 75-92c: {len(d)} markets ({len(d)/len(m):.1%} of all), "
      f"win {d.fav_win.mean():.3f} vs price {d.fav_price.mean():.3f}, pnl net {d.pnl.mean():+.4f} "
      f"(se {d.pnl.std()/np.sqrt(len(d)):.4f}), with 1c slip {d.pnl.mean()-0.01:+.4f}")
# by week, to see stability
d = d.assign(week=pd.to_datetime(d.index, unit="s").to_period("W").astype(str))
print(d.groupby("week").agg(n=("pnl", "size"), win=("fav_win", "mean"), price=("fav_price", "mean"), pnl_net=("pnl", "mean")).round(4).to_string())
