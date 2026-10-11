"""Record live order books of the current BTC 5-min market (Up and Down tokens) + Coinbase spot, ~1 Hz.
Usage: python record_book.py <minutes> <outfile.jsonl>
"""
import json, sys, time, requests
S = requests.Session()
def get(url, params=None):
    try:
        r = S.get(url, params=params, timeout=5); return r.json() if r.status_code == 200 else None
    except Exception: return None
def tokens(ts):
    ev = get("https://gamma-api.polymarket.com/events", {"slug": f"btc-updown-5m-{ts}"})
    if not ev: return None
    return json.loads(ev[0]["markets"][0]["clobTokenIds"])
def top(book, n=5):
    if not book: return None
    bids = sorted(book["bids"], key=lambda x: -float(x["price"]))[:n]
    asks = sorted(book["asks"], key=lambda x: float(x["price"]))[:n]
    return {"bids": [[float(x["price"]), float(x["size"])] for x in bids],
            "asks": [[float(x["price"]), float(x["size"])] for x in asks],
            "last": book.get("last_trade_price"), "bt": book.get("timestamp")}
minutes, out = float(sys.argv[1]), sys.argv[2]
end = time.time() + minutes * 60
cur, toks = None, None
with open(out, "a") as f:
    while time.time() < end:
        t0 = time.time(); ts = int(t0) // 300 * 300
        if ts != cur:
            toks = tokens(ts); cur = ts if toks else None
        if toks:
            up = get("https://clob.polymarket.com/book", {"token_id": toks[0]})
            dn = get("https://clob.polymarket.com/book", {"token_id": toks[1]})
            cb = get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            f.write(json.dumps({"t": round(t0, 3), "ts": ts, "up": top(up), "dn": top(dn),
                                "cb": float(cb["price"]) if cb and "price" in cb else None}) + "\n"); f.flush()
        time.sleep(max(0, 1 - (time.time() - t0)))
