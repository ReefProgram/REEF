# NBA Bookie: autonomous paper trader for Kalshi NBA markets

A disciplined "Las Vegas bookie" persona that works for Jack, trading **Kalshi NBA event contracts only** in a **paper ledger** that starts at $500. Each day, during a 3-4 PM PT run, it scores picks 0-100, applies a confidence gate (75) and a full **after-cost** EV gate (spread + depth slippage + Kalshi fees + estimated exit costs), sizes trades in tiers (10/15/20%), caps exposure, and writes a post-run report. An optional live monitor paper-trades intra-game repricing.

**Safety:** it uses only Kalshi's public read-only market-data API. It uses no credentials, never calls an order or portfolio endpoint (the code refuses them), and places no real orders. "Placed" means a row in `data/bets.csv`.

## Kalshi data (verified Oct 8, 2026)
- **Base URL:** `https://external-api.kalshi.com/trade-api/v2` ([docs](https://docs.kalshi.com/getting_started/quick_start_market_data)). The fallback is `https://api.elections.kalshi.com/trade-api/v2`. No auth is needed for `/series`, `/events`, `/markets` or `/markets/{ticker}/orderbook`.
- **NBA series:** `KXNBAGAME` (winner, one market per team, for example `KXNBAGAME-26OCT08SACLAL-LAL`), `KXNBASPREAD` (wins by over X.5, for example `KXNBASPREAD-26OCT08SACLAL-SAC8`) and `KXNBATOTAL` (over X.5, for example `KXNBATOTAL-26OCT08SACLAL-240`). The event ticker format is `<SERIES>-<YYMONDD><AWAY><HOME>`. Kalshi has many more NBA series (quarters, halves, props); these three are the configured core.
- **Orderbook:** `orderbook_fp.yes_dollars` and `no_dollars` are **bids** only, as [price, qty]. YES ask = 1 − best NO bid.
- **Fees:** taker `roundup(0.07 × C × P × (1−P))`, maker `roundup(0.0175 × C × P × (1−P))` ([fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf), effective July 7, 2026). The NBA series API reports `fee_type: quadratic_with_maker_fees` and `fee_multiplier: 1`. There's no settlement fee. Fees are rounded up to the cent per fill, which is conservative.

## Structure
```
nba-bookie/
├── README.md  SYSTEM_PROMPT.md  WORKFLOW.md  config.yaml  bookie.py
├── data/
│   ├── bankroll.json        # starting/current bankroll + history
│   ├── bets.csv             # paper ledger (placed/passed/settled/exited, fee math, EV, CLV)
│   ├── summaries/           # post-run reports YYYY-MM-DD.md
│   └── fair/                # live fair-prob files for `monitor` (gitignored)
└── examples/
    ├── sample_picks.json        # EXAMPLE DATA: 6 picks, inline books
    ├── sample_run_output.txt    # fee math, passes, tiers, cap scaling
    ├── sample_summary.md        # post-run report
    └── sample_fair_file.json    # monitor input format (test only)
```

## Usage
```bash
python3 bookie.py markets --date 2026-10-22                     # discover tickers
python3 bookie.py book KXNBAGAME-26OCT22BOSNYK-NYK --contracts 100 --fair 0.62
python3 bookie.py fee --price 0.55 --contracts 100              # fee calculator
python3 bookie.py size picks.json                               # dry run
python3 bookie.py run picks.json                                # auto paper-place + summary
python3 bookie.py monitor --event KXNBAGAME-26OCT22BOSNYK --dry-run
python3 bookie.py close 1 --live | --price 0.58                 # CLV
python3 bookie.py exit 3 --live | --price 0.52                  # paper trade-out
python3 bookie.py settle 1 win|loss|void
python3 bookie.py status | summary [--date D] | reset
```
It runs on the Python stdlib alone. PyYAML is optional, and a built-in parser reads the flat `config.yaml`.

⚠️ Tiers of 10/15/20% per trade, with a 60% nightly cap, are far above 1-5% Kelly sizing and carry a high risk of ruin. This is Jack's rule, kept but flagged.
