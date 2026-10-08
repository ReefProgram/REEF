# NBA Bookie: autonomous paper-trading NBA betting agent

A disciplined "Las Vegas bookie" persona that works for Jack. Each day, during a 3-4 PM PT run, it scores NBA picks 0-100, auto-places those scoring **75+** in a **paper ledger** (at most 5 a night), logs everything below the gate as `passed`, tracks closing-line value, and settles results the next morning.

**Paper only.** "Placed" means written to `data/bets.csv`. There is no sportsbook integration and there never will be. No API keys are required (`ODDS_API_KEY` for The Odds API is optional).

## Structure
```
nba-bookie/
├── README.md            # this file
├── SYSTEM_PROMPT.md     # agent role, hard rules, 0-100 rubric, decision process, output format
├── WORKFLOW.md          # daily run (3-4 PM PT) -> close -> settle
├── config.yaml          # bankroll, gate, sizing, caps
├── bookie.py            # CLI (stdlib; PyYAML optional)
├── data/
│   ├── bankroll.json    # starting/current bankroll + history
│   ├── bets.csv         # ledger: placed / passed / settled, CLV, results
│   └── summaries/       # post-run reports YYYY-MM-DD.md
└── examples/
    ├── sample_picks.json        # 5 mixed-confidence picks
    ├── sample_picks_cap.json    # 4 picks over the gate -> exposure-cap scaling
    ├── sample_summary.md        # example post-run report
    └── sample_cap_scaling.txt
```

## Rules enforced by the CLI
- Gate: `min_confidence: 75`. Picks below it are logged `passed` and never placed.
- Stake: 15% of the current bankroll at 75, rising linearly to 20% at 95+, and never more than `per_bet_cap` ($100).
- Nightly exposure is capped at 60% of the bankroll, with proportional scale-down. At most 5 bets. Never the whole bankroll.
- Correlation flags: same slate, same team, side + total in the same game, shared injury news, shared factors.
- ⚠️ 15-20% per bet is far above typical 1-5% Kelly sizing and carries a high risk of ruin. This is Jack's rule, kept but flagged.

## Usage
```bash
python3 bookie.py lines [--date 2026-10-22]       # ESPN public odds (or The Odds API if ODDS_API_KEY set)
python3 bookie.py size picks.json                 # dry run
python3 bookie.py run picks.json [--date D]       # auto-place/pass + write post-run summary
python3 bookie.py close ID --line X --odds Y      # closing line -> CLV
python3 bookie.py settle ID win|loss|push         # updates bankroll
python3 bookie.py summary [--date D]              # regenerate report
python3 bookie.py status                          # bankroll, record, ROI, CLV, open bets
python3 bookie.py reset [--bankroll 500]          # clean ledger
```
The config is `config.yaml`, parsed with PyYAML if it's installed or a built-in flat parser if not. A `config.json` is used if there's no YAML file.

Ledger statuses are `placed` (paper bet, open), `passed` (below the gate or over the 5-bet max, $0), and `settled` (with result, profit and bankroll_after).
