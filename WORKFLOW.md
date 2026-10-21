# Daily workflow (NBA on Kalshi, paper only)

**Run window: 3:00-4:00 PM PT daily.** This comes after afternoon injury reports and before 4:00 PM PT (7:00 ET) tips. It uses only public read-only Kalshi data, places no real orders, and uses no credentials.

## 1. RUN (3:00-4:00 PM PT)
| Step | What | Command |
|---|---|---|
| 1. Gather markets and lines | List tonight's Kalshi NBA events and tickers. Pull sportsbook lines (ESPN, Covers, Action Network, VegasInsider) as the fair-probability reference and de-vig them. | `python3 reef.py markets --date YYYY-MM-DD` |
| 2. Research | Injuries and minutes limits, rest and back-to-backs, travel, coaching and rotations, motivation, matchup, movement. | notes |
| 3. Score | Rubric 0-100. Choose side (YES or NO), style (hold or trade) and the fair probability for that side. Check the live cost: | `python3 reef.py book TICKER --side yes --contracts 100 --fair 0.62` |
| 4. Size (dry run) | Confidence gate (75), cost gate (≥2¢ **and** ≥3% after all costs), tiers 10/15/20%, $100 cap per trade, 60% nightly cap, max 5, correlation flags. | `python3 reef.py size picks.json` |
| 5. Auto-place | Trades that pass both gates are logged `placed`. The rest are logged `passed` with reasons. | `python3 reef.py run picks.json` |
| 6. Summary | Post-run report with the fee math. | `data/summaries/YYYY-MM-DD.md` (written automatically) |

## 2. LIVE / INTRA-GAME (optional, trade style)
```
# fair probs (YES side) the agent keeps updated during the game:
#   data/fair/KXNBAGAME-26OCT22BOSNYK.json
#   {"KXNBAGAME-26OCT22BOSNYK-NYK": {"fair": 0.64, "confidence": 82, "style": "trade", "reasoning": "..."}}
python3 reef.py monitor --event KXNBAGAME-26OCT22BOSNYK --dry-run       # flag only
python3 reef.py monitor --event KXNBAGAME-26OCT22BOSNYK --interval 15   # paper-execute
```
- **Entries** need confidence ≥75 and EV after fees, spread, slippage and estimated exit costs ≥ the hurdle. They must also fit the tier, the $ cap, the 60% cap and the max of 5.
- **Exits** happen when net sale proceeds after walking the bids and paying the exit fee beat hold EV by ≥2¢ per contract.
- Polling uses `poll_seconds` (15 by default, with a floor of `min_poll_seconds`, 5). Requests are spaced at least `request_spacing_seconds` apart, and a 429 triggers a back-off. Every action is logged to `data/monitor_<event>.log` and the ledger.

### In-game exit rules (run automatically inside `monitor`)
Priority: **confidence decay → stop-loss → blowout → Q3 review → profit target**. All thresholds are in `config.yaml` under `exits:`.
- **Profit target:** net mark (bids walked, minus the exit fee) ≥ +30% → sell 40%, let the rest ride (once per tier).
- **Stop-loss:** net mark ≤ −15% on 2 consecutive polls → exit the remainder.
- **Blowout** (ESPN live score): lead ≥20 with ≤4:00 left in Q3, ≥15 in Q4, or ≥10 with ≤3:00 left. Wrong side → exit. Right side → trim 50%.
- **End-of-Q3 review:** a logged hold, trim, or exit for every open position. Losers get trimmed 25/40/50%, or exited if down 10+.
- **Confidence decay:** live confidence below entry − 3 for 2 polls → exit. You can set `"live_confidence": NN` per ticker in the fair file to override the built-in model.
- **Fee-aware hold:** no sale if the upside to $1 is less than the exit fee + spread, or if the sale nets ≤ $0. In that case it holds to settlement.
- Simulation (EXAMPLE DATA, temp ledger, no network): `python3 examples/sim_exit_timeline.py`

## 3. CLOSE (5-15 min before each tip)
```
python3 reef.py close 1 --live          # last Kalshi mid for your side -> CLV (cents and implied pp)
python3 reef.py close 1 --price 0.58    # or a manual price
```

## 4. SETTLE (next morning)
```
python3 reef.py settle 1 win            # win | loss | void ($1/contract on win, no settlement fee)
python3 reef.py exit 3 --live           # paper trade-out before settlement (walks bids, exit fee)
python3 reef.py status                  # bankroll, W-L, fees paid, CLV, open positions
```

## Weekly
Check CLV and fees paid against gross EV. If fees eat most of the edge, raise `min_edge_cents`. If CLV is negative over 50 or more trades, raise `min_confidence`.
