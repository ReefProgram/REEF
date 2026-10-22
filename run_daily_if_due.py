#!/usr/bin/env python3
"""Tip-relative daily paper runner (no fixed 3 PM cron).

Intended to be invoked often (e.g. hourly GitHub Actions). Logic:

  1. Find NBA games for today (America/Los_Angeles) via ESPN scoreboard (no API key).
     Fallback: Kalshi KXNBAGAME markets' occurrence_datetime when ESPN has none.
  2. If no games → exit 0 (skip).
  3. first_tip = earliest tip time; run window = [first_tip - buffer, first_tip).
     Default buffer = 45 minutes (config: draft_buffer_minutes).
  4. If now is outside that window → exit 0 (skip).
  5. If already ran for this PT date (state file) → exit 0 (skip).
  6. Else: draft_picks.py → launch_paper.py daily --execute (paper only).

Flags:
  --check-only     print due/skip decision; never draft or execute
  --draft-only     if due, draft picks.json only (setup / dry); no daily --execute
  --force          ignore window + already-ran (still no real orders)

No Kalshi credentials. Never places real Kalshi orders.
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import draft_picks
import reef

PT = ZoneInfo("America/Los_Angeles")
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "data", "automation_state.json")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        return json.load(open(STATE_PATH))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(st, open(STATE_PATH, "w"), indent=2)
    open(STATE_PATH, "a").write("\n")


def buffer_minutes(cfg):
    src = cfg.get("draft") if isinstance(cfg.get("draft"), dict) else cfg
    return int(src.get("draft_buffer_minutes", cfg.get("draft_buffer_minutes", 45)))


def espn_tips(day):
    """Return list of aware UTC datetimes for NBA tips on PT calendar day. No API key."""
    url = f"{ESPN}?dates={day.strftime('%Y%m%d')}"
    req = urllib.request.Request(url, headers={"User-Agent": "reef-paper/1.0", "Accept": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    tips = []
    for ev in data.get("events") or []:
        raw = None
        comps = ev.get("competitions") or []
        if comps:
            raw = comps[0].get("date") or comps[0].get("startDate")
        raw = raw or ev.get("date")
        if not raw:
            continue
        # ESPN uses e.g. 2026-10-21T23:00Z
        raw = raw.replace("Z", "+00:00")
        try:
            tips.append(datetime.fromisoformat(raw).astimezone(timezone.utc))
        except ValueError:
            continue
    return sorted(tips)


def kalshi_tips(cfg, day):
    """Fallback tip times from Kalshi market occurrence_datetime (may lag/lead ESPN)."""
    tips = []
    for e in draft_picks.list_game_events(cfg, day):
        ms = reef.api_get(cfg, f"/markets?event_ticker={e['event_ticker']}").get("markets", [])
        for m in ms:
            raw = m.get("occurrence_datetime") or m.get("expected_expiration_time")
            if not raw:
                continue
            raw = str(raw).replace("Z", "+00:00")
            try:
                tips.append(datetime.fromisoformat(raw).astimezone(timezone.utc))
            except ValueError:
                continue
            break
    return sorted(tips)


def decide(cfg, now=None, force=False):
    now = now or datetime.now(PT)
    day = now.astimezone(PT).date()
    buf = buffer_minutes(cfg)
    source = "espn"
    try:
        tips = espn_tips(day)
    except Exception as e:
        print(f"ESPN schedule unavailable ({e}); falling back to Kalshi occurrence_datetime", flush=True)
        tips = kalshi_tips(cfg, day)
        source = "kalshi"
    if not tips:
        # Confirm Kalshi also empty → truly no slate
        if source == "espn":
            k = kalshi_tips(cfg, day)
            if k:
                tips, source = k, "kalshi"
    if not tips:
        return {"action": "skip", "reason": "no_nba_games", "date": day.isoformat(), "source": source}

    first = tips[0].astimezone(PT)
    window_start = first - timedelta(minutes=buf)
    now_pt = now.astimezone(PT)
    st = load_state()
    already = st.get("last_run_date") == day.isoformat()

    info = {
        "date": day.isoformat(),
        "source": source,
        "n_games": len(tips),
        "first_tip_pt": first.isoformat(timespec="minutes"),
        "window_start_pt": window_start.isoformat(timespec="minutes"),
        "buffer_minutes": buf,
        "now_pt": now_pt.isoformat(timespec="minutes"),
        "already_ran": already,
    }
    if already and not force:
        return {**info, "action": "skip", "reason": "already_ran"}
    if force:
        return {**info, "action": "run", "reason": "force"}
    if now_pt < window_start:
        return {**info, "action": "skip", "reason": "before_window"}
    if now_pt >= first:
        return {**info, "action": "skip", "reason": "past_first_tip"}
    return {**info, "action": "run", "reason": "in_pre_tip_window"}


def run_pipeline(draft_only=False):
    day = datetime.now(PT).date()
    print(f"Drafting AUTO_DRAFT picks for {day.isoformat()}...", flush=True)
    draft_picks.draft_for_date(reef.load_config(), day, out_path=os.path.join(ROOT, "picks.json"))
    if draft_only:
        print("draft-only: skipping launch_paper.py daily --execute", flush=True)
        return
    print("Running paper daily --execute (no real Kalshi orders)...", flush=True)
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "launch_paper.py"), "daily", "--execute", "--date", day.isoformat()],
        cwd=ROOT, check=True,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--draft-only", action="store_true",
                    help="if due, write picks.json only; do not paper-execute")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = reef.load_config()
    decision = decide(cfg, force=a.force)
    print(json.dumps(decision, indent=2), flush=True)
    if a.check_only:
        sys.exit(0)
    if decision["action"] != "run":
        print(f"SKIP: {decision['reason']}", flush=True)
        sys.exit(0)
    run_pipeline(draft_only=a.draft_only)
    st = load_state()
    st["last_run_date"] = decision["date"]
    st["last_run_at"] = datetime.now(PT).isoformat(timespec="seconds")
    st["last_reason"] = decision["reason"]
    st["last_first_tip_pt"] = decision.get("first_tip_pt")
    save_state(st)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
