#!/usr/bin/env python3
"""EXAMPLE DATA: simulated NBA night that exercises Reef's in-game exit rules end to end.

Runs the same code path as `reef.py monitor` (reef.manage_exits -> exits.decide -> paper ledger) against
made-up orderbooks and game states, in a TEMP ledger. The real data/ ledger is never touched.
No network, no orders.   Run:  python3 examples/sim_exit_timeline.py
"""
import argparse, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reef  # noqa: E402

tmp = tempfile.mkdtemp(prefix="reef-sim-")
reef.DATA, reef.BETS = tmp, os.path.join(tmp, "bets.csv")
reef.BANKROLL, reef.SUMMARIES = os.path.join(tmp, "bankroll.json"), os.path.join(tmp, "summaries")
cfg = reef.load_config()
D = "2026-10-22"
LOG = []


def say(m):
    LOG.append(m); print("   " + m)


def book(bid, ask, depth=200, thin=None):
    """YES-side book: YES bids, plus NO bids that make the YES ask (ask = 1 - NO bid). thin = [(px, qty)] on top."""
    yes = [list(x) for x in (thin or [])] + [[bid, depth], [round(bid - 0.02, 2), depth]]
    return {"yes": yes, "no": [[round(1 - ask, 4), depth]] if ask is not None else []}


def game(per, clock, away, a_pts, home, h_pts, state="in"):
    return {"state": state, "period": per, "clock": reef.exits.clock_secs(clock), "detail": "", "scores": {away: a_pts, home: h_pts}}


# ---- positions placed at the 3 PM run (EXAMPLE) ----
POS = [  # event, team, entry bid/ask, contracts, fair, conf, title
    ("KXNBAGAME-26OCT22BOSNYK", "NYK", .53, .55, 20, 0.62, 88, "Knicks win"),
    ("KXNBAGAME-26OCT22LALDEN", "LAL", .47, .49, 20, 0.55, 80, "Lakers win"),
    ("KXNBAGAME-26OCT22PHXGSW", "GSW", .60, .62, 15, 0.68, 82, "Warriors win"),
    ("KXNBAGAME-26OCT22MIACHI", "CHI", .50, .52, 15, 0.58, 78, "Bulls win"),
]
reef.save_bankroll({"starting": 500.0, "current": 500.0, "history": []})
rows = []
print("=" * 100 + "\nREEF EXIT-RULES SIMULATION  ***  EXAMPLE DATA (made-up books and scores; temp ledger; paper only)  ***\n" + "=" * 100)
for i, (ev, team, b, a, n, fair, conf, title) in enumerate(POS, 1):
    t = f"{ev}-{team}"
    q = reef.entry_quote(book(b, a), "yes", n, fair, "hold", cfg)
    r = reef.row_from({"ticker": t, "event": ev, "title": title, "side": "yes", "style": "hold", "fair_prob": fair,
                       "confidence": conf, "reasoning": "EXAMPLE pick", "source": "EXAMPLE"}, D, "EXAMPLE", "placed", q)
    r["id"] = i; rows.append(r)
    print(f"#{i} {t} BUY YES x{n} avg {q['avg_entry']:.2f} all-in ${q['all_in_cost']:.2f} (fee {q['fee_math']}) conf {conf}")
reef.write_bets(rows)

# ---- timeline: (label, {ticker: (game, book)}) ----
NYK, LAL, GSW, CHI = (f"{p[0]}-{p[1]}" for p in POS)
S = {NYK: (game(1, "12:00", "BOS", 0, "NYK", 0), book(.53, .55)),
     LAL: (game(1, "12:00", "LAL", 0, "DEN", 0), book(.47, .49)),
     GSW: (game(1, "12:00", "PHX", 0, "GSW", 0), book(.60, .62)),
     CHI: (game(1, "12:00", "MIA", 0, "CHI", 0), book(.50, .52))}
T = [
    ("Tip-off", {}),
    ("Q2 6:00  NYK +9; LAL thin-book tick (2 lots at .47, then .40)",
     {NYK: (game(2, "6:00", "BOS", 44, "NYK", 53), book(.76, .78)),
      LAL: (game(2, "6:00", "LAL", 50, "DEN", 50), book(.40, .49, thin=[(.47, 2)]))}),
    ("Q2 5:45  LAL book refills (debounce resets)", {LAL: (game(2, "5:45", "LAL", 50, "DEN", 50), book(.46, .48))}),
    ("Q3 8:00  LAL tied but price slides on injury news (breach 1)",
     {LAL: (game(3, "8:00", "LAL", 70, "DEN", 70), book(.40, .42)),
      CHI: (game(3, "8:00", "MIA", 60, "CHI", 62), book(.53, .55))}),
    ("Q3 7:45  LAL still below stop (breach 2 = confirmed)", {LAL: (game(3, "7:45", "LAL", 70, "DEN", 71), book(.40, .42))}),
    ("Q3 3:30  GSW down 22 (wrong-side blowout)", {GSW: (game(3, "3:30", "PHX", 84, "GSW", 62), book(.04, .06))}),
    ("End of Q3: mandatory review (NYK +8, CHI -1)",
     {NYK: (game(3, "0.0", "BOS", 80, "NYK", 88), book(.80, .82)),
      CHI: (game(3, "0.0", "MIA", 74, "CHI", 73), book(.49, .51))}),
    ("Q4 11:30  CHI -3 (live read stays below entry)", {CHI: (game(4, "11:30", "MIA", 77, "CHI", 74), book(.40, .42))}),
    ("Q4 6:00  NYK +12", {NYK: (game(4, "6:00", "BOS", 90, "NYK", 102), book(.90, .92))}),
    ("Q4 2:30  NYK +18, YES bid .98, no offers (right-side blowout)",
     {NYK: (game(4, "2:30", "BOS", 96, "NYK", 114), book(.98, None))}),
]
states, ts, seen = {}, 1_000_000.0, []
for label, upd in T:
    S.update(upd); ts += 15
    print(f"\n-- {label}")
    for r in [r for r in reef.read_bets() if r["status"] == "placed"]:
        g, bk = S[r["ticker"]]
        n0 = len(LOG)
        reef.manage_exits(r, bk, "yes", g, states, {}, cfg, D, say, False, now_ts=ts)
        seen += [(label, r["id"], m) for m in LOG[n0:]]

print("\n-- Final: NYK wins; remaining NYK contracts settle at $1 (no settlement fee)")
reef.cmd_settle(argparse.Namespace(id="1", result="win"), cfg)

# ---- checks ----
def hit(pid, *needles):
    return any(p == str(pid) and all(x in m for x in needles) for _, p, m in seen)

checks = {
    "profit-target trim (NYK, 40%)": hit(1, "TRIM [profit_target#1]", "sell 8"),
    "stop debounce: thin tick = breach 1/2 only, then reset": any(l.startswith("Q2 6:00") and "stop breach 1/2" in m
        for l, _, m in seen) and any(l.startswith("Q3 8:00") and "stop breach 1/2" in m for l, _, m in seen),
    "debounced stop-loss exit (LAL, 2nd poll)": hit(2, "EXIT [stop_loss]", "confirmed 2 polls"),
    "wrong-side blowout exit (GSW)": hit(3, "EXIT [blowout]", "WRONG SIDE"),
    "Q3 review HOLD (NYK winning)": hit(1, "HOLD [q3_review]"),
    "Q3 review TRIM (CHI losing)": hit(4, "TRIM [q3_review]"),
    "confidence-decay exit (CHI)": hit(4, "EXIT [confidence_decay]"),
    "right-side blowout fee-aware HOLD (NYK)": hit(1, "HOLD [blowout]", "HOLD TO SETTLEMENT"),
}
rows = reef.read_bets(); b = reef.load_bankroll()
print("\n-- Ledger (temp)")
for r in rows:
    print(f"   #{r['id']} {r['ticker'][-3:]} {r['status']:<8} x{r['contracts']:<3} cost ${float(r['all_in_cost']):6.2f} "
          f"exit {r['exit_price'] or '-':<7} fee {r['exit_fee'] or '-':<5} P/L {r['pnl'] or '-':>7} | {r['reasoning'][:90]}")
pnl_sum = sum(float(r["pnl"] or 0) for r in rows)
checks["ledger P/L ties to bankroll"] = abs(500 + pnl_sum - b["current"]) < 0.005
checks["real ledger untouched"] = os.path.abspath(reef.BETS).startswith(tmp)
print(f"\nBankroll $500.00 -> ${b['current']:.2f}  (sum of row P/L {pnl_sum:+.2f})")
print("\n-- Checks")
for k, v in checks.items():
    print(f"   [{'PASS' if v else 'FAIL'}] {k}")
print(f"\n{'ALL CHECKS PASSED' if all(checks.values()) else 'SOME CHECKS FAILED'}  (EXAMPLE DATA; temp ledger {tmp})")
sys.exit(0 if all(checks.values()) else 1)
