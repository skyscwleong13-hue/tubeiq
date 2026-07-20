#!/usr/bin/env python
"""
tubeiq - local vidIQ replacement. No API keys, no subscription.

  suggest   <seed>          autocomplete fan-out (raw keyword ideas)
  keywords  <seed>          scored keyword research (demand/competition/overall)
  search    <query>         YouTube search with views, age, views/day
  outliers  <query>         find videos overperforming their channel median
  channel   <@handle|UC..>  channel audit: cadence, medians, top performers
  video     <id|url>        single video breakdown incl. tags
  title     "<title>" ...   score titles 0-100 with fix suggestions
  track     add|rm|list|snap|report    competitor tracking (SQLite)

Add --json to any command for machine-readable output.
"""

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yt

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "tubeiq.db")

# Windows consoles default to cp1252; video titles contain emoji.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- output

def table(rows, headers):
    if not rows:
        print("(no results)")
        return
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def emit(args, data, human_fn):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        human_fn(data)


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def log_scale(views, ceiling=7.0):
    """views -> 0..100 on a log10 scale (10M+ = 100)."""
    if not views or views <= 0:
        return 0.0
    return clamp(math.log10(views) / ceiling * 100)


# ---------------------------------------------------------------- suggest

QUESTION_PREFIXES = ["how", "why", "what", "is", "can", "does", "best",
                     "when", "should"]


def fanout(seed, deep=False):
    """Autocomplete fan-out. Returns {keyword: weight} — weight approximates
    demand (base-list position + how often it recurs across expansions)."""
    weights = {}

    def add(sugs, base_w):
        for i, s in enumerate(sugs):
            s = s.lower().strip()
            if not s or s == seed.lower():
                continue
            w = base_w * (1.0 - i / (len(sugs) + 1))
            weights[s] = weights.get(s, 0) + w

    add(yt.autocomplete(seed), 3.0)
    letters = "abcdefghijklmnopqrstuvwxyz" if deep else "abcdefghijkmpstw"
    for ch in letters:
        add(yt.autocomplete(f"{seed} {ch}"), 1.0)
    for q in QUESTION_PREFIXES:
        add(yt.autocomplete(f"{q} {seed}"), 1.5)
    return dict(sorted(weights.items(), key=lambda kv: -kv[1]))


def cmd_suggest(args):
    w = fanout(args.seed, deep=args.deep)
    data = [{"keyword": k, "weight": round(v, 2)} for k, v in w.items()]
    emit(args, data, lambda d: table(
        [[x["keyword"], x["weight"]] for x in d[:args.n]],
        ["keyword", "demand-weight"]))


# ---------------------------------------------------------------- keywords

def analyze_keyword(kw, auto_weight, max_auto):
    vids = yt.search(kw, limit=15)
    top = [v for v in vids if v["views"] is not None][:10]
    if not top:
        return None
    views = [v["views"] for v in top]
    med = statistics.median(views)
    top3 = statistics.mean(sorted(views, reverse=True)[:3])
    ages = [v["age_days"] for v in top if v["age_days"] is not None]
    med_age = statistics.median(ages) if ages else None
    weak = sum(1 for v in views if v < 10_000)

    competition = log_scale(med)
    auto_norm = (auto_weight / max_auto * 100) if max_auto else 0
    demand = clamp(0.45 * auto_norm + 0.55 * log_scale(top3))
    bonus = 0
    if med_age and med_age > 365:
        bonus += 10          # top results are stale -> gap
    if weak >= 2:
        bonus += 10          # small videos ranking -> beatable
    overall = clamp(0.5 * demand + 0.35 * (100 - competition) + bonus)
    return {
        "keyword": kw,
        "overall": round(overall),
        "demand": round(demand),
        "competition": round(competition),
        "median_views": int(med),
        "top3_avg_views": int(top3),
        "median_age_days": round(med_age) if med_age else None,
        "weak_spots": weak,
    }


def cmd_keywords(args):
    print(f"fanning out autocomplete for '{args.seed}' ...", file=sys.stderr)
    w = fanout(args.seed)
    max_auto = max(w.values()) if w else 1
    candidates = [args.seed] + list(w.keys())[:args.top]
    results = []
    for kw in candidates:
        print(f"  analyzing: {kw}", file=sys.stderr)
        try:
            r = analyze_keyword(kw, w.get(kw, max_auto), max_auto)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  ! {kw}: {e}", file=sys.stderr)
    results.sort(key=lambda r: -r["overall"])

    def human(res):
        table([[r["keyword"], r["overall"], r["demand"], r["competition"],
                yt.fmt_count(r["median_views"]),
                yt.fmt_count(r["top3_avg_views"]),
                yt.fmt_age(r["median_age_days"]), r["weak_spots"]]
               for r in res],
              ["keyword", "score", "demand", "compet", "med-views",
               "top3-avg", "top-age", "weak"])
        print("\nscore = demand + low competition + bonuses "
              "(stale top results, weak videos ranking).")
        print("weak = videos under 10K views in the top 10 (openings).")
    emit(args, results, human)


# ---------------------------------------------------------------- search

def _vpd(v):
    if v["views"] is not None and v["age_days"]:
        return v["views"] / max(v["age_days"], 0.04)
    return None


def cmd_search(args):
    vids = yt.search(args.query, sort=args.sort, period=args.period,
                     limit=args.n)
    for v in vids:
        v["views_per_day"] = round(_vpd(v)) if _vpd(v) else None

    def human(vs):
        table([[v["videoId"], (v["title"] or "")[:58],
                (v["channel"] or "")[:22], yt.fmt_count(v["views"]),
                yt.fmt_age(v["age_days"]),
                yt.fmt_count(v["views_per_day"])] for v in vs],
              ["id", "title", "channel", "views", "age", "views/day"])
    emit(args, vids, human)


# ---------------------------------------------------------------- outliers

def channel_median(channel_id, exclude_id=None, cache={}):
    if channel_id in cache:
        return cache[channel_id]
    vids = yt.channel_videos(channel_id, limit=30)
    views = [v["views"] for v in vids
             if v["views"] is not None and v["videoId"] != exclude_id
             and (v["duration_s"] is None or v["duration_s"] > 62)]
    med = statistics.median(views) if len(views) >= 4 else None
    cache[channel_id] = (med, len(views))
    return med, len(views)


def cmd_outliers(args):
    print(f"searching '{args.query}' ({args.period}, by views) ...",
          file=sys.stderr)
    vids = yt.search(args.query, sort="views", period=args.period, limit=30)
    vids = [v for v in vids
            if v["views"] and v["views"] >= args.min_views and v["channelId"]
            and (v["duration_s"] is None or v["duration_s"] > 62)]
    out = []
    for v in vids[:args.n]:
        print(f"  baseline for {v['channel']} ...", file=sys.stderr)
        try:
            med, nvids = channel_median(v["channelId"], v["videoId"])
        except Exception as e:
            print(f"  ! {v['channel']}: {e}", file=sys.stderr)
            continue
        if not med:
            continue
        v = dict(v)
        v["channel_median"] = int(med)
        v["multiplier"] = round(v["views"] / med, 1)
        out.append(v)
    out.sort(key=lambda v: -v["multiplier"])

    def human(vs):
        table([[f'{v["multiplier"]}x', (v["title"] or "")[:56],
                (v["channel"] or "")[:20], yt.fmt_count(v["views"]),
                yt.fmt_count(v["channel_median"]), yt.fmt_age(v["age_days"]),
                v["videoId"]] for v in vs],
              ["mult", "title", "channel", "views", "ch-median", "age", "id"])
        print("\nmult = video views / that channel's median recent video."
              "\n3x+ means the TOPIC/PACKAGING outperformed, not the channel.")
    emit(args, out, human)


# ---------------------------------------------------------------- channel

def cmd_channel(args):
    cid = yt.resolve_channel(args.channel)
    meta = yt.channel_meta(cid)
    vids = yt.channel_videos(cid, limit=args.n)
    longs = [v for v in vids
             if v["duration_s"] is None or v["duration_s"] > 62]
    views = [v["views"] for v in longs if v["views"] is not None]
    med = statistics.median(views) if views else None
    ages = sorted(v["age_days"] for v in longs if v["age_days"] is not None)
    cadence = None
    if len(ages) >= 2 and ages[-1] > 0:
        cadence = round(len(ages) / (ages[-1] / 7), 2)   # uploads/week
    for v in vids:
        v["multiplier"] = (round(v["views"] / med, 1)
                           if med and v["views"] is not None else None)
    data = {"meta": meta, "median_views": int(med) if med else None,
            "uploads_per_week": cadence, "videos": vids}

    def human(d):
        m = d["meta"]
        print(f'{m["title"]}  ({m["channelId"]})')
        print(f'subs: {yt.fmt_count(m["subs"])}   videos: '
              f'{yt.fmt_count(m["videoCount"])}   recent median: '
              f'{yt.fmt_count(d["median_views"])}   cadence: '
              f'{d["uploads_per_week"]}/wk\n')
        table([[f'{v["multiplier"]}x' if v["multiplier"] else "",
                (v["title"] or "")[:60], yt.fmt_count(v["views"]),
                yt.fmt_age(v["age_days"]), v["videoId"]]
               for v in d["videos"]],
              ["mult", "title", "views", "age", "id"])
        winners = [v for v in d["videos"]
                   if v["multiplier"] and v["multiplier"] >= 2]
        if winners:
            print("\nOVERPERFORMERS (study these titles/thumbnails):")
            for v in winners:
                print(f'  {v["multiplier"]}x  {v["title"]}')
    emit(args, data, human)


# ---------------------------------------------------------------- video

def cmd_video(args):
    vid = yt.video_id_from(args.video)
    info = yt.video_info(vid)
    if info.get("publishDate"):
        try:
            pub = time.strptime(info["publishDate"][:10], "%Y-%m-%d")
            days = max((time.time() - time.mktime(pub)) / 86400, 0.04)
            info["age_days"] = round(days, 1)
            if info["views"]:
                info["views_per_day"] = round(info["views"] / days)
        except ValueError:
            pass

    def human(i):
        print(f'{i["title"]}\n{i["channel"]}  |  {i.get("category") or "?"}')
        print(f'views: {yt.fmt_count(i["views"])}   published: '
              f'{i.get("publishDate") or "?"}   views/day: '
              f'{yt.fmt_count(i.get("views_per_day"))}   length: '
              f'{(i["duration_s"] or 0)//60}m{(i["duration_s"] or 0)%60:02d}s')
        if i["tags"]:
            print(f'\ntags ({len(i["tags"])}): {", ".join(i["tags"])}')
        if i["description"]:
            print(f'\ndescription:\n{i["description"]}')
    emit(args, info, human)


# ---------------------------------------------------------------- title

POWER_WORDS = """secret secrets proven mistake mistakes never stop avoid
instantly actually truth nobody hidden science warning shocking simple easy
fast finally exactly real reason why revealed banned dangerous worst best
free ultimate insane crazy weird surprising doctors experts""".split()

CURIOSITY_PATTERNS = [
    r"\bno one\b", r"\bnobody\b", r"\bwhat happen", r"\bthis is why\b",
    r"\bthe real\b", r"\buntil\b", r"\bbefore you\b", r"\bwhat i\b",
    r"\bquietly\b", r"\bactually\b",
]


def score_title(title):
    t = title.strip()
    low = t.lower()
    words = t.split()
    score, notes, tips = 20, [], []

    n = len(t)
    if 35 <= n <= 60:
        score += 15; notes.append(f"length {n} ideal")
    elif 61 <= n <= 70:
        score += 8; notes.append(f"length {n} ok")
    elif n > 70:
        score -= 10; tips.append(f"too long ({n} chars) - truncates in "
                                 "search/suggested; cut to under 60")
    else:
        tips.append(f"short ({n} chars) - room to add specificity")

    if re.search(r"\d", t):
        score += 10; notes.append("has a number")
    else:
        tips.append("add a specific number (age, count, timeframe)")

    if re.match(r"(how|why|what|when|which|is|can|do|does)\b", low) \
            or t.endswith("?"):
        score += 8; notes.append("question/how-why framing")

    pw = [w for w in POWER_WORDS if re.search(rf"\b{w}\b", low)]
    if pw:
        add = min(len(pw) * 5, 15)
        score += add; notes.append(f"power words: {', '.join(pw[:4])}")
    else:
        tips.append("no power words (truth / mistake / actually / hidden ...)")

    if re.search(r"[\[\(].+[\]\)]", t):
        score += 5; notes.append("bracket clarifier")
    if re.search(r"\b20\d\d\b", t):
        score += 4; notes.append("has year")

    cur = [p for p in CURIOSITY_PATTERNS if re.search(p, low)]
    if cur:
        score += 8; notes.append("curiosity gap")
    if re.search(r"\b(stop|avoid|mistake|worst|warning|never)\b", low):
        score += 6; notes.append("stakes/negativity (drives CTR)")

    caps = [w for w in words if len(w) > 2 and w.isupper()]
    if len(caps) > 2:
        score -= 5; tips.append("too many ALL-CAPS words")
    if t == low and n > 15:
        score -= 5; tips.append("use Title Case or sentence case")

    return {"title": t, "score": clamp(round(score)),
            "hits": notes, "fixes": tips}


def cmd_title(args):
    results = [score_title(t) for t in args.titles]
    results.sort(key=lambda r: -r["score"])

    def human(res):
        for r in res:
            print(f'\n[{r["score"]}/100]  {r["title"]}')
            for h in r["hits"]:
                print(f"   + {h}")
            for f in r["fixes"]:
                print(f"   - {f}")
    emit(args, results, human)


# ---------------------------------------------------------------- track

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS channels(
        id TEXT PRIMARY KEY, handle TEXT, title TEXT, added_ts INT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots(
        channel_id TEXT, ts INT, subs INT, median_views INT,
        latest_video_id TEXT, latest_title TEXT)""")
    return con


def snap_channel(con, cid):
    meta = yt.channel_meta(cid)
    vids = yt.channel_videos(cid, limit=30)
    views = [v["views"] for v in vids if v["views"] is not None]
    med = int(statistics.median(views)) if views else None
    latest = vids[0] if vids else {}
    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                (cid, int(time.time()), meta["subs"], med,
                 latest.get("videoId"), latest.get("title")))
    con.commit()
    return meta, med, latest


def cmd_track(args):
    con = db()
    if args.action == "add":
        cid = yt.resolve_channel(args.target)
        meta, med, latest = snap_channel(con, cid)
        con.execute("INSERT OR REPLACE INTO channels VALUES(?,?,?,?)",
                    (cid, args.target, meta["title"], int(time.time())))
        con.commit()
        print(f'tracking {meta["title"]} ({yt.fmt_count(meta["subs"])} subs, '
              f'median {yt.fmt_count(med)})')
    elif args.action == "rm":
        cid = yt.resolve_channel(args.target)
        con.execute("DELETE FROM channels WHERE id=?", (cid,))
        con.execute("DELETE FROM snapshots WHERE channel_id=?", (cid,))
        con.commit()
        print("removed.")
    elif args.action == "list":
        rows = con.execute("SELECT id, handle, title FROM channels").fetchall()
        table(rows, ["channelId", "handle", "title"])
    elif args.action == "snap":
        for cid, title in con.execute(
                "SELECT id, title FROM channels").fetchall():
            print(f"snapshot: {title} ...", file=sys.stderr)
            try:
                snap_channel(con, cid)
            except Exception as e:
                print(f"  ! {e}", file=sys.stderr)
        print("done.")
    elif args.action == "report":
        rows = []
        for cid, title in con.execute(
                "SELECT id, title FROM channels").fetchall():
            snaps = con.execute(
                "SELECT ts, subs, median_views, latest_title FROM snapshots "
                "WHERE channel_id=? ORDER BY ts DESC LIMIT 2",
                (cid,)).fetchall()
            if not snaps:
                continue
            new = snaps[0]
            old = snaps[1] if len(snaps) > 1 else None
            dsubs = (new[1] - old[1]) if old and None not in (new[1], old[1]) \
                else None
            days = round((new[0] - old[0]) / 86400, 1) if old else None
            rows.append([title, yt.fmt_count(new[1]),
                         (f"+{dsubs}" if dsubs and dsubs >= 0 else dsubs)
                         if dsubs is not None else "",
                         f"{days}d" if days else "first snap",
                         yt.fmt_count(new[2]),
                         (new[3] or "")[:45]])
        table(rows, ["channel", "subs", "subs-delta", "since",
                     "median-views", "latest video"])


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(
        prog="tubeiq", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true",
                   help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("suggest", help="autocomplete fan-out")
    s.add_argument("seed")
    s.add_argument("-n", type=int, default=40)
    s.add_argument("--deep", action="store_true", help="full a-z fan-out")
    s.set_defaults(fn=cmd_suggest)

    s = sub.add_parser("keywords", help="scored keyword research")
    s.add_argument("seed")
    s.add_argument("--top", type=int, default=8,
                   help="how many fan-out keywords to analyze (default 8)")
    s.set_defaults(fn=cmd_keywords)

    s = sub.add_parser("search", help="search with stats")
    s.add_argument("query")
    s.add_argument("--sort", default="relevance",
                   choices=["relevance", "date", "views", "rating"])
    s.add_argument("--period", default="all",
                   choices=["all", "hour", "day", "week", "month", "year"])
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("outliers", help="videos beating their channel median")
    s.add_argument("query")
    s.add_argument("--period", default="month",
                   choices=["all", "day", "week", "month", "year"])
    s.add_argument("-n", type=int, default=10,
                   help="candidates to baseline (default 10)")
    s.add_argument("--min-views", type=int, default=10_000)
    s.set_defaults(fn=cmd_outliers)

    s = sub.add_parser("channel", help="channel audit")
    s.add_argument("channel", help="@handle, UC id, or URL")
    s.add_argument("-n", type=int, default=30)
    s.set_defaults(fn=cmd_channel)

    s = sub.add_parser("video", help="single video breakdown")
    s.add_argument("video", help="video id or URL")
    s.set_defaults(fn=cmd_video)

    s = sub.add_parser("title", help="score title(s) 0-100")
    s.add_argument("titles", nargs="+")
    s.set_defaults(fn=cmd_title)

    s = sub.add_parser("track", help="competitor tracking")
    s.add_argument("action", choices=["add", "rm", "list", "snap", "report"])
    s.add_argument("target", nargs="?", help="@handle or channel id")
    s.set_defaults(fn=cmd_track)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
