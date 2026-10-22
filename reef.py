#!/usr/bin/env python3
"""Reef CLI: autonomous PAPER trader on Kalshi NBA markets.

Read-only public Kalshi market data (series/events/markets/orderbook). No credentials,
no order or portfolio endpoints, no real orders. "Placed" = a row in data/bets.csv.

Commands:
  markets  [--date YYYY-MM-DD]            discover NBA events/markets (KXNBAGAME/KXNBASPREAD/KXNBATOTAL)
  book     TICKER [--side yes|no] [--contracts N]   live orderbook + depth-walk cost + fee math
  fee      --price P --contracts N        Kalshi fee calculator
  size     PICKS.json                     dry run: gate, cost model, tiers, caps, flags (no writes)
  run      PICKS.json [--date D]          auto-place/pass in paper ledger + write post-run summary
  monitor  --event EVT [--dry-run]        poll orderbook; paper entries/exits net of all costs + in-game exit rules (exits.py)
  close    ID [--price P | --live]        record last Kalshi price before tip -> CLV
  exit     ID [--price P | --live]        paper trade-out (walks bids, charges exit fee)
  settle   ID win|loss|void               settle at expiry ($1 or $0 per contract; no settlement fee)
  summary  [--date D]                     (re)write data/summaries/D.md
  status                                  bankroll, record, open positions, CLV, fees paid
  reset    [--bankroll 500]               wipe ledger + summaries, reset bankroll
"""
import argparse, csv, glob, json, math, os, re, sys, time, urllib.request, urllib.error
from datetime import date, datetime
import exits

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BANKROLL = os.path.join(DATA, "bankroll.json")
BETS = os.path.join(DATA, "bets.csv")
SUMMARIES = os.path.join(DATA, "summaries")
FAIR_DIR = os.path.join(DATA, "fair")
FIELDS = ["id", "date", "source", "event", "ticker", "title", "side", "style", "contracts", "fair_prob",
          "consensus", "confidence", "tier_pct", "best_bid", "best_ask", "avg_entry", "spread_cost",
          "slippage_cost", "entry_fee", "exit_cost_est", "all_in_cost", "ev_per_contract", "ev_total",
          "edge_pct", "pct_bankroll", "status", "reasoning", "fee_math", "correlation_flags",
          "close_price", "clv_cents", "clv_prob", "exit_price", "exit_fee", "result", "pnl",
          "bankroll_after", "placed_at", "closed_at", "settled_at"]
DEFAULTS = {"league": "NBA", "starting_bankroll": 500, "run_window_pt": "15:00-16:00",
            "api_base": "https://external-api.kalshi.com/trade-api/v2",
            "api_base_fallback": "https://api.elections.kalshi.com/trade-api/v2",
            "series": "KXNBAGAME,KXNBASPREAD,KXNBATOTAL", "orderbook_depth": 20,
            "taker_fee_rate": 0.07, "maker_fee_rate": 0.0175, "fee_multiplier": 1, "fee_round_to": 0.01,
            "assume_taker": True, "min_edge_cents": 2, "min_edge_pct": 0.03, "slippage_buffer_cents": 0,
            "min_confidence": 75, "tier_95_pct": 0.20, "tier_85_pct": 0.15, "tier_75_pct": 0.10,
            "per_trade_cap": 100, "nightly_exposure_cap": 0.60, "max_picks": 20,
            "live": False,
            "poll_seconds": 15, "min_poll_seconds": 5, "request_spacing_seconds": 1}
OPEN = ("placed",)
_LAST_CALL = [0.0]


# ---------------- config / storage ----------------
def _scalar(v):
    v = v.strip().strip('"').strip("'")
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(v.lower(), v)


def load_config():
    cfg = dict(DEFAULTS)
    yp, jp = os.path.join(ROOT, "config.yaml"), os.path.join(ROOT, "config.json")
    if os.path.exists(yp):
        try:
            import yaml  # optional
            cfg.update(yaml.safe_load(open(yp)) or {})
        except ImportError:  # stdlib fallback: flat key: value
            for line in open(yp):
                line = line.split(" #", 1)[0].strip()
                if line and not line.startswith("#") and ":" in line:
                    k, v = line.split(":", 1)
                    cfg[k.strip()] = _scalar(v)
    elif os.path.exists(jp):
        cfg.update(json.load(open(jp)))
    if str(cfg["league"]).upper() != "NBA":
        sys.exit("NBA only.")
    return cfg


def load_bankroll():
    return json.load(open(BANKROLL))


def save_bankroll(b):
    b["updated"] = now()
    json.dump(b, open(BANKROLL, "w"), indent=2)


def read_bets():
    return list(csv.DictReader(open(BETS, newline=""))) if os.path.exists(BETS) else []


def write_bets(rows):
    with open(BETS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def now():
    return datetime.now().isoformat(timespec="seconds")


def fnum(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ---------------- Kalshi public API (GET only) ----------------
def api_get(cfg, path):
    """Read-only GET against public market-data endpoints. Refuses anything else."""
    segs = path.split("?")[0].strip("/").split("/")
    if segs[0] not in ("series", "events", "markets", "exchange") or {"orders", "portfolio", "order"} & set(segs):
        raise PermissionError(f"forbidden endpoint {path}: public market data only, never order/portfolio")
    last = None
    gap = float(cfg.get("request_spacing_seconds", 1.0))
    wait = gap - (time.time() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)  # polite: space out requests
    _LAST_CALL[0] = time.time()
    for base in (cfg["api_base"], cfg["api_base_fallback"]):
        try:
            req = urllib.request.Request(base + path, headers={"User-Agent": "reef-paper/1.0",
                                                               "Accept": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=15))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:  # rate limited: back off once, honoring Retry-After
                time.sleep(min(float(e.headers.get("Retry-After") or 5), 30))
                try:
                    return json.load(urllib.request.urlopen(req, timeout=15))
                except Exception as e2:
                    last = e2
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
    raise RuntimeError(f"Kalshi GET {path} failed: {last}")


def fetch_book(cfg, ticker):
    d = api_get(cfg, f"/markets/{ticker}/orderbook?depth={int(cfg['orderbook_depth'])}")
    ob = d.get("orderbook_fp") or {}
    if ob:
        yes = [[float(p), float(q)] for p, q in ob.get("yes_dollars") or []]
        no = [[float(p), float(q)] for p, q in ob.get("no_dollars") or []]
    else:  # legacy cents format
        ob = d.get("orderbook") or {}
        yes = [[p / 100, q] for p, q in ob.get("yes") or []]
        no = [[p / 100, q] for p, q in ob.get("no") or []]
    return {"yes": yes, "no": no}


# ---------------- fee + cost model ----------------
def kalshi_fee(cfg, price, contracts, maker=False):
    rate = cfg["maker_fee_rate"] if maker else cfg["taker_fee_rate"]
    raw = cfg["fee_multiplier"] * rate * contracts * price * (1 - price)
    step = float(cfg["fee_round_to"])
    return math.ceil(round(raw / step, 6)) * step, raw


def asks_for(book, side):
    """Asks to BUY `side`: YES ask = 1 - NO bid (and vice versa). Cheapest first."""
    other = book["no"] if side == "yes" else book["yes"]
    return sorted([[round(1 - p, 4), q] for p, q in other], key=lambda x: x[0])


def bids_for(book, side):
    return sorted([[p, q] for p, q in book[side]], key=lambda x: -x[0])


def walk(levels, n, cfg):
    """Fill n contracts through levels; fee charged per price-level fill (each rounded up)."""
    left, fills = n, []
    for p, q in levels:
        if left <= 0:
            break
        take = min(left, int(q))
        if take > 0:
            fee, raw = kalshi_fee(cfg, p, take, maker=not cfg["assume_taker"])
            fills.append({"price": p, "qty": take, "fee": fee, "raw_fee": raw})
            left -= take
    if left > 0:
        return None
    return fills


def entry_quote(book, side, n, fair, style, cfg):
    asks, bids = asks_for(book, side), bids_for(book, side)
    if not asks:
        return None
    fills = walk(asks, n, cfg)
    if not fills:
        return None
    best_ask = asks[0][0]
    best_bid = bids[0][0] if bids else 0.0
    notional = sum(f["price"] * f["qty"] for f in fills)
    fee = sum(f["fee"] for f in fills)
    avg = notional / n
    mid = (best_ask + best_bid) / 2 if bids else best_ask
    buf = cfg["slippage_buffer_cents"] / 100 * n
    spread_cost = (best_ask - mid) * n
    slip = (avg - best_ask) * n + buf
    exit_est = 0.0
    if style == "trade":  # est. exit: cross half the spread + taker fee at fair price
        exit_est = (best_ask - best_bid) / 2 * n + kalshi_fee(cfg, min(max(fair, 0.01), 0.99), n)[0]
    all_in = notional + fee + buf
    ev_total = fair * n - all_in - exit_est
    evc = ev_total / n
    cost_pc = (all_in + exit_est) / n
    fee_math = " + ".join(f"ceil(0.07x{f['qty']}x{f['price']:.2f}x{1 - f['price']:.2f}={f['raw_fee']:.4f})=${f['fee']:.2f}"
                          for f in fills).replace("0.07", f"{cfg['taker_fee_rate']:g}")
    if exit_est:
        fee_math += f" | est exit: half-spread ${(best_ask - best_bid) / 2 * n:.2f} + fee ${kalshi_fee(cfg, min(max(fair, .01), .99), n)[0]:.2f}"
    return {"contracts": n, "best_ask": best_ask, "best_bid": best_bid, "avg_entry": avg, "notional": notional,
            "entry_fee": fee, "spread_cost": spread_cost, "slippage_cost": slip, "exit_cost_est": exit_est,
            "all_in_cost": all_in, "ev_total": ev_total, "ev_per_contract": evc,
            "edge_pct": evc / cost_pc if cost_pc else 0, "fee_math": fee_math, "fills": fills}


def clears_hurdle(q, cfg):
    return q and q["ev_per_contract"] * 100 >= cfg["min_edge_cents"] and q["edge_pct"] >= cfg["min_edge_pct"]


def max_contracts(book, side, budget, fair, style, cfg):
    """Largest n whose all-in cost (notional + fees) fits the dollar budget."""
    best, n = None, 1
    while True:
        q = entry_quote(book, side, n, fair, style, cfg)
        if not q or q["all_in_cost"] > budget:
            return best
        best, n = q, n + 1


def tier_pct(conf, cfg):
    if conf >= 95:
        return cfg["tier_95_pct"]
    if conf >= 85:
        return cfg["tier_85_pct"]
    if conf >= cfg["min_confidence"]:
        return cfg["tier_75_pct"]
    return 0.0


# ---------------- correlation ----------------
def correlation_flags(ps):
    flags, slate = {i: [] for i in range(len(ps))}, []
    if len(ps) > 1:
        slate.append(f"SAME SLATE: all {len(ps)} trades resolve tonight; one bad night hits every position")
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            a, b, out = ps[i], ps[j], []
            tag = f"#{a['_n']}<->#{b['_n']}"
            ga, gb = game_key(a), game_key(b)
            if ga and ga == gb:
                sa, sb = series_of(a["ticker"]), series_of(b["ticker"])
                if {sa, sb} >= {"KXNBATOTAL"} and len({sa, sb}) == 2:
                    out.append(f"SIDE+TOTAL same game ({ga}) {tag}")
                else:
                    out.append(f"SAME GAME ({ga}) {tag}")
            ta, tb = {t.lower() for t in a.get("teams", [])}, {t.lower() for t in b.get("teams", [])}
            if ta & tb and ga != gb:
                out.append(f"SAME TEAM ({', '.join(sorted(ta & tb))}) {tag}")
            for key, lab in (("injury_news", "SHARED INJURY NEWS"), ("factors", "SHARED FACTOR")):
                s = {x.lower() for x in a.get(key, [])} & {x.lower() for x in b.get(key, [])}
                if s:
                    out.append(f"{lab} ({', '.join(sorted(s))}) {tag}")
            flags[i] += out; flags[j] += out
    return flags, slate


def series_of(t):
    return t.split("-")[0]


def game_key(p):
    parts = p.get("event", p["ticker"]).split("-")
    return parts[1] if len(parts) > 1 else ""


# ---------------- evaluate picks ----------------
def evaluate(picks, bankroll, cfg, books=None):
    notes, cand, passed = [], [], []
    for n, p in enumerate(picks, 1):
        p = dict(p, _n=n, confidence=float(p.get("confidence", 0)), side=p.get("side", "yes").lower(),
                 style=p.get("style", "hold").lower(), fair_prob=float(p["fair_prob"]))
        book = p.get("book") or (books or {}).get(p["ticker"])
        if book is None:
            book = fetch_book(cfg, p["ticker"]); p["source"] = "LIVE"
        p["_book"] = book
        q1 = entry_quote(book, p["side"], 1, p["fair_prob"], p["style"], cfg)
        p["_q1"] = q1
        if p["confidence"] < cfg["min_confidence"]:
            p["pass_reason"] = f"confidence {p['confidence']:.0f} < {cfg['min_confidence']}"
        elif not clears_hurdle(q1, cfg):
            p["pass_reason"] = ("no liquidity" if not q1 else
                                f"edge after costs {q1['ev_per_contract'] * 100:+.1f}c/{q1['edge_pct']:+.1%} "
                                f"< hurdle {cfg['min_edge_cents']}c & {cfg['min_edge_pct']:.0%}")
        (passed if "pass_reason" in p else cand).append(p)
    cand.sort(key=lambda p: -p["confidence"])
    mp = int(cfg["max_picks"])
    for p in cand[mp:]:
        p["pass_reason"] = f"over max_picks ({mp})"; passed.append(p)
    cand = sorted(cand[:mp], key=lambda p: p["_n"])
    for p in cand:
        p["tier_pct"] = tier_pct(p["confidence"], cfg)
        p["budget_raw"] = bankroll * p["tier_pct"]
        p["budget"] = min(p["budget_raw"], float(cfg["per_trade_cap"]))
    cap = bankroll * cfg["nightly_exposure_cap"]
    tot = sum(p["budget"] for p in cand)
    scale = 1.0
    if tot > cap > 0:
        scale = cap / tot
        notes.append(f"Pre-cap budgets ${tot:.2f} > nightly cap ${cap:.2f}; budgets scaled x{scale:.3f}.")
    placed = []
    for p in cand:
        p["budget_scaled"] = p["budget"] * scale
        q = max_contracts(p["_book"], p["side"], p["budget_scaled"], p["fair_prob"], p["style"], cfg)
        if not clears_hurdle(q, cfg):
            p["pass_reason"] = "edge disappears at size (depth/slippage)" if q else "budget below 1 contract"
            passed.append(p); continue
        p["q"] = q; placed.append(p)
    flags, slate = correlation_flags(placed)
    for i, p in enumerate(placed):
        p["flags"] = flags[i]
    total = sum(p["q"]["all_in_cost"] for p in placed)
    assert total < bankroll
    return {"bankroll": bankroll, "placed": placed, "passed": sorted(passed, key=lambda p: p["_n"]),
            "total": total, "cap": cap, "scale": scale, "slate_flags": slate, "notes": notes}


def all_flags(res):
    out = list(res["slate_flags"])
    for p in res["placed"]:
        out += [f for f in p["flags"] if f not in out]
    return out or ["none"]


def print_eval(res, cfg, src):
    b = res["bankroll"]
    print(f"[{src}] Bankroll ${b:.2f} | gate {cfg['min_confidence']} | tiers 75-84={cfg['tier_75_pct']:.0%} "
          f"85-94={cfg['tier_85_pct']:.0%} 95+={cfg['tier_95_pct']:.0%} | cap/trade ${cfg['per_trade_cap']} | "
          f"nightly cap {cfg['nightly_exposure_cap']:.0%} = ${res['cap']:.2f} | hurdle {cfg['min_edge_cents']}c & {cfg['min_edge_pct']:.0%}")
    rows = sorted(res["placed"] + res["passed"], key=lambda p: p["_n"])
    for p in rows:
        q1 = p["_q1"]
        head = (f"#{p['_n']} {p['ticker']} BUY {p['side'].upper()} ({p['style']}) conf {p['confidence']:.0f} "
                f"fair {p['fair_prob']:.2f}")
        if q1:
            head += f" | bid {q1['best_bid']:.2f} ask {q1['best_ask']:.2f}"
        print(head)
        if p in res["placed"]:
            q = p["q"]
            print(f"   PLACE {q['contracts']} @ avg {q['avg_entry']:.4f} | tier {p['tier_pct']:.0%} -> ${p['budget_raw']:.2f}"
                  f"{' cap->$%.2f' % p['budget'] if p['budget'] < p['budget_raw'] else ''}"
                  f"{' scaled->$%.2f' % p['budget_scaled'] if res['scale'] < 1 else ''}")
            print(f"   cost: notional ${q['notional']:.2f} + fee ${q['entry_fee']:.2f} = all-in ${q['all_in_cost']:.2f} "
                  f"({q['all_in_cost'] / b:.1%} BR) | spread ${q['spread_cost']:.2f} slippage ${q['slippage_cost']:.2f} "
                  f"est-exit ${q['exit_cost_est']:.2f}")
            print(f"   fee math: {q['fee_math']}")
            print(f"   EV after all costs: ${q['ev_total']:.2f} = {q['ev_per_contract'] * 100:+.2f}c/contract ({q['edge_pct']:+.1%})")
        else:
            if q1:
                print(f"   1-lot: all-in {q1['all_in_cost']:.4f} fee math {q1['fee_math']} -> EV {q1['ev_per_contract'] * 100:+.2f}c")
            print(f"   PASS: {p['pass_reason']}")
    print(f"Placed {len(res['placed'])}, passed {len(res['passed'])}. Exposure ${res['total']:.2f} = "
          f"{res['total'] / b:.1%} of bankroll (cap {cfg['nightly_exposure_cap']:.0%})")
    for n in res["notes"]:
        print("NOTE:", n)
    print("Correlation flags:", "; ".join(all_flags(res)))


# ---------------- commands ----------------
def load_picks(path):
    d = json.load(open(path))
    return d.get("picks", []), d.get("games_analyzed", []), d.get("source", "LIVE")


def cmd_size(a, cfg):
    picks, _, src = load_picks(a.picks)
    bank = a.bankroll if a.bankroll is not None else load_bankroll()["current"]
    print_eval(evaluate(picks, bank, cfg), cfg, src)


def new_id(rows):
    return max([int(r["id"]) for r in rows] or [0]) + 1


def row_from(p, d, src, status, q=None):
    r = {"date": d, "source": p.get("source", src), "event": p.get("event", ""), "ticker": p["ticker"],
         "title": p.get("title", ""), "side": p["side"], "style": p["style"], "fair_prob": f"{p['fair_prob']:.4f}",
         "consensus": p.get("consensus", ""), "confidence": f"{p['confidence']:.0f}", "status": status,
         "reasoning": p.get("reasoning", "") + ("" if status == "placed" else f" [PASSED: {p.get('pass_reason', '')}]"),
         "correlation_flags": " | ".join(p.get("flags", []))}
    q = q or p.get("_q1")
    if q:
        r.update(best_bid=f"{q['best_bid']:.4f}", best_ask=f"{q['best_ask']:.4f}", fee_math=q["fee_math"])
    if status == "placed":
        r.update(contracts=q["contracts"], tier_pct=p.get("tier_pct", ""), avg_entry=f"{q['avg_entry']:.4f}",
                 spread_cost=f"{q['spread_cost']:.2f}", slippage_cost=f"{q['slippage_cost']:.2f}",
                 entry_fee=f"{q['entry_fee']:.2f}", exit_cost_est=f"{q['exit_cost_est']:.2f}",
                 all_in_cost=f"{q['all_in_cost']:.2f}", ev_per_contract=f"{q['ev_per_contract']:.4f}",
                 ev_total=f"{q['ev_total']:.2f}", edge_pct=f"{q['edge_pct']:.4f}", placed_at=now())
    else:
        r.update(contracts=0, all_in_cost="0.00")
    return r


def cmd_run(a, cfg):
    picks, games, src = load_picks(a.picks)
    d = a.date or date.today().isoformat()
    rows = read_bets()
    ex = [r for r in rows if r["date"] == d]
    if ex and not a.replace:
        sys.exit(f"{d} already run. Use --replace to redo (only if nothing settled).")
    if a.replace:
        if any(r["status"] not in ("placed", "passed") for r in ex):
            sys.exit("Refusing: some rows already settled/exited.")
        rows = [r for r in rows if r["date"] != d]
    bank = load_bankroll()["current"]
    res = evaluate(picks, bank, cfg)
    print_eval(res, cfg, src)
    nid = new_id(rows)
    for p in sorted(res["placed"] + res["passed"], key=lambda p: p["_n"]):
        st = "placed" if p in res["placed"] else "passed"
        r = row_from(p, d, src, st, p.get("q"))
        r["id"] = nid
        if st == "placed":
            r["pct_bankroll"] = f"{p['q']['all_in_cost'] / bank:.4f}"
        rows.append(r); nid += 1
    write_bets(rows)
    os.makedirs(SUMMARIES, exist_ok=True)
    json.dump({"games_analyzed": games, "bankroll_at_run": bank, "slate_flags": res["slate_flags"],
               "notes": res["notes"], "scale": res["scale"], "source": src},
              open(os.path.join(SUMMARIES, f"{d}.meta.json"), "w"), indent=2)
    print(f"\n[paper ledger updated; post-run report -> {os.path.relpath(write_summary(d, cfg), ROOT)}]")


def write_summary(d, cfg):
    allrows = read_bets()
    rows = [r for r in allrows if r["date"] == d]
    mp = os.path.join(SUMMARIES, f"{d}.meta.json")
    meta = json.load(open(mp)) if os.path.exists(mp) else {}
    b = load_bankroll()
    rb = meta.get("bankroll_at_run", b["current"])
    pl = [r for r in rows if r["status"] not in ("passed",)]
    ps = [r for r in rows if r["status"] == "passed"]
    exp = sum(float(r["all_in_cost"] or 0) for r in pl)
    fees = sum(float(r["entry_fee"] or 0) + float(r["exit_fee"] or 0) for r in pl)
    src = meta.get("source", "LIVE")
    L = [f"# Reef post-run report: {d}" + ("  (EXAMPLE DATA)" if src == "EXAMPLE" else ""), "",
         "**Mode:** autonomous PAPER trading on Kalshi NBA markets. \"Placed\" = logged in the ledger; no real orders.",
         f"**Bankroll:** ${b['current']:,.2f} now (${rb:,.2f} at run, start ${b['starting']:,.2f})", "",
         f"**Games analyzed ({len(meta.get('games_analyzed', []))}):** " + ("; ".join(meta.get("games_analyzed", [])) or "n/a"), "",
         f"## Placed ({len(pl)})"]
    if pl:
        L += ["| ID | Market | Side/Style | Conf | Tier | Qty @ avg | All-in | % BR | Fair | EV net | CLV | Status | Why |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in pl:
            L.append(f"| {r['id']} | {r['title'] or r['ticker']} | {r['side'].upper()}/{r['style']} | {r['confidence']} | "
                     f"{float(r['tier_pct'] or 0):.0%} | {r['contracts']} @ {float(r['avg_entry']):.2f} | ${float(r['all_in_cost']):.2f} | "
                     f"{float(r['pct_bankroll'] or 0):.1%} | {float(r['fair_prob']):.2f} | {float(r['ev_per_contract']) * 100:+.1f}c "
                     f"(${float(r['ev_total']):.2f}) | {fmt_clv(r)} | {r['status']}{' ' + r['result'] if r['result'] and r['result'] != r['status'] else ''} | {r['reasoning']} |")
        L += ["", "**Fee math (Kalshi taker: roundup(0.07 x C x P x (1-P)) per fill):**"]
        L += [f"- #{r['id']}: {r['fee_math']} | spread ${float(r['spread_cost']):.2f}, slippage ${float(r['slippage_cost']):.2f}"
              + (f", exit fee ${float(r['exit_fee']):.2f}" if r["exit_fee"] else "") for r in pl]
    else:
        L.append("None. Nothing cleared confidence 75 AND the after-cost edge hurdle. A pass is a position.")
    L += ["", f"## Passed ({len(ps)})"]
    L += [f"- #{r['id']} {r['title'] or r['ticker']} {r['side'].upper()} (conf {r['confidence']}, fair {float(r['fair_prob']):.2f}, "
          f"ask {fnum(r['best_ask'], 0):.2f}): {r['reasoning']}" for r in ps] or ["- none"]
    L += ["", f"**Exposure:** ${exp:.2f} = {exp / rb:.1%} of run bankroll vs {cfg['nightly_exposure_cap']:.0%} cap "
          f"(${rb * cfg['nightly_exposure_cap']:.2f})." + (f" Budgets scaled x{meta['scale']:.2f}." if meta.get("scale", 1) < 1 else ""),
          f"**Fees tonight:** ${fees:.2f}", "", "**Correlation flags:**"]
    fl = list(meta.get("slate_flags", []))
    for r in pl:
        fl += [f for f in r["correlation_flags"].split(" | ") if f and f not in fl]
    L += [f"- {f}" for f in fl] or ["- none"]
    L += [f"- Note: {n}" for n in meta.get("notes", [])]
    L += ["", f"**CLV tonight:** {clv_line(pl)}  |  **all-time:** {clv_line([r for r in allrows if r['status'] != 'passed'])}", "",
          f"**Risk note:** {cfg['tier_75_pct']:.0%}/{cfg['tier_85_pct']:.0%}/{cfg['tier_95_pct']:.0%} per trade is far above "
          "1-5% Kelly-style sizing; with up to 60% exposed a night, one bad night can cut the bankroll by more than half.", ""]
    path = os.path.join(SUMMARIES, f"{d}.md")
    os.makedirs(SUMMARIES, exist_ok=True)
    open(path, "w").write("\n".join(L))
    return path


def fmt_clv(r):
    if r.get("clv_cents", "") == "":
        return "pending"
    return f"{float(r['clv_cents']):+.1f}c / {float(r['clv_prob']) * 100:+.1f}pp"


def clv_line(rows):
    c = [r for r in rows if r.get("clv_cents", "") != ""]
    if not c:
        return "no closing prices recorded yet"
    beat = sum(float(r["clv_cents"]) > 0 for r in c)
    return (f"{len(c)} closed, beat close {beat}/{len(c)}, avg {sum(float(r['clv_cents']) for r in c) / len(c):+.2f}c "
            f"({sum(float(r['clv_prob']) for r in c) / len(c) * 100:+.2f}pp implied)")


def side_mid(cfg, ticker, side):
    bk = fetch_book(cfg, ticker)
    a, b = asks_for(bk, side), bids_for(bk, side)
    if not a or not b:
        sys.exit("No two-sided book for live price.")
    return (a[0][0] + b[0][0]) / 2


def find(rows, i):
    for r in rows:
        if r["id"] == str(i):
            return r
    sys.exit(f"#{i} not found")


def cmd_close(a, cfg):
    rows = read_bets(); r = find(rows, a.id)
    if r["status"] == "passed":
        sys.exit("CLV is tracked on placed trades only.")
    px = a.price if a.price is not None else side_mid(cfg, r["ticker"], r["side"])
    c = (px - float(r["avg_entry"])) * 100
    r.update(close_price=f"{px:.4f}", clv_cents=f"{c:.2f}", clv_prob=f"{c / 100:.4f}", closed_at=now())
    write_bets(rows); write_summary(r["date"], cfg)
    print(f"#{a.id} {r['ticker']} {r['side'].upper()} entry {float(r['avg_entry']):.4f} -> last pre-tip {px:.4f}: CLV {fmt_clv(r)}")


def book_pnl(r, b, pnl, res, status):
    b["current"] = round(b["current"] + pnl, 2)
    r.update(status=status, result=res, pnl=f"{pnl:.2f}", bankroll_after=f"{b['current']:.2f}", settled_at=now())
    b.setdefault("history", []).append({"ts": now(), "id": int(r["id"]), "result": res, "pnl": round(pnl, 2),
                                        "bankroll": b["current"]})


def exit_quote(book, side, n, cfg):
    fills = walk(bids_for(book, side), n, cfg)
    if not fills:
        return None
    proceeds = sum(f["price"] * f["qty"] for f in fills)
    fee = sum(f["fee"] for f in fills)
    return {"proceeds": proceeds, "fee": fee, "avg": proceeds / n, "net": proceeds - fee,
            "fee_math": " + ".join(f"ceil({cfg['taker_fee_rate']:g}x{f['qty']}x{f['price']:.2f}x{1 - f['price']:.2f})=${f['fee']:.2f}" for f in fills)}


def cmd_exit(a, cfg):
    rows, b = read_bets(), load_bankroll(); r = find(rows, a.id)
    if r["status"] != "placed":
        sys.exit(f"#{a.id} is {r['status']}")
    n = int(r["contracts"])
    if a.price is not None:
        fee = kalshi_fee(cfg, a.price, n)[0]
        q = {"proceeds": a.price * n, "fee": fee, "avg": a.price, "net": a.price * n - fee,
             "fee_math": f"ceil({cfg['taker_fee_rate']:g}x{n}x{a.price:.2f}x{1 - a.price:.2f})=${fee:.2f}"}
    else:
        q = exit_quote(fetch_book(cfg, r["ticker"]), r["side"], n, cfg)
        if not q:
            sys.exit("Not enough bid depth to exit.")
    pnl = q["net"] - float(r["all_in_cost"])
    r.update(exit_price=f"{q['avg']:.4f}", exit_fee=f"{q['fee']:.2f}", fee_math=r["fee_math"] + f" | EXIT {q['fee_math']}")
    book_pnl(r, b, pnl, "exited", "exited")
    write_bets(rows); save_bankroll(b); write_summary(r["date"], cfg)
    print(f"#{a.id} paper exit {n} @ {q['avg']:.4f}, exit fee ${q['fee']:.2f}: P/L {pnl:+.2f}. Bankroll ${b['current']:.2f}")


def cmd_settle(a, cfg):
    rows, b = read_bets(), load_bankroll(); r = find(rows, a.id)
    if r["status"] != "placed":
        sys.exit(f"#{a.id} is {r['status']}; only open placed trades settle.")
    n, cost = int(r["contracts"]), float(r["all_in_cost"])
    pnl = {"win": n * 1.0 - cost, "loss": -cost, "void": 0.0}[a.result]  # no settlement fee
    book_pnl(r, b, pnl, a.result, "settled")
    write_bets(rows); save_bankroll(b); write_summary(r["date"], cfg)
    print(f"#{a.id} {a.result}: P/L {pnl:+.2f}. Bankroll ${b['current']:.2f}")


def cmd_status(a, cfg):
    b, rows = load_bankroll(), read_bets()
    done = [r for r in rows if r["status"] in ("settled", "exited")]
    w = sum(fnum(r["pnl"], 0) > 0 for r in done); l = sum(fnum(r["pnl"], 0) < 0 for r in done)
    pnl = b["current"] - b["starting"]
    fees = sum(fnum(r["entry_fee"], 0) + fnum(r["exit_fee"], 0) for r in rows if r["status"] != "passed")
    print(f"Bankroll ${b['current']:.2f} (start ${b['starting']:.2f}, P/L {pnl:+.2f}, {pnl / b['starting']:+.1%}) | W-L {w}-{l} | fees paid ${fees:.2f}")
    print(f"Next run: tiers ${b['current'] * cfg['tier_75_pct']:.2f}/${b['current'] * cfg['tier_85_pct']:.2f}/"
          f"${min(b['current'] * cfg['tier_95_pct'], cfg['per_trade_cap']):.2f} (cap ${cfg['per_trade_cap']}), "
          f"nightly cap ${b['current'] * cfg['nightly_exposure_cap']:.2f}")
    print("CLV:", clv_line([r for r in rows if r["status"] != "passed"]))
    op = [r for r in rows if r["status"] == "placed"]
    print(f"Passed logged: {sum(r['status'] == 'passed' for r in rows)} | Open ({len(op)}): " +
          ("; ".join(f"#{r['id']} {r['ticker']} {r['side']} x{r['contracts']} ${float(r['all_in_cost']):.2f}" for r in op) or "none"))


def cmd_reset(a, cfg):
    amt = float(a.bankroll if a.bankroll is not None else cfg["starting_bankroll"])
    save_bankroll({"starting": amt, "current": amt, "history": []})
    write_bets([])
    for f in glob.glob(os.path.join(SUMMARIES, "*.md")) + glob.glob(os.path.join(SUMMARIES, "*.json")):
        os.remove(f)
    print(f"Paper ledger cleared. Bankroll ${amt:.2f}.")


def cmd_fee(a, cfg):
    fee, raw = kalshi_fee(cfg, a.price, a.contracts, maker=a.maker)
    rate = cfg["maker_fee_rate"] if a.maker else cfg["taker_fee_rate"]
    print(f"{'maker' if a.maker else 'taker'} fee = roundup({cfg['fee_multiplier']} x {rate} x {a.contracts} x {a.price} x {1 - a.price:.2f}) "
          f"= roundup({raw:.4f}) = ${fee:.2f}")


def cmd_markets(a, cfg):
    tag = None
    if a.date:
        dt = datetime.strptime(a.date, "%Y-%m-%d")
        tag = dt.strftime("%y%b%d").upper()
    for s in str(cfg["series"]).split(","):
        s = s.strip()
        ser = api_get(cfg, f"/series/{s}")["series"]
        print(f"== {s}: {ser['title']} (fee_type {ser.get('fee_type')}, multiplier {ser.get('fee_multiplier')})",
              flush=True)
        evs = api_get(cfg, f"/events?series_ticker={s}&status=open&limit=200").get("events", [])
        n = 0
        for e in evs:
            if tag and tag not in e["event_ticker"]:
                continue
            n += 1
            line = f"  {e['event_ticker']:<32} {e.get('title', '')}"
            # Quotes are an extra API call each; default on for game markets, always with --verbose.
            if s == "KXNBAGAME" or a.verbose:
                ms = api_get(cfg, f"/markets?event_ticker={e['event_ticker']}").get("markets", [])
                line += "  | " + "  ".join(f"{m['ticker'].split('-')[-1]} {m.get('yes_bid_dollars')}/{m.get('yes_ask_dollars')}" for m in ms[:4])
            print(line, flush=True)
        if tag and n == 0:
            print(f"  (no open {s} events matching {tag})", flush=True)


def cmd_book(a, cfg):
    bk = fetch_book(cfg, a.ticker)
    asks, bids = asks_for(bk, a.side), bids_for(bk, a.side)
    print(f"LIVE {a.ticker} BUY {a.side.upper()}: best bid {bids[0][0] if bids else '-'} | best ask {asks[0][0] if asks else '-'}")
    print("  asks (price x qty):", ", ".join(f"{p:.2f}x{q:g}" for p, q in asks[:6]))
    print("  bids (price x qty):", ", ".join(f"{p:.2f}x{q:g}" for p, q in bids[:6]))
    if a.contracts and asks:
        fair = a.fair if a.fair is not None else asks[0][0]
        q = entry_quote(bk, a.side, a.contracts, fair, a.style, cfg)
        if q:
            print(f"  {a.contracts} contracts: avg {q['avg_entry']:.4f}, notional ${q['notional']:.2f}, fee ${q['entry_fee']:.2f}, "
                  f"all-in ${q['all_in_cost']:.2f}, spread ${q['spread_cost']:.2f}, slippage ${q['slippage_cost']:.2f}")
            print(f"  fee math: {q['fee_math']}")
            print(f"  EV vs fair {fair:.2f}: {q['ev_per_contract'] * 100:+.2f}c/contract, hurdle {'CLEARED' if clears_hurdle(q, cfg) else 'NOT cleared'}")
        else:
            print("  not enough depth for that size")


def load_fair(path, cli):
    fair = {}
    if path and os.path.exists(path):
        fair.update(json.load(open(path)))
    for kv in cli or []:
        t, p = kv.split("=")
        fair.setdefault(t, {})["fair"] = float(p)
    return fair


# ---------------- in-game exit rules (exits.py) -> paper ledger ----------------
EXIT_TAG = re.compile(r"\[(profit_target#\d+|blowout|q3_review|confidence_decay|stop_loss)\]")


def manage_exits(r, bk, side, game, states, f, cfg, d, say, dry_run, now_ts=None):
    """Run exits.decide() for one open position and write any trim/exit/hold to the paper ledger.
    Returns True if the position changed this poll (caller then skips the legacy fair-value exit)."""
    ex = exits.exit_config(cfg)
    if not ex["exit_rules_enabled"]:
        return False
    a_, b_ = asks_for(bk, side), bids_for(bk, side)
    if not b_:
        return False
    bid, ask = b_[0][0], (a_[0][0] if a_ else None)
    eb, ea = fnum(r.get("best_bid")), fnum(r.get("best_ask"))
    pos = {"id": r["id"], "contracts": int(r["contracts"]), "cost": float(r["all_in_cost"]),
           "entry_conf": float(r["confidence"]), "p0": float(r["fair_prob"]),
           "entry_mid": (eb + ea) / 2 if eb is not None and ea is not None else None,
           "team": f.get("team") or exits.position_team(r["ticker"], side, r.get("event") or None),
           "done": set(EXIT_TAG.findall(r.get("reasoning", "")))}
    mkt = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2 if ask is not None else bid,
           "quote": lambda k: exit_quote(bk, side, k, cfg)}
    lc = f.get("live_confidence")
    dec = exits.decide(pos, mkt, game, states.setdefault(r["id"], {}), ex, now_ts or time.time(),
                       None if lc is None else float(lc))
    act, tag = dec["action"], f"#{r['id']} {r['ticker']} {side} x{pos['contracts']}"
    if act in ("none", "wait"):
        if act == "wait":
            say(f"WATCH {tag}: {dec['reason']}")
        return False
    if act == "hold":
        say(f"{'FLAG ' if dry_run else ''}HOLD [{dec['rule']}] {tag}: {dec['reason']}")
        if not dry_run and dec.get("tag"):
            allr = read_bets(); rr = find(allr, r["id"])
            rr["reasoning"] += f" | [{dec['rule']}] HOLD: {dec['reason']}"
            write_bets(allr)
        return False
    q, k, n = dec["q"], dec["k"], pos["contracts"]
    msg = (f"{act.upper()} [{dec['rule']}] {tag}: sell {k} @ avg {q['avg']:.4f}, proceeds ${q['proceeds']:.2f} "
           f"- exit fee ${q['fee']:.2f} = net ${q['net']:.2f} | {dec['reason']}")
    if dry_run:
        say("FLAG " + msg); return False
    allr, b = read_bets(), load_bankroll(); rr = find(allr, r["id"])
    if act == "exit":
        pnl = q["net"] - float(rr["all_in_cost"])
        rr.update(exit_price=f"{q['avg']:.4f}", exit_fee=f"{q['fee']:.2f}", fee_math=rr["fee_math"] + f" | EXIT {q['fee_math']}",
                  reasoning=rr["reasoning"] + f" | [{dec['rule']}] EXIT {k}: {dec['reason']}")
        book_pnl(rr, b, pnl, "exited", "exited")
    else:  # trim: split sold contracts into a child row (status 'trimmed'); parent keeps the remainder
        share = k / n
        child = dict(rr, id=new_id(allr), contracts=k)
        for fld in ("all_in_cost", "entry_fee", "ev_total", "spread_cost", "slippage_cost", "exit_cost_est"):
            v = fnum(rr.get(fld))
            if v is not None:
                child[fld] = f"{v * share:.2f}"; rr[fld] = f"{v - v * share:.2f}"
        rr["contracts"] = n - k
        pnl = q["net"] - float(child["all_in_cost"])
        child.update(exit_price=f"{q['avg']:.4f}", exit_fee=f"{q['fee']:.2f}",
                     fee_math=rr["fee_math"] + f" | EXIT {q['fee_math']}",
                     reasoning=f"[{dec['rule']}] TRIM {k}/{n} of #{rr['id']}: {dec['reason']}")
        rr["reasoning"] += f" | [{dec['rule']}] trimmed {k}/{n} -> #{child['id']}"
        book_pnl(child, b, pnl, "trim", "trimmed")
        allr.append(child)
    write_bets(allr); save_bankroll(b)
    say(f"PAPER {msg} -> P/L {pnl:+.2f}, bankroll ${b['current']:.2f}")
    return True


def cmd_monitor(a, cfg):
    """Poll orderbooks; paper-enter/exit only when EV clears fees + spread + slippage + hurdle."""
    interval = max(float(a.interval or cfg["poll_seconds"]), float(cfg["min_poll_seconds"]))
    fpath = a.fair_file or os.path.join(FAIR_DIR, f"{a.event}.json")
    markets = [m["ticker"] for m in api_get(cfg, f"/markets?event_ticker={a.event}").get("markets", [])]
    if not markets:
        sys.exit(f"No markets for {a.event}")
    d = date.today().isoformat()
    mode = "DRY-RUN (flag only)" if a.dry_run else "PAPER-EXECUTE"
    log = os.path.join(DATA, f"monitor_{a.event}.log")
    print(f"Monitoring {a.event} ({len(markets)} markets) every {interval:.0f}s, {mode}. Fair file: {os.path.relpath(fpath, ROOT)}")

    def say(msg):
        line = f"{now()} {msg}"
        print(line); open(log, "a").write(line + "\n")

    polls, exit_states = 0, {}
    while a.max_polls is None or polls < a.max_polls:
        polls += 1
        fair = load_fair(fpath, a.fair)  # re-read each poll so the agent can update fair probs live
        try:  # live game state for exit rules (ESPN public scoreboard, read-only)
            game = exits.fetch_game(a.event, cfg)
        except Exception as e:
            game = None; say(f"game state unavailable ({e}); blowout/Q3 rules skipped this poll")
        rows, bank = read_bets(), load_bankroll()["current"]
        tonight = [r for r in rows if r["date"] == d and r["status"] == "placed"]
        exposure = sum(float(r["all_in_cost"]) for r in tonight)
        cap = bank * cfg["nightly_exposure_cap"]
        n_tonight = len([r for r in rows if r["date"] == d and r["status"] in ("placed", "settled", "exited")])
        for t in markets:
            try:
                bk = fetch_book(cfg, t)
            except RuntimeError as e:
                say(f"{t} fetch error: {e}"); continue
            for side in ("yes", "no"):
                a_, b_ = asks_for(bk, side), bids_for(bk, side)
                bb, ba = (b_[0][0] if b_ else None), (a_[0][0] if a_ else None)
                f = fair.get(t, {})
                fp = f.get("fair")
                if fp is not None and side == "no":
                    fp = 1 - fp
                # exits: open positions on this ticker/side
                for r in [r for r in tonight if r["ticker"] == t and r["side"] == side]:
                    if manage_exits(r, bk, side, game, exit_states, f, cfg, d, say, a.dry_run):
                        continue  # in-game exit rule acted (priority over the fair-value exit below)
                    if fp is None:
                        continue
                    xq = exit_quote(bk, side, int(r["contracts"]), cfg)
                    if not xq:
                        continue
                    hold_ev = fp * int(r["contracts"])  # settlement: $1 x prob, no settlement fee
                    gain = xq["net"] - hold_ev
                    if gain / int(r["contracts"]) * 100 >= cfg["min_edge_cents"]:
                        msg = (f"EXIT #{r['id']} {t} {side} x{r['contracts']}: sell net ${xq['net']:.2f} "
                               f"(fees {xq['fee_math']}) > hold EV ${hold_ev:.2f} by {gain / int(r['contracts']) * 100:.1f}c/contract")
                        if a.dry_run:
                            say("FLAG " + msg)
                        else:
                            pnl = xq["net"] - float(r["all_in_cost"])
                            allr, b = read_bets(), load_bankroll(); rr = find(allr, r["id"])
                            rr.update(exit_price=f"{xq['avg']:.4f}", exit_fee=f"{xq['fee']:.2f}",
                                      fee_math=rr["fee_math"] + f" | EXIT {xq['fee_math']}",
                                      reasoning=rr["reasoning"] + f" | monitor exit: {msg}")
                            book_pnl(rr, b, pnl, "exited", "exited"); write_bets(allr); save_bankroll(b)
                            say("PAPER " + msg + f" -> P/L {pnl:+.2f}")
                # entries
                if fp is None or ba is None:
                    continue
                conf = float(f.get("confidence", 0))
                style = f.get("style", "trade")
                q1 = entry_quote(bk, side, 1, fp, style, cfg)
                if not clears_hurdle(q1, cfg):
                    continue
                why = f.get("reasoning", "")
                if conf < cfg["min_confidence"]:
                    say(f"SKIP {t} {side}: edge {q1['ev_per_contract'] * 100:+.1f}c but confidence {conf:.0f} < {cfg['min_confidence']}")
                    continue
                if any(r["ticker"] == t for r in tonight) or n_tonight >= cfg["max_picks"]:
                    continue
                budget = min(bank * tier_pct(conf, cfg), cfg["per_trade_cap"], cap - exposure)
                q = max_contracts(bk, side, budget, fp, style, cfg) if budget > 0 else None
                if not clears_hurdle(q, cfg):
                    say(f"SKIP {t} {side}: edge but no room under caps (exposure ${exposure:.2f}/${cap:.2f})")
                    continue
                msg = (f"ENTRY {t} BUY {side.upper()} x{q['contracts']} avg {q['avg_entry']:.4f} all-in ${q['all_in_cost']:.2f} "
                       f"| fair {fp:.2f} conf {conf:.0f} | EV {q['ev_per_contract'] * 100:+.2f}c ({q['edge_pct']:+.1%}) after "
                       f"fee ${q['entry_fee']:.2f}, spread ${q['spread_cost']:.2f}, slip ${q['slippage_cost']:.2f}, "
                       f"est exit ${q['exit_cost_est']:.2f} | {q['fee_math']} | {why}")
                if a.dry_run:
                    say("FLAG " + msg)
                else:
                    allr = read_bets()
                    p = {"ticker": t, "event": a.event, "title": f.get("title", t), "side": side, "style": style,
                         "fair_prob": fp, "confidence": conf, "reasoning": "monitor: " + why, "tier_pct": tier_pct(conf, cfg),
                         "consensus": f.get("consensus", ""), "source": "LIVE"}
                    r = row_from(p, d, "LIVE", "placed", q); r["id"] = new_id(allr)
                    r["pct_bankroll"] = f"{q['all_in_cost'] / bank:.4f}"
                    allr.append(r); write_bets(allr)
                    tonight.append(r); exposure += q["all_in_cost"]; n_tonight += 1
                    say("PAPER " + msg)
        if not fair:
            say(f"poll {polls}: no fair probs in {os.path.relpath(fpath, ROOT)}; watching only")
        else:
            say(f"poll {polls}: done; exposure ${exposure:.2f}/${cap:.2f}")
        if a.max_polls is None or polls < a.max_polls:
            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("markets"); s.add_argument("--date"); s.add_argument("--verbose", action="store_true")
    s = sp.add_parser("book"); s.add_argument("ticker"); s.add_argument("--side", default="yes", choices=["yes", "no"])
    s.add_argument("--contracts", type=int); s.add_argument("--fair", type=float); s.add_argument("--style", default="hold")
    s = sp.add_parser("fee"); s.add_argument("--price", type=float, required=True)
    s.add_argument("--contracts", type=int, required=True); s.add_argument("--maker", action="store_true")
    s = sp.add_parser("size"); s.add_argument("picks"); s.add_argument("--bankroll", type=float)
    s = sp.add_parser("run"); s.add_argument("picks"); s.add_argument("--date"); s.add_argument("--replace", action="store_true")
    s = sp.add_parser("monitor"); s.add_argument("--event", required=True); s.add_argument("--interval", type=float)
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--max-polls", type=int)
    s.add_argument("--fair-file"); s.add_argument("--fair", action="append", help="TICKER=prob (YES side)")
    for name in ("close", "exit"):
        s = sp.add_parser(name); s.add_argument("id"); s.add_argument("--price", type=float); s.add_argument("--live", action="store_true")
    s = sp.add_parser("settle"); s.add_argument("id"); s.add_argument("result", choices=["win", "loss", "void"])
    s = sp.add_parser("summary"); s.add_argument("--date")
    sp.add_parser("status")
    s = sp.add_parser("reset"); s.add_argument("--bankroll", type=float)
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd in ("close", "exit") and a.price is None and not a.live:
        sys.exit("Give --price P or --live")
    {"markets": cmd_markets, "book": cmd_book, "fee": cmd_fee, "size": cmd_size, "run": cmd_run,
     "monitor": cmd_monitor, "close": cmd_close, "exit": cmd_exit, "settle": cmd_settle,
     "summary": lambda a, c: print(open(write_summary(a.date or date.today().isoformat(), c)).read()),
     "status": cmd_status, "reset": cmd_reset}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
