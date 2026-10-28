"""Weekly POI map (owner 25 Oct 2026): every Sunday evening (20:00 Dubai = 16:00 UTC) the CRT read of the coming week, the
model of the owner's CRT v6 LF 9.9 on the weekly candle (ICT auto map: 4H chart -> W). No fixed trade: two if / then sides.

  Range   = the week that is closing (Monday 00:00 UTC to Sunday 20:00 Dubai): its high, low and 50 %.
  Long    if next week takes the range low, look for the reaction at one of the POIs below (the swept low first, then the
          deeper liquidity: untaken 4H lows, open bullish 4H FVGs, older weekly lows), confirmed by the script's 4H CISD
          (app/cisd.py: purge + 0.66 ATR stretch). Then expect the 50 %, then the DOL = the range high. Short = the mirror.
  Why     Replay of these rules on SOL, Feb - Oct 2026 (35 weeks, exact CISD): the 1st reaction at the swept level lost
          (23 trades, -6.1R); the reaction at a deeper POI paid (6 trades, TP1 5, DOL 4, +16.7R). Daily -> 1H lost;
          15m -> 4H (LODESTAR v0.2) won (+5.1R, TP1 10 / 14). Small samples: the live weeks keep measuring.
  Live    the page tracks each side through the week (swept? which POI reacted? CISD? TP1 / DOL / stopped).
Measured from market.db candles; Luna writes the story around these levels and never invents one.

Measured, not guessed: every price on the page comes from market.db candles (4h structure, 1h detail, 1d / 1w context).
Stored in data/<ASSET>/weekly/<YYYY-MM-DD>.json (the Sunday it was made) and in data/gameplan.db table weekly, where the
week's outcome is filled in as it plays out (filled? TP1? target? stopped?) for the review.

Page: /levels/weekly · API: /levels/api/v1/<asset>/weekly[?week=YYYY-MM-DD], /levels/api/v1/<asset>/weekly/list
"""
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import llm

LOG = logging.getLogger("lunaflow.weekly")
UTC = timezone.utc
H = 3_600_000
DUE_HOUR_UTC = 16                     # Sunday 16:00 UTC = 20:00 Dubai (owner's sol_smc_api.py schedule)
MODEL = os.environ.get("WEEKLY_MODEL", os.environ.get("DAYPLAN_MODEL", "claude-sonnet-5-5"))
DIR = "weekly"


# ── candles ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _bars(data_dir, asset, tf, n, until_ms):
    p = Path(data_dir) / "market.db"
    if not p.exists():
        return []
    ms = {"1h": H, "4h": 4 * H, "1d": 24 * H, "1w": 7 * 24 * H}[tf]
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    try:
        rows = db.execute("SELECT t,o,h,l,c FROM candles WHERE asset=? AND tf=? AND t+?<=? ORDER BY t DESC LIMIT ?",
                          (asset, tf, ms, until_ms, n)).fetchall()
    finally:
        db.close()
    return [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in rows[::-1]]


def atr(bars, n=14):
    if len(bars) < 2:
        return None
    tr = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars, bars[1:])]
    tr = tr[-n:]
    return sum(tr) / len(tr)


def ema(vals, n):
    if not vals:
        return None
    k, e = 2 / (n + 1), vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


# ── structure ───────────────────────────────────────────────────────────────────────────────────────────────────────
def pivots(bars, k=3):
    """Fractal swing points: a high higher than the k bars on each side (and the mirror for lows)."""
    out = []
    for i in range(k, len(bars) - k):
        hs, ls = [bars[j]["h"] for j in range(i - k, i + k + 1)], [bars[j]["l"] for j in range(i - k, i + k + 1)]
        if bars[i]["h"] == max(hs) and hs.count(bars[i]["h"]) == 1:
            out.append({"i": i, "t": bars[i]["t"], "kind": "H", "price": bars[i]["h"]})
        if bars[i]["l"] == min(ls) and ls.count(bars[i]["l"]) == 1:
            out.append({"i": i, "t": bars[i]["t"], "kind": "L", "price": bars[i]["l"]})
    return out


def trend(piv, closes):
    hs = [p["price"] for p in piv if p["kind"] == "H"][-2:]
    ls = [p["price"] for p in piv if p["kind"] == "L"][-2:]
    e20, e50 = ema(closes[-120:], 20), ema(closes[-150:], 50)
    s = 0
    if len(hs) == 2 and len(ls) == 2:
        s += 1 if hs[1] > hs[0] and ls[1] > ls[0] else -1 if hs[1] < hs[0] and ls[1] < ls[0] else 0
    if e20 and e50:
        s += 1 if e20 > e50 else -1
    lab = "BULLISH" if s >= 1 else "BEARISH" if s <= -1 else "RANGING"
    # A structure label needs two highs AND two lows; either side may be absent in sparse data.
    struct = ("higher highs and higher lows" if len(hs) == 2 and len(ls) == 2 and hs[1] > hs[0] and ls[1] > ls[0] else
              "lower highs and lower lows" if len(hs) == 2 and len(ls) == 2 and hs[1] < hs[0] and ls[1] < ls[0] else "mixed swings")
    return {"label": lab, "score": s, "structure": struct, "ema20": e20, "ema50": e50}


def fvgs(bars, a, min_atr=0.25):
    """Three-candle fair value gaps that are still open (not fully filled by a later candle)."""
    out = []
    for i in range(2, len(bars)):
        a0, c2 = bars[i - 2], bars[i]
        if c2["l"] > a0["h"] and c2["l"] - a0["h"] >= min_atr * a:
            lo, hi, side = a0["h"], c2["l"], "bull"
        elif c2["h"] < a0["l"] and a0["l"] - c2["h"] >= min_atr * a:
            lo, hi, side = c2["h"], a0["l"], "bear"
        else:
            continue
        later = bars[i + 1:]
        filled = any(b["l"] <= lo for b in later) if side == "bull" else any(b["h"] >= hi for b in later)
        if filled:
            continue
        touched = any(b["l"] < hi for b in later) if side == "bull" else any(b["h"] > lo for b in later)
        out.append({"side": side, "lo": lo, "hi": hi, "t": bars[i - 1]["t"], "i": i - 1, "touched": touched})
    return out


def liquidity(bars, piv, a, price, pct_tol=0.0015):
    """Untaken swing highs / lows and equal highs / lows ($$$) — where resting orders sit."""
    tol = max(price * pct_tol, 0.2 * a)
    pools = []
    for kind in ("H", "L"):
        ps = [p for p in piv if p["kind"] == kind]
        for p in ps:
            later = bars[p["i"] + 1:]
            taken = any(b["h"] > p["price"] for b in later) if kind == "H" else any(b["l"] < p["price"] for b in later)
            if taken:
                continue
            twins = [q for q in ps if q is not p and abs(q["price"] - p["price"]) <= tol]
            pools.append({"side": "buy" if kind == "H" else "sell", "price": p["price"], "t": p["t"], "i": p["i"],
                          "equal": bool(twins), "t2": max([q["t"] for q in twins] + [p["t"]])})
    # merge equal pairs: keep the extreme of each cluster
    pools.sort(key=lambda x: x["price"])
    merged = []
    for p in pools:
        if merged and merged[-1]["side"] == p["side"] and abs(merged[-1]["price"] - p["price"]) <= tol:
            m = merged[-1]
            m["equal"] = True
            m["t"], m["t2"] = min(m["t"], p["t"]), max(m["t2"], p["t2"], p["t"])
            m["price"] = max(m["price"], p["price"]) if p["side"] == "buy" else min(m["price"], p["price"])
        else:
            merged.append(dict(p))
    return merged


def sweeps(bars, piv, look=30):
    """Turtle soup: a candle trades through an untaken swing and closes back on the other side. Most recent first."""
    out = []
    n = len(bars)
    for j in range(max(0, n - look), n):
        b = bars[j]
        for p in piv:
            if p["i"] >= j - 1 or j - p["i"] > 150:
                continue
            between = bars[p["i"] + 1:j]
            if p["kind"] == "H" and b["h"] > p["price"] and b["c"] < p["price"] and not any(x["h"] > p["price"] for x in between):
                out.append({"side": "bear", "t": b["t"], "i": j, "level": p["price"], "extreme": b["h"], "level_t": p["t"]})
            if p["kind"] == "L" and b["l"] < p["price"] and b["c"] > p["price"] and not any(x["l"] < p["price"] for x in between):
                out.append({"side": "bull", "t": b["t"], "i": j, "level": p["price"], "extreme": b["l"], "level_t": p["t"]})
    out.sort(key=lambda s: -s["i"])
    return out


def mitigation_block(bars, fvg):
    """The base the displacement left from: the last opposite candles before the FVG's displacement (an order block)."""
    if not fvg:
        return None
    i = fvg["i"] - 1
    want_down = fvg["side"] == "bull"
    j = i
    while j > 0 and not ((bars[j]["c"] < bars[j]["o"]) if want_down else (bars[j]["c"] > bars[j]["o"])):
        j -= 1
    if j <= 0:
        return None
    k = j
    while k - 1 > 0 and k > j - 3 and ((bars[k - 1]["c"] < bars[k - 1]["o"]) if want_down else (bars[k - 1]["c"] > bars[k - 1]["o"])):
        k -= 1
    blk = bars[k:j + 1]
    return {"side": fvg["side"], "lo": min(b["l"] for b in blk), "hi": max(b["h"] for b in blk), "t": blk[0]["t"], "t_end": blk[-1]["t"]}


# ── the read ───────────────────────────────────────────────────────────────────────────────────────────────────────
def week_of(now):
    """The Sunday (UTC date) this analysis belongs to: made on Sunday, for the week starting Monday."""
    d = datetime.fromtimestamp(now, UTC)
    back = (d.weekday() + 1) % 7                     # Sunday -> 0, Monday -> 1 ...
    return datetime.fromtimestamp(now - back * 86400, UTC).strftime("%Y-%m-%d")


def smc_trap(bars, piv, label):
    """The owner's "SMC trap" (sol_smc_api.py): the retail trendline through the last two swing lows in an up move (two swing
    highs in a down move). Its stops are the liquidity smart money takes. Extended to the last candle."""
    kind = "H" if label == "BEARISH" else "L"
    ps = [p for p in piv if p["kind"] == kind][-2:]
    if len(ps) < 2 or ps[1]["i"] == ps[0]["i"]:
        return None
    slope = (ps[1]["price"] - ps[0]["price"]) / (ps[1]["i"] - ps[0]["i"])
    n = len(bars) - 1
    end = ps[0]["price"] + slope * (n - ps[0]["i"])
    broken = (bars[-1]["c"] > end) if kind == "H" else (bars[-1]["c"] < end)
    return {"t0": ps[0]["t"], "p0": ps[0]["price"], "t1": bars[-1]["t"], "p1": end, "kind": kind, "broken": broken}


def measure(data_dir, asset, now=None, tf="4h"):
    """Everything the page draws, from closed candles only. No model involved. tf: the structure timeframe (4h or 1h)."""
    now = now or time.time()
    until = int(now * 1000)
    b1 = _bars(data_dir, asset, "1h", 300 if tf == "1h" else 240, until)
    b4 = b1 if tf == "1h" else _bars(data_dir, asset, "4h", 300, until)
    bd, bw = _bars(data_dir, asset, "1d", 30, until), _bars(data_dir, asset, "1w", 6, until)
    if len(b4) < 60:
        raise RuntimeError(f"not enough {tf} candles for {asset} ({len(b4)})")
    a4, a1 = atr(b4), atr(b1) if len(b1) > 20 else None
    price = b4[-1]["c"]
    piv = pivots(b4, 3)
    tr = trend(piv, [b["c"] for b in b4])
    rng_bars = b4[-120:]
    hi_i = max(range(len(rng_bars)), key=lambda i: rng_bars[i]["h"])
    lo_i = min(range(len(rng_bars)), key=lambda i: rng_bars[i]["l"])
    rhi, rlo = rng_bars[hi_i]["h"], rng_bars[lo_i]["l"]
    eq = (rhi + rlo) / 2
    zone = "PREMIUM" if price > eq else "DISCOUNT"
    gaps = fvgs(b4, a4)
    pools = liquidity(b4, piv, a4, price)
    sw = sweeps(b4, piv)
    mon = int((now - ((datetime.fromtimestamp(now, UTC).weekday()) * 86400)) // 86400 * 86400 * 1000)    # Monday 00:00 UTC
    wk = [b for b in b1 if b["t"] >= mon]
    lastw = {"t": mon, "o": wk[0]["o"], "h": max(b["h"] for b in wk), "l": min(b["l"] for b in wk), "c": wk[-1]["c"]} if wk else (bw[-1] if bw else None)
    pw = next((w for w in reversed(bw) if w["t"] < mon), None)          # the full week before
    adr = (sum(d["h"] - d["l"] for d in bd[-14:]) / len(bd[-14:])) if bd else None
    above = sorted([p for p in pools if p["side"] == "buy" and p["price"] > price], key=lambda p: p["price"])
    below = sorted([p for p in pools if p["side"] == "sell" and p["price"] < price], key=lambda p: -p["price"])
    gap_up = sorted([g for g in gaps if g["lo"] > price], key=lambda g: g["lo"])
    gap_dn = sorted([g for g in gaps if g["hi"] < price], key=lambda g: -g["hi"])
    gap_in = [g for g in gaps if g["lo"] <= price <= g["hi"]]
    piv1 = pivots(b1, 2) if len(b1) > 20 else []
    return {"asset": asset, "tf": tf, "now_ms": until, "price": price, "atr4h": a4, "atr1h": a1, "adr": adr,
            "trap": smc_trap(b4, piv, tr["label"]),
            "trend": tr, "range": {"hi": rhi, "lo": rlo, "eq": eq, "hi_t": rng_bars[hi_i]["t"], "lo_t": rng_bars[lo_i]["t"], "zone": zone},
            "pivots": piv[-40:], "pivots_1h": piv1[-40:], "fvgs": gaps[-20:], "fvg_above": gap_up[:3], "fvg_below": gap_dn[:3],
            "fvg_inside": gap_in[:2], "pools_above": above[:4], "pools_below": below[:4], "sweeps": sw[:4],
            "last_week": lastw, "prev_week": pw, "b4": b4, "b1": b1}


def _r(x, a):
    return None if x is None else round(x, 2 if a > 1 else 5)


def _lvl(x):
    return f"{x:,.4f}".rstrip("0").rstrip(".") if x < 10 else f"{x:,.2f}"


FINDINGS = {
    "weekly_4h": {"label": "Weekly CRT, 4H CISD (Feb-Oct 2026, 35 weeks)", "rows": [
        {"rule": "1st reaction at the swept weekly level", "trades": 23, "tp1": 7, "dol": 3, "r": -6.08},
        {"rule": "Reaction at a deeper POI (2nd)", "trades": 6, "tp1": 5, "dol": 4, "r": 16.69}]},
    "m15_4h": {"label": "4H CRT, 15m CISD = LODESTAR v0.2 (22 Sep-25 Oct 2026, 198 candles)", "rows": [
        {"rule": "The script as is (live sweep + CISD)", "trades": 14, "tp1": 10, "dol": 5, "r": 5.12},
        {"rule": "Same, setup kept open 2 x 4H", "trades": 31, "tp1": 24, "dol": 10, "r": 15.81}]},
    "d_1h": {"label": "Daily CRT, 1H CISD (Aug-Oct 2026)", "rows": [
        {"rule": "The script as is", "trades": 4, "tp1": 1, "dol": 1, "r": -1.88}]},
    "confirmed": {"label": "After a CONFIRMED CRT (swept and closed back inside): reached before the sweep extreme broke", "rows": [
        {"rule": "Daily CRT (29)", "trades": 29, "tp1": "76 %", "dol": "52 %", "r": None},
        {"rule": "4H CRT (63)", "trades": 63, "tp1": "75 %", "dol": "51 %", "r": None},
        {"rule": "Weekly CRT (14)", "trades": 14, "tp1": "64 %", "dol": "43 %", "r": None}]},
    "note": "Exact port of the script's CISD (Solana preset). Stop = the sweep extreme + 0.2 ATR, TP1 = 50 %, TP2 = the DOL, stop to entry after TP1. "
            "Closed candles only, no look-ahead. Small samples: not proven; the live record and these weekly maps keep measuring."}


def crt(c1, c2):
    """Candle Range Theory on two candles: c1 = the range, c2 = the next candle. Bullish CRT: c2 took c1's low and closed back
    above it (draw: c1's 50 %, then its high). Bearish is the mirror. Expansion: c2 closed beyond c1."""
    if not c1 or not c2:
        return None
    mid = (c1["h"] + c1["l"]) / 2
    took_lo, took_hi = c2["l"] < c1["l"], c2["h"] > c1["h"]
    if took_lo and not took_hi and c2["c"] > c1["l"]:
        return {"kind": "BULLISH CRT", "dir": 1, "swept": c1["l"], "mid": mid, "draw": c1["h"]}
    if took_hi and not took_lo and c2["c"] < c1["h"]:
        return {"kind": "BEARISH CRT", "dir": -1, "swept": c1["h"], "mid": mid, "draw": c1["l"]}
    if took_hi and c2["c"] > c1["h"]:
        return {"kind": "BULLISH EXPANSION", "dir": 1, "swept": c1["h"], "mid": mid, "draw": None}
    if took_lo and c2["c"] < c1["l"]:
        return {"kind": "BEARISH EXPANSION", "dir": -1, "swept": c1["l"], "mid": mid, "draw": None}
    if took_hi and took_lo:
        return {"kind": "OUTSIDE WEEK", "dir": 1 if c2["c"] > mid else -1, "swept": None, "mid": mid, "draw": None}
    return {"kind": "INSIDE WEEK", "dir": 0, "swept": None, "mid": mid, "draw": None}


def _ladder(m, rng, up, a, bw):
    """The POIs on one side, nearest first: the swept range extreme, then the deeper liquidity within 1.5 ranges."""
    lvl, span = (rng["l"], rng["h"] - rng["l"]) if up else (rng["h"], rng["h"] - rng["l"])
    far = lvl - 1.5 * span if up else lvl + 1.5 * span
    cand = []
    for q in (m["pools_below"] if up else m["pools_above"]):
        p = q["price"]
        if (far <= p < lvl - 0.3 * a) if up else (lvl + 0.3 * a < p <= far):
            cand.append({"kind": "liquidity", "price": p, "lo": p, "hi": p, "label": ("$$$ EQL" if q["equal"] else "4H low") if up else ("$$$ EQH" if q["equal"] else "4H high")})
    for g in m["fvgs"]:
        if up and g["side"] == "bull" and far <= g["hi"] < lvl - 0.3 * a:
            cand.append({"kind": "fvg", "price": g["hi"], "lo": g["lo"], "hi": g["hi"], "label": "4H FVG"})
        if not up and g["side"] == "bear" and lvl + 0.3 * a < g["lo"] <= far:
            cand.append({"kind": "fvg", "price": g["lo"], "lo": g["lo"], "hi": g["hi"], "label": "4H FVG"})
    for w in bw[:-1]:
        p = w["l"] if up else w["h"]
        later = [x for x in bw if x["t"] > w["t"]]
        untaken = all(x["l"] > p for x in later) if up else all(x["h"] < p for x in later)
        if untaken and ((far <= p < lvl - 0.3 * a) if up else (lvl + 0.3 * a < p <= far)):
            cand.append({"kind": "weekly", "price": p, "lo": p, "hi": p, "label": "older weekly low" if up else "older weekly high"})
    cand.sort(key=lambda c: -c["price"] if up else c["price"])
    out = []
    for c in cand:                                           # merge POIs closer than 0.3 ATR(4H)
        if out and abs(out[-1]["price"] - c["price"]) < 0.3 * a:
            out[-1]["label"] += " + " + c["label"]
            out[-1]["lo"], out[-1]["hi"] = min(out[-1]["lo"], c["lo"]), max(out[-1]["hi"], c["hi"])
            continue
        out.append(dict(c))
    first = {"kind": "sweep", "price": lvl, "lo": lvl, "hi": lvl, "label": "range low (1st reaction)" if up else "range high (1st reaction)"}
    return [first] + out[:3]


def week_range(m):
    """The range for the coming week: the week that is closing (Monday 00:00 UTC to now)."""
    return m.get("last_week")



# ── owner 25 Oct 2026, 22:00: all past liquidity, what was taken recently and how price reacted, and the CONFIRMED read ──
def _agg_month(bd):
    out = {}
    for b in bd:
        k = datetime.fromtimestamp(b["t"] / 1000, UTC).strftime("%Y-%m")
        x = out.get(k)
        out[k] = {"t": b["t"], "end_t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} if not x else {**x, "end_t": b["t"], "h": max(x["h"], b["h"]), "l": min(x["l"], b["l"]), "c": b["c"]}
    return [out[k] for k in sorted(out)]


def liquidity_book(data_dir, asset, now, a):
    """Every untaken high / low from the past that price can still be drawn to: weekly and monthly highs / lows, daily swing
    highs / lows (90 days), with their age; levels within 0.3 ATR(4H) merged. Also the levels taken in the last 7 days and how
    price reacted: a close back inside the same day (a sweep: a reaction) or a close beyond (accepted: a breakout)."""
    until = int(now * 1000)
    bd = _bars(data_dir, asset, "1d", 400, until)          # closed daily candles
    bw = _bars(data_dir, asset, "1w", 30, until)
    if len(bd) < 30:
        return None
    wk_start = until - (datetime.fromtimestamp(now, UTC).weekday() * 86400 + (int(now) % 86400)) * 1000
    cand = []
    for w in bw[-26:]:
        if w["t"] >= wk_start:
            continue
        lab = datetime.fromtimestamp(w["t"] / 1000, UTC).strftime("wk %d %b")
        cand += [{"price": w["h"], "side": "high", "src": "weekly", "label": lab + " high", "t": w["t"]},
                 {"price": w["l"], "side": "low", "src": "weekly", "label": lab + " low", "t": w["t"]}]
    for mo in _agg_month(bd)[-13:-1]:
        lab = datetime.fromtimestamp(mo["t"] / 1000, UTC).strftime("%b %Y")
        # The monthly extreme is only known after the month's final daily candle; start its liquidity line then.
        cand += [{"price": mo["h"], "side": "high", "src": "monthly", "label": lab + " high", "t": mo["end_t"]},
                 {"price": mo["l"], "side": "low", "src": "monthly", "label": lab + " low", "t": mo["end_t"]}]
    d90 = bd[-90:]
    for i in range(2, len(d90) - 2):
        b = d90[i]
        if b["h"] > max(d90[j]["h"] for j in (i - 2, i - 1, i + 1, i + 2)):
            cand.append({"price": b["h"], "side": "high", "src": "daily", "label": datetime.fromtimestamp(b["t"] / 1000, UTC).strftime("%d %b") + " high", "t": b["t"]})
        if b["l"] < min(d90[j]["l"] for j in (i - 2, i - 1, i + 1, i + 2)):
            cand.append({"price": b["l"], "side": "low", "src": "daily", "label": datetime.fromtimestamp(b["t"] / 1000, UTC).strftime("%d %b") + " low", "t": b["t"]})
    for b in bd[-8:]:                                   # each recent day's own high / low (PDH / PDL style)
        lab = datetime.fromtimestamp(b["t"] / 1000, UTC).strftime("%a %d %b")
        cand += [{"price": b["h"], "side": "high", "src": "day", "label": lab + " high", "t": b["t"]},
                 {"price": b["l"], "side": "low", "src": "day", "label": lab + " low", "t": b["t"]}]
    # this week so far from 4h (daily candles of today are still open)
    b4 = _bars(data_dir, asset, "4h", 60, until)
    later = lambda t: [b for b in bd if b["t"] > t] + [b for b in b4 if b["t"] > bd[-1]["t"]]
    price = (b4 or bd)[-1]["c"]
    open_, taken = [], []
    for c in cand:
        # Include the first candle after a weekly period closes (its timestamp equals start + 7 days).
        # Monthly timestamps are the last daily candle's open; all candles within that month are excluded.
        lt = later(c["t"] + (7 * 86_400_000 - 1 if c["src"] == "weekly" else 0))
        hit = next((b for b in lt if (b["h"] > c["price"] if c["side"] == "high" else b["l"] < c["price"])), None)
        if hit is None:
            open_.append(c)
        elif hit["t"] >= until - 7 * 86_400_000:
            back = (hit["c"] < c["price"]) if c["side"] == "high" else (hit["c"] > c["price"])
            taken.append({**c, "taken_t": hit["t"], "extreme": hit["h"] if c["side"] == "high" else hit["l"], "close": hit["c"],
                          "reaction": "swept and closed back (a reaction)" if back else "taken and accepted (a breakout)"})
    def merge(xs, desc):
        xs = sorted(xs, key=lambda c: -c["price"] if desc else c["price"])
        out = []
        for c in xs:
            if out and abs(out[-1]["price"] - c["price"]) < 0.3 * a:
                if c["src"] in ("monthly", "weekly") and out[-1]["src"] == "daily":
                    out[-1].update(src=c["src"])
                out[-1]["label"] += " · " + c["label"]
                continue
            out.append(dict(c))
        return out
    above = merge([c for c in open_ if c["price"] > price], False)[:8]
    below = merge([c for c in open_ if c["price"] < price], True)[:8]
    for x in above + below:
        x["dist_pct"] = round((x["price"] - price) / price * 100, 2)
    taken.sort(key=lambda c: (-c["taken_t"], c["price"]))
    tk = []
    for c in taken:                                     # one row per level: the labels of the same price are joined
        same = next((x for x in tk if abs(x["price"] - c["price"]) < 0.3 * a and x["taken_t"] == c["taken_t"]), None)
        if same:
            if c["label"] not in same["label"]:
                same["label"] += " · " + c["label"]
            continue
        tk.append(dict(c))
    return {"price": price, "above": above, "below": below, "taken": tk[:10]}


def confirmed_read(data_dir, asset, now):
    """The CRTs that are already CONFIRMED (closed): the last daily candle against the one before, and this week (Monday to
    now) against last week. A sweep that closed back inside points to the range's 50 %, then its other end (the DOL).
    Replay on SOL (closed candles, the order of events on the lower timeframe): after a confirmed daily CRT the 50 % came
    before the sweep extreme broke 76 % of the time (22 / 29) and the DOL 52 %; weekly 64 % and 43 %; 4H 75 % and 51 %."""
    until = int(now * 1000)
    bd = _bars(data_dir, asset, "1d", 5, until)
    out = []
    if len(bd) >= 2:
        c = crt(bd[-2], bd[-1])
        if c and c["kind"].endswith("CRT"):
            out.append({"tf": "daily", "kind": c["kind"], "dir": c["dir"], "range_hi": bd[-2]["h"], "range_lo": bd[-2]["l"], "swept": c["swept"],
                        "extreme": bd[-1]["l"] if c["dir"] > 0 else bd[-1]["h"], "close": bd[-1]["c"], "tp1": c["mid"], "dol": c["draw"],
                        "date": datetime.fromtimestamp(bd[-1]["t"] / 1000, UTC).strftime("%a %d %b"),
                        "odds": {"tp1": 76, "dol": 52, "n": 29}})
    return out


def scenarios(m, bw):
    rng = week_range(m)
    if not rng:
        return None
    a = m["atr4h"]
    mid = (rng["h"] + rng["l"]) / 2
    out = {"range": {"h": rng["h"], "l": rng["l"], "mid": mid, "t": rng["t"], "o": rng["o"], "c": rng["c"]},
           "crt": crt(m.get("prev_week"), rng), "sides": []}
    for up in (True, False):
        lad = _ladder(m, rng, up, a, bw)
        out["sides"].append({"dir": "long" if up else "short", "trigger": rng["l"] if up else rng["h"], "tp1": mid,
                             "dol": rng["h"] if up else rng["l"], "ladder": lad,
                             "rule": (f"If next week takes {_lvl(rng['l'])}, wait for a 4H CISD (after a purge) at one of the POIs below; "
                                      f"then expect {_lvl(mid)} (50 %) and the DOL {_lvl(rng['h'])}." if up else
                                      f"If next week takes {_lvl(rng['h'])}, wait for a 4H CISD (after a purge) at one of the POIs above; "
                                      f"then expect {_lvl(mid)} (50 %) and the DOL {_lvl(rng['l'])}."),
                             "cancel": (f"A deeper move through every POI with no CISD; or the DOL {_lvl(rng['h'])} trades first." if up else
                                        f"A move through every POI with no CISD; or the DOL {_lvl(rng['l'])} trades first.")})
    c = out["crt"] or {}
    fav = 0 if not c else c["dir"]
    if c.get("kind", "").endswith("CRT"):
        pw = m.get("prev_week")
        out["weekly_active"] = {"tf": "weekly", "kind": c["kind"], "dir": c["dir"], "range_hi": pw["h"], "range_lo": pw["l"], "swept": c["swept"],
                                "extreme": rng["l"] if c["dir"] > 0 else rng["h"], "close": rng["c"], "tp1": c["mid"], "dol": c["draw"],
                                "date": "this week", "odds": {"tp1": 64, "dol": 43, "n": 14}}
    if fav < 0:
        out["sides"].reverse()
    out["favoured"] = out["sides"][0]["dir"] if fav else None
    return out


def track(data_dir, asset, doc, now_ms=None):
    """Where each side stands after the analysis: swept? which POI reacted (the script's 4H CISD after the sweep)? then
    TP1 / DOL / stopped. Up to 3 reactions per side; a stopped reaction waits for the next POI. Closed 4H candles only."""
    from . import cisd as X
    sc = doc.get("scenarios")
    if not sc:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    made, end = doc["made_ms"], doc["made_ms"] + 7 * 24 * H
    rows = _bars(data_dir, asset, "4h", 400, min(now_ms, end + 4 * H))
    if not rows:
        return None
    rr = [[b["t"], b["o"], b["h"], b["l"], b["c"]] for b in rows]
    atr = X.wilder_atr(rr)
    conf = {}
    for c in X.scan(rr, "sol"):
        conf.setdefault(c["bar"], {})["bull" if c["bull"] else "bear"] = c
    idx = [i for i, r in enumerate(rr) if r[0] >= made and r[0] < end]
    out = []
    for sd in sc["sides"]:
        up = sd["dir"] == "long"
        sg = 1 if up else -1
        st = {"dir": sd["dir"], "status": "waiting for the sweep", "swept_t": None, "reactions": [], "dol_t": None, "tp1_t": None}
        sw = next((i for i in idx if ((rr[i][3] < sd["trigger"]) if up else (rr[i][2] > sd["trigger"]))), None)
        if sw is None:
            hit = next((i for i in idx if ((rr[i][2] >= sd["dol"]) if up else (rr[i][3] <= sd["dol"]))), None)
            if hit is not None:
                st["status"] = "the DOL side traded first: this side is off"
            out.append(st)
            continue
        st.update(swept_t=rr[sw][0], status="swept: waiting for the 4H CISD at a POI")
        ext, i, n = None, sw, 0
        while i < (idx[-1] + 1 if idx else sw) and n < 3:
            r = rr[i]
            ext = (min(ext, r[3]) if ext is not None else r[3]) if up else (max(ext, r[2]) if ext is not None else r[2])
            hit = conf.get(i, {}).get("bull" if up else "bear")
            if hit and i >= sw:
                e = r[4]
                stop = ext - sg * 0.2 * (atr[i] or 0)
                risk = sg * (e - stop)
                if risk > 0 and sg * (sd["dol"] - e) / risk >= 1.5:
                    poi = min(sd["ladder"], key=lambda p: abs(p["price"] - ext))
                    rec = {"t": r[0], "entry": e, "stop": stop, "ext": ext, "poi": poi["label"], "poi_price": poi["price"], "result": "open"}
                    n += 1
                    be = False
                    # OHLC bars do not reveal intrabar order; conservatively count the stop first if one candle hits both.
                    for k in range(i + 1, len(rr)):
                        q = rr[k]
                        if (up and q[3] <= (e if be else stop)) or (not up and q[2] >= (e if be else stop)):
                            rec["result"] = "stop to entry after TP1" if be else "stopped"
                            rec["end_t"] = q[0]
                            break
                        if not be and ((up and q[2] >= sd["tp1"]) or (not up and q[3] <= sd["tp1"])):
                            be = True
                            rec["tp1_t"] = q[0]
                            st["tp1_t"] = st["tp1_t"] or q[0]
                        if (up and q[2] >= sd["dol"]) or (not up and q[3] <= sd["dol"]):
                            rec["result"], rec["end_t"] = "DOL hit", q[0]
                            st["dol_t"] = q[0]
                            break
                    st["reactions"].append(rec)
                    if rec["result"] == "stopped":
                        i = next((k for k in range(i + 1, len(rr)) if rr[k][0] >= rec["end_t"]), len(rr))
                        ext = None
                        continue
                    break
            i += 1
        if not st["reactions"] and now_ms >= end:
            st["status"] = "swept, but no 4H CISD at a POI during the week"
        if st["reactions"]:
            last = st["reactions"][-1]
            st["status"] = (f"reacted at {last['poi']} ({_lvl(last['poi_price'])}): {sd['dir']} from {_lvl(last['entry'])}, " + last["result"])
        out.append(st)
    return out


SYSTEM = """You are Luna, the LunaFlow desk's ICT / CRT analyst. Every Sunday evening you write the weekly map for one market for a
trader who reads it on his phone. The map is the owner's CRT model on the weekly candle: the closing week is the range; next
week, a sweep of one side should react at one of the POIs (the swept level first, then deeper liquidity / FVGs) and, after the
script's 4H CISD, deliver to the 50 % and then the other side (the DOL). Measured fact you must respect: on SOL the first
reaction at the swept weekly level usually failed and the reaction at a deeper POI paid; say so plainly.
Rules: use ONLY the prices in the facts, exactly as given; never invent a level, an indicator or news. Plain English, short
sentences, no hype, no certainty words ("will"): use "should" / "if". Never give a fixed entry: both sides are conditional.
LEAD WITH WHAT ALREADY HAPPENED: if a sweep is already confirmed ("confirmed_now": a daily or weekly candle took the previous
candle's low / high and closed back inside), the headline and the main scenario are that read: its direction, its 50 % and
its DOL, the measured odds, and the invalidation (a break of the sweep extreme). Use "taken_last_7_days" to say which
liquidity was taken and how price reacted, and "liquidity_above" / "liquidity_below" for the next targets beyond the DOL.
The two if / then sides stay as the plan for a new sweep. Write prices, never annotation ids, in the text; only the
"ref" of each note is an annotation id."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["headline", "bias", "summary", "last_week", "scenario_main", "scenario_alt", "wait_for", "invalidation", "notes"],
          "properties": {
              "headline": {"type": "string"}, "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
              "summary": {"type": "string"}, "last_week": {"type": "string"},
              "scenario_main": {"type": "object", "additionalProperties": False, "required": ["title", "text"],
                                "properties": {"title": {"type": "string"}, "text": {"type": "string"}}},
              "scenario_alt": {"type": "object", "additionalProperties": False, "required": ["title", "text"],
                               "properties": {"title": {"type": "string"}, "text": {"type": "string"}}},
              "wait_for": {"type": "string"}, "invalidation": {"type": "string"},
              "notes": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["ref", "text"],
                                                   "properties": {"ref": {"type": "string"}, "text": {"type": "string"}}}}}}


def annotations(m, sc):
    """What the chart draws: the range (high / low / 50 %), each side's POI ladder, the DOLs, the SMC trap, open FVGs."""
    out = []
    tp = m.get("trap")
    if tp:
        out.append({"id": "trap", "kind": "trend", "role": "trap", "t0": tp["t0"], "p0": tp["p0"], "t1": tp["t1"], "p1": tp["p1"],
                    "label": "SMC TRAP" + (" (broken)" if tp["broken"] else "")})
    if not sc:
        return out
    for k, a_ in enumerate(sc.get("active") or []):
        up = a_["dir"] > 0
        out.append({"id": f"act{k}x", "kind": "line", "role": "sweep", "price": a_["extreme"], "t0": m["now_ms"] - (36 if a_["tf"] == "daily" else 168) * H,
                    "label": f"{'D' if a_['tf'] == 'daily' else 'W'} sweep {'low' if up else 'high'}"})
        out.append({"id": f"act{k}t", "kind": "line", "role": "tp", "price": a_["tp1"], "t0": m["now_ms"], "label": f"{'D' if a_['tf'] == 'daily' else 'W'} 50%"})
        if a_.get("dol") is not None:
            out.append({"id": f"act{k}d", "kind": "line", "role": "target", "price": a_["dol"], "t0": m["now_ms"], "label": f"{'D' if a_['tf'] == 'daily' else 'W'} DOL"})
    bk = sc.get("book") or {}
    for side, arr in (("liq_buy", bk.get("above") or []), ("liq_sell", bk.get("below") or [])):
        for k, q in enumerate(arr[:3]):
            if q["src"] in ("weekly", "monthly"):
                out.append({"id": f"bk{side[4]}{k}", "kind": "line", "role": "week", "price": q["price"], "t0": q["t"], "label": q["label"].split(" · ")[0]})
    r = sc["range"]
    out.append({"id": "wkh", "kind": "line", "role": "liq_buy", "price": r["h"], "t0": r["t"], "label": "Wk high"})
    out.append({"id": "wkl", "kind": "line", "role": "liq_sell", "price": r["l"], "t0": r["t"], "label": "Wk low"})
    out.append({"id": "wkeq", "kind": "line", "role": "eq", "price": r["mid"], "t0": r["t"], "label": "Wk 50%"})
    for sd in sc["sides"]:
        up = sd["dir"] == "long"
        for n, p in enumerate(sd["ladder"][1:], start=2):
            pid = ("pl" if up else "ps") + str(n)
            if p["kind"] == "fvg":
                out.append({"id": pid, "kind": "zone", "role": "fvg", "lo": p["lo"], "hi": p["hi"], "t0": m["b4"][max(0, len(m["b4"]) - 60)]["t"],
                            "label": f"POI {n} {'▲' if up else '▼'} FVG"})
            else:
                out.append({"id": pid, "kind": "line", "role": "idm", "price": p["price"], "t0": m["b4"][max(0, len(m["b4"]) - 60)]["t"],
                            "label": f"POI {n} {'▲' if up else '▼'}"})
    return out


def facts(m, sc, ann):
    f = lambda x: _r(x, m["price"])
    c = (sc or {}).get("crt") or {}
    return {"market": m["asset"], "price": f(m["price"]), "atr_4h": f(m["atr4h"]),
            "trend_4h": {k: m["trend"][k] for k in ("label", "structure")},
            "closing_week_range": sc and {"high": f(sc["range"]["h"]), "low": f(sc["range"]["l"]), "fifty_pct": f(sc["range"]["mid"]),
                                          "open": f(sc["range"]["o"]), "close_so_far": f(sc["range"]["c"])},
            "week_before": m.get("prev_week") and {"high": f(m["prev_week"]["h"]), "low": f(m["prev_week"]["l"]), "close": f(m["prev_week"]["c"])},
            "closing_week_vs_week_before": c and {"read": c.get("kind"), "swept": f(c.get("swept")) if c.get("swept") else None,
                                                  "draw": f(c.get("draw")) if c.get("draw") else None},
            "sides": [{"side": sd["dir"].upper(), "trigger_sweep_of": f(sd["trigger"]), "pois": [{"n": n + 1, "price": f(p["price"]), "what": p["label"]}
                       for n, p in enumerate(sd["ladder"])], "tp1_fifty_pct": f(sd["tp1"]), "dol": f(sd["dol"])} for sd in (sc or {}).get("sides", [])],
            "favoured_side": (sc or {}).get("favoured"),
            "confirmed_now": [{"tf": a_["tf"], "read": a_["kind"], "when": a_["date"], "swept": f(a_["swept"]), "sweep_extreme": f(a_["extreme"]),
                               "closed": f(a_["close"]), "tp1_fifty_pct": f(a_["tp1"]), "dol": f(a_["dol"]) if a_.get("dol") else None,
                               "measured_odds": f"50 % first {a_['odds']['tp1']} %, DOL first {a_['odds']['dol']} % (n={a_['odds']['n']})"}
                              for a_ in (sc or {}).get("active") or []],
            "liquidity_above": [{"price": f(q["price"]), "what": q["label"], "dist_pct": q["dist_pct"]} for q in ((sc or {}).get("book") or {}).get("above", [])],
            "liquidity_below": [{"price": f(q["price"]), "what": q["label"], "dist_pct": q["dist_pct"]} for q in ((sc or {}).get("book") or {}).get("below", [])],
            "taken_last_7_days": [{"level": f(q["price"]), "what": q["label"], "when": datetime.fromtimestamp(q["taken_t"] / 1000, UTC).strftime("%a %d %b"),
                                   "extreme": f(q["extreme"]), "reaction": q["reaction"]} for q in ((sc or {}).get("book") or {}).get("taken", [])],
            "measured": {"weekly_first_reaction": "23 trades, -6.1R", "weekly_deeper_poi_reaction": "6 trades, TP1 5, DOL 4, +16.7R",
                         "intraday_4h_15m_script": "14 trades, TP1 10, DOL 5, +5.1R"},
            "smc_trap": m.get("trap") and {"through": "swing highs" if m["trap"]["kind"] == "H" else "swing lows", "broken": m["trap"]["broken"]},
            "annotations": [{"id": x["id"], "label": x["label"]} for x in ann]}


def _claude(fx):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not llm.claude_available():
        return None
    body = {"model": MODEL, "max_tokens": 3000, "system": SYSTEM,
            "messages": [{"role": "user", "content": "This week's facts (JSON):\n" + json.dumps(fx, default=str)}],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            d = json.loads(r.read().decode())
        return next(b["text"] for b in d["content"] if b.get("type") == "text"), MODEL
    except urllib.error.HTTPError as e:
        if llm.claude_failed(e, e.read().decode(errors="replace")):
            return None
        raise
    except Exception as e:
        if llm.claude_failed(e):
            return None
        raise


def story_rules(m, sc):
    if not sc:
        return {"headline": f"{m['asset']}: no weekly range yet", "bias": "NEUTRAL", "summary": "", "last_week": "",
                "scenario_main": {"title": "Wait", "text": ""}, "scenario_alt": {"title": "", "text": ""}, "wait_for": "", "invalidation": "", "notes": []}
    r, c = sc["range"], sc.get("crt") or {}
    a, b = sc["sides"][0], sc["sides"][1]
    pois = lambda sd: ", ".join(_lvl(p["price"]) for p in sd["ladder"])
    bias = {1: "BULLISH", -1: "BEARISH"}.get(c.get("dir"), "NEUTRAL")
    act = sc.get("active") or []
    if act:
        a0 = act[0]
        up = a0["dir"] > 0
        bias = "BULLISH" if up else "BEARISH"
        main = {"title": f"{'LONG' if up else 'SHORT'}: {a0['tf']} sweep confirmed ({a0['date']})",
                "text": (f"{a0['date']} took {_lvl(a0['swept'])} to {_lvl(a0['extreme'])} and closed back at {_lvl(a0['close'])}: a {a0['kind'].lower()}. "
                         f"Expect {_lvl(a0['tp1'])} (50 %), then {_lvl(a0['dol'])} (the DOL). Measured: the 50 % came first {a0['odds']['tp1']} % of the time, "
                         f"the DOL {a0['odds']['dol']} %. Off on a break of {_lvl(a0['extreme'])}.")}
        return {"headline": f"{m['asset']}: {a0['tf']} sweep of {_lvl(a0['swept'])} confirmed, draw to {_lvl(a0['tp1'])} then {_lvl(a0['dol'])}",
                "bias": bias, "summary": " ".join(f"{x['tf'].capitalize()}: {x['kind'].lower()} (took {_lvl(x['swept'])}, closed back {_lvl(x['close'])})." for x in act),
                "last_week": f"Opened {_lvl(r['o'])}, high {_lvl(r['h'])}, low {_lvl(r['l'])}, now {_lvl(r['c'])}.",
                "scenario_main": main,
                "scenario_alt": {"title": f"If {_lvl(a0['extreme'])} breaks", "text": (f"The confirmed sweep failed and the {'sell' if up else 'buy'}-side liquidity "
                                 + ("below" if up else "above") + " is next: " + ", ".join(f"{_lvl(q['price'])} ({q['label'].split(' · ')[0]})"
                                 for q in ((sc.get('book') or {}).get('below' if up else 'above') or [])[:3]) + ". Then the two-sided map applies again.")},
                "wait_for": "A pullback into a POI on the lower timeframe for the entry; the targets are the 50 % and the DOL.",
                "invalidation": f"A break of {_lvl(a0['extreme'])}.", "notes": []}
    return {"headline": f"{m['asset']} week map: range {_lvl(r['l'])}–{_lvl(r['h'])}, 50% {_lvl(r['mid'])}",
            "bias": bias,
            "summary": (f"The closing week ranged {_lvl(r['l'])}–{_lvl(r['h'])}. Against the week before it reads as {c.get('kind', 'no CRT').lower()}. "
                        "Next week: a sweep of one side, a reaction at a POI with the script's 4H CISD, then the 50 % and the other side."),
            "last_week": f"Opened {_lvl(r['o'])}, high {_lvl(r['h'])}, low {_lvl(r['l'])}, now {_lvl(r['c'])}.",
            "scenario_main": {"title": f"{a['dir'].upper()} if {_lvl(a['trigger'])} is taken", "text": a["rule"] + f" POIs: {pois(a)}."},
            "scenario_alt": {"title": f"{b['dir'].upper()} if {_lvl(b['trigger'])} is taken", "text": b["rule"] + f" POIs: {pois(b)}."},
            "wait_for": "The sweep, then the CISD. On SOL the first reaction at the swept level usually failed; the deeper POI paid.",
            "invalidation": "A side is off when price runs through all its POIs without a CISD, or when its DOL trades first.", "notes": []}


def make(cfg, asset, now=None, use_model=True):
    now = now or time.time()
    m = measure(cfg["data_dir"], asset, now)
    bw = _bars(cfg["data_dir"], asset, "1w", 12, int(now * 1000))
    sc = scenarios(m, bw)
    book = liquidity_book(cfg["data_dir"], asset, now, m["atr4h"])
    active = confirmed_read(cfg["data_dir"], asset, now) + ([sc["weekly_active"]] if sc and sc.get("weekly_active") else [])
    if sc is not None:
        sc["active"] = active
        sc["book"] = book
        if active:                                   # the confirmed sweep sets the favoured side
            d0 = active[0]["dir"]
            sc["favoured"] = "long" if d0 > 0 else "short"
            if sc["sides"][0]["dir"] != sc["favoured"]:
                sc["sides"].reverse()
    ann = annotations(m, sc)
    fx = facts(m, sc, ann)
    story, model, err = None, "rules", None
    if use_model:
        try:
            got = _claude(fx) or llm.fallback(SYSTEM, ["This week's facts (JSON):\n" + json.dumps(fx, default=str)], schema=SCHEMA,
                                              max_tokens=3000, budget=170, task="chat")
            story, model = json.loads(got[0]), got[1]
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:160]}"
            LOG.warning("%s weekly: no model answer (%s), rules only", asset, err)
    if not isinstance(story, dict) or not story.get("headline"):
        story = story_rules(m, sc)
    ids = {x["id"] for x in ann}
    story["notes"] = [n for n in story.get("notes") or [] if isinstance(n, dict) and n.get("ref") in ids][:12]
    week = week_of(now)
    doc = {"asset": asset, "week": week, "made_at": datetime.fromtimestamp(now, UTC).isoformat(), "made_ms": int(now * 1000),
           "kind": "poi_map", "price": m["price"], "atr4h": m["atr4h"], "trend": m["trend"], "range": m["range"], "scenarios": sc,
           "annotations": ann, "story": story, "facts": fx, "model": model, "error": err, "findings": FINDINGS}
    d = Path(cfg["data_dir"]) / asset / DIR
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (week + ".json.tmp")
    tmp.write_text(json.dumps(doc, default=str))
    tmp.replace(d / (week + ".json"))
    _store(cfg["data_dir"], doc)
    return doc


def due(cfg, asset, now=None):
    now = now or time.time()
    d = datetime.fromtimestamp(now, UTC)
    if d.weekday() != 6 or d.hour < DUE_HOUR_UTC:
        return False
    p = Path(cfg["data_dir"]) / asset / DIR / (week_of(now) + ".json")
    if not p.exists():
        return True
    try:                                         # owner 25 Oct 2026: a trade-idea doc of this week is replaced by the POI map
        return json.loads(p.read_text()).get("kind") != "poi_map"
    except Exception:
        return True


def load(data_dir, asset, week=None):
    d = Path(data_dir) / asset / DIR
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if week:
        files = [f for f in files if f.stem == week]
    if not files:
        return None
    return json.loads(files[-1].read_text())


def weeks(data_dir, asset):
    d = Path(data_dir) / asset / DIR
    return sorted((f.stem for f in d.glob("*.json")), reverse=True) if d.exists() else []


# ── review (gameplan.db table weekly): each map's sides, tracked through the week ──────────────────────────────────
WEEKLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly (asset TEXT NOT NULL, week TEXT NOT NULL, made_ms INTEGER NOT NULL, bias TEXT, side TEXT,
  entry REAL, entry_type TEXT, stop REAL, tp1 REAL, target REAL, rr REAL, headline TEXT, model TEXT,
  filled_ms INTEGER, outcome TEXT, tp1_ms INTEGER, target_ms INTEGER, stop_ms INTEGER, mfe_r REAL, mae_r REAL,
  review_note TEXT, reviewed_ms INTEGER, json TEXT, PRIMARY KEY (asset, week));
"""


def _db(data_dir):
    db = sqlite3.connect(str(Path(data_dir) / "gameplan.db"), timeout=15)
    db.executescript(WEEKLY_SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(weekly)")}
    if "track_json" not in cols:
        db.execute("ALTER TABLE weekly ADD COLUMN track_json TEXT")
    return db


def _store(data_dir, doc):
    s, sc = doc["story"], doc.get("scenarios") or {}
    r = sc.get("range") or {}
    db = _db(data_dir)
    try:
        db.execute("INSERT OR REPLACE INTO weekly (asset, week, made_ms, bias, side, entry, entry_type, stop, tp1, target, rr, headline, model, json) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (doc["asset"], doc["week"], doc["made_ms"], s.get("bias"), "map", None, "poi_map", None, r.get("mid"), None, None,
                    s.get("headline"), doc["model"], json.dumps(doc, default=str)))
        db.commit()
    finally:
        db.close()


def review(data_dir, asset, now=None):
    """Track every POI map that is not finished (7 days after it was made) and store each side's status."""
    now_ms = int((now or time.time()) * 1000)
    if not (Path(data_dir) / "gameplan.db").exists():
        return 0
    db = _db(data_dir)
    n = 0
    try:
        rows = db.execute("SELECT week, json FROM weekly WHERE asset=? AND entry_type='poi_map' AND (reviewed_ms IS NULL OR reviewed_ms < made_ms + ?)",
                          (asset, 8 * 24 * H)).fetchall()
        for week, js in rows:
            doc = json.loads(js)
            tr = track(data_dir, asset, doc, now_ms)
            if tr is None:
                continue
            summ = " · ".join(f"{t['dir']}: {t['status']}" for t in tr)
            db.execute("UPDATE weekly SET outcome=?, track_json=?, reviewed_ms=? WHERE asset=? AND week=?",
                       (summ[:500], json.dumps(tr, default=str), now_ms, asset, week))
            n += 1
        db.commit()
    finally:
        db.close()
    return n


def outcomes(data_dir, asset, limit=12):
    if not (Path(data_dir) / "gameplan.db").exists():
        return []
    db = _db(data_dir)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute("SELECT week, bias, side, entry_type, tp1, outcome, track_json, review_note FROM weekly "
                                            "WHERE asset=? ORDER BY week DESC LIMIT ?", (asset, limit))]
    finally:
        db.close()


if __name__ == "__main__":          # python -m app.weekly make SOL  (uses LEVELS_CONFIG)
    import sys
    from .config import load_config
    logging.basicConfig(level=logging.WARNING)
    if len(sys.argv) >= 3 and sys.argv[1] == "make":
        d = make(load_config(), sys.argv[2].upper())
        print(d["week"], d["story"].get("headline"), d["model"])
