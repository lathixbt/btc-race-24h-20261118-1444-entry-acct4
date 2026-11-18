"""Snapback-120: a long-only dip-and-bounce bot for the BTC 24h race.

The bot watches one asset (CBBTC) minute by minute and takes at most
three shots per window: one sharp-dip buy and up to two crash-rebound
buys. Every buy spends all cash. A dip buy aims at a fixed take-profit
(+120 bps) with a hard stop (-60 bps) and no trailing; a rebound buy
rides a trailing stop (arm/trail 80/80). Stops and targets are fixed
at decision time from the decision bar's close, never from the fill.

The dip only fires early: it reads the high of the last 60 bars, so
it can catch a drop that prints in the first hour, long before a
120-bar lookback exists. The rebound is the crash-day saver: it needs
a 550 bps fall from the 720-bar high, so it stays silent on ordinary
range days.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

ASSET = "CBBTC"
UNIT = 10_000

# --- Crash-rebound setup (needs a real capitulation) ---
RB_FALL_A = 150        # fall over 60 bars (prev bar), or ...
RB_FALL_B = 250        # ... fall over 120 bars (prev bar)
RB_FALL_TOP = 550      # fall from the highest high of the last 720 bars
RB_WICK_MIN = 20       # wick bar range at least 20 bps
RB_WICK_PART = 60      # close in the top 40% of its bar (lower wick >= 60%)
RB_WICK_SPAN = 20      # wick bar within the last 20 bars
RB_LIFT = 60           # reversal bar up >= 60 bps and above the prev high
RB_PAD = 20            # stop sits 20 bps under the capitulation low
RB_STOP_MIN = 100      # stop clamped to 50..100 bps below the entry
RB_STOP_MAX = 50
RB_ARM = 80            # trailing stop arms at +80 bps
RB_TRAIL = 80          # ... then trails the peak by 80 bps
RB_HOLD = 300
RB_LIMIT = 2

# --- Sharp-dip setup (short lookback, fixed take-profit) ---
DP_PEAK_SPAN = 60      # high lookback for the dip: 60 bars, not 120
DP_DROP = 120          # fall from the highest high of the last 60 bars
DP_CAP = 550           # the 720-bar fall must stay UNDER this
DP_RSI = 50            # RSI(14) under 50
DP_SWIFT = 150         # ... AND a fall of >= 150 bps over the last 60 bars
DP_STOP = 60           # hard stop 60 bps under the entry
DP_TAKE = 120          # fixed take-profit 120 bps above the decision close
DP_HOLD = 300
DP_LIMIT = 1

# --- Guards ---
ENTRY_MAX = 3          # at most 3 entries per window (1 dip + 2 rebound)
RB_MISS_MAX = 2        # stop after 2 consecutive REBOUND losses
LOSS_HALT = 200        # stop entering if equity drops under 98% of start
COOL_BARS = 60         # bars to wait after an exit
SLIP_CAP = 200         # max slippage vs the latest close
WARM_BARS = 62         # closes of history before any entry
ENTRY_LAST = 1320      # no entries after this bar
RSI_N = 14

# --- Per-run state ---
closes = []            # closes, newest last
highs = []             # highs, newest last
lows = []              # lows, newest last
rsi_series = []        # RSI(14) per bar, None until enough closes
_gain = None           # Wilder RSI running averages
_loss = None
_seen_t = None         # batch time of the last bar processed
_cash0 = None          # start capital from hello
_dip_n = 0
_rb_n = 0
_rb_misses = 0
_rest_until = -1       # bar seq before which no new entry
_awaiting = None       # order decided this batch, awaiting its fill
_trade = None          # open position


def _below(ref_px, px):
    """How far px sits below ref_px, in basis points (integer math)."""
    if ref_px <= 0:
        return 0
    return (ref_px - px) * UNIT // ref_px


def _drop_from_top(span):
    """Fall from the max high over `span` bars to the previous close, bps.

    The window ends just before the previous bar (highs[-span-1:-1]).
    Returns 0 when there is not enough history yet.
    """
    if len(highs) < span + 1:
        return 0
    top = max(highs[-span - 1:-1])
    prev = closes[-2]
    if top <= 0:
        return 0
    return (top - prev) * UNIT // top


def _drop_close(n):
    """Close-to-close fall over n bars: close n bars ago vs prev close."""
    if len(closes) < n + 2:
        return 0
    return _below(closes[-n - 2], closes[-2])


def _wick_found():
    """A long lower-wick bar printed in the last RB_WICK_SPAN bars."""
    n = len(closes)
    start = max(0, n - RB_WICK_SPAN)
    for j in range(start, n):
        hi = highs[j]
        lo = lows[j]
        c = closes[j]
        if hi <= lo or c <= 0:
            continue
        rng = (hi - lo) * UNIT // c
        if rng < RB_WICK_MIN:
            continue
        # close in the top 40% of the range: lower wick >= 60% of it.
        if (c - lo) * 100 // (hi - lo) >= RB_WICK_PART:
            return True
    return False


def _rsi_push():
    """Append the RSI(14) for the newest close (Wilder's smoothing)."""
    global _gain, _loss
    n = len(closes)
    if n < 2:
        rsi_series.append(None)
        return
    gain = closes[-1] - closes[-2]
    if gain < 0:
        gain = 0
    loss = closes[-2] - closes[-1]
    if loss < 0:
        loss = 0
    if n == RSI_N + 1:
        # First value: plain mean of the first RSI_N changes.
        gs = 0
        ls = 0
        for i in range(1, RSI_N + 1):
            d = closes[i] - closes[i - 1]
            if d > 0:
                gs += d
            else:
                ls += -d
        _gain = gs / RSI_N
        _loss = ls / RSI_N
    elif n > RSI_N + 1:
        _gain = (_gain * (RSI_N - 1) + gain) / RSI_N
        _loss = (_loss * (RSI_N - 1) + loss) / RSI_N
    else:
        rsi_series.append(None)
        return
    if _loss == 0:
        rsi_series.append(100.0)
    else:
        rs = _gain / _loss
        rsi_series.append(100.0 - 100.0 / (1.0 + rs))


def _rebound_ok():
    """Check every rebound condition on the latest bars.

    Returns (ok, stop_px): the prospective stop when the setup fires,
    else (False, 0). All fall measures anchor on the previous bar
    (idx -2), because the reversal bar itself lifts the close and
    would shrink the drop.
    """
    m = len(closes)
    if m < 2 or rsi_series[-1] is None or rsi_series[-2] is None:
        return (False, 0)
    prev = closes[-2]
    cur = closes[-1]
    # 1. deep fall measured at the previous bar: 60 bars or 120 bars.
    if _drop_close(60) < RB_FALL_A and _drop_close(120) < RB_FALL_B:
        return (False, 0)
    # 2. capitulation: the fall from the 720-bar high must reach 550 bps.
    top720 = max(highs[-721:-1])
    if _drop_from_top(720) < RB_FALL_TOP:
        return (False, 0)
    # 3. a long lower wick printed in the last 20 bars.
    if not _wick_found():
        return (False, 0)
    # 4. reversal: this bar closes up >= 60 bps and above the prev high.
    if (cur - prev) * UNIT // prev < RB_LIFT:
        return (False, 0)
    if cur <= highs[-2]:
        return (False, 0)
    # 5. RSI(14) turning up (no level filter).
    if rsi_series[-1] <= rsi_series[-2]:
        return (False, 0)
    # 6. reward/risk: room back to the 720-bar high at least twice the risk.
    low720 = min(lows[-721:-1])
    stop0 = low720 * (UNIT - RB_PAD) // UNIT
    lo_stop = cur * (UNIT - RB_STOP_MIN) // UNIT
    hi_stop = cur * (UNIT - RB_STOP_MAX) // UNIT
    stop = min(max(stop0, lo_stop), hi_stop)
    if top720 - cur < 2 * (cur - stop):
        return (False, 0)
    return (True, stop)


def _dip_ok():
    """Check every dip condition on the latest bars.

    A range dip that is also a FAST fall: the close dropped hard from
    the 60-bar high, is not a capitulation (720-bar fall under 550
    bps), RSI(14) sits under 50, this bar closes up, and the close
    also fell at least 150 bps over the last 60 bars (the sharp-drop
    filter, close-to-close).
    """
    m = len(closes)
    if m < 2 or rsi_series[-1] is None:
        return False
    prev = closes[-2]
    cur = closes[-1]
    # 1. fall from the highest high of the last 60 bars (prev bar).
    if _drop_from_top(DP_PEAK_SPAN) < DP_DROP:
        return False
    # 2. not a capitulation: the 720-bar fall stays under 550 bps.
    if _drop_from_top(720) >= DP_CAP:
        return False
    # 3. RSI(14) under 50.
    if rsi_series[-1] >= DP_RSI:
        return False
    # 4. this bar closes up (any amount).
    if cur <= prev:
        return False
    # 5. sharp drop: close-to-close fall over the last 60 bars >= 150 bps.
    if _drop_close(60) < DP_SWIFT:
        return False
    return True


def _trade_won(exit_px):
    """Win/loss call for the exit price vs entry (cost-aware)."""
    if _trade is None:
        return False
    return exit_px > _trade["entry"]


def on_hello(state):
    """Capture the start capital from hello (never a hardcoded constant)."""
    global _cash0
    _cash0 = int(state.hello["capital"]["quote_units"])


def on_batch(state, batch):
    """Decide the orders for this batch. Returns a list of orders (or [])."""
    global _seen_t, _dip_n, _rb_n, _rb_misses
    global _rest_until, _awaiting, _trade, _cash0

    # Find this batch's CBBTC bar; skip the batch when it carries none.
    bar = None
    for b in batch.get("bars", []):
        if b.get("symbol") == ASSET:
            bar = b
            break
    if bar is None:
        return []
    # Duplicate-bar guard: skip a batch that carries no new bar.
    t = batch.get("t")
    if t is not None and t == _seen_t:
        return []
    _seen_t = t

    cur = int(bar["c"])
    hi = int(bar["h"])
    lo = int(bar["l"])
    closes.append(cur)
    highs.append(hi)
    lows.append(lo)
    _rsi_push()

    if _cash0 is None:
        _cash0 = int(state.hello["capital"]["quote_units"])

    holding = state.positions.get(ASSET)
    seq = state.seq

    # A pending buy either filled (position now held) or was rejected.
    if _awaiting is not None:
        if holding is not None:
            # Filled at this bar's open: entry is the fill bar's open.
            # The stop and target were already fixed at decision time
            # from the decision close.
            _trade = {
                "kind": _awaiting["kind"],
                "entry": int(bar["o"]),
                "stop": _awaiting["stop"],
                "take": _awaiting["take"],
                "peak": cur,
                "held": 0,
                "armed": False,
                "arm": _awaiting["arm"],
                "trail": _awaiting["trail"],
                "hold": _awaiting["hold"],
            }
            if _awaiting["kind"] == "DP":
                _dip_n += 1
            else:
                _rb_n += 1
        _awaiting = None

    # --- Manage an open position: stop, target, trailing, max hold. ---
    if _trade is not None and holding is not None:
        _trade["held"] += 1
        if cur > _trade["peak"]:
            _trade["peak"] = cur
        if not _trade["armed"] and cur >= _trade["entry"] * (UNIT + _trade["arm"]) // UNIT:
            _trade["armed"] = True
        exit_px = None
        if _trade["kind"] == "DP":
            # Fixed take-profit, no trailing for the dip.
            if cur >= _trade["take"]:
                exit_px = cur
            elif cur <= _trade["stop"]:
                exit_px = _trade["stop"]
            elif _trade["held"] >= _trade["hold"]:
                exit_px = cur
        else:
            if cur <= _trade["stop"]:
                exit_px = _trade["stop"]
            elif _trade["armed"] and cur <= _trade["peak"] * (UNIT - _trade["trail"]) // UNIT:
                exit_px = _trade["peak"] * (UNIT - _trade["trail"]) // UNIT
            elif _trade["held"] >= _trade["hold"]:
                exit_px = cur
        if exit_px is not None:
            won = _trade_won(exit_px)
            kind = _trade["kind"]
            _trade = None
            _rest_until = seq + COOL_BARS
            if kind == "RB":
                if won:
                    _rb_misses = 0
                else:
                    _rb_misses += 1
            return [{"side": "sell", "symbol": ASSET,
                     "qty": holding.qty, "max_slippage_bps": SLIP_CAP}]
        return []

    if _trade is not None and holding is None:
        # Position vanished without our sell (should not happen): clear.
        _trade = None

    # --- No position: look for an entry. ---
    if len(closes) < WARM_BARS:
        return []
    if seq > ENTRY_LAST:
        return []
    if _dip_n + _rb_n >= ENTRY_MAX:
        return []
    if seq < _rest_until:
        return []
    if _cash0 is not None and state.equity < _cash0 * (UNIT - LOSS_HALT) // UNIT:
        return []

    # Rebound first; dip second. They are mutually exclusive by the
    # 720-bar fall (rebound needs >= 550, dip needs < 550).
    if _rb_n < RB_LIMIT and _rb_misses < RB_MISS_MAX:
        ok, stop_px = _rebound_ok()
        if ok:
            _awaiting = {"kind": "RB", "stop": stop_px, "take": 0,
                         "arm": RB_ARM, "trail": RB_TRAIL, "hold": RB_HOLD}
            return [{"side": "buy", "symbol": ASSET,
                     "notional_quote_units": state.cash,
                     "max_slippage_bps": SLIP_CAP}]
    if _dip_n < DP_LIMIT and _dip_ok():
        stop_px = cur * (UNIT - DP_STOP) // UNIT
        take_px = cur * (UNIT + DP_TAKE) // UNIT
        _awaiting = {"kind": "DP", "stop": stop_px, "take": take_px,
                     "arm": 0, "trail": 0, "hold": DP_HOLD}
        return [{"side": "buy", "symbol": ASSET,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": SLIP_CAP}]
    return []


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello)
