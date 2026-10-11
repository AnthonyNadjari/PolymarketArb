"""Market calibration of Polymarket BTC 5-min Up/Down trade prices.
Usage: python analyze.py <datadir> [<datadir2> ...]
"""
import json, sys
import numpy as np, pandas as pd

FEE_RATE = 0.07  # V2 crypto fee: taker pays rate * p * (1-p) per share

def load(dirs):
    ms, ts = [], []
    for d in dirs:
        ms += [json.loads(l) for l in open(f"{d}/markets.jsonl")]
        ts.append(pd.read_csv(f"{d}/trades.csv", dtype={"ts": "int64", "t": "int64", "side": "category",
                              "outcomeIndex": "int8", "price": "float32", "size": "float32"}))
    m = pd.DataFrame(ms); m = m[(~m.missing) & m.up.notna()]
    t = pd.concat(ts, ignore_index=True)
    m["up"] = m.up.astype("int8")
    t = t.merge(m[["ts", "up", "feeType"]], on="ts")
    t["feeType"] = t.feeType.astype("category")
    t["p_up"] = np.where(t.outcomeIndex == 0, t.price, 1 - t.price).astype("float32")
    t["tau"] = (t.ts + 300 - t.t).astype("int16")         # seconds remaining at trade time
    t["pnl_up"] = t.up - t.p_up           # buy Up at trade price, no fee
    return m, t

def table(t, pbins, taubins):
    t = t.copy()
    t["pb"] = pd.cut(t.p_up, pbins, right=False)
    t["tb"] = pd.cut(t.tau, taubins, right=False)
    g = t.groupby(["tb", "pb"], observed=True)
    out = g.agg(n=("p_up", "size"), vol=("size", "sum"), price=("p_up", "mean"),
                freq=("up", "mean")).reset_index()
    vw = g.apply(lambda x: np.average(x.up, weights=x["size"]), include_groups=False).rename("freq_vw")
    out = out.merge(vw.reset_index(), on=["tb", "pb"])
    out["se"] = np.sqrt(out.price * (1 - out.price) / out.n)
    out["fee"] = FEE_RATE * out.price * (1 - out.price)
    out["mis"] = out.freq - out.price                     # >0: Up underpriced
    out["edge_best_side"] = out.mis.abs() - out.fee        # taker edge after fee, best side
    return out

if __name__ == "__main__":
    m, t = load(sys.argv[1:])
    print(f"markets: {len(m)}  up-rate: {m.up.mean():.3f}  trades: {len(t)}  volume: {t['size'].sum():,.0f} shares")
    print("fee types:", m.feeType.value_counts().to_dict())
    inwin = t[(t.tau >= 0) & (t.tau <= 300)]
    print(f"trades inside window: {len(inwin)} ({len(inwin)/len(t):.1%}); before start: {(t.tau>300).sum()}, after end: {(t.tau<0).sum()}")
    t = inwin
    pd.set_option("display.width", 200); pd.set_option("display.max_rows", 500)

    print("\n=== Calibration by Up-price bucket (all times in window) ===")
    pbins = np.arange(0, 1.0001, 0.05)
    tb = table(t, pbins, [0, 301])
    print(tb[["pb", "n", "vol", "price", "freq", "freq_vw", "se", "fee", "mis", "edge_best_side"]].round(4).to_string(index=False))

    print("\n=== Calibration by time remaining x price (10c buckets) ===")
    pbins = np.arange(0, 1.0001, 0.10)
    taubins = [0, 30, 60, 120, 180, 240, 301]
    tb2 = table(t, pbins, taubins)
    print(tb2[["tb", "pb", "n", "vol", "price", "freq", "freq_vw", "se", "fee", "mis", "edge_best_side"]].round(4).to_string(index=False))

    print("\n=== Naive taker PnL: buy the traded side at trade price, per share, by time bucket ===")
    t = t.copy(); t["tb"] = pd.cut(t.tau, taubins, right=False)
    # pnl of the taker side of each trade (side BUY = taker bought the outcome)
    bought_up = (t.side == "BUY") == (t.outcomeIndex == 0)
    p_side = np.where(bought_up, t.p_up, 1 - t.p_up)
    win = np.where(bought_up, t.up, 1 - t.up)
    t["pnl_taker"] = win - p_side
    t["fee"] = FEE_RATE * p_side * (1 - p_side)
    t["pnl_taker_net"] = t.pnl_taker - t.fee
    g = t.groupby("tb", observed=True)
    print(pd.DataFrame({
        "n": g.size(), "vol": g["size"].sum(),
        "pnl_gross_per_share": g.apply(lambda x: np.average(x.pnl_taker, weights=x["size"]), include_groups=False),
        "pnl_net_per_share": g.apply(lambda x: np.average(x.pnl_taker_net, weights=x["size"]), include_groups=False),
    }).round(4).to_string())
    tb.to_csv("calib_price.csv", index=False); tb2.to_csv("calib_price_tau.csv", index=False)
