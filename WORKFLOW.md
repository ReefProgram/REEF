# Daily workflow (NBA only, paper trading)

**Run window: 3:00-4:00 PM PT daily.** That's late enough for afternoon injury reports and shootaround news, and early enough to beat 4:00 PM PT (7:00 ET) tips. Use `python3 bookie.py lines --date YYYY-MM-DD` with tonight's date.

## 1. RUN (3:00-4:00 PM PT)
| Step | What | Command / output |
|---|---|---|
| 1. Gather lines | Pull tonight's slate. Cross-check Covers, Action Network and VegasInsider for the best number, openers and splits. | `python3 bookie.py lines` -> `data/lines_YYYYMMDD.json` |
| 2. Research | For each game: injuries and minutes limits, rest/B2B/3-in-4, travel, coaching and rotation changes, motivation, matchup, line movement. | notes |
| 3. Score | Rubric 0-100 (value 30, injury/rest 25, matchup 20, situational 15, movement 10). Write `picks.json`. | `picks.json` |
| 4. Size (dry run) | Check the gate, the 15-20% sizing, the $ cap, the 60% exposure scaling and the correlation flags. | `python3 bookie.py size picks.json` |
| 5. Auto-place | Picks scoring 75+ (top 5) are logged `placed`. The rest are logged `passed` with reasons. Paper only. | `python3 bookie.py run picks.json` |
| 6. Summary | The post-run report is written automatically. | `data/summaries/YYYY-MM-DD.md` |

No approval step. The ledger is the only "sportsbook." Nothing is ever sent anywhere.

## 2. CLOSE (5-15 min before each tip)
Record the closing line and odds for every placed bet:
```
python3 bookie.py close 1 --line 2.5 --odds -110     # spread/total
python3 bookie.py close 3 --odds +105                # moneyline
```
This computes CLV (points and implied probability) and refreshes the summary.

## 3. SETTLE (next morning)
```
python3 bookie.py settle 1 win      # win | loss | push
python3 bookie.py status            # bankroll, record, ROI, CLV, open bets
```
The bankroll updates from American odds, and the next run sizes off the new bankroll automatically.

## Weekly
Review `status`: is the bets' CLV positive, and how much did the passed picks save? If CLV is negative over 50 or more bets, raise `min_confidence` before changing anything else.

## Scheduling (optional)
To automate this on a box, use cron with PT times, for example `30 15 * * *` for the run, then close and settle as above. The research and scoring step still needs the agent. The CLI only enforces the rules and keeps the books.
