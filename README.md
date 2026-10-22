# Reef: autonomous paper trader for Kalshi NBA markets

Reef is a disciplined "Las Vegas bookie"-style persona that works for Jack, trading **Kalshi NBA event contracts only** in a **paper ledger** that starts at $500. Each day, during a 3-4 PM PT run, it scores picks 0-100, applies a confidence gate (75) and a full **after-cost** EV gate (spread + depth slippage + Kalshi fees + estimated exit costs), sizes trades in tiers (10/15/20%), caps exposure, and writes a post-run report. An optional live monitor paper-trades intra-game repricing.

**Safety:** it uses only Kalshi's public read-only market-data API. It uses no credentials, never calls an order or portfolio endpoint (the code refuses them), and places no real orders. "Placed" means a row in `data/bets.csv`.

## Kalshi data (verified Oct 8, 2026)
- **Base URL:** `https://external-api.kalshi.com/trade-api/v2` ([docs](https://docs.kalshi.com/getting_started/quick_start_market_data)). The fallback is `https://api.elections.kalshi.com/trade-api/v2`. No auth is needed for `/series`, `/events`, `/markets` or `/markets/{ticker}/orderbook`.
- **NBA series:** `KXNBAGAME` (winner, one market per team, for example `KXNBAGAME-26OCT08SACLAL-LAL`), `KXNBASPREAD` (wins by over X.5, for example `KXNBASPREAD-26OCT08SACLAL-SAC8`) and `KXNBATOTAL` (over X.5, for example `KXNBATOTAL-26OCT08SACLAL-240`). The event ticker format is `<SERIES>-<YYMONDD><AWAY><HOME>`. Kalshi has many more NBA series (quarters, halves, props); these three are the configured core.
- **Orderbook:** `orderbook_fp.yes_dollars` and `no_dollars` are **bids** only, as [price, qty]. YES ask = 1 − best NO bid.
- **Fees:** taker `roundup(0.07 × C × P × (1−P))`, maker `roundup(0.0175 × C × P × (1−P))` ([fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf), effective July 7, 2026). The NBA series API reports `fee_type: quadratic_with_maker_fees` and `fee_multiplier: 1`. There's no settlement fee. Fees are rounded up to the cent per fill, which is conservative.

## Structure
```
reef/
├── README.md  SYSTEM_PROMPT.md  WORKFLOW.md  config.yaml
├── reef.py                 # CLI
├── exits.py                # in-game exit rules (used by monitor)
├── draft_picks.py          # AUTO_DRAFT picks.json from live Kalshi
├── run_daily_if_due.py     # tip-relative schedule entrypoint
├── launch_paper.py         # paper launch helper
├── .github/workflows/daily-paper.yml
├── data/
│   ├── bankroll.json        # starting/current bankroll + history
│   ├── bets.csv             # paper ledger (placed/passed/settled/exited, fee math, EV, CLV)
│   ├── summaries/           # post-run reports YYYY-MM-DD.md
│   └── fair/                # live fair-prob files for `monitor` (gitignored)
└── examples/
    ├── sample_picks.json        # EXAMPLE DATA: 6 picks, inline books
    ├── picks.template.json      # copy to ../picks.json for a live daily run
    ├── sim_exit_timeline.py     # offline exit-rules regression
    ├── sample_run_output.txt    # fee math, passes, tiers, cap scaling
    ├── sample_summary.md        # post-run report
    └── sample_fair_file.json    # monitor input format (test only)
```

## Quick launch (paper, no secrets)
```bash
python3 launch_paper.py check          # egress + smoke + exit sim
python3 launch_paper.py markets        # tonight's Kalshi NBA slate
python3 launch_paper.py draft          # AUTO_DRAFT picks.json from live Kalshi
python3 launch_paper.py due            # tip-relative due/skip decision
python3 launch_paper.py daily --draft --execute   # draft + paper-place
python3 launch_paper.py demo           # EXAMPLE sample picks only
python3 run_daily_if_due.py            # scheduled entrypoint (hourly GHA)
```

## Schedule (tip-relative, not fixed 3 PM)

GitHub Action [`.github/workflows/daily-paper.yml`](.github/workflows/daily-paper.yml) runs **hourly** (`cron: 15 * * * *`).  
`run_daily_if_due.py` then:

1. Loads today’s NBA tips from the **ESPN public scoreboard** (no API key); falls back to Kalshi `occurrence_datetime`.
2. **Skips** (exit 0) if there are no games, we’re outside `[first_tip − 45m, first_tip)`, or we already ran for that PT date.
3. Otherwise drafts `picks.json` (`AUTO_DRAFT`) and runs `launch_paper.py daily --execute` (paper ledger only — no real orders).

AUTO_DRAFT defaults (`config.yaml` → `draft:`): confidence **76**, fair = Kalshi mid **+0.03**. Not human research; Reef gates may still pass/reject.

## Usage
```bash
python3 reef.py markets --date 2026-10-22                     # discover tickers
python3 reef.py book KXNBAGAME-26OCT22BOSNYK-NYK --contracts 100 --fair 0.62
python3 reef.py fee --price 0.55 --contracts 100              # fee calculator
python3 reef.py size picks.json                               # dry run
python3 reef.py run picks.json                                # auto paper-place + summary
python3 reef.py monitor --event KXNBAGAME-26OCT22BOSNYK --dry-run
python3 reef.py close 1 --live | --price 0.58                 # CLV
python3 reef.py exit 3 --live | --price 0.52                  # paper trade-out
python3 reef.py settle 1 win|loss|void
python3 reef.py status | summary [--date D] | reset
```
It runs on the Python stdlib alone. PyYAML is optional, and a built-in parser reads the flat `config.yaml`.

⚠️ Tiers of 10/15/20% per trade, with a 60% nightly cap, are far above 1-5% Kelly sizing and carry a high risk of ruin. This is Jack's rule, kept but flagged.
