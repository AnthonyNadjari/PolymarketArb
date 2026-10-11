"""Collect resolved Polymarket BTC 5-min Up/Down markets and their trades.

Usage: python collect.py <start_unix> <end_unix> <outdir>
Writes <outdir>/markets.jsonl and <outdir>/trades.csv (append-safe, skips done markets).
"""
import csv, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

GAMMA = "https://gamma-api.polymarket.com/events"
DATA = "https://data-api.polymarket.com/trades"
S = requests.Session()
S.headers["User-Agent"] = "pm-calib-research/0.1"

def get(url, params, tries=6):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1)); continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None

def fetch_market(ts):
    ev = get(GAMMA, {"slug": f"btc-updown-5m-{ts}"})
    if not ev:
        return {"ts": ts, "missing": True}
    m = ev[0]["markets"][0]
    try:
        op = json.loads(m.get("outcomePrices") or "[]")
    except Exception:
        op = []
    return {
        "ts": ts, "missing": False,
        "conditionId": m["conditionId"],
        "status": m.get("umaResolutionStatus"),
        "outcomePrices": op,
        "up": (1 if op and float(op[0]) == 1.0 else (0 if op and float(op[1]) == 1.0 else None)),
        "volume": m.get("volumeNum") or m.get("volume"),
        "feeType": m.get("feeType"), "feeSchedule": m.get("feeSchedule"),
        "description": (m.get("description") or "")[:120],
    }

def fetch_trades(cid):
    out, off = [], 0
    while True:
        d = get(DATA, {"market": cid, "limit": 1000, "offset": off})
        if d is None:
            return None
        out.extend(d)
        if len(d) < 1000:
            return out
        off += 1000
        if off > 20000:
            return out

def main():
    start, end, outdir = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    os.makedirs(outdir, exist_ok=True)
    mpath, tpath = f"{outdir}/markets.jsonl", f"{outdir}/trades.csv"
    done = set()
    if os.path.exists(mpath):
        for line in open(mpath):
            done.add(json.loads(line)["ts"])
    todo = [ts for ts in range(start, end, 300) if ts not in done]
    print(f"{len(todo)} markets to fetch ({len(done)} already done)", flush=True)
    new_t = not os.path.exists(tpath)
    mf = open(mpath, "a"); tf = open(tpath, "a", newline="")
    tw = csv.writer(tf)
    if new_t:
        tw.writerow(["ts", "t", "side", "outcomeIndex", "price", "size"])
    n_ok = n_tr = 0
    def job(ts):
        m = fetch_market(ts)
        trades = None
        if not m["missing"] and m["up"] is not None:
            trades = fetch_trades(m["conditionId"])
        return m, trades
    CHUNK = 120
    i = -1
    for c0 in range(0, len(todo), CHUNK):
      with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(job, ts) for ts in todo[c0:c0 + CHUNK]]
        for f in as_completed(futs):
            i += 1
            m, trades = f.result()
            f._result = None  # drop reference so memory is freed
            if trades is None and not m["missing"] and m["up"] is not None:
                continue  # trades failed; leave for a retry run
            m["n_trades"] = len(trades) if trades else 0
            mf.write(json.dumps(m) + "\n")
            if trades:
                for t in trades:
                    tw.writerow([m["ts"], t["timestamp"], t["side"], t["outcomeIndex"], t["price"], t["size"]])
                n_tr += len(trades)
            n_ok += 1
            if i % 100 == 0:
                mf.flush(); tf.flush()
                print(f"{i}/{len(todo)} markets, {n_tr} trades", flush=True)
    mf.close(); tf.close()
    print(f"done: {n_ok} markets, {n_tr} trades", flush=True)

if __name__ == "__main__":
    main()
