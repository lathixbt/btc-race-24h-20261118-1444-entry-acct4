# Snapback-120 — BTC 24h Race Bot

[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/lathixbt/btc-race-24h-20261118-1444-entry-acct4/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/lathixbt/btc-race-24h-20261118-1444-entry-acct4/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

A long-only bot that trades CBBTC minute by minute in the BTC 24h race.
It takes at most three shots per window: one sharp-dip buy and up to two
crash-rebound buys, each spending all cash.

## How it trades

**Sharp dip (short lookback, fixed take-profit).** The dip reads the high
of the last 60 bars, so it can catch a drop that prints in the first
hour, long before a 120-bar lookback exists. It fires when the close
fell at least 120 bps from that 60-bar high, the 720-bar fall stays
under 550 bps (not a capitulation), RSI(14) sits under 50, the close
also fell at least 150 bps over the last 60 bars, and this bar closes
up. It exits at a fixed take-profit of +120 bps or a hard stop of
-60 bps, with no trailing, and holds at most 300 bars.

**Crash rebound.** Fires only on a real capitulation: a fall of at
least 150 bps over 60 bars (or 250 bps over 120 bars) plus a fall of
at least 550 bps from the 720-bar high, a long lower-wick bar in the
last 20 bars, a reversal bar closing up at least 60 bps above the
previous bar's high, RSI(14) turning up, and room back to the 720-bar
high of at least twice the risk. The stop sits below the capitulation
low, clamped to 50–100 bps under the entry. It exits on a trailing
stop (arms at +80 bps, trails the peak by 80 bps) or after 300 bars.

**Guards.** At most 3 entries per window; no entries after 2
consecutive rebound losses; no entries if equity falls under 98% of
the start; a 60-bar cooldown after every exit; no entries after bar
1320; max slippage 200 bps; at least 62 closes of history before the
first entry.

## Files

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the kit's bars-replay shim, copied unchanged.
