"""Does the (lagged) model add information to the market price? Logistic regression
y ~ a*logit(market) + b*logit(model), fit on first half of markets, scored on second half.
Usage: python incremental.py <datadir> [...]  [--lags 5,10]
"""
import sys, numpy as np, pandas as pd
from analyze import load, FEE_RATE
from model import load_klines, realized_sigma, model_prob, logloss, TWAP

def logit(p): p = np.clip(p, 1e-4, 1 - 1e-4); return np.log(p / (1 - p))

def fit_logreg(X, y, iters=50):
    X = np.column_stack([np.ones(len(X)), X]); w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w)); W = p * (1 - p)
        H = X.T @ (X * W[:, None]) + 1e-6 * np.eye(X.shape[1]); g = X.T @ (y - p)
        w += np.linalg.solve(H, g)
    return w

def predict(w, X): X = np.column_stack([np.ones(len(X)), X]); return 1 / (1 + np.exp(-X @ w))

args = [a for a in sys.argv[1:] if not a.startswith("--")]
lags = [5, 10]
for a in sys.argv[1:]:
    if a.startswith("--lags="): lags = [int(x) for x in a.split("=")[1].split(",")]
m, t = load(args)
t = t[(t.tau >= 0) & (t.tau <= 300)].copy()
days = sorted(set(pd.to_datetime(t.t, unit="s").dt.strftime("%Y-%m-%d")))
close = load_klines(days)
t = t[(t.t >= close.index.min() + 3600) & (t.t <= close.index.max())].copy()
m = m[m.ts.isin(t.ts.unique())].copy()
cs = close.cumsum()
m["K"] = (cs.reindex(m.ts.values).values - cs.reindex(m.ts.values - TWAP).values) / TWAP
t = t.merge(m[["ts", "K"]], on="ts")
sig_all = realized_sigma(close, 3600)
# subsample: one trade per market per 10-second slot to reduce within-market redundancy
t["slot"] = (t.t // 10); t = t.groupby(["ts", "slot"]).first().reset_index()
split = np.quantile(t.ts.unique(), 0.5)
tr, te = t.ts < split, t.ts >= split
y = t.up.values; pm = t.p_up.values
print(f"markets {len(m)}, sampled trades {len(t)}; train markets {t[tr].ts.nunique()}, test {t[te].ts.nunique()}")
w0 = fit_logreg(logit(pm)[tr, None], y[tr])
print(f"market alone: test log-loss raw {logloss(pm[te], y[te]):.4f}, recalibrated {logloss(predict(w0, logit(pm)[te, None]), y[te]):.4f}  (a={w0[1]:.3f})")
for L in lags:
    tt = t.t.values - L; S = close.reindex(tt).values; end = t.ts.values + 300
    n_known = np.clip(TWAP - (end - tt), 0, TWAP)
    known_avg = np.where(n_known > 0, (cs.reindex(tt).values - cs.reindex(end - TWAP).values) / np.maximum(n_known, 1), S)
    p = model_prob(S, t.K.values, sig_all.reindex(tt).values, end - tt, known_avg)
    X = np.column_stack([logit(pm), logit(p)])
    w = fit_logreg(X[tr], y[tr]); pt = predict(w, X[te])
    w1 = fit_logreg(logit(p)[tr, None], y[tr])
    print(f"L={L:2d}s  model alone recal {logloss(predict(w1, logit(p)[te, None]), y[te]):.4f} | "
          f"market+model: test log-loss {logloss(pt, y[te]):.4f}  coef market a={w[1]:.3f} model b={w[2]:.3f}")
    # trade the combined estimate on the test set, taker at trade price, fee V2
    for theta in [0.03, 0.05, 0.10]:
        bu = (pt - pm[te]) > theta; bd = (pm[te] - pt) > theta; sel = bu | bd
        price = np.where(bu, pm[te], 1 - pm[te]); win = np.where(bu, y[te], 1 - y[te])
        pnl = win - price - FEE_RATE * price * (1 - price)
        d = pd.DataFrame({"pnl": pnl, "ts": t.ts.values[te]})[sel]
        f = d.groupby("ts").first()
        print(f"       theta={theta:.2f}: {len(d):6d} signals in {len(f):4d} mkts, pnl/share all {d.pnl.mean():+.4f}, "
              f"first-per-mkt {f.pnl.mean():+.4f} (se {f.pnl.std()/np.sqrt(max(len(f),1)):.4f}), with 1c slip {f.pnl.mean()-0.01:+.4f}")
