"""Reef in-game exit rules (PAPER only).

Pure decision functions (no I/O) plus one read-only fetch of ESPN's public NBA scoreboard.
`reef.py monitor` calls `decide()` once per open position per poll and writes the result to
the paper ledger. Nothing here can place an order.

Priority (first rule that acts wins): confidence decay -> stop-loss -> blowout -> Q3 review -> profit target.
Every sell passes the fee-aware hold check: if the upside left to $1 is smaller than the exit fee +
spread (+ depth slippage), or the sale nets <= $0 after fees, hold to settlement instead.
"""
import json, math, urllib.request
from statistics import NormalDist

DEFAULTS = {
    "exit_rules_enabled": True,
    "profit_targets": "0.30:0.40",          # net-mark gain : fraction of contracts to sell; each tier fires once
    "stop_loss_pct": 0.15,                  # exit remainder when net mark <= -15% vs entry cost
    "stop_debounce_polls": 2,               # breach must hold this many consecutive polls...
    "stop_debounce_seconds": 0,             # ...or, if > 0, this many seconds since first breach (time mode)
    "blowout_thresholds": "3:4:00:20, 4:12:00:15, 4:3:00:10",   # period:clock_left:lead
    "blowout_trim_fraction": 0.50,          # right side of a blowout: trim 50%
    "q3_review": True,                      # mandatory review once Q3 has ended
    "q3_exit_deficit": 10,                  # losing at Q3 review AND trailing by >= this -> exit
    "trim_steps": "0.25,0.40,0.50",         # partial-scaling ladder (Q3 losing trims pick by severity)
    "confidence_decay": True,
    "decay_buffer": 3,                      # exit when live conf < entry conf - buffer (0 = strict)
    "decay_debounce_polls": 2,              # live read must stay below for this many polls
    "decay_sigma": 12.0,                    # NBA final-margin std dev (points) for the live win-prob model
    "decay_w_model": 0.5,                   # conf points per 1pp change in live model probability
    "decay_w_price": 0.25,                  # conf points per 1c move in the Kalshi mid vs entry mid
    "espn_scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
}
PRIORITY = ("confidence_decay", "stop_loss", "blowout", "q3_review", "profit_target")
# Kalshi ticker codes -> ESPN abbreviations (where they differ)
ESPN_ALIAS = {"NYK": "NY", "GSW": "GS", "SAS": "SA", "NOP": "NO", "UTA": "UTAH", "WAS": "WSH", "PHO": "PHX", "BRK": "BKN"}
QUARTER = 720.0
GAME = 4 * QUARTER


# ---------------- config parsing ----------------
def _list(s):
    return [x.strip() for x in str(s).split(",") if x.strip()]


def clock_secs(c):
    """'4:00' -> 240.0, '45.2' -> 45.2, 240 -> 240.0"""
    if isinstance(c, (int, float)):
        return float(c)
    parts = str(c).strip().split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def exit_config(cfg):
    """Merge the `exits:` section (PyYAML -> nested dict; stdlib flat parser -> top-level keys) over defaults."""
    src = cfg.get("exits") if isinstance(cfg.get("exits"), dict) else cfg
    ex = dict(DEFAULTS)
    ex.update({k: src[k] for k in DEFAULTS if k in src})
    ex["_targets"] = sorted((float(g), float(f)) for g, f in (t.split(":") for t in _list(ex["profit_targets"])))
    bl = []
    for t in _list(ex["blowout_thresholds"]):
        p, rest = t.split(":", 1)
        clk, lead = rest.rsplit(":", 1)
        bl.append((int(p), clock_secs(clk), float(lead)))
    ex["_blowouts"] = bl
    ex["_steps"] = sorted(float(x) for x in _list(ex["trim_steps"]))
    return ex


# ---------------- game state (ESPN public scoreboard, read-only) ----------------
def teams_from_event(event):
    """KXNBAGAME-26OCT22BOSNYK -> ('26OCT22', 'BOS', 'NYK')  (away, home)."""
    seg = event.split("-")[1]
    return seg[:7], seg[7:10], seg[10:13]


def position_team(ticker, side, event=None):
    """Team the position wins with. Game/spread tickers end in the team code; NO flips it. Totals -> None."""
    parts = ticker.split("-")
    if parts[0] == "KXNBATOTAL" or len(parts) < 3:
        return None
    _, away, home = teams_from_event(event or "-".join(parts[:2]))
    code = "".join(ch for ch in parts[-1] if ch.isalpha())
    if code not in (away, home):
        return None
    return code if side == "yes" else (home if code == away else away)


def parse_espn(js, away, home):
    """Find the game in an ESPN scoreboard payload and normalize it. Pure."""
    want = {ESPN_ALIAS.get(away, away), ESPN_ALIAS.get(home, home)}
    back = {ESPN_ALIAS.get(t, t): t for t in (away, home)}
    for ev in js.get("events", []):
        c = ev["competitions"][0]
        abbr = {x["team"]["abbreviation"]: x for x in c["competitors"]}
        if set(abbr) != want:
            continue
        st = c.get("status") or ev.get("status") or {}
        return {"state": st.get("type", {}).get("state", "pre"), "period": int(st.get("period") or 0),
                "clock": float(st.get("clock") or 0.0), "detail": st.get("type", {}).get("shortDetail", ""),
                "scores": {back[a]: int(float(x.get("score") or 0)) for a, x in abbr.items()}}
    return None


def fetch_game(event, cfg):
    """Read-only GET of ESPN's public scoreboard for the event's date; returns normalized game state or None."""
    ex = exit_config(cfg)
    d, away, home = teams_from_event(event)
    from datetime import datetime
    ymd = datetime.strptime(d, "%y%b%d").strftime("%Y%m%d")
    req = urllib.request.Request(f"{ex['espn_scoreboard']}?dates={ymd}", headers={"User-Agent": "reef-paper/1.0"})
    return parse_espn(json.load(urllib.request.urlopen(req, timeout=10)), away, home)


def remaining_frac(g):
    if g["period"] <= 0:
        return 1.0
    left = (4 - g["period"]) * QUARTER + g["clock"] if g["period"] <= 4 else g["clock"]
    return max(left / GAME, 1 / GAME)


def lead_for(g, team):
    other = [s for t, s in g["scores"].items() if t != team]
    return g["scores"].get(team, 0) - (other[0] if other else 0)


def fmt_clock(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def game_str(g):
    return f"Q{g['period']} {fmt_clock(g['clock'])} " + "-".join(f"{t} {s}" for t, s in g["scores"].items())


def blowout(g, team, ex):
    """First matching (period, clock_left, lead) threshold. A threshold also applies in any later period."""
    if not g or not team or g["state"] != "in":
        return None
    lead = lead_for(g, team)
    for p, c, L in ex["_blowouts"]:
        if (g["period"] > p or (g["period"] == p and g["clock"] <= c)) and abs(lead) >= L:
            return {"side": "right" if lead > 0 else "wrong", "lead": lead,
                    "rule": f"lead {abs(lead):.0f}>={L:.0f} (Q{p} <= {fmt_clock(c)} left or later)"}
    return None


def q3_over(g):
    return bool(g) and g["state"] == "in" and (g["period"] >= 4 or (g["period"] == 3 and g["clock"] <= 0))


# ---------------- live confidence ----------------
def live_confidence(pos, g, mid, ex):
    """entry conf + w_model * (live model prob - entry fair, pp) + w_price * (Kalshi mid - entry mid, c).
    Live model: P(win) = Phi((lead + mu*r) / (sigma*sqrt(r))), mu = sigma*Phi^-1(entry fair), r = time left fraction."""
    nd = NormalDist()
    conf, p0, parts = pos["entry_conf"], min(max(pos["p0"], 0.01), 0.99), []
    model_pts = 0.0
    if g and pos.get("team") and g["state"] == "in":
        r, sig = remaining_frac(g), float(ex["decay_sigma"])
        mu = sig * nd.inv_cdf(p0)
        prob = nd.cdf((lead_for(g, pos["team"]) + mu * r) / (sig * math.sqrt(r)))
        model_pts = ex["decay_w_model"] * 100 * (prob - p0)
        parts.append(f"model {p0:.2f}->{prob:.2f} ({model_pts:+.1f})")
    price_pts = ex["decay_w_price"] * 100 * (mid - pos["entry_mid"]) if mid is not None and pos.get("entry_mid") else 0.0
    if price_pts:
        parts.append(f"mid {pos['entry_mid']:.3f}->{mid:.3f} ({price_pts:+.1f})")
    return max(0.0, min(100.0, conf + model_pts + price_pts)), ", ".join(parts) or "no live inputs"


# ---------------- fee-aware hold check ----------------
def hold_check(q, k, bid, ask):
    """True = hold to settlement instead of selling k contracts.
    Sell only if upside left to $1 (1 - bid) >= exit fee/contract + spread + depth slippage, and the sale nets > $0."""
    if not q:
        return True, "no bid depth for that size"
    fee_pc = q["fee"] / k
    spread = (ask if ask is not None else 1.0) - bid
    slip = bid - q["avg"]
    upside = 1.0 - bid
    math_ = (f"upside to $1 {upside:.3f} vs exit fee {fee_pc:.4f} + spread {spread:.3f} + slip {slip:.3f} "
             f"= {fee_pc + spread + slip:.4f}/contract")
    if q["net"] <= 0:
        return True, f"sale nets ${q['net']:.2f} after fees; let it settle ({math_})"
    if upside < fee_pc + spread + slip:
        return True, f"HOLD TO SETTLEMENT: {math_}"
    return False, math_


# ---------------- decision ----------------
def _sell(rule, frac, n, quote, bid, ask, why, ex):
    k = n if frac >= 1 else max(1, min(n, int(round(n * frac))))
    q = quote(k)
    hold, hm = hold_check(q, k, bid, ask)
    if hold:
        return {"action": "hold", "rule": rule, "k": 0, "q": q, "reason": f"{why} | fee-aware check: {hm}", "tag": True}
    return {"action": "exit" if k >= n else "trim", "rule": rule, "k": k, "q": q, "reason": f"{why} | {hm}"}


def decide(pos, mkt, g, st, ex, now_ts, live_conf=None):
    """One poll for one open position. Pure apart from mutating `st` (per-position debounce counters).

    pos: id, contracts, cost (all-in $ for the remaining contracts), entry_conf, p0, entry_mid, team, done (set of
         rule tags already fired: 'profit_target#1', 'blowout', 'q3_review').
    mkt: bid, ask, mid (for the side held), quote(k) -> exit quote dict (proceeds, fee, avg, net, fee_math) or None.
    g:   normalized game state or None. live_conf: optional override (fair file 'live_confidence').
    Returns {action: exit|trim|hold|wait|none, rule, k, q, reason, mark, conf}.
    """
    n, cost = int(pos["contracts"]), float(pos["cost"])
    base = {"action": "none", "rule": "", "k": 0, "q": None}
    if n <= 0 or (g and g["state"] == "post"):
        return dict(base, reason="closed or game over (settles)")
    qf = mkt["quote"](n)
    if not qf:
        return dict(base, reason="no bid depth to mark position")
    mark = qf["net"] / cost - 1
    if live_conf is not None:
        conf, cdet = float(live_conf), "agent live re-score"
    else:
        conf, cdet = live_confidence(pos, g, mkt["mid"], ex)
    mk = (f"net mark ${qf['net']:.2f} (walk {n} @ avg {qf['avg']:.4f} - exit fee ${qf['fee']:.2f}) vs cost ${cost:.2f} "
          f"= {mark:+.1%}")
    ctx = mk + (f" | {game_str(g)}" if g else "")
    out = lambda d: dict(d, mark=mark, conf=conf)
    bid, ask, done, sell = mkt["bid"], mkt["ask"], pos.get("done", set()), []

    # debounce counters (always updated)
    decay_now = ex["confidence_decay"] and conf < pos["entry_conf"] - float(ex["decay_buffer"])
    st["decay_n"] = st.get("decay_n", 0) + 1 if decay_now else 0
    stop_now = mark <= -float(ex["stop_loss_pct"])
    if stop_now:
        st["stop_n"] = st.get("stop_n", 0) + 1
        st.setdefault("stop_t0", now_ts)
    else:
        st["stop_n"] = 0; st.pop("stop_t0", None)
    secs = float(ex["stop_debounce_seconds"])
    stop_ok = stop_now and ((now_ts - st["stop_t0"] >= secs) if secs > 0 else st["stop_n"] >= int(ex["stop_debounce_polls"]))

    # 1. confidence decay
    if decay_now and st["decay_n"] >= int(ex["decay_debounce_polls"]):
        why = (f"CONFIDENCE DECAY: live conf {conf:.1f} < entry {pos['entry_conf']:.0f}"
               f"{' - buffer %g' % ex['decay_buffer'] if ex['decay_buffer'] else ''} for {st['decay_n']} polls ({cdet}); "
               f"exit regardless of price | {ctx}")
        return out(_sell("confidence_decay", 1, n, mkt["quote"], bid, ask, why, ex))
    if decay_now:
        sell.append(f"decay pending {st['decay_n']}/{ex['decay_debounce_polls']} (conf {conf:.1f})")
    # 2. stop-loss
    if stop_ok:
        why = (f"STOP-LOSS: mark {mark:+.1%} <= -{float(ex['stop_loss_pct']):.0%}, confirmed "
               f"{'%d polls' % st['stop_n'] if secs <= 0 else '%.0fs' % (now_ts - st['stop_t0'])} | {ctx}")
        return out(_sell("stop_loss", 1, n, mkt["quote"], bid, ask, why, ex))
    if stop_now:
        sell.append(f"stop breach {st['stop_n']}/{ex['stop_debounce_polls']} (double-check pending)")
    # 3. blowout
    b = blowout(g, pos.get("team"), ex)
    if b and b["side"] == "wrong":
        return out(_sell("blowout", 1, n, mkt["quote"], bid, ask,
                         f"BLOWOUT WRONG SIDE: {pos['team']} trails, {b['rule']}; exit | {ctx}", ex))
    if b and "blowout" not in done:
        return out(_sell("blowout", float(ex["blowout_trim_fraction"]), n, mkt["quote"], bid, ask,
                         f"BLOWOUT RIGHT SIDE: {pos['team']} leads, {b['rule']}; trim "
                         f"{float(ex['blowout_trim_fraction']):.0%} | {ctx}", ex))
    # 4. mandatory end-of-Q3 review (once)
    q3 = None
    if ex["q3_review"] and q3_over(g) and "q3_review" not in done:
        lead = lead_for(g, pos["team"]) if pos.get("team") else 0
        steps = ex["_steps"]
        if mark < 0 and pos.get("team") and lead <= -float(ex["q3_exit_deficit"]):
            return out(_sell("q3_review", 1, n, mkt["quote"], bid, ask,
                             f"Q3 REVIEW: losing ({mark:+.1%}) and trailing by {-lead}; exit before Q4 | {ctx}", ex))
        if mark < 0:
            sev = -mark / float(ex["stop_loss_pct"])  # 0..1 of the way to the stop
            frac = steps[0] if sev < 1 / 3 else steps[min(1, len(steps) - 1)] if sev < 2 / 3 else steps[-1]
            d = _sell("q3_review", frac, n, mkt["quote"], bid, ask,
                      f"Q3 REVIEW: losing ({mark:+.1%}, {sev:.0%} of the way to stop), lead {lead:+d}; trim {frac:.0%} "
                      f"(scale out, don't carry full size into Q4) | {ctx}", ex)
            if d["action"] != "hold":
                return out(d)
            q3 = d
        else:
            q3 = {"action": "hold", "rule": "q3_review", "k": 0, "q": None, "tag": True,
                  "reason": f"Q3 REVIEW: HOLD, winning ({mark:+.1%}), lead {lead:+d}, live conf {conf:.1f} >= entry "
                            f"{pos['entry_conf']:.0f}; no blowout | {ctx}"}
    # 5. profit target tiers (each once)
    for i, (gain, frac) in enumerate(ex["_targets"], 1):
        if f"profit_target#{i}" in done:
            continue
        if mark >= gain:
            d = _sell(f"profit_target#{i}", frac, n, mkt["quote"], bid, ask,
                      f"PROFIT TARGET {i}: mark {mark:+.1%} >= +{gain:.0%}; sell {frac:.0%}, let the rest ride | {ctx}", ex)
            if d["action"] != "hold" or q3 is None:
                return out(d)
        break
    if q3:
        return out(q3)
    if sell:
        return out(dict(base, action="wait", reason="; ".join(sell) + f" | {ctx}"))
    return out(dict(base, reason=ctx))
