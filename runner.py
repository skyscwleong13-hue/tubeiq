"""
runner.py - scheduled cloud run. Reads config.json, refreshes everything the
dashboard shows, writes docs/data/dashboard.json (+ appends history.json).
Run by .github/workflows/daily.yml.
"""

import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yt
from tubeiq import keywords_data, outliers_data

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "docs", "data")


def log(s):
    print(s, file=sys.stderr, flush=True)


def snapshot_channel(ref):
    cid = yt.resolve_channel(ref)
    meta = yt.channel_meta(cid)
    vids = yt.channel_videos(cid, limit=30)
    views = [v["views"] for v in vids if v["views"] is not None]
    latest = vids[0] if vids else {}
    return {
        "ref": ref,
        "channelId": cid,
        "title": meta["title"],
        "subs": meta["subs"],
        "median_views": int(statistics.median(views)) if views else None,
        "latest_video": latest.get("title"),
        "latest_video_id": latest.get("videoId"),
        "latest_age_days": latest.get("age_days"),
    }


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    os.makedirs(DATA, exist_ok=True)
    now = int(time.time())
    dash = {"generated": now, "niches": {}, "keywords": {}, "competitors": []}

    ocfg = cfg.get("outliers", {})
    for niche in cfg.get("niches", []):
        log(f"== outliers: {niche}")
        try:
            dash["niches"][niche] = outliers_data(
                niche, period=ocfg.get("period", "month"),
                n=ocfg.get("n", 8), min_views=ocfg.get("min_views", 20000),
                log=log)
        except Exception as e:
            log(f"!! outliers '{niche}' failed: {e}")
            dash["niches"][niche] = []

    for seed in cfg.get("keyword_seeds", []):
        log(f"== keywords: {seed}")
        try:
            dash["keywords"][seed] = keywords_data(seed, top=6, log=log)
        except Exception as e:
            log(f"!! keywords '{seed}' failed: {e}")
            dash["keywords"][seed] = []

    # competitor snapshots + deltas vs previous run
    hist_path = os.path.join(DATA, "history.json")
    history = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            history = json.load(f)
    prev = {c["channelId"]: c for c in history[-1]["channels"]} \
        if history else {}

    snaps = []
    for ref in cfg.get("tracked", []):
        log(f"== snapshot: {ref}")
        try:
            snaps.append(snapshot_channel(ref))
        except Exception as e:
            log(f"!! snapshot '{ref}' failed: {e}")
    for s in snaps:
        p = prev.get(s["channelId"])
        s["subs_delta"] = (s["subs"] - p["subs"]) \
            if p and s["subs"] is not None and p.get("subs") is not None \
            else None
        s["new_upload"] = bool(
            p and s.get("latest_video_id")
            and s["latest_video_id"] != p.get("latest_video_id"))
    dash["competitors"] = snaps

    history.append({"ts": now, "channels": snaps})
    history = history[-90:]
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f)
    with open(os.path.join(DATA, "dashboard.json"), "w",
              encoding="utf-8") as f:
        json.dump(dash, f, ensure_ascii=False)
    log("dashboard.json written.")


if __name__ == "__main__":
    main()
