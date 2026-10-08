# Reef post-run report: 2026-10-22  (EXAMPLE DATA)

**Mode:** autonomous PAPER trading on Kalshi NBA markets. "Placed" = logged in the ledger; no real orders.
**Bankroll:** $500.00 now ($500.00 at run, start $500.00)

**Games analyzed (6):** BOS @ NYK; LAL @ PHX; DEN @ MIN; MIA @ ORL; GSW @ SAC; DAL @ HOU

## Placed (4)
| ID | Market | Side/Style | Conf | Tier | Qty @ avg | All-in | % BR | Fair | EV net | CLV | Status | Why |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Knicks win | YES/hold | 96 | 20% | 165 @ 0.54 | $92.04 | 18.4% | 0.62 | +6.2c ($10.26) | pending | placed | BOS 2nd night of B2B after OT; NYK rested; Kalshi 0.53 lags books 61.5% |
| 2 | BOS-NYK Over 221.5 | NO/hold | 86 | 15% | 125 @ 0.53 | $68.69 | 13.7% | 0.58 | +3.0c ($3.81) | pending | placed | Under via NO on Over 221.5: tired BOS legs + NYK slow pace |
| 3 | Suns win | YES/trade | 80 | 10% | 96 @ 0.46 | $45.83 | 9.2% | 0.53 | +3.0c ($2.89) | pending | placed | LAL star doubtful (ankle); trade style: exit if price reprices to fair |
| 6 | Kings win | YES/hold | 95 | 20% | 167 @ 0.53 | $91.77 | 18.4% | 0.60 | +5.1c ($8.43) | pending | placed | GSW 3rd road game in 4 nights, Curry minutes limit |

**Fee math (Kalshi taker: roundup(0.07 x C x P x (1-P)) per fill):**
- #1: ceil(0.07x50x0.53x0.47=0.8719)=$0.88 + ceil(0.07x60x0.54x0.46=1.0433)=$1.05 + ceil(0.07x55x0.55x0.45=0.9529)=$0.96 | spread $1.65, slippage $1.70
- #2: ceil(0.07x100x0.53x0.47=1.7437)=$1.75 + ceil(0.07x25x0.54x0.46=0.4347)=$0.44 | spread $2.50, slippage $0.25
- #3: ceil(0.07x96x0.46x0.54=1.6692)=$1.67 | est exit: half-spread $0.48 + fee $1.68 | spread $0.48, slippage $0.00
- #6: ceil(0.07x60x0.52x0.48=1.0483)=$1.05 + ceil(0.07x60x0.53x0.47=1.0462)=$1.05 + ceil(0.07x47x0.55x0.45=0.8143)=$0.82 | spread $2.51, slippage $2.01

## Passed (2)
- #4 Wolves win by over 2.5 YES (conf 82, fair 0.53, ask 0.52): Small edge on paper but price is at fair once costs are counted [PASSED: edge after costs -1.0c/-1.9% < hurdle 2c & 3%]
- #5 Heat win YES (conf 70, fair 0.44, ask 0.36): Spoelstra dog angle but ORL defense matchup bad for MIA shooters [PASSED: confidence 70 < 75]

**Exposure:** $298.33 = 59.7% of run bankroll vs 60% cap ($300.00). Budgets scaled x0.92.
**Fees tonight:** $9.67

**Correlation flags:**
- SAME SLATE: all 4 trades resolve tonight; one bad night hits every position
- SIDE+TOTAL same game (26OCT22BOSNYK) #1<->#2
- SHARED FACTOR (back-to-back) #1<->#2
- Note: Pre-cap budgets $325.00 > nightly cap $300.00; budgets scaled x0.923.

**CLV tonight:** no closing prices recorded yet  |  **all-time:** no closing prices recorded yet

**Risk note:** 10%/15%/20% per trade is far above 1-5% Kelly-style sizing; with up to 60% exposed a night, one bad night can cut the bankroll by more than half.
