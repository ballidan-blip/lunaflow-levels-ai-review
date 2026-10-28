"""Weekly SMC analysis (owner 25 Oct 2026): measured levels, the trade idea's geometry, Sunday schedule, the week's review,
the API and the page."""
import json
import math
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import weekly as W

H = 3_600_000
SUN = 1_792_946_400          # Sun 25 Oct 2026 16:40 UTC


def _feed(tmp_path, end_s=SUN, drift=-0.04):
    """A falling market with waves: 4h, 1h, 1d, 1w candles built from one 1h path."""
    db = sqlite3.connect(tmp_path / "market.db")
    db.execute("CREATE TABLE candles (asset TEXT, tf TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL)")
    end = end_s * 1000 // H * H
    n = 24 * 70
    px, rows1 = 130.0, []
    for k in range(n):
        t = end - (n - k) * H
        o = px
        px = px + drift + 1.1 * math.sin(k / 9.0) * 0.35 + 0.4 * math.sin(k / 31.0)
        hi, lo = max(o, px) + 0.25, min(o, px) - 0.25
        rows1.append((t, o, hi, lo, px))
    def agg(ms):
        out, cur = [], None
        for t, o, h, l, c in rows1:
            b = t - t % ms
            if cur and cur[0] == b:
                cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
            else:
                if cur:
                    out.append(tuple(cur))
                cur = [b, o, h, l, c]
        out.append(tuple(cur))
        return out
    for tf, ms in (("1h", H), ("4h", 4 * H), ("1d", 24 * H)):
        db.executemany("INSERT INTO candles VALUES ('SOL',?,?,?,?,?,?)", [(tf, *r) for r in (rows1 if tf == "1h" else agg(ms))])
    # weekly candles open Monday 00:00 UTC (epoch was a Thursday: shift by 4 days)
    wk, cur = [], None
    for t, o, h, l, c in rows1:
        b = t - (t - 4 * 24 * H) % (7 * 24 * H)
        if cur and cur[0] == b:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
        else:
            if cur:
                wk.append(tuple(cur))
            cur = [b, o, h, l, c]
    wk.append(tuple(cur))
    db.executemany("INSERT INTO candles VALUES ('SOL','1w',?,?,?,?,?)", wk)
    db.commit()
    db.close()


def test_make_builds_the_poi_map(tmp_path):
    _feed(tmp_path)
    d = W.make({"data_dir": str(tmp_path)}, "SOL", now=SUN, use_model=False)
    assert d["week"] == "2026-10-25" and d["kind"] == "poi_map" and (tmp_path / "SOL" / "weekly" / "2026-10-25.json").exists()
    sc = d["scenarios"]
    r = sc["range"]
    assert r["l"] < r["mid"] < r["h"]
    sides = {s_["dir"]: s_ for s_ in sc["sides"]}
    assert sides["long"]["trigger"] == r["l"] and sides["long"]["dol"] == r["h"] and sides["long"]["tp1"] == r["mid"]
    assert sides["short"]["trigger"] == r["h"] and sides["short"]["dol"] == r["l"]
    lad = sides["long"]["ladder"]
    assert lad[0]["kind"] == "sweep" and all(lad[i]["price"] > lad[i + 1]["price"] for i in range(len(lad) - 1))
    lad = sides["short"]["ladder"]
    assert all(lad[i]["price"] < lad[i + 1]["price"] for i in range(len(lad) - 1))
    assert {"wkh", "wkl", "wkeq"} <= {a["id"] for a in d["annotations"]}
    assert "active" in sc and sc["book"] and {"above", "below", "taken"} <= set(sc["book"])
    assert d["story"]["headline"] and "findings" in d and W.weeks(str(tmp_path), "SOL") == ["2026-10-25"]


def test_crt_reads():
    c1 = {"h": 110, "l": 100, "c": 105}
    assert W.crt(c1, {"h": 108, "l": 98, "c": 103})["kind"] == "BULLISH CRT"
    assert W.crt(c1, {"h": 112, "l": 101, "c": 104})["kind"] == "BEARISH CRT"
    assert W.crt(c1, {"h": 115, "l": 101, "c": 114})["kind"] == "BULLISH EXPANSION"
    assert W.crt(c1, {"h": 109, "l": 101, "c": 104})["kind"] == "INSIDE WEEK"


def test_due_only_sunday_evening_once(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    assert W.due(cfg, "SOL", now=SUN - 41 * 60) is False          # Sunday 15:59 UTC: one minute early
    assert W.due(cfg, "SOL", now=SUN - 40 * 60) is True            # Sunday 16:00 UTC: exact due boundary
    assert W.due(cfg, "SOL", now=SUN) is True                       # Sunday 16:40 UTC = 20:40 Dubai
    assert W.due(cfg, "SOL", now=SUN - 2 * 3600) is False           # Sunday 14:40 UTC: before 20:00 Dubai
    assert W.due(cfg, "SOL", now=SUN - 86400) is False              # Saturday
    _feed(tmp_path)
    W.make(cfg, "SOL", now=SUN, use_model=False)
    assert W.due(cfg, "SOL", now=SUN + 3600) is False               # made already this week


def test_track_and_review(tmp_path):
    _feed(tmp_path)
    d = W.make({"data_dir": str(tmp_path)}, "SOL", now=SUN, use_model=False)
    lo = d["scenarios"]["range"]["l"]
    m = sqlite3.connect(tmp_path / "market.db")
    t0 = (d["made_ms"] // (4 * H) + 1) * 4 * H                     # next 4H candles: a sweep of the range low, nothing else
    m.executemany("INSERT INTO candles VALUES ('SOL','4h',?,?,?,?,?)", [(t0, lo + 1, lo + 1.2, lo - 0.5, lo + 0.2)])
    m.commit(); m.close()
    tr = W.track(str(tmp_path), "SOL", d, d["made_ms"] + 9 * H)
    long_ = next(t for t in tr if t["dir"] == "long")
    assert long_["swept_t"] == t0 and long_["status"].startswith("swept")
    assert W.review(str(tmp_path), "SOL", now=(d["made_ms"] + 9 * H) / 1000) == 1
    o = W.outcomes(str(tmp_path), "SOL")[0]
    assert o["entry_type"] == "poi_map" and "long: swept" in o["outcome"]


def test_api_and_page(tmp_path):
    _feed(tmp_path)
    W.make({"data_dir": str(tmp_path)}, "SOL", now=SUN, use_model=False)
    from app.web import create_app
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
    cfg["data_dir"] = str(tmp_path)
    c = create_app(cfg).test_client()
    j = c.get("/levels/api/v1/SOL/weekly").get_json()
    assert j["week"] == "2026-10-25" and len(j["candles"]["4h"]) > 50 and len(j["candles"]["1h"]) > 50 and j["weeks"] == ["2026-10-25"]
    assert c.get("/levels/api/v1/SOL/weekly?week=2020-01-05").status_code == 404
    page = c.get("/levels/weekly")
    assert page.status_code == 200 and b"Weekly Analysis" in page.data

def test_trend_handles_two_highs_and_no_lows():
    piv = [
        {"kind": "H", "price": 10},
        {"kind": "H", "price": 12},
    ]
    got = W.trend(piv, [])
    assert got["structure"] == "mixed swings"
    assert got["label"] == "RANGING"


def test_trend_handles_two_highs_and_one_low():
    piv = [
        {"kind": "H", "price": 12},
        {"kind": "H", "price": 10},
        {"kind": "L", "price": 5},
    ]
    assert W.trend(piv, [])["structure"] == "mixed swings"


def test_trend_labels_higher_highs_and_higher_lows():
    piv = [
        {"kind": "H", "price": 10},
        {"kind": "L", "price": 4},
        {"kind": "H", "price": 12},
        {"kind": "L", "price": 6},
    ]
    assert W.trend(piv, [])["structure"] == "higher highs and higher lows"


def test_trend_labels_lower_highs_and_lower_lows():
    piv = [
        {"kind": "H", "price": 12},
        {"kind": "L", "price": 6},
        {"kind": "H", "price": 10},
        {"kind": "L", "price": 4},
    ]
    assert W.trend(piv, [])["structure"] == "lower highs and lower lows"



def test_month_aggregation_records_last_daily_candle():
    t0 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    t1 = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
    months = W._agg_month([
        {"t": t0, "o": 100, "h": 110, "l": 95, "c": 105},
        {"t": t1, "o": 105, "h": 108, "l": 90, "c": 99},
    ])
    assert len(months) == 1
    assert months[0]["t"] == t0
    assert months[0]["end_t"] == t1
    assert months[0]["h"] == 110 and months[0]["l"] == 90 and months[0]["c"] == 99


def test_bars_returns_only_closed_candles_in_time_order(tmp_path):
    db = sqlite3.connect(tmp_path / "market.db")
    db.execute("CREATE TABLE candles (asset TEXT, tf TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL)")
    db.executemany("INSERT INTO candles VALUES ('SOL','1h',?,?,?,?,?)", [
        (2 * H, 3, 4, 2, 3), (H, 2, 3, 1, 2), (0, 1, 2, 0, 1),
    ])
    db.commit()
    db.close()
    got = W._bars(str(tmp_path), "SOL", "1h", 10, 2 * H)
    assert [b["t"] for b in got] == [0, H]
    assert [b["c"] for b in got] == [1, 2]


def test_measure_fails_clearly_when_history_is_missing(tmp_path):
    db = sqlite3.connect(tmp_path / "market.db")
    db.execute("CREATE TABLE candles (asset TEXT, tf TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL)")
    db.commit()
    db.close()
    import pytest
    with pytest.raises(RuntimeError, match="not enough 4h candles"):
        W.measure(str(tmp_path), "SOL", now=SUN)


def test_liquidity_merges_nearby_equal_highs():
    bars = [
        {"t": i * H, "o": 100, "h": h, "l": 95, "c": 100}
        for i, h in enumerate([110, 109, 109, 108])
    ]
    piv = [
        {"i": 0, "t": 0, "kind": "H", "price": 110},
        {"i": 1, "t": H, "kind": "H", "price": 109},
    ]
    got = W.liquidity(bars, piv, a=10, price=100)
    assert len(got) == 1
    assert got[0]["side"] == "buy" and got[0]["equal"] is True
    assert got[0]["price"] == 110


def test_track_uses_conservative_stop_first_when_one_candle_hits_both(monkeypatch):
    from app import cisd as X
    rows = []
    for i in range(16):
        o, h, l, c = 100, 101, 99, 100
        if i == 14:
            h, l, c = 101, 98, 100       # sweep and CISD bar
        elif i == 15:
            h, l, c = 111, 97, 100       # touches both stop and DOL after entry
        rows.append({"t": i * 4 * H, "o": o, "h": h, "l": l, "c": c})
    monkeypatch.setattr(W, "_bars", lambda *args, **kwargs: rows)
    monkeypatch.setattr(X, "scan", lambda *args, **kwargs: [{"bar": 14, "bull": True}])
    doc = {
        "made_ms": 14 * 4 * H,
        "scenarios": {"sides": [{
            "dir": "long", "trigger": 99, "dol": 110, "tp1": 105,
            "ladder": [{"price": 98, "label": "sweep"}],
        }]},
    }
    got = W.track("unused", "SOL", doc, now_ms=15 * 4 * H + 1)
    assert got[0]["reactions"][0]["result"] == "stopped"
    assert got[0]["reactions"][0].get("tp1_t") is None



def test_weekly_liquidity_counts_first_candle_after_week_close(tmp_path):
    start = datetime(2025, 9, 21, tzinfo=timezone.utc)
    daily, weeks = [], {}
    day = start
    while day <= datetime(2026, 10, 24, tzinfo=timezone.utc):
        t = int(day.timestamp() * 1000)
        high = 110 if day.date() == datetime(2026, 10, 12).date() else 111 if day.date() == datetime(2026, 10, 19).date() else 105
        row = (t, 100, high, 95, 100)
        daily.append(row)
        monday = day - timedelta(days=day.weekday())
        wt = int(monday.timestamp() * 1000)
        if wt not in weeks:
            weeks[wt] = [wt, 100, high, 95, 100]
        else:
            weeks[wt][2] = max(weeks[wt][2], high)
            weeks[wt][3] = min(weeks[wt][3], 95)
            weeks[wt][4] = 100
        day += timedelta(days=1)

    db = sqlite3.connect(tmp_path / "market.db")
    db.execute("CREATE TABLE candles (asset TEXT, tf TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL)")
    db.executemany("INSERT INTO candles VALUES ('SOL','1d',?,?,?,?,?)", daily)
    db.executemany("INSERT INTO candles VALUES ('SOL','1w',?,?,?,?,?)", list(weeks.values()))
    db.commit()
    db.close()

    book = W.liquidity_book(str(tmp_path), "SOL", now=SUN, a=1)
    assert book is not None
    # The weekly candle closes Monday 19 Oct 00:00 UTC. Its first following daily candle
    # takes the prior week's high; the strict cutoff must not skip that exact timestamp.
    assert any("wk 12 Oct high" in item["label"] for item in book["taken"])
