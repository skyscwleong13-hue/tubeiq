# tubeiq — local vidIQ replacement

**Web dashboard (any device): https://skyscwleong13-hue.github.io/tubeiq/**

The same engine also runs in the cloud, free, on GitHub Actions:

- **Daily refresh** (06:00 MYT) — outliers for each niche, keyword scores for each
  seed, competitor snapshots with deltas → shown on the dashboard. Edit
  [`config.json`](config.json) to change niches/seeds/tracked channels.
- **On-demand queries from any device** — dashboard → *Run* tab. Two ways:
  1. Paste a fine-grained GitHub token once per device (repo `tubeiq` only,
     permissions *Actions: read & write* + *Contents: read*) and hit Run.
  2. No token: GitHub → Actions → *query* → *Run workflow* (works fine on phone).
  Results land back on the dashboard in ~2–4 min (Actions run + Pages deploy).
- Cloud limitation: the `video` command's player endpoint is bot-checked from
  datacenter IPs, so it's local-CLI only. Everything else passes from CI.

Cloud files: `runner.py` (daily), `cloud_query.py` (on-demand),
`.github/workflows/daily.yml` + `query.yml`, `docs/` (the dashboard).

No API key, no account, no subscription. Talks to the same internal endpoints
the youtube.com web app uses (`youtubei/v1/*`) plus Google's free autocomplete
endpoint. Read-only public data. Lives next to the editor as another
"replace the paid tool" CLI.

```
python tubeiq.py <command>            # run from this folder
python tubeiq.py --json <command>    # machine-readable (for autotube etc.)
```

---

## How vidIQ actually works (the breakdown)

vidIQ is not magic — it is four data sources plus scoring formulas plus an
LLM wrapper. Feature by feature:

| vidIQ feature | What it really is under the hood | tubeiq equivalent |
|---|---|---|
| **Keyword research** (search volume, competition, overall score) | Search *volume* is an estimate modeled from Google/YouTube autocomplete presence + their users' aggregate data. *Competition* is derived from how strong the currently-ranking videos are (views, channel size, recency). "Overall" = demand vs competition formula. | `keywords <seed>` — autocomplete fan-out measures demand; strength/staleness of the current top 10 measures competition; same demand-vs-competition score 0–100 |
| **Outliers / trending** | A video's views divided by its own channel's typical views. That's the whole feature. | `outliers <query>` — views ÷ channel's median recent video, sorted by multiplier |
| **Competitors tab** | Periodic snapshots of competitor channels stored server-side; shows deltas (subs gained, new uploads, velocity). | `track add/snap/report` — same snapshots, stored in local SQLite |
| **Channel audit / "what's working"** | Your (or any) channel's recent uploads vs their median; flags overperformers. | `channel <@handle>` — median, upload cadence, per-video multiplier, overperformer list |
| **Title/SEO score** | Heuristics: length, keyword placement, numbers, power words, CTR patterns. Not ML — a checklist. | `title "<title>"` — same checklist, 0–100, tells you what to fix |
| **Video tags / stats panel** | Reads the video page metadata (tags are public in page source). | `video <id/url>` — views/day, tags, category, publish date |
| **Daily ideas / AI titles / scripts** | GPT wrapper with your channel context pasted into the prompt. | You already have Claude + your VIDEO-SYSTEM.md. Ask Claude; feed it `--json` output from the commands above |
| **Subscriber/view "real-time" stats** | YouTube's own public counts, polled. | `track snap` on a schedule |

The only thing vidIQ has that cannot be rebuilt is their proprietary aggregate
user data (their "search volume" numbers, which are estimates anyway). The
autocomplete-presence proxy correlates with it well enough to rank keywords,
which is all you actually use it for.

---

## Commands

### Find what to make
```
python tubeiq.py suggest "cortisol"                  # raw keyword ideas, demand-weighted
python tubeiq.py keywords "cortisol belly fat"       # scored: demand / competition / overall
python tubeiq.py outliers "cortisol" --period month  # topics proven to outperform right now
python tubeiq.py search "sleep hormones" --sort views --period year
```

`keywords` columns: **score** (make this video?), **demand**, **compet**,
**top-age** (stale top results = gap), **weak** (videos under 10K ranking =
beatable page).

`outliers` mult: 3x+ means the *topic/packaging* won, not the channel — steal
the angle. Same logic as the IG playbook (15–239x reels).

### Spy on channels
```
python tubeiq.py channel "@hubermanlab"       # audit: median, cadence, overperformers
python tubeiq.py track add "@somecompetitor"  # start tracking
python tubeiq.py track snap                   # snapshot all tracked (run weekly)
python tubeiq.py track report                 # deltas since last snapshot
python tubeiq.py video <id or url>            # tags, views/day, category
```

### Package
```
python tubeiq.py title "Why You Wake Up at 3AM (The Cortisol Mistake Nobody Talks About)"
```
Scores 0–100 against CTR heuristics (length, number, curiosity gap, power
words, stakes). Write 5 candidates, score them all, keep the winner.

---

## Weekly workflow (replaces the vidIQ dashboard)

1. `outliers "<niche term>" --period month` → shortlist 3 proven angles.
2. `keywords "<angle>"` → pick the phrasing with best score; note the *weak* count.
3. Draft 5 titles → `title ...` → keep the highest with score ≥ 70.
4. `track snap` + `track report` → see who's growing and off which video.
5. Feed any of it to Claude with `--json` for scripts/ideas (that's all
   vidIQ's AI tab does).

## Notes / limits

- Data comes from YouTube's own web endpoints; if a layout change ever breaks
  parsing, the fix is in `yt.py` (parsers already handle both the old
  `videoRenderer` and new `lockupViewModel` layouts).
- Subscriber counts are public rounded values ("1.2M"), same as vidIQ shows.
- Search-volume numbers are a demand *proxy* (autocomplete presence), good for
  ranking keywords against each other, not absolute volumes — vidIQ's are
  estimates too.
- Be polite: calls are throttled to ~3/sec; `keywords` does ~15 searches,
  `outliers` does 1 search + 1 channel fetch per candidate.
- Tracking DB: `data/tubeiq.db` (SQLite).
