# Perp slippage snapshot — 2026-11-02T03:52:43+00:00

Total cost = slippage vs mid + base-tier taker fee, in bps, averaged over buy & sell. `*` = book snapshot too shallow to fill the clip.

## BTC

| venue | spread bps | $10,000 | $100,000 | $500,000 | $1,000,000 |
|---|---|---|---|---|---|
| aster | 0.01 | 4.16 | 4.59 | 4.85 | 5.25 |
| extended | 0.13 | 2.56 | 2.64 | 2.93 | 3.34 |
| grvt | 0.01 | 4.51 | 4.52 | 5.06 | 5.20* |
| hyperliquid | 0.13 | 4.56 | 4.75 | 5.39 | 5.81 |
| lighter | 0.04 | 0.02 | 0.17 | 0.50 | 0.80 |
| paradex | 1.77 | 0.88 | 0.88 | 35.14 | 257.73* |

## ETH

| venue | spread bps | $10,000 | $100,000 | $500,000 | $1,000,000 |
|---|---|---|---|---|---|
| aster | 0.04 | 4.63 | 4.68 | 4.85 | 5.24 |
| extended | 0.43 | 2.72 | 2.72 | 3.70 | 4.56 |
| grvt | 0.04 | 4.52 | 4.82 | 5.57 | 5.60* |
| hyperliquid | 0.43 | 5.02 | 5.42 | 5.71 | 5.95 |
| lighter | 0.56 | 0.29 | 0.68 | 1.56 | 2.43 |
| paradex | 1.69 | 0.87 | 0.87 | 81.56 | 115.45* |

## SOL

| venue | spread bps | $10,000 | $100,000 | $500,000 | $1,000,000 |
|---|---|---|---|---|---|
| aster | 1.05 | 4.57 | 5.00 | 6.35 | 7.88 |
| extended | 1.05 | 3.02 | 3.51 | 5.15 | 7.01 |
| grvt | 1.05 | 5.28 | 6.54 | 8.60 | 10.51* |
| hyperliquid | 1.26 | 5.17 | 5.66 | 7.42 | 8.66 |
| lighter | 0.63 | 0.44 | 1.73 | 3.96 | 5.64 |
| paradex | 2.93 | 1.47 | 19.61 | 390.74* | 390.74* |

## HYPE

| venue | spread bps | $10,000 | $100,000 | $500,000 | $1,000,000 |
|---|---|---|---|---|---|
| aster | 1.25 | 4.94 | 5.39 | 8.86 | 13.79 |
| extended | 0.11 | 2.74 | 4.22 | 10.93 | 20.01 |
| grvt | 0.57 | 4.78 | 5.57 | 6.10* | 6.10* |
| hyperliquid | 0.34 | 6.41 | 7.95 | 10.66 | 13.16 |
| lighter | 1.52 | 2.21 | 3.37 | 6.34 | 8.94 |
| paradex | 2.95 | 1.47 | 19.38 | 161.56* | 161.56* |

## Errors
- edgex / BTC: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
- edgex / ETH: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
- edgex / SOL: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
- edgex / HYPE: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
