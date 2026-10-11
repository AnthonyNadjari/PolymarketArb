"""Calibration with market-clustered bootstrap standard errors.
Usage: python calib_boot.py <datadir> [...]
"""
import sys, numpy as np, pandas as pd
from analyze import load, FEE_RATE

def boot_table(t, pbins, taubins, B=300, seed=0):
    rng = np.random.default_rng(seed)
    t = t.copy()
    t["pb"] = pd.cut(t.p_up, pbins, right=False).cat.codes
    t["tb"] = pd.cut(t.tau, taubins, right=False).cat.codes
    t = t[(t.pb >= 0) & (t.tb >= 0)]
    # per-market, per-cell sums -> bootstrap over markets is then cheap
    cell = t.groupby(["ts", "tb", "pb"]).agg(n=("p_up", "size"), v=("size", "sum"),
            sp=("p_up", "sum"), su=("up", "sum")).reset_index()
    cell["vu"] = cell.v * t.groupby(["ts", "tb", "pb"]).apply(
        lambda x: np.average(x.up, weights=x["size"]), include_groups=False).values
    mkts = cell.ts.unique(); M = len(mkts)
    idx = {m: i for i, m in enumerate(mkts)}
    cell["mi"] = cell.ts.map(idx)
    rows = []
    for (tb, pb), g in cell.groupby(["tb", "pb"]):
        n, sp, su, v, vu = g.n.sum(), g.sp.sum(), g.su.sum(), g.v.sum(), g.vu.sum()
        price, freq, freq_vw = sp / n, su / n, vu / v
        # bootstrap over markets: resample market weights ~ Multinomial
        arr_n = np.zeros(M); arr_su = np.zeros(M); arr_v = np.zeros(M); arr_vu = np.zeros(M)
        arr_n[g.mi] = g.n; arr_su[g.mi] = g.su; arr_v[g.mi] = g.v; arr_vu[g.mi] = g.vu
        W = rng.multinomial(M, np.ones(M) / M, size=B)
        bf = (W @ arr_su) / np.maximum(W @ arr_n, 1)
        bfv = (W @ arr_vu) / np.maximum(W @ arr_v, 1e-9)
        rows.append(dict(tb=tb, pb=pb, n_mkts=len(g), n=n, vol=v, price=price, freq=freq,
                         se_cl=bf.std(), freq_vw=freq_vw, se_vw_cl=bfv.std(),
                         fee=FEE_RATE * price * (1 - price), mis=freq - price))
    out = pd.DataFrame(rows)
    out["z"] = out.mis / out.se_cl
    out["edge_best_side"] = out.mis.abs() - out.fee
    return out

if __name__ == "__main__":
    m, t = load(sys.argv[1:])
    t = t[(t.tau >= 0) & (t.tau <= 300)]
    print(f"markets {len(m)}, trades {len(t)}")
    pd.set_option("display.width", 220); pd.set_option("display.max_rows", 500)
    print("\n=== by 10c price bucket, all tau (clustered se) ===")
    a = boot_table(t, np.arange(0, 1.0001, 0.10), [0, 301])
    print(a.round(4).to_string(index=False))
    print("\n=== by tau x 10c price bucket (clustered se) ===")
    b = boot_table(t, np.arange(0, 1.0001, 0.10), [0, 30, 60, 120, 180, 240, 301])
    print(b.round(4).to_string(index=False))
    print("\ncells with |z|>2.5:")
    print(b[b.z.abs() > 2.5].round(4).to_string(index=False))
    a.to_csv("calib_boot_price.csv", index=False); b.to_csv("calib_boot_price_tau.csv", index=False)
