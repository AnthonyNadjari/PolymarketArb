"""Paper-trading of Polymarket liquidity rewards.

Every minute the bot "posts" a two-sided quote on the K markets with the best expected reward share,
using the live order books. It earns its estimated share of each market's daily reward pool (Polymarket
scoring: quadratic in distance to the midpoint, two-sided minimum, sampled per minute) and it is filled
by real trades that cross its prices between two samples, so it also carries the inventory risk.
Net = rewards + trading PnL (marked at mid, settled at resolution).

No real orders are sent. Usage: python rewards_paper.py [--state rewards_state.json] [--k 15] [--capital 1000]
"""
import argparse, json, math, os, time, traceback
from concurrent.futures import ThreadPoolExecutor

import requests

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com/trades"
C = 3.0

ap = argparse.ArgumentParser()
ap.add_argument("--state", default="rewards_state.json")
ap.add_argument("--k", type=int, default=15, help="markets quoted at once")
ap.add_argument("--capital", type=float, default=1000.0, help="$ per market (both sides together)")
ap.add_argument("--min-rate", type=float, default=20.0, help="ignore markets paying less than this $/day")
ap.add_argument("--min-hours-left", type=float, default=2.0, help="do not start quoting a market ending sooner")
ap.add_argument("--placement", type=float, default=0.5, help="quote distance from mid as a fraction of max spread")
ap.add_argument("--reselect-min", type=float, default=15.0)
ap.add_argument("--duration-min", type=float, default=0, help="stop after N minutes (0 = forever)")
ARGS = ap.parse_args()
S = requests.Session()
S.headers["User-Agent"] = "pm-rewards-paper/0.1"


# ---------------------------------------------------------------- market data
def get_json(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200: return r.json()
        except requests.RequestException:
            pass
        time.sleep(1 + i)
    return None


def sampling_markets():
    out, cur = [], ""
    for _ in range(100):
        r = get_json(f"{CLOB}/sampling-markets", {"next_cursor": cur})
        if not r: break
        out += r.get("data", []); cur = r.get("next_cursor")
        if not cur or cur == "LTE=": break
    return out


def books(token_ids):
    res = {}
    chunks = [token_ids[i:i + 100] for i in range(0, len(token_ids), 100)]
    def get(ch):
        for _ in range(4):
            try:
                r = S.post(f"{CLOB}/books", json=[{"token_id": t} for t in ch], timeout=20)
                if r.status_code == 200: return r.json()
            except requests.RequestException:
                pass
            time.sleep(1)
        return []
    with ThreadPoolExecutor(8) as ex:
        for lst in ex.map(get, chunks):
            for b in lst or []: res[b["asset_id"]] = b
    return res


def yes_levels(by, bn):
    """YES book only. Polymarket mirrors every order into the complementary book (a YES bid at p is also
    shown as a NO ask at 1-p), so adding the NO book would count each order twice."""
    bids = [(float(x["price"]), float(x["size"])) for x in by.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in by.get("asks", [])]
    return sorted(bids, key=lambda x: -x[0]), sorted(asks, key=lambda x: x[0])


def adjusted_mid(bids, asks, min_size):
    def first(levels):
        cum = 0.0
        for p, s in levels:
            cum += s
            if cum >= min_size: return p
        return None
    b, a = first(bids), first(asks)
    return None if b is None or a is None or a <= b else (a + b) / 2


def score(dist_c, v):
    return ((v - dist_c) / v) ** 2 if 0 <= dist_c < v else 0.0


def q_min(q1, q2, mid):
    if 0.10 <= mid <= 0.90: return max(min(q1, q2), max(q1, q2) / C)
    return min(q1, q2)


def rate_of(m):
    return sum(r.get("rewards_daily_rate", 0) for r in (m.get("rewards", {}).get("rates") or []))


def end_ts(m):
    e = m.get("end_date_iso")
    if not e: return None
    try:
        return time.mktime(time.strptime(e[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return None


# ---------------------------------------------------------------- one market position
class Pos:
    def __init__(self, m=None, d=None):
        if d:
            self.__dict__.update(d); return
        self.cid = m["condition_id"]; self.q = m["question"]; self.slug = m.get("market_slug", "")
        self.yes, self.no = m["tokens"][0]["token_id"], m["tokens"][1]["token_id"]
        self.rate, self.v = rate_of(m), m["rewards"].get("max_spread", 0)
        self.min_size = m["rewards"].get("min_size", 0); self.tick = float(m.get("minimum_tick_size") or 0.01)
        self.end = end_ts(m); self.tags = m.get("tags", [])[:4]
        self.active = True                    # currently quoting
        self.n_yes = self.n_no = 0.0; self.cash = 0.0
        self.rewards = 0.0; self.fills = 0; self.samples = 0; self.share_sum = 0.0
        self.bid = self.ask = None; self.size = 0.0; self.bid_ahead = self.ask_ahead = 0.0
        self.mid = None; self.share = 0.0; self.last_trade_t = int(time.time()); self.seen = []
        self.resolved = None; self.started = time.time()
        self.qhist = []                      # [t_set, bid, ask, bid_ahead, ask_ahead]: quotes as they were live

    def value(self):
        if self.resolved is not None:
            return self.n_yes * self.resolved + self.n_no * (1 - self.resolved) + self.cash
        if self.mid is None: return self.cash + self.n_yes * 0.5 + self.n_no * 0.5
        return self.n_yes * self.mid + self.n_no * (1 - self.mid) + self.cash

    def inv(self): return self.n_yes - self.n_no


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.pos = {}; self.curve = []; self.t0 = time.time(); self.last_select = 0; self.log = []
        self.candidates = []; self.samples = 0
        if os.path.exists(ARGS.state):
            try:
                st = json.load(open(ARGS.state))
                self.pos = {k: Pos(d=v) for k, v in st.get("positions", {}).items()}
                self.curve = st.get("curve", []); self.t0 = st.get("started", self.t0); self.samples = st.get("samples", 0)
                for p in self.pos.values(): p.last_trade_t = int(time.time())   # do not replay trades missed while down
                print(f"restored {len(self.pos)} positions", flush=True)
            except Exception as e:
                print("restore failed:", e, flush=True)

    def say(self, s):
        self.log.insert(0, time.strftime("%H:%M:%S ") + s); self.log = self.log[:40]; print(s, flush=True)

    # ---- selection
    def select(self):
        ms = [m for m in sampling_markets() if m.get("accepting_orders") and m.get("rewards") and len(m.get("tokens", [])) == 2]
        now = time.time()
        ms = [m for m in ms if rate_of(m) >= ARGS.min_rate and m["rewards"].get("max_spread", 0) > 0
              and (end_ts(m) or now + 1e9) > now + ARGS.min_hours_left * 3600]
        bk = books([t["token_id"] for m in ms for t in m["tokens"]])
        cands = []
        for m in ms:
            y, n = m["tokens"][0]["token_id"], m["tokens"][1]["token_id"]
            if y not in bk or n not in bk: continue
            bids, asks = yes_levels(bk[y], bk[n]); v = m["rewards"]["max_spread"]; ms_ = m["rewards"].get("min_size", 0)
            mid = adjusted_mid(bids, asks, ms_)
            if mid is None or not (0.04 <= mid <= 0.96): continue
            q1 = sum(score((mid - p) * 100, v) * s for p, s in bids if s >= ms_)
            q2 = sum(score((p - mid) * 100, v) * s for p, s in asks if s >= ms_)
            others = q_min(q1, q2, mid)
            d = v * ARGS.placement
            n_sh = ARGS.capital / max(0.05, (mid - d / 100) + (1 - (mid + d / 100)))
            if n_sh < ms_: continue
            mine = score(d, v) * n_sh
            share = mine / (mine + others)
            cands.append((rate_of(m) * share, m, share))
        cands.sort(key=lambda x: -x[0])
        self.candidates = [{"q": m["question"], "rate": rate_of(m), "exp_usd_day": round(e, 2), "share": round(s, 3)} for e, m, s in cands[:40]]
        keep = {m["condition_id"] for _, m, _ in cands[:ARGS.k]}
        for cid, p in self.pos.items():
            if p.active and cid not in keep:
                p.active = False; p.bid = p.ask = None; self.say(f"stop quoting: {p.q[:60]}")
        for _, m, _ in cands[:ARGS.k]:
            cid = m["condition_id"]
            if cid in self.pos:
                if not self.pos[cid].active and self.pos[cid].resolved is None:
                    self.pos[cid].active = True; self.say(f"quote again: {m['question'][:60]}")
            else:
                self.pos[cid] = Pos(m); self.say(f"start quoting: {m['question'][:60]} (${rate_of(m):.0f}/day)")
        self.last_select = time.time()

    # ---- fills from real trades since last sample
    def process_trades(self, p):
        hist = getattr(p, "qhist", None) or [[0, p.bid, p.ask, p.bid_ahead, p.ask_ahead]]
        if all(h[1] is None and h[2] is None for h in hist): return
        tr = get_json(DATA, {"market": p.cid, "limit": 200}) or []
        new = [t for t in tr if int(t.get("timestamp", 0)) >= p.last_trade_t and t.get("transactionHash") not in p.seen]
        new.sort(key=lambda t: int(t["timestamp"]))
        for t in new:
            ts = int(t["timestamp"]) - 4                  # on-chain timestamp is a few seconds after the match
            live = [h for h in hist if h[0] <= ts]
            if not live: continue                         # before we started quoting
            h = live[-1]                                  # the quote that was resting at that moment
            bid, ask = h[1], h[2]
            price, size = float(t["price"]), float(t["size"])
            taker_buy = t.get("side") == "BUY"
            if int(t.get("outcomeIndex", 0)) == 0:
                hits_bids, x = (not taker_buy), price
            else:
                hits_bids, x = taker_buy, round(1 - price, 4)
            if hits_bids and bid is not None and x <= bid + 1e-9 and p.inv() < p.size:
                same = abs(x - bid) < 1e-9
                avail = size - (h[3] if same else 0)
                if same: h[3] = max(0.0, h[3] - size)
                q = min(max(0.0, avail), p.size - max(0.0, p.inv()))
                if q >= 1:
                    p.n_yes += q; p.cash -= q * bid; p.fills += 1
                    self.say(f"fill BUY YES {q:.0f} @ {bid:.3f} | {p.q[:50]}")
            elif (not hits_bids) and ask is not None and x >= ask - 1e-9 and -p.inv() < p.size:
                same = abs(x - ask) < 1e-9
                avail = size - (h[4] if same else 0)
                if same: h[4] = max(0.0, h[4] - size)
                q = min(max(0.0, avail), p.size - max(0.0, -p.inv()))
                if q >= 1:
                    p.n_no += q; p.cash -= q * (1 - ask); p.fills += 1
                    self.say(f"fill SELL YES {q:.0f} @ {ask:.3f} | {p.q[:50]}")
        if new:
            p.last_trade_t = int(new[-1]["timestamp"])
            p.seen = (p.seen + [t.get("transactionHash") for t in new])[-60:]

    # ---- quote + reward accrual for one sample
    def sample(self):
        live = [p for p in self.pos.values() if p.resolved is None]
        bk = books([t for p in live for t in (p.yes, p.no)])
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(self.process_trades, [p for p in live if p.active or p.bid or p.ask]))
        dt_days = 1 / 1440
        for p in live:
            if p.yes not in bk or p.no not in bk:
                p.bid = p.ask = None; continue
            bids, asks = yes_levels(bk[p.yes], bk[p.no])
            mid = adjusted_mid(bids, asks, p.min_size)
            if mid is None: p.bid = p.ask = None; continue
            p.mid = round(mid, 4)
            if not p.active or (p.end and time.time() > p.end - 600):
                p.bid = p.ask = None; p.share = 0.0
                p.qhist = (getattr(p, "qhist", []) + [[time.time(), None, None, 0, 0]])[-15:]; continue
            d = p.v * ARGS.placement / 100
            best_bid = bids[0][0] if bids else 0.0; best_ask = asks[0][0] if asks else 1.0
            bid = math.floor((mid - d) / p.tick + 1e-9) * p.tick; ask = math.ceil((mid + d) / p.tick - 1e-9) * p.tick
            bid = min(bid, best_ask - p.tick); ask = max(ask, best_bid + p.tick)
            p.size = round(ARGS.capital / max(0.05, bid + (1 - ask)))
            p.bid = round(bid, 4) if p.inv() < p.size and bid > 0 else None
            p.ask = round(ask, 4) if -p.inv() < p.size and ask < 1 else None
            p.bid_ahead = sum(s for x, s in bids if abs(x - (p.bid or -1)) < 1e-9)
            p.ask_ahead = sum(s for x, s in asks if abs(x - (p.ask or -1)) < 1e-9)
            if not hasattr(p, "qhist"): p.qhist = []
            p.qhist = (p.qhist + [[time.time(), p.bid, p.ask, p.bid_ahead, p.ask_ahead]])[-15:]
            q1 = sum(score((mid - x) * 100, p.v) * s for x, s in bids if s >= p.min_size)
            q2 = sum(score((x - mid) * 100, p.v) * s for x, s in asks if s >= p.min_size)
            others = q_min(q1, q2, mid)
            m1 = score((mid - p.bid) * 100, p.v) * p.size if p.bid is not None else 0.0
            m2 = score((p.ask - mid) * 100, p.v) * p.size if p.ask is not None else 0.0
            mine = q_min(m1, m2, mid) if p.size >= p.min_size else 0.0
            p.share = mine / (mine + others) if mine > 0 else 0.0
            p.rewards += p.rate * p.share * dt_days; p.samples += 1; p.share_sum += p.share
        self.samples += 1

    def settle(self):
        for p in self.pos.values():
            if p.resolved is not None or not p.end or time.time() < p.end: continue
            if abs(p.n_yes) < 1e-9 and abs(p.n_no) < 1e-9:
                p.resolved = p.mid if p.mid is not None else 0.5; p.active = False; continue
            mk = get_json(f"{CLOB}/markets/{p.cid}")
            if not mk: continue
            win = [t for t in mk.get("tokens", []) if t.get("winner")]
            if win:
                p.resolved = 1.0 if win[0]["token_id"] == p.yes else 0.0
                p.active = False; p.bid = p.ask = None
                self.say(f"resolved {'YES' if p.resolved else 'NO'}: {p.q[:50]} | pnl {p.value():+.2f} rewards {p.rewards:.2f}")

    def write_state(self):
        ps = list(self.pos.values())
        rewards = sum(p.rewards for p in ps); trading = sum(p.value() for p in ps)
        net = rewards + trading
        self.curve.append([round(time.time()), round(net, 2), round(rewards, 2), round(trading, 2)])
        self.curve = self.curve[-3000:]
        active = [p for p in ps if p.active and p.resolved is None]
        rows = []
        for p in sorted(ps, key=lambda p: (not p.active, -(p.rewards + p.value()))):
            rows.append({"q": p.q, "slug": p.slug, "rate": p.rate, "active": p.active, "resolved": p.resolved,
                         "mid": p.mid, "bid": p.bid, "ask": p.ask, "size": p.size, "share": round(p.share, 3),
                         "avg_share": round(p.share_sum / p.samples, 3) if p.samples else 0, "rewards": round(p.rewards, 2),
                         "trading": round(p.value(), 2), "net": round(p.rewards + p.value(), 2), "inv": round(p.inv(), 1),
                         "fills": p.fills, "end": p.end, "tags": p.tags})
        st = {"updated": time.time(), "started": self.t0, "samples": self.samples,
              "params": {"k": ARGS.k, "capital": ARGS.capital, "min_rate": ARGS.min_rate, "placement": ARGS.placement,
                         "min_hours_left": ARGS.min_hours_left},
              "totals": {"rewards": round(rewards, 2), "trading": round(trading, 2), "net": round(net, 2),
                         "quoting": len(active), "capital_quoting": round(len(active) * ARGS.capital),
                         "fills": sum(p.fills for p in ps), "markets_total": len(ps),
                         "exp_usd_day": round(sum(p.rate * p.share for p in active), 2)},
              "curve": self.curve, "rows": rows[:200], "candidates": self.candidates, "log": self.log,
              "positions": {k: p.__dict__ for k, p in self.pos.items()}}
        tmp = ARGS.state + ".tmp"
        with open(tmp, "w") as f: json.dump(st, f)
        os.replace(tmp, ARGS.state)

    def run(self):
        stop = time.time() + ARGS.duration_min * 60 if ARGS.duration_min else None
        while stop is None or time.time() < stop:
            t = time.time()
            try:
                if t - self.last_select > ARGS.reselect_min * 60: self.select()
                self.sample(); self.settle(); self.write_state()
            except Exception as e:
                self.say(f"error: {e}"); traceback.print_exc()
            time.sleep(max(1, 60 - (time.time() - t)))


if __name__ == "__main__":
    Bot().run()
