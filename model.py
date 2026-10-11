"""TWAP-aware Black-Scholes model for BTC 5-min Up/Down vs market trade prices.
Usage: python model.py <datadir> [...]
Needs btc_raw/BTCUSDT-1s-YYYY-MM-DD.zip covering the trade dates.
"""
import glob, io, sys, zipfile
import numpy as np, pandas as pd
from scipy.stats import norm
from analyze import load, FEE_RATE

TWAP = 60   # seconds of TWAP window for both open and close

def load_klines(days):
    frames = []
    for d in days:
        fs = glob.glob(f"btc_raw/BTCUSDT-1s-{d}.zip")
        if not fs or not zipfile.is_zipfile(fs[0]): continue
        z = zipfile.ZipFile(fs[0]); name = z.namelist()[0]
        k = pd.read_csv(io.BytesIO(z.read(name)), header=None, usecols=[0, 4], names=["t", "close"])
        k["t"] = (k.t // 1_000_000).astype("int64")  # microseconds -> seconds
        frames.append(k)
    k = pd.concat(frames).drop_duplicates("t").set_index("t").sort_index()
    full = pd.RangeIndex(k.index.min(), k.index.max() + 1)
    return k.reindex(full).ffill()["close"]

def realized_sigma(close, W):
    r = np.log(close).diff()
    v = (r ** 2).rolling(W, min_periods=W // 2).mean()
    return np.sqrt(v)  # per-second vol

def model_prob(S, K, sigma, tau, known_avg):
    """P(TWAP_end >= K). known_avg: mean of prices already inside the closing TWAP window (tau<60)."""
    tau = np.asarray(tau, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        far = tau >= TWAP
        mean = np.where(far, np.log(S / K), np.log(((TWAP - tau) * known_avg + tau * S) / TWAP / K))
        sd = np.where(far, sigma * np.sqrt(np.maximum(tau - TWAP + TWAP / 3, 1e-9)),
                      sigma * np.sqrt(tau ** 3 / 3) / TWAP)
        z = mean / sd
    p = norm.cdf(z)
    p = np.where(tau <= 0, (mean >= 0).astype(float), p)
    return np.clip(p, 1e-6, 1 - 1e-6)

def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6); return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))

if __name__ == "__main__":
    LAG = 5
    args = []
    for a in sys.argv[1:]:
        if a.startswith("--lag="): LAG = int(a.split("=")[1])
        else: args.append(a)
    m, t = load(args)
    t = t[(t.tau >= 0) & (t.tau <= 300)].copy()
    days = sorted(set(pd.to_datetime(t.t, unit="s").dt.strftime("%Y-%m-%d")))
    close = load_klines(days)
    t = t[(t.t >= close.index.min() + 3600) & (t.t <= close.index.max())].copy()
    m = m[m.ts.isin(t.ts.unique())].copy()
    print(f"markets {len(m)}, trades {len(t)}, klines {close.index.min()}..{close.index.max()}")

    # Binance proxy for open TWAP (K) and close TWAP (A); check proxy agrees with actual outcome
    cs = close.cumsum()
    def twap_end(ts_end):  # mean of close over (ts_end-60, ts_end]
        return (cs.reindex(ts_end).values - cs.reindex(ts_end - TWAP).values) / TWAP
    m["K"] = twap_end(m.ts.values); m["A"] = twap_end((m.ts + 300).values)
    m["up_proxy"] = (m.A >= m.K).astype(int)
    agree = (m.up_proxy == m.up).mean()
    print(f"Binance TWAP proxy agrees with Chainlink outcome in {agree:.1%} of markets "
          f"(|A-K| median {np.nanmedian(np.abs(m.A-m.K)):.1f} USD)")
    dis = m[m.up_proxy != m.up]
    print(f"  disagreements: {len(dis)}, median |A-K| there {np.median(np.abs(dis.A-dis.K)) if len(dis) else float('nan'):.1f} USD")

    t = t.merge(m[["ts", "K"]], on="ts")
    tt = t.t.values - LAG   # Binance info available LAG seconds before the on-chain timestamp
    t["S"] = close.reindex(tt).values
    end = t.ts.values + 300
    print(f"using Binance lag {LAG}s (trade timestamps are on-chain, ~4s after the match)")
    # known part of the closing TWAP at trade time: mean close over (end-60, t]
    t["tau_eff"] = end - tt
    n_known = np.clip(TWAP - t.tau_eff.values, 0, TWAP)
    known_sum = cs.reindex(tt).values - cs.reindex(end - TWAP).values
    t["known_avg"] = np.where(n_known > 0, known_sum / np.maximum(n_known, 1), t.S)
    t["known_avg"] = t.known_avg.fillna(t.S)

    y = t.up.values; pm = t.p_up.values
    print(f"\nmarket log-loss {logloss(pm, y):.4f}  brier {np.mean((pm-y)**2):.4f}")
    res = {}
    for W in [60, 300, 900, 1800, 3600]:
        sig = realized_sigma(close, W).reindex(tt).values
        p = model_prob(t.S.values, t.K.values, sig, t.tau_eff.values, t.known_avg.values)
        res[W] = p
        print(f"model W={W:5d}s  log-loss {logloss(p, y):.4f}  brier {np.mean((p-y)**2):.4f}  "
              f"corr(model,market) {np.corrcoef(p, pm)[0,1]:.3f}")
    # blend check: does the model add information on top of the market?
    W = 900; p = res[W]; t["p_model"] = p
    for w in [0.25, 0.5]:
        print(f"blend {w:.2f}*model + {1-w:.2f}*market  log-loss {logloss(w*p+(1-w)*pm, y):.4f}")

    # calibration of model vs market, by model bucket
    t["mb"] = pd.cut(t.p_model, np.arange(0, 1.0001, 0.1), right=False)
    g = t.groupby("mb", observed=True)
    print("\n=== model calibration (W=900) ===")
    print(pd.DataFrame({"n": g.size(), "n_mkts": g.ts.nunique(), "model": g.p_model.mean(),
                        "market": g.p_up.mean(), "freq": g.up.mean()}).round(4).to_string())

    # value-betting simulation: take the trade price when model disagrees by > theta
    print("\n=== value betting at trade price (taker, fee V2), by threshold ===")
    tb = pd.cut(t.tau, [0, 30, 60, 120, 180, 240, 301], right=False)
    rows = []
    for theta in [0.02, 0.05, 0.10, 0.20]:
        for slip in [0.0, 0.01]:
            buy_up = (t.p_model - t.p_up) > theta
            buy_dn = (t.p_up - t.p_model) > theta
            sel = buy_up | buy_dn
            price = np.where(buy_up, t.p_up, 1 - t.p_up) + slip
            win = np.where(buy_up, t.up, 1 - t.up)
            fee = FEE_RATE * price * (1 - price)
            pnl = win - price - fee
            d = pd.DataFrame({"pnl": pnl, "sel": sel, "ts": t.ts, "tb": tb, "size": t["size"]})[sel]
            first = d.groupby("ts").first()   # one bet per market: first signal
            rows.append(dict(theta=theta, slip=slip, n_signals=int(sel.sum()), n_mkts=d.ts.nunique(),
                             pnl_per_share_all=d.pnl.mean(), pnl_first_per_mkt=first.pnl.mean(),
                             se_first=first.pnl.std() / np.sqrt(max(len(first), 1)),
                             share_of_mkts=d.ts.nunique() / len(m)))
    print(pd.DataFrame(rows).round(4).to_string(index=False))

    theta = 0.05
    buy_up = (t.p_model - t.p_up) > theta; buy_dn = (t.p_up - t.p_model) > theta; sel = buy_up | buy_dn
    price = np.where(buy_up, t.p_up, 1 - t.p_up); win = np.where(buy_up, t.up, 1 - t.up)
    pnl = win - price - FEE_RATE * price * (1 - price)
    d = pd.DataFrame({"pnl": pnl, "tb": tb, "ts": t.ts})[sel]
    print(f"\n=== theta=0.05, no slippage, by time remaining ===")
    g = d.groupby("tb", observed=True)
    print(pd.DataFrame({"n": g.size(), "n_mkts": g.ts.nunique(), "pnl_per_share": g.pnl.mean(),
                        "se_naive": g.pnl.std() / np.sqrt(g.size())}).round(4).to_string())
