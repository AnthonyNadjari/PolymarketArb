"""Paper-trading market maker for Polymarket "Bitcoin Up or Down - 5 min" markets.

No real orders are sent. The bot listens to the live Polymarket order book and trade feed and to the
Coinbase BTC-USD ticker, keeps simulated two-sided quotes on the Up token, and credits a fill only when a
real taker trade prints at (or through) our price, for the part of the trade that exceeds the displayed
size already resting at that level (we assume we are last in the queue). Positions are held to expiry and
settled with the official outcome from the Gamma API. A naive twin strategy that never pulls its quotes is
tracked alongside to show the value of dodging fast moves.

Usage: python mm_paper.py [--port 8787] [--state state.json] [--size 50] [--max-inv 200]
Dashboard: http://localhost:8787/
"""
import argparse, asyncio, json, os, threading, time, traceback
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import requests, websockets

GAMMA = "https://gamma-api.polymarket.com/events"
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS = "wss://ws-feed.exchange.coinbase.com"
FEE_RATE, REBATE_SHARE = 0.07, 0.20          # taker fee = 0.07*p*(1-p) per share; makers get 20% of it back
TICK = 0.01

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8787)
ap.add_argument("--state", default="state.json")
ap.add_argument("--size", type=float, default=50, help="shares per quote side")
ap.add_argument("--max-inv", type=float, default=200, help="max net inventory (shares) per market")
ap.add_argument("--pull-move", type=float, default=0.0001, help="pull quotes if |BTC move over 2s| exceeds this fraction (1 bp)")
ap.add_argument("--pull-ticks", type=int, default=2, help="pull quotes if the Polymarket mid moved this many ticks in 2s")
ap.add_argument("--no-quote-last", type=float, default=10, help="stop quoting in the last N seconds of the window")
ap.add_argument("--pull-secs", type=float, default=3.0, help="stay out this many seconds after a fast move")
ap.add_argument("--bankroll", type=float, default=1000.0)
ARGS = ap.parse_args()


class Strategy:
    """One simulated maker. `dodge=True` pulls quotes after fast BTC moves."""

    def __init__(self, name, dodge):
        self.name, self.dodge = name, dodge
        self.reset_market()
        self.fills = []            # all fills (dicts)
        self.market_rows = []      # settled markets
        self.pnl_gross = self.rebate = 0.0
        self.toxic = self.fills_n = 0
        self.curve = [(time.time(), 0.0)]

    def reset_market(self):
        self.inv = 0.0             # net Up shares (negative = short Up = long Down)
        self.cost = 0.0            # signed cash paid (buy +, sell -)
        self.mkt_fills = []
        self.bid = self.ask = None # our quoted prices (None = not quoting that side)
        self.pulled_until = 0.0

    def quote(self, best_bid, best_ask, pulled):
        if best_bid is None or best_ask is None:
            self.bid = self.ask = None; return
        if self.dodge and pulled:
            self.bid = self.ask = None; return
        spread = round(best_ask - best_bid, 4)
        bid = round(best_bid + TICK, 2) if spread > TICK + 1e-9 else best_bid   # improve when there is room
        ask = round(best_ask - TICK, 2) if spread > TICK + 1e-9 else best_ask
        if bid >= ask: bid, ask = best_bid, best_ask
        self.bid = bid if self.inv < ARGS.max_inv and 0.05 <= bid <= 0.95 else None
        self.ask = ask if self.inv > -ARGS.max_inv and 0.05 <= ask <= 0.95 else None

    def on_trade(self, side, price, size, level_size_before, mid_now, t):
        """side = taker side in Up terms ('SELL' hits bids, 'BUY' lifts asks)."""
        qty = 0.0
        if side == "SELL" and self.bid is not None and price <= self.bid + 1e-9:
            ahead = level_size_before if abs(price - self.bid) < 1e-9 else 0.0
            qty = min(ARGS.size, max(0.0, size - ahead)); px = self.bid; sign = +1
        elif side == "BUY" and self.ask is not None and price >= self.ask - 1e-9:
            ahead = level_size_before if abs(price - self.ask) < 1e-9 else 0.0
            qty = min(ARGS.size, max(0.0, size - ahead)); px = self.ask; sign = -1
        if qty < 1: return None
        self.inv += sign * qty; self.cost += sign * qty * px
        reb = REBATE_SHARE * FEE_RATE * px * (1 - px) * qty
        f = {"t": t, "side": "BUY" if sign > 0 else "SELL", "price": px, "qty": round(qty, 2), "mid": mid_now,
             "rebate": reb, "toxic": None, "strategy": self.name}
        self.mkt_fills.append(f); self.fills.append(f); self.fills_n += 1
        return f

    def settle(self, ts, outcome_up, provisional):
        payout = self.inv * (1.0 if outcome_up else 0.0)
        gross = payout - self.cost
        reb = sum(f["rebate"] for f in self.mkt_fills)
        self.pnl_gross += gross; self.rebate += reb
        row = {"ts": ts, "outcome": "Up" if outcome_up else "Down", "fills": len(self.mkt_fills),
               "shares": round(sum(f["qty"] for f in self.mkt_fills), 1), "inv_end": round(self.inv, 1),
               "gross": round(gross, 3), "rebate": round(reb, 3), "net": round(gross + reb, 3),
               "provisional": provisional, "strategy": self.name}
        self.market_rows.append(row)
        self.curve.append((time.time(), round(self.pnl_gross + self.rebate, 4)))
        self.reset_market()
        return row


class Bot:
    def __init__(self):
        self.strats = {"dodge": Strategy("dodge", True), "naive": Strategy("naive", False)}
        self.btc = deque(maxlen=600)       # (t, price)
        self.btc_px = None
        self.book = {"bids": {}, "asks": {}}  # Up token book: price -> size
        self.best_bid = self.best_ask = None
        self.market = None                 # dict(ts, cid, up, dn, question)
        self.pending = []                  # markets waiting for settlement
        self.pulled_until = 0.0
        self.last_pull_reason = ""
        self.n_pulls = 0
        self.pulled_secs = 0.0
        self._pull_t0 = None
        self.status = "starting"
        self.start = time.time()
        self.events = 0
        self.recent_fills = deque(maxlen=40)
        self.log = deque(maxlen=30)
        self.mid_hist = deque(maxlen=400)  # (t, mid) for toxicity marking

    # ---------- helpers ----------
    def say(self, msg):
        self.log.appendleft(f"{time.strftime('%H:%M:%S')} {msg}"); print(msg, flush=True)

    def mid(self):
        if self.best_bid is None or self.best_ask is None: return None
        return round((self.best_bid + self.best_ask) / 2, 4)

    def fetch_market(self, ts, tries=20):
        last_err = None
        for i in range(tries):
            try:
                r = requests.get(GAMMA, params={"slug": f"btc-updown-5m-{ts}"}, timeout=10)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}: {r.text[:120]}"
                else:
                    ev = r.json()
                    if ev:
                        m = ev[0]["markets"][0]; toks = json.loads(m["clobTokenIds"])
                        return {"ts": ts, "cid": m["conditionId"], "up": toks[0], "dn": toks[1], "question": m["question"]}
                    last_err = f"empty answer for slug btc-updown-5m-{ts} (clock off? now={int(time.time())})"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if i == 0 and last_err: self.say(f"gamma api problem: {last_err}")
            time.sleep(3)
        self.say(f"gamma api still failing: {last_err}")
        return None

    def fetch_outcome(self, ts):
        try:
            ev = requests.get(GAMMA, params={"slug": f"btc-updown-5m-{ts}"}, timeout=10).json()
            m = ev[0]["markets"][0]
            if m.get("umaResolutionStatus") == "resolved" or m.get("closed"):
                op = json.loads(m.get("outcomePrices") or "[]")
                if op and (float(op[0]) == 1.0 or float(op[1]) == 1.0):
                    return float(op[0]) == 1.0
        except Exception:
            pass
        return None

    def btc_move(self, secs=2.0):
        if len(self.btc) < 2: return 0.0
        now = self.btc[-1][0]; ref = None
        for t, p in reversed(self.btc):
            ref = p
            if now - t >= secs: break
        return (self.btc[-1][1] - ref) / ref if ref else 0.0

    # ---------- book ----------
    def apply_snapshot(self, d):
        self.book = {"bids": {float(x["price"]): float(x["size"]) for x in d.get("bids", [])},
                     "asks": {float(x["price"]): float(x["size"]) for x in d.get("asks", [])}}
        self.refresh_best()

    def apply_change(self, ch):
        side = "bids" if ch["side"] == "BUY" else "asks"
        p, s = float(ch["price"]), float(ch["size"])
        if s <= 0: self.book[side].pop(p, None)
        else: self.book[side][p] = s
        self.refresh_best()

    def refresh_best(self):
        self.best_bid = max(self.book["bids"]) if self.book["bids"] else None
        self.best_ask = min(self.book["asks"]) if self.book["asks"] else None
        m = self.mid()
        if m is not None:
            now = time.time(); self.mid_hist.append((now, m))
            old = [x for t, x in self.mid_hist if now - t >= 2.0]
            if old and abs(m - old[-1]) >= ARGS.pull_ticks * TICK - 1e-9:
                self.pull(f"book moved {(m-old[-1])*100:+.0f}c in 2s")

    # ---------- core ----------
    def pull(self, reason):
        now = time.time()
        if now >= self.pulled_until:
            self.n_pulls += 1; self._pull_t0 = now
        self.pulled_until = now + ARGS.pull_secs
        self.last_pull_reason = reason

    def requote(self):
        now = time.time()
        if self.market and now > self.market["ts"] + 300 - ARGS.no_quote_last:
            for s in self.strats.values(): s.bid = s.ask = None
            self.status = "last seconds: not quoting"; return
        pulled = now < self.pulled_until
        if not pulled and self._pull_t0 is not None:
            self.pulled_secs += now - self._pull_t0; self._pull_t0 = None
        for s in self.strats.values(): s.quote(self.best_bid, self.best_ask, pulled)
        self.status = ("pulled: " + self.last_pull_reason) if pulled else ("quoting" if self.market else "no market")

    def on_btc(self, px):
        self.btc.append((time.time(), px)); self.btc_px = px
        mv = self.btc_move(2.0)
        if abs(mv) > ARGS.pull_move:
            self.pull(f"BTC {mv*1e4:+.1f} bp in 2s")
        self.requote()

    def on_trade(self, asset, side, price, size, t_ms):
        # convert Down-token trades into Up-token terms
        if asset == self.market["dn"]:
            price = round(1 - price, 2); side = "SELL" if side == "BUY" else "BUY"
        lvl = self.book["bids" if side == "SELL" else "asks"].get(price, 0.0)
        m = self.mid()
        for s in self.strats.values():
            f = s.on_trade(side, price, size, lvl, m, t_ms / 1000)
            if f:
                f["market_ts"] = self.market["ts"]; self.recent_fills.appendleft(f)
                self.say(f"[{s.name}] fill {f['side']} {f['qty']} @ {f['price']:.2f} (taker {side} {size} @ {price:.2f}, ahead {lvl:.0f})")

    def mark_toxicity(self):
        """A fill is toxic if the mid moved >= 2c against us within 10s after the fill."""
        now = time.time()
        for s in self.strats.values():
            for f in s.fills[-200:]:
                if f["toxic"] is not None or f["mid"] is None or now - f["t"] < 10: continue
                after = [m for t, m in self.mid_hist if f["t"] + 8 <= t <= f["t"] + 12]
                if not after: f["toxic"] = False; continue
                m10 = after[-1]
                adverse = (f["mid"] - m10) if f["side"] == "BUY" else (m10 - f["mid"])
                f["toxic"] = bool(adverse >= 0.02)
                if f["toxic"]: s.toxic += 1

    def settle_pending(self):
        now = time.time()
        for pm in list(self.pending):
            if now < pm["ts"] + 300 + 20: continue
            out = self.fetch_outcome(pm["ts"]); provisional = False
            if out is None:
                if now < pm["ts"] + 300 + 600: continue   # wait up to 10 min for resolution
                out = pm.get("twap_up", True); provisional = True
            for s in self.strats.values():
                st = pm["state"][s.name]
                s.inv, s.cost, s.mkt_fills = st["inv"], st["cost"], st["fills"]
                row = s.settle(pm["ts"], out, provisional)
                if s.name == "dodge" and row["fills"]:
                    self.say(f"settled {pm['ts']} -> {row['outcome']}: dodge net {row['net']:+.3f} $ on {row['fills']} fills")
            self.pending.remove(pm)

    def end_market(self):
        """Freeze positions of the current market for later settlement; start fresh."""
        if not self.market: return
        state = {}
        for s in self.strats.values():
            state[s.name] = {"inv": s.inv, "cost": s.cost, "fills": s.mkt_fills}
            s.reset_market()
        # provisional outcome from Coinbase TWAP, in case Gamma is slow
        ts = self.market["ts"]
        p0 = [p for t, p in self.btc if ts - 60 <= t <= ts]; p1 = [p for t, p in self.btc if ts + 240 <= t <= ts + 300]
        twap_up = (sum(p1) / len(p1) >= sum(p0) / len(p0)) if p0 and p1 else True
        self.pending.append({"ts": ts, "state": state, "twap_up": twap_up})
        self.market = None; self.book = {"bids": {}, "asks": {}}; self.best_bid = self.best_ask = None

    # ---------- state file ----------
    def write_state(self):
        d, n = self.strats["dodge"], self.strats["naive"]
        def summary(s):
            net = s.pnl_gross + s.rebate
            return {"pnl_gross": round(s.pnl_gross, 3), "rebate": round(s.rebate, 3), "pnl_net": round(net, 3),
                    "fills": s.fills_n, "shares": round(sum(f["qty"] for f in s.fills), 1),
                    "toxic_fills": s.toxic, "toxic_rate": round(s.toxic / max(1, sum(1 for f in s.fills if f["toxic"] is not None)), 3),
                    "markets_traded": sum(1 for r in s.market_rows if r["fills"]), "markets_settled": len(s.market_rows),
                    "wins": sum(1 for r in s.market_rows if r["net"] > 0), "losses": sum(1 for r in s.market_rows if r["net"] < 0),
                    "inventory": round(s.inv, 1), "bid": s.bid, "ask": s.ask,
                    "return_on_bankroll_pct": round(100 * net / ARGS.bankroll, 3)}
        state = {
            "updated": time.time(), "started": self.start, "uptime_s": round(time.time() - self.start),
            "bankroll": ARGS.bankroll, "params": {"size": ARGS.size, "max_inv": ARGS.max_inv, "pull_move_bp": ARGS.pull_move * 1e4, "pull_ticks": ARGS.pull_ticks, "no_quote_last": ARGS.no_quote_last,
                                                   "pull_secs": ARGS.pull_secs, "fee_rate": FEE_RATE, "rebate_share": REBATE_SHARE},
            "status": self.status, "events": self.events, "btc": self.btc_px, "pulls": self.n_pulls,
            "pulled_pct": round(100 * self.pulled_secs / max(1, time.time() - self.start), 1),
            "market": {"ts": self.market["ts"], "question": self.market["question"],
                       "secs_left": max(0, int(self.market["ts"] + 300 - time.time()))} if self.market else None,
            "book": {"best_bid": self.best_bid, "best_ask": self.best_ask, "mid": self.mid(),
                     "bid_size": self.book["bids"].get(self.best_bid, 0) if self.best_bid else 0,
                     "ask_size": self.book["asks"].get(self.best_ask, 0) if self.best_ask else 0},
            "dodge": summary(d), "naive": summary(n),
            "curve": {"dodge": d.curve[-2000:], "naive": n.curve[-2000:]},
            "markets": (d.market_rows[-300:]), "markets_naive": (n.market_rows[-300:]),
            "recent_fills": list(self.recent_fills), "log": list(self.log), "pending": len(self.pending),
        }
        tmp = ARGS.state + ".tmp"
        with open(tmp, "w") as f: json.dump(state, f)
        os.replace(tmp, ARGS.state)

    # ---------- feeds ----------
    async def coinbase(self):
        while True:
            try:
                async with websockets.connect(CB_WS, ping_interval=15) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
                    async for msg in ws:
                        d = json.loads(msg)
                        if d.get("type") == "ticker" and "price" in d: self.on_btc(float(d["price"]))
            except Exception as e:
                self.say(f"coinbase ws error: {type(e).__name__}: {e}"); await asyncio.sleep(2)

    async def polymarket(self):
        while True:
            ts = int(time.time()) // 300 * 300
            m = await asyncio.to_thread(self.fetch_market, ts)
            if not m:
                self.say(f"market {ts} not found, retrying"); await asyncio.sleep(5); continue
            self.market = m; self.say(f"market {ts}: {m['question']}")
            deadline = ts + 300
            try:
                async with websockets.connect(PM_WS, ping_interval=10) as ws:
                    await ws.send(json.dumps({"assets_ids": [m["up"], m["dn"]], "type": "market"}))
                    while time.time() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
                        except asyncio.TimeoutError:
                            break
                        data = json.loads(msg); items = data if isinstance(data, list) else [data]
                        for d in items:
                            self.events += 1; et = d.get("event_type")
                            if et == "book" and d.get("asset_id") == m["up"]: self.apply_snapshot(d)
                            elif et == "price_change":
                                for ch in d.get("price_changes", []):
                                    if ch.get("asset_id") == m["up"]: self.apply_change(ch)
                            elif et == "last_trade_price" and d.get("asset_id") in (m["up"], m["dn"]):
                                self.on_trade(d["asset_id"], d["side"], float(d["price"]), float(d["size"]), int(d["timestamp"]))
                        self.requote()
            except Exception as e:
                self.say(f"polymarket ws error: {e}"); traceback.print_exc()
            self.end_market()
            # if the websocket dropped before the window ended, wait for the next boundary; otherwise go straight on
            if int(time.time()) // 300 * 300 == ts:
                await asyncio.sleep(max(0.2, deadline - time.time() + 0.2))

    async def housekeeping(self):
        while True:
            try:
                await asyncio.to_thread(self.settle_pending)
                self.mark_toxicity(); self.write_state()
            except Exception as e:
                self.say(f"housekeeping error: {e}"); traceback.print_exc()
            await asyncio.sleep(2)

    async def run(self):
        await asyncio.gather(self.coinbase(), self.polymarket(), self.housekeeping())


def serve(port):
    here = os.path.dirname(os.path.abspath(__file__))
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=here, **k)
        def log_message(self, *a): pass
        def end_headers(self):
            self.send_header("Cache-Control", "no-store"); super().end_headers()
        def do_GET(self):
            if self.path in ("/", "", "/dashboard.html"):
                body = open(os.path.join(here, "dashboard.html"), "rb").read()
                page = (b'<!doctype html><html lang="fr"><head><meta charset="utf-8">'
                        b'<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>' + body + b'</body></html>')
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            return super().do_GET()
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, args=(ARGS.port,), daemon=True).start()
    print(f"dashboard on http://localhost:{ARGS.port}/  (state: {ARGS.state})", flush=True)
    asyncio.run(Bot().run())
