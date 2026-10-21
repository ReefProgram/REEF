#!/usr/bin/env python3
"""Reef paper launch helper — no credentials, no real Kalshi orders.

Orchestrates the WORKFLOW.md daily path as far as it can go without research picks
or a scheduler. Subcommands:

  check     egress + CLI smoke (fee, size sample, exit sim)
  markets   list tonight's Kalshi NBA slate (public API)
  demo      paper-place EXAMPLE sample_picks into the ledger + print summary/status
  monitor   short dry-run monitor on an event (default: first KXNBAGAME today)
  daily     markets + size/run picks.json if present; otherwise print next steps

Examples:
  python3 launch_paper.py check
  python3 launch_paper.py markets
  python3 launch_paper.py demo
  python3 launch_paper.py monitor --max-polls 2
  python3 launch_paper.py daily
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, urllib.request
from datetime import date, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
REEF = os.path.join(ROOT, "reef.py")
DATA = os.path.join(ROOT, "data")
PICKS = os.path.join(ROOT, "picks.json")
SAMPLE = os.path.join(ROOT, "examples", "sample_picks.json")


def sh(args, check=True):
    print("+", " ".join(args), flush=True)
    return subprocess.run([sys.executable, *args], cwd=ROOT, check=check)


def ensure_dirs():
    for p in (DATA, os.path.join(DATA, "fair"), os.path.join(DATA, "summaries")):
        os.makedirs(p, exist_ok=True)
        keep = os.path.join(p, ".gitkeep")
        if p != DATA and not os.path.exists(keep) and not any(
            n for n in os.listdir(p) if not n.startswith(".")
        ):
            open(keep, "a").close()


def probe(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "reef-paper/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"OK {r.status}"
    except Exception as e:
        return f"FAIL {type(e).__name__}: {e}"


def cmd_check(_):
    ensure_dirs()
    print("=== Secrets / auth ===", flush=True)
    print("Paper mode: NO Kalshi API key, NO ESPN key, NO env secrets required.", flush=True)
    print("Live/authenticated Kalshi trading is intentionally unsupported by this codebase.", flush=True)
    print("=== Egress ===", flush=True)
    print("Kalshi:", probe("https://external-api.kalshi.com/trade-api/v2/exchange/status"), flush=True)
    print("ESPN:  ", probe("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"), flush=True)
    sh([REEF, "fee", "--price", "0.55", "--contracts", "100"])
    sh([REEF, "size", SAMPLE])
    sh([os.path.join(ROOT, "examples", "sim_exit_timeline.py")])
    sh([REEF, "status"])
    print("CHECK OK", flush=True)


def cmd_markets(a):
    ensure_dirs()
    d = a.date or date.today().isoformat()
    sh([REEF, "markets", "--date", d])


def cmd_demo(a):
    """Paper-run EXAMPLE sample picks (inline books, no live fetch for sizing)."""
    ensure_dirs()
    d = a.date or date.today().isoformat()
    print(f"DEMO paper run for {d} using EXAMPLE sample_picks.json (not live research).", flush=True)
    sh([REEF, "run", SAMPLE, "--date", d, "--replace"])
    sh([REEF, "summary", "--date", d])
    sh([REEF, "status"])
    print("DEMO DONE — reset with: python3 reef.py reset", flush=True)


def first_game_event(d: str) -> str | None:
    # Reuse reef API helpers without spawning a long markets listing.
    sys.path.insert(0, ROOT)
    import reef
    cfg = reef.load_config()
    tag = datetime.strptime(d, "%Y-%m-%d").strftime("%y%b%d").upper()
    evs = reef.api_get(cfg, "/events?series_ticker=KXNBAGAME&status=open&limit=200").get("events", [])
    for e in evs:
        if tag in e["event_ticker"]:
            return e["event_ticker"]
    return None


def cmd_monitor(a):
    ensure_dirs()
    d = a.date or date.today().isoformat()
    event = a.event or first_game_event(d)
    if not event:
        sys.exit(f"No open KXNBAGAME event for {d}; pass --event explicitly.")
    # Seed a dry fair file so monitor has something to evaluate (low conf = no entry).
    fpath = os.path.join(DATA, "fair", f"{event}.json")
    if not os.path.exists(fpath):
        # Pick first market ticker if we can; else leave empty watch-only file.
        sys.path.insert(0, ROOT)
        import reef
        cfg = reef.load_config()
        ms = reef.api_get(cfg, f"/markets?event_ticker={event}").get("markets", [])
        fair = {}
        if ms:
            t = ms[0]["ticker"]
            fair[t] = {"fair": 0.50, "confidence": 50, "style": "trade",
                       "title": t, "reasoning": "launch_paper dry seed — below confidence gate"}
        json.dump(fair, open(fpath, "w"), indent=2)
        print(f"Wrote seed fair file {os.path.relpath(fpath, ROOT)}", flush=True)
    args = [REEF, "monitor", "--event", event, "--dry-run", "--max-polls", str(a.max_polls)]
    if a.interval:
        args += ["--interval", str(a.interval)]
    sh(args)


def cmd_daily(a):
    ensure_dirs()
    d = a.date or date.today().isoformat()
    print(f"=== Daily paper path for {d} (window 3:00–4:00 PM PT) ===", flush=True)
    print("Secrets: none required for paper. No real orders.", flush=True)
    sh([REEF, "markets", "--date", d])
    if os.path.exists(PICKS):
        print(f"Found {PICKS} — sizing then paper-running.", flush=True)
        sh([REEF, "size", PICKS])
        if a.execute:
            sh([REEF, "run", PICKS, "--date", d, "--replace"])
            sh([REEF, "summary", "--date", d])
        else:
            print("Dry daily complete. Re-run with --execute to paper-place into data/bets.csv.", flush=True)
    else:
        print(f"No {PICKS} yet — research + score picks (see WORKFLOW.md / SYSTEM_PROMPT.md),", flush=True)
        print(f"copy examples/picks.template.json -> picks.json, then re-run:", flush=True)
        print(f"  python3 launch_paper.py daily --execute", flush=True)
        print("Intra-game: python3 launch_paper.py monitor --max-polls 2", flush=True)
    sh([REEF, "status"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check", help="egress + smoke tests")
    p = sp.add_parser("markets", help="list Kalshi NBA slate")
    p.add_argument("--date")
    p = sp.add_parser("demo", help="paper-run EXAMPLE sample picks")
    p.add_argument("--date")
    p = sp.add_parser("monitor", help="short dry-run monitor")
    p.add_argument("--date")
    p.add_argument("--event")
    p.add_argument("--max-polls", type=int, default=2)
    p.add_argument("--interval", type=float)
    p = sp.add_parser("daily", help="markets + size/run picks.json if present")
    p.add_argument("--date")
    p.add_argument("--execute", action="store_true", help="paper-place (still no real orders)")
    a = ap.parse_args()
    {"check": cmd_check, "markets": cmd_markets, "demo": cmd_demo,
     "monitor": cmd_monitor, "daily": cmd_daily}[a.cmd](a)


if __name__ == "__main__":
    main()
