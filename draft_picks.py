#!/usr/bin/env python3
"""Draft picks.json from live Kalshi NBA markets (paper automation helper).

This is an AUTOMATED DRAFT, not human research. It does not scrape sportsbooks or
injuries. Defaults are intentionally conservative / mechanical:

  - One candidate per KXNBAGAME event (favorite by Kalshi mid).
  - fair_prob = clamp(mid + draft_fair_bump)  (default bump +3¢)
  - confidence = draft_confidence            (default 76, just over the 75 gate)
  - style = hold; side = yes on the favorite ticker

Reef's confidence + after-cost EV gates still apply on size/run — many AUTO_DRAFT
picks will PASS. No Kalshi credentials. Never places real orders.

Usage:
  python3 draft_picks.py                     # tonight PT → picks.json
  python3 draft_picks.py --date 2026-10-21 --out picks.json
  python3 draft_picks.py --dry-run           # print JSON, do not write
"""
from __future__ import annotations

import argparse, json, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

import reef

PT = ZoneInfo("America/Los_Angeles")
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, "picks.json")

DRAFT_DEFAULTS = {
    "draft_confidence": 76,       # just above min_confidence 75; not researched
    "draft_fair_bump": 0.03,      # fair = market mid + bump (YES side)
    "draft_style": "hold",
    "draft_max_picks": 5,         # align with max_picks
    "draft_series": "KXNBAGAME",  # game winners only for the auto draft
}


def pt_today():
    return datetime.now(PT).date()


def date_tag(d):
    return d.strftime("%y") + d.strftime("%b").upper() + d.strftime("%d")


def mid_yes(m):
    """Best-effort YES mid from market dollars fields."""
    try:
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 and ask <= 0:
        return None
    if bid <= 0:
        return ask
    if ask <= 0:
        return bid
    return (bid + ask) / 2


def clamp01(x):
    return max(0.01, min(0.99, float(x)))


def draft_config(cfg):
    out = dict(DRAFT_DEFAULTS)
    src = cfg.get("draft") if isinstance(cfg.get("draft"), dict) else cfg
    for k in DRAFT_DEFAULTS:
        if k in src:
            out[k] = src[k]
    return out


def list_game_events(cfg, d):
    tag = date_tag(d)
    series = str(draft_config(cfg)["draft_series"]).split(",")[0].strip()
    evs = reef.api_get(cfg, f"/events?series_ticker={series}&status=open&limit=200").get("events", [])
    return [e for e in evs if tag in e["event_ticker"]]


def draft_for_date(cfg, d, out_path=None, dry_run=False):
    dc = draft_config(cfg)
    events = list_game_events(cfg, d)
    games, picks = [], []
    for e in events:
        et = e["event_ticker"]
        title = e.get("title") or et
        games.append(title.replace(" vs ", " @ "))
        ms = reef.api_get(cfg, f"/markets?event_ticker={et}").get("markets", [])
        scored = []
        for m in ms:
            mid = mid_yes(m)
            if mid is None:
                continue
            scored.append((mid, m))
        if not scored:
            continue
        scored.sort(key=lambda t: -t[0])
        mid, m = scored[0]
        team = (m.get("yes_sub_title") or m["ticker"].split("-")[-1]).strip()
        fair = clamp01(mid + float(dc["draft_fair_bump"]))
        conf = float(dc["draft_confidence"])
        picks.append({
            "event": et,
            "ticker": m["ticker"],
            "title": m.get("title") or f"{team} win",
            "teams": [team],
            "side": "yes",
            "style": str(dc["draft_style"]),
            "fair_prob": round(fair, 4),
            "consensus": f"AUTO_DRAFT Kalshi mid={mid:.3f}; fair=mid+{float(dc['draft_fair_bump']):.2f}",
            "confidence": conf,
            "rubric": {"value": 0, "injury_rest": 0, "matchup": 0, "situational": 0, "movement": 0},
            "reasoning": ("AUTO_DRAFT (not human research): favorite by Kalshi YES mid; "
                          f"fair set to mid+{float(dc['draft_fair_bump']):.2f}; "
                          f"confidence fixed at {conf:.0f}. Gates may still pass/reject."),
            "factors": ["auto_draft"],
            "injury_news": [],
            "occurrence_datetime": m.get("occurrence_datetime") or m.get("expected_expiration_time"),
        })

    # Keep the strongest favorites (highest Kalshi mid implied in fair-bump consensus).
    def _mid_from_pick(p):
        # consensus like "AUTO_DRAFT Kalshi mid=0.665; ..."
        try:
            return float(p["consensus"].split("mid=")[1].split(";")[0])
        except (IndexError, ValueError):
            return float(p["fair_prob"])
    picks.sort(key=_mid_from_pick, reverse=True)
    picks = picks[: int(dc["draft_max_picks"])]
    picks.sort(key=lambda p: p["event"])

    doc = {
        "source": "AUTO_DRAFT",
        "_note": ("Automated draft from live Kalshi KXNBAGAME markets. Not sportsbook research. "
                  "Paper only — reef.py never places real orders."),
        "date": d.isoformat(),
        "drafted_at": datetime.now(PT).isoformat(timespec="seconds"),
        "games_analyzed": games,
        "picks": picks,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if dry_run:
        print(text, end="")
    else:
        path = out_path or DEFAULT_OUT
        with open(path, "w") as f:
            f.write(text)
        print(f"Wrote {len(picks)} AUTO_DRAFT picks ({len(events)} games) -> {path}", flush=True)
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today America/Los_Angeles)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = reef.load_config()
    d = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else pt_today()
    doc = draft_for_date(cfg, d, out_path=a.out, dry_run=a.dry_run)
    if not doc["picks"] and not a.dry_run:
        print("No open KXNBAGAME events for", d.isoformat(), flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
