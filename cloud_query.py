"""
cloud_query.py - one on-demand query from the dashboard / GitHub UI.
Usage: python cloud_query.py <command> <query> [period]
Writes docs/data/ondemand.json. Run by .github/workflows/query.yml.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yt
from tubeiq import (keywords_data, outliers_data, channel_data, fanout,
                    score_title, _vpd)

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    command = sys.argv[1]
    query = sys.argv[2]
    period = sys.argv[3] if len(sys.argv) > 3 else "month"

    if command == "suggest":
        results = [{"keyword": k, "weight": round(w, 2)}
                   for k, w in list(fanout(query).items())[:40]]
    elif command == "keywords":
        results = keywords_data(query, top=8)
    elif command == "outliers":
        results = outliers_data(query, period=period, n=10, min_views=10_000)
    elif command == "search":
        results = yt.search(query, sort="views", period=period, limit=20)
        for v in results:
            v["views_per_day"] = round(_vpd(v)) if _vpd(v) else None
    elif command == "channel":
        results = channel_data(query, n=30)
    elif command == "title":
        titles = [t.strip() for t in query.replace("|", "\n").splitlines()
                  if t.strip()]
        results = sorted((score_title(t) for t in titles),
                         key=lambda r: -r["score"])
    else:
        raise SystemExit(f"unknown command: {command}")

    out = {"command": command, "query": query, "period": period,
           "generated": int(time.time()), "results": results}
    path = os.path.join(HERE, "docs", "data", "ondemand.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"wrote ondemand.json ({command}: {query})", file=sys.stderr)


if __name__ == "__main__":
    main()
