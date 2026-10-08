#!/usr/bin/env python3
"""NBA Bookie CLI: autonomous PAPER trader. NBA only.

"Placed" means written to data/bets.csv with status 'placed'. This tool has
no sportsbook integration, never places real bets, and never should.

Commands:
  lines    [--date YYYY-MM-DD]       pull NBA lines (ESPN public JSON; The Odds API if ODDS_API_KEY set)
  size     PICKS.json                dry run: threshold, sizing, caps, correlation flags (no writes)
  run      PICKS.json [--date D]     score gate -> size -> auto-place/pass in ledger -> write summary
  close    ID --line X --odds Y      record closing line/odds before tip; computes CLV
  settle   ID win|loss|push          settle a placed bet, update bankroll
  summary  [--date D]                (re)write data/summaries/D.md post-run report from the ledger
  log      --game G --pick P ...     manually log one pick (auto placed/passed by threshold, no sizing caps applied)
  status                             bankroll, record, open bets, CLV
  reset    [--bankroll 500]          wipe ledger and summaries, reset bankroll
"""
import argparse, csv, glob, json, math, os, sys, urllib.request
from datetime import date, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BANKROLL = os.path.join(DATA, "bankroll.json")
BETS = os.path.join(DATA, "bets.csv")
SUMMARIES = os.path.join(DATA, "summaries")
FIELDS = ["id", "date", "game", "team", "market", "pick", "line", "odds", "confidence", "stake",
          "pct_bankroll", "status", "reasoning", "correlation_flags", "close_line", "close_odds",
          "clv_pts", "clv_prob", "result", "profit", "bankroll_after", "placed_at", "closed_at",
          "settled_at"]
DEFAULTS = {"league": "NBA", "starting_bankroll": 500, "min_confidence": 75, "full_confidence": 95,
            "min_pct": 0.15, "max_pct": 0.20, "per_bet_cap": 100, "nightly_exposure_cap": 0.60,
            "max_picks": 5, "round_to": 1}


# ---------- config / storage ----------
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
    ypath, jpath = os.path.join(ROOT, "config.yaml"), os.path.join(ROOT, "config.json")
    if os.path.exists(ypath):
        try:
            import yaml  # optional
            with open(ypath) as f:
                cfg.update(yaml.safe_load(f) or {})
        except ImportError:  # stdlib fallback: flat key: value YAML
            with open(ypath) as f:
                for line in f:
                    line = line.split(" #", 1)[0].strip()
                    if line and not line.startswith("#") and ":" in line:
                        k, v = line.split(":", 1)
                        cfg[k.strip()] = _scalar(v)
    elif os.path.exists(jpath):
        with open(jpath) as f:
            cfg.update(json.load(f))
    if str(cfg.get("league", "NBA")).upper() != "NBA":
        sys.exit("This agent is NBA only.")
    return cfg


def load_bankroll():
    with open(BANKROLL) as f:
        return json.load(f)


def save_bankroll(b):
    b["updated"] = now()
    with open(BANKROLL, "w") as f:
        json.dump(b, f, indent=2)


def read_bets():
    if not os.path.exists(BETS):
        return []
    with open(BETS, newline="") as f:
        return list(csv.DictReader(f))


def write_bets(rows):
    with open(BETS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def now():
    return datetime.now().isoformat(timespec="seconds")


def fnum(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------- odds math ----------
def implied_prob(odds):
    odds = float(odds)
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def profit_on_win(stake, odds):
    odds = float(odds)
    return stake * (odds / 100) if odds > 0 else stake * (100 / -odds)


def compute_clv(row, close_line, close_odds):
    """CLV in points (spread/total) and implied probability. Positive = beat the close."""
    mkt, pick = row.get("market", "").lower(), row.get("pick", "").lower()
    bl = fnum(row.get("line"))
    pts = None
    if bl is not None and close_line is not None:
        if mkt == "spread":            # line from bettor's side: +3.5 -> +2.5 = +1.0
            pts = bl - close_line
        elif mkt == "total":
            pts = (close_line - bl) if "over" in pick else (bl - close_line)
    prob = implied_prob(close_odds) - implied_prob(row["odds"]) if close_odds is not None else None
    return pts, prob


# ---------- scoring gate + sizing ----------
def correlation_flags(picks):
    flags = {i: [] for i in range(len(picks))}
    slate = []
    n = len(picks)
    if n > 1:
        slate.append(f"SAME SLATE: all {n} placed bets settle tonight; one bad night hits every stake")
    for i in range(n):
        for j in range(i + 1, n):
            a, b = picks[i], picks[j]
            tag = f"#{a['_n']}<->#{b['_n']}"
            out = []
            if a.get("game") and a.get("game") == b.get("game"):
                mk = {a.get("market", "").lower(), b.get("market", "").lower()}
                out.append(f"SIDE+TOTAL same game ({a['game']}) {tag}" if "total" in mk and mk & {"spread", "moneyline"}
                           else f"SAME GAME ({a['game']}) {tag}")
            ta = {t.lower() for t in a.get("teams", [a.get("team", "")]) if t}
            tb = {t.lower() for t in b.get("teams", [b.get("team", "")]) if t}
            if ta & tb and a.get("game") != b.get("game"):
                out.append(f"SAME TEAM ({', '.join(sorted(ta & tb))}) {tag}")
            inj = {x.lower() for x in a.get("injury_news", [])} & {x.lower() for x in b.get("injury_news", [])}
            if inj:
                out.append(f"SHARED INJURY NEWS ({', '.join(sorted(inj))}) {tag}")
            fac = {x.lower() for x in a.get("factors", [])} & {x.lower() for x in b.get("factors", [])}
            if fac:
                out.append(f"SHARED FACTOR ({', '.join(sorted(fac))}) {tag}")
            flags[i] += out; flags[j] += out
    return flags, slate


def evaluate(picks, bankroll, cfg):
    """Apply confidence gate, max_picks, per-bet sizing, caps, exposure scaling, correlation flags."""
    notes, placed, passed = [], [], []
    for n, p in enumerate(picks, 1):
        p = dict(p, _n=n, confidence=float(p.get("confidence", 0)))
        if p["confidence"] < cfg["min_confidence"]:
            p["pass_reason"] = f"confidence {p['confidence']:.0f} < {cfg['min_confidence']}"
            passed.append(p)
        else:
            placed.append(p)
    placed.sort(key=lambda p: -p["confidence"])
    mp = int(cfg["max_picks"])
    for p in placed[mp:]:
        p["pass_reason"] = f"over max_picks ({mp}); lower confidence than the top {mp}"
        passed.append(p)
    placed = sorted(placed[:mp], key=lambda p: p["_n"])
    lo, hi = float(cfg["min_confidence"]), float(cfg["full_confidence"])
    for p in placed:
        frac = min(max((p["confidence"] - lo) / (hi - lo), 0), 1) if hi > lo else 1
        p["target_pct"] = cfg["min_pct"] + frac * (cfg["max_pct"] - cfg["min_pct"])
        p["raw_stake"] = bankroll * p["target_pct"]
        p["pre_scale_stake"] = min(p["raw_stake"], float(cfg["per_bet_cap"]))
    cap_total = bankroll * cfg["nightly_exposure_cap"]
    total = sum(p["pre_scale_stake"] for p in placed)
    scale = 1.0
    if total > cap_total > 0:
        scale = cap_total / total
        notes.append(f"Pre-cap exposure ${total:.2f} > nightly cap ${cap_total:.2f}; "
                     f"all stakes scaled x{scale:.3f}.")
    r = float(cfg.get("round_to", 1)) or 1
    for p in placed:
        p["stake"] = math.floor(p["pre_scale_stake"] * scale / r) * r
        p["pct_bankroll"] = p["stake"] / bankroll if bankroll else 0
        p["to_win"] = round(profit_on_win(p["stake"], p["odds"]), 2)
    if scale < 1:
        notes.append(f"Scaled stakes sit below the {cfg['min_pct']:.0%} floor: the nightly cap overrides the floor.")
    flags, slate = correlation_flags(placed)
    for i, p in enumerate(placed):
        p["flags"] = flags[i]
    for p in passed:
        p.update(stake=0, pct_bankroll=0, flags=[])
    total = sum(p["stake"] for p in placed)
    assert total < bankroll, "never bet the whole bankroll"
    return {"bankroll": bankroll, "placed": placed, "passed": sorted(passed, key=lambda p: p["_n"]),
            "total": total, "cap_total": cap_total, "exposure_pct": total / bankroll if bankroll else 0,
            "scale": scale, "slate_flags": slate, "notes": notes}


def print_eval(res, cfg):
    b = res["bankroll"]
    print(f"Bankroll ${b:.2f} | gate {cfg['min_confidence']} | stake {cfg['min_pct']:.0%}@{cfg['min_confidence']}"
          f" -> {cfg['max_pct']:.0%}@{cfg['full_confidence']}+ | per-bet cap ${cfg['per_bet_cap']} | "
          f"nightly cap {cfg['nightly_exposure_cap']:.0%} = ${res['cap_total']:.2f}")
    print(f"{'#':<3}{'Pick':<22}{'Odds':>6}{'Conf':>6}  {'Decision':<8}{'Target':>8}{'PreScale':>10}{'Stake':>7}{'%BR':>7}")
    rows = sorted(res["placed"] + res["passed"], key=lambda p: p["_n"])
    for p in rows:
        if p in res["placed"]:
            print(f"{p['_n']:<3}{p['pick'][:21]:<22}{int(float(p['odds'])):>+6}{p['confidence']:>6.0f}  {'PLACE':<8}"
                  f"{p['raw_stake']:>8.2f}{p['pre_scale_stake']:>10.2f}{p['stake']:>7.0f}{p['pct_bankroll']:>7.1%}")
        else:
            print(f"{p['_n']:<3}{p['pick'][:21]:<22}{int(float(p['odds'])):>+6}{p['confidence']:>6.0f}  {'PASS':<8}"
                  f"  ({p['pass_reason']})")
    print(f"Placed {len(res['placed'])}, passed {len(res['passed'])}. Exposure ${res['total']:.0f} = "
          f"{res['exposure_pct']:.1%} of bankroll (cap {cfg['nightly_exposure_cap']:.0%})")
    for n in res["notes"]:
        print("NOTE:", n)
    print("Correlation flags:")
    for f in all_flags(res):
        print("  -", f)


def all_flags(res):
    out = list(res["slate_flags"])
    for p in res["placed"]:
        out += [f for f in p["flags"] if f not in out]
    return out or ["none"]


# ---------- commands ----------
def load_picks(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, []
    return data.get("picks", []), data.get("games_analyzed", [])


def cmd_size(a, cfg):
    picks, _ = load_picks(a.picks)
    bank = a.bankroll if a.bankroll is not None else load_bankroll()["current"]
    print_eval(evaluate(picks, bank, cfg), cfg)


def cmd_run(a, cfg):
    picks, games = load_picks(a.picks)
    d = a.date or date.today().isoformat()
    rows = read_bets()
    existing = [r for r in rows if r["date"] == d]
    if existing and not a.replace:
        sys.exit(f"{d} already run ({len(existing)} rows). Use --replace to redo unsettled rows.")
    if a.replace:
        if any(r["status"] == "settled" for r in existing):
            sys.exit(f"{d} has settled bets; refusing to replace.")
        rows = [r for r in rows if r["date"] != d]
    bank = load_bankroll()["current"]
    res = evaluate(picks, bank, cfg)
    print_eval(res, cfg)
    nid = max([int(r["id"]) for r in rows] or [0]) + 1
    for p in sorted(res["placed"] + res["passed"], key=lambda p: p["_n"]):
        is_placed = p in res["placed"]
        reason = p.get("reasoning", "") + ("" if is_placed else f" [PASSED: {p['pass_reason']}]")
        rows.append({"id": nid, "date": d, "game": p.get("game", ""), "team": p.get("team", ""),
                     "market": p.get("market", ""), "pick": p["pick"], "line": p.get("line", ""),
                     "odds": p["odds"], "confidence": f"{p['confidence']:.0f}", "stake": f"{p['stake']:.2f}",
                     "pct_bankroll": f"{p['pct_bankroll']:.4f}", "status": "placed" if is_placed else "passed",
                     "reasoning": reason, "correlation_flags": " | ".join(p["flags"]),
                     "placed_at": now() if is_placed else ""})
        nid += 1
    write_bets(rows)
    meta = {"games_analyzed": games, "bankroll_at_run": bank, "slate_flags": res["slate_flags"],
            "notes": res["notes"], "scale": res["scale"]}
    os.makedirs(SUMMARIES, exist_ok=True)
    with open(os.path.join(SUMMARIES, f"{d}.meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    path = write_summary(d, cfg)
    print(f"\n[paper ledger updated; post-run report -> {os.path.relpath(path, ROOT)}]")


def write_summary(d, cfg):
    rows = [r for r in read_bets() if r["date"] == d]
    mp = os.path.join(SUMMARIES, f"{d}.meta.json")
    meta = json.load(open(mp)) if os.path.exists(mp) else {}
    b = load_bankroll()
    run_bank = meta.get("bankroll_at_run", b["current"])
    placed = [r for r in rows if r["status"] in ("placed", "settled")]
    passed = [r for r in rows if r["status"] == "passed"]
    exp = sum(float(r["stake"]) for r in placed)
    games = meta.get("games_analyzed", [])
    L = [f"# NBA Bookie post-run report: {d}", "",
         f"**Mode:** autonomous paper trading. \"Placed\" = logged in the ledger; no real bets.",
         f"**Bankroll:** ${b['current']:,.2f} now (${run_bank:,.2f} at run, start ${b['starting']:,.2f})", "",
         f"**Games analyzed ({len(games)}):** " + ("; ".join(games) if games else "n/a"), "",
         f"## Placed ({len(placed)})"]
    if placed:
        L += ["| ID | Pick | Line/Odds | Conf | Stake | % BR | CLV | Result | Why |", "|---|---|---|---|---|---|---|---|---|"]
        for r in placed:
            L.append(f"| {r['id']} | {r['pick']} | {fmt_lo(r)} | {r['confidence']} | ${float(r['stake']):.0f} | "
                     f"{float(r['pct_bankroll']):.1%} | {fmt_clv(r)} | {r['result'] or 'open'} | {r['reasoning']} |")
    else:
        L.append("None. No pick cleared the confidence gate. A pass is a position.")
    L += ["", f"## Passed ({len(passed)})"]
    L += [f"- #{r['id']} {r['pick']} ({fmt_odds(r['odds'])}, conf {r['confidence']}): {r['reasoning']}" for r in passed] or ["- none"]
    L += ["", f"**Exposure:** ${exp:.0f} = {exp / run_bank:.1%} of run bankroll vs "
          f"{cfg['nightly_exposure_cap']:.0%} cap (${run_bank * cfg['nightly_exposure_cap']:.0f})."
          + (f" Stakes scaled x{meta['scale']:.2f} to fit." if meta.get("scale", 1) < 1 else ""),
          "", "**Correlation flags:**"]
    fl = list(meta.get("slate_flags", []))
    for r in placed:
        fl += [f for f in r["correlation_flags"].split(" | ") if f and f not in fl]
    L += [f"- {f}" for f in fl] or ["- none"]
    L += [f"- Note: {n}" for n in meta.get("notes", [])]
    L += ["", f"**CLV tonight:** {clv_line(placed)}  |  **CLV all-time:** "
          f"{clv_line([r for r in read_bets() if r['status'] in ('placed', 'settled')])}", "",
          f"**Risk note:** {cfg['min_pct']:.0%}-{cfg['max_pct']:.0%} per bet is far above 1-5% Kelly-style sizing; "
          "a cold night can cut the bankroll by more than half.", ""]
    path = os.path.join(SUMMARIES, f"{d}.md")
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path


def fmt_lo(r):
    ln = fnum(r.get("line"))
    if ln is None:
        return fmt_odds(r["odds"])
    return f"{ln:+g} / {fmt_odds(r['odds'])}" if r.get("market", "").lower() == "spread" else f"{ln:g} / {fmt_odds(r['odds'])}"


def fmt_odds(o):
    return f"{int(float(o)):+d}"


def fmt_clv(r):
    if r.get("clv_prob", "") == "":
        return "pending"
    s = []
    if r["clv_pts"] != "":
        s.append(f"{float(r['clv_pts']):+.1f} pts")
    s.append(f"{float(r['clv_prob']) * 100:+.1f}% imp")
    return ", ".join(s)


def clv_line(rows):
    c = [r for r in rows if r.get("clv_prob", "") != ""]
    if not c:
        return "no closing lines recorded yet"
    pts = [float(r["clv_pts"]) for r in c if r["clv_pts"] != ""]
    beat = sum(1 for r in c if (fnum(r["clv_pts"]) or 0) > 0 or (r["clv_pts"] == "" and float(r["clv_prob"]) > 0))
    s = f"{len(c)} closed, beat close {beat}/{len(c)}, avg {sum(float(r['clv_prob']) for r in c) / len(c) * 100:+.2f}% implied"
    return s + (f", avg {sum(pts) / len(pts):+.2f} pts" if pts else "")


def cmd_summary(a, cfg):
    d = a.date or date.today().isoformat()
    path = write_summary(d, cfg)
    print(open(path).read())
    print(f"[wrote {os.path.relpath(path, ROOT)}]")


def cmd_close(a, cfg):
    rows = read_bets()
    for r in rows:
        if r["id"] == str(a.id):
            if r["status"] not in ("placed", "settled"):
                sys.exit(f"Bet #{a.id} is '{r['status']}'; CLV is tracked on placed bets only.")
            pts, prob = compute_clv(r, a.line, a.odds)
            r.update(close_line="" if a.line is None else a.line, close_odds=a.odds,
                     clv_pts="" if pts is None else f"{pts:.2f}", clv_prob=f"{prob:.4f}", closed_at=now())
            write_bets(rows)
            write_summary(r["date"], cfg)
            print(f"Bet #{a.id} {r['pick']} ({r['line'] or ''} {fmt_odds(r['odds'])}) closed at "
                  f"{a.line if a.line is not None else ''} {fmt_odds(a.odds)}: CLV {fmt_clv(r)}")
            return
    sys.exit(f"Bet #{a.id} not found.")


def cmd_settle(a, cfg):
    rows, b = read_bets(), load_bankroll()
    for r in rows:
        if r["id"] == str(a.id):
            if r["status"] != "placed":
                sys.exit(f"Bet #{a.id} is '{r['status']}'; only placed bets can be settled.")
            stake = float(r["stake"])
            profit = {"win": profit_on_win(stake, r["odds"]), "loss": -stake, "push": 0.0}[a.result]
            b["current"] = round(b["current"] + profit, 2)
            r.update(status="settled", result=a.result, profit=f"{profit:.2f}",
                     bankroll_after=f"{b['current']:.2f}", settled_at=now())
            b.setdefault("history", []).append({"ts": now(), "bet_id": int(a.id), "result": a.result,
                                                "profit": round(profit, 2), "bankroll": b["current"]})
            write_bets(rows); save_bankroll(b)
            write_summary(r["date"], cfg)
            print(f"Bet #{a.id} {a.result}: {profit:+.2f}. Bankroll ${b['current']:.2f}")
            return
    sys.exit(f"Bet #{a.id} not found.")


def cmd_log(a, cfg):
    rows, bank = read_bets(), load_bankroll()["current"]
    nid = max([int(r["id"]) for r in rows] or [0]) + 1
    ok = a.confidence >= cfg["min_confidence"]
    stake = min(a.stake, cfg["per_bet_cap"], bank * cfg["max_pct"]) if ok else 0
    rows.append({"id": nid, "date": a.date or date.today().isoformat(), "game": a.game, "team": a.team,
                 "market": a.market, "pick": a.pick, "line": "" if a.line is None else a.line, "odds": a.odds,
                 "confidence": f"{a.confidence:.0f}", "stake": f"{stake:.2f}", "pct_bankroll": f"{stake / bank:.4f}",
                 "status": "placed" if ok else "passed",
                 "reasoning": a.reasoning + ("" if ok else f" [PASSED: confidence < {cfg['min_confidence']}]"),
                 "placed_at": now() if ok else ""})
    write_bets(rows)
    print(f"Logged #{nid} as '{'placed' if ok else 'passed'}' (stake ${stake:.2f}). "
          "Manual log skips nightly exposure scaling; prefer `run`.")


def cmd_status(a, cfg):
    b, rows = load_bankroll(), read_bets()
    st = [r for r in rows if r["status"] == "settled"]
    w, l, p = (sum(r["result"] == x for r in st) for x in ("win", "loss", "push"))
    pnl = b["current"] - b["starting"]
    risked = sum(float(r["stake"]) for r in st)
    print(f"Bankroll ${b['current']:.2f} (start ${b['starting']:.2f}, P/L {pnl:+.2f}, {pnl / b['starting']:+.1%})"
          f" | Record {w}-{l}-{p}" + (f" | ROI {sum(float(r['profit']) for r in st) / risked:+.1%}" if risked else ""))
    print(f"Next run sizing: ${b['current'] * cfg['min_pct']:.2f}-${min(b['current'] * cfg['max_pct'], cfg['per_bet_cap']):.2f}"
          f" per bet, nightly cap ${b['current'] * cfg['nightly_exposure_cap']:.2f}, gate {cfg['min_confidence']}")
    print("CLV:", clv_line([r for r in rows if r["status"] in ("placed", "settled")]))
    print(f"Passed picks logged: {sum(r['status'] == 'passed' for r in rows)}")
    op = [r for r in rows if r["status"] == "placed"]
    if op:
        print(f"Open ({len(op)}): " + "; ".join(f"#{r['id']} {r['pick']} ${float(r['stake']):.0f} CLV {fmt_clv(r)}" for r in op))


def cmd_reset(a, cfg):
    amt = float(a.bankroll if a.bankroll is not None else cfg["starting_bankroll"])
    save_bankroll({"starting": amt, "current": amt, "history": []})
    write_bets([])
    for f in glob.glob(os.path.join(SUMMARIES, "*.md")) + glob.glob(os.path.join(SUMMARIES, "*.meta.json")):
        os.remove(f)
    print(f"Paper ledger cleared. Bankroll ${amt:.2f}.")


def cmd_lines(a, cfg):
    d = (a.date or date.today().isoformat()).replace("-", "")
    key = os.environ.get("ODDS_API_KEY")
    games = []
    if key:  # optional; read-only odds feed
        url = ("https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"
               f"?apiKey={key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
        try:
            for g in json.load(urllib.request.urlopen(url, timeout=20)):
                games.append({"source": "the-odds-api", "game": f"{g['away_team']} @ {g['home_team']}",
                              "start": g["commence_time"], "books": g.get("bookmakers", [])[:3]})
        except Exception as e:
            print("Odds API failed, falling back to ESPN:", e, file=sys.stderr)
    if not games:
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for ev in json.load(urllib.request.urlopen(req, timeout=20)).get("events", []):
            comp = ev["competitions"][0]
            odds = (comp.get("odds") or [{}])[0]
            games.append({"source": "espn", "game": ev.get("shortName"), "start": ev.get("date"),
                          "spread": odds.get("details"), "total": odds.get("overUnder"),
                          "provider": (odds.get("provider") or {}).get("name"),
                          "season_type": ev.get("season", {}).get("slug")})
    out = os.path.join(DATA, f"lines_{d}.json")
    with open(out, "w") as f:
        json.dump(games, f, indent=2)
    for g in games:
        print(f"{g['game']:<14} {g.get('spread') or '':<12} O/U {g.get('total') or '-'}  ({g['source']})")
    print(f"{len(games)} game(s) -> {os.path.relpath(out, ROOT)}. Cross-check Covers / Action Network / VegasInsider.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("size"); s.add_argument("picks"); s.add_argument("--bankroll", type=float)
    s = sp.add_parser("run"); s.add_argument("picks"); s.add_argument("--date"); s.add_argument("--replace", action="store_true")
    s = sp.add_parser("close"); s.add_argument("id"); s.add_argument("--line", type=float)
    s.add_argument("--odds", type=float, required=True)
    s = sp.add_parser("settle"); s.add_argument("id"); s.add_argument("result", choices=["win", "loss", "push"])
    s = sp.add_parser("summary"); s.add_argument("--date")
    s = sp.add_parser("log")
    for k in ("game", "pick", "reasoning"):
        s.add_argument(f"--{k}", required=True)
    for k in ("odds", "stake", "confidence"):
        s.add_argument(f"--{k}", type=float, required=True)
    s.add_argument("--line", type=float); s.add_argument("--team", default="")
    s.add_argument("--market", default=""); s.add_argument("--date")
    sp.add_parser("status")
    s = sp.add_parser("reset"); s.add_argument("--bankroll", type=float)
    s = sp.add_parser("lines"); s.add_argument("--date")
    a = ap.parse_args()
    cfg = load_config()
    {"size": cmd_size, "run": cmd_run, "close": cmd_close, "settle": cmd_settle, "summary": cmd_summary,
     "log": cmd_log, "status": cmd_status, "reset": cmd_reset, "lines": cmd_lines}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
