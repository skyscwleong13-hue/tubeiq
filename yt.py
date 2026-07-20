"""
yt.py - zero-key YouTube data layer for tubeiq.

Talks to the same "Innertube" endpoints the youtube.com web app uses
(youtubei/v1/*) plus the public Google autocomplete endpoint. No API key,
no login, read-only public data.

Endpoints used:
  POST youtubei/v1/search                  -> search results
  POST youtubei/v1/browse                  -> channel pages (videos tab, about)
  POST youtubei/v1/player                  -> single video metadata
  POST youtubei/v1/navigation/resolve_url  -> @handle -> channel id
  GET  suggestqueries.google.com           -> autocomplete keywords
"""

import base64
import json
import re
import time
import urllib.parse

import requests

BASE = "https://www.youtube.com/youtubei/v1"
CLIENT = {
    "context": {
        "client": {
            "clientName": "WEB",
            "clientVersion": "2.20250715.00.00",
            "hl": "en",
            "gl": "US",
        }
    }
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Origin": "https://www.youtube.com",
}

_session = requests.Session()
_session.headers.update(HEADERS)
_last_call = 0.0


def _throttle(min_gap=0.35):
    global _last_call
    wait = min_gap - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _post(endpoint, payload):
    _throttle()
    body = dict(CLIENT)
    body.update(payload)
    r = _session.post(f"{BASE}/{endpoint}?prettyPrint=false", json=body, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- helpers

def find_all(obj, key):
    """Recursively yield every value stored under `key` anywhere in obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            else:
                yield from find_all(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_all(item, key)


def find_strings(obj, pattern):
    """Recursively yield every string in obj matching regex `pattern`."""
    rx = re.compile(pattern)
    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for item in o:
                yield from walk(item)
        elif isinstance(o, str) and rx.search(o):
            yield o
    yield from walk(obj)


_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_count(text):
    """'1.2M views' / '12,345 subscribers' / 'No views' -> int or None."""
    if not text:
        return None
    t = text.lower().replace(",", "").strip()
    if t.startswith("no "):
        return 0
    m = re.search(r"([\d.]+)\s*([kmb])?", t)
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2):
        n *= _MULT[m.group(2)]
    return int(n)


_AGE_UNITS = {
    "second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24,
    "day": 1, "week": 7, "month": 30.4, "year": 365,
}


def parse_age_days(text):
    """'3 weeks ago' / 'Streamed 2 days ago' -> float days, else None."""
    if not text:
        return None
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text)
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNITS[m.group(2)]


def fmt_count(n):
    if n is None:
        return "?"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_age(days):
    if days is None:
        return "?"
    if days >= 365:
        return f"{days/365:.1f}y"
    if days >= 30.4:
        return f"{days/30.4:.0f}mo"
    if days >= 7:
        return f"{days/7:.0f}w"
    return f"{days:.0f}d"


def _runs_text(node):
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return node["simpleText"]
    if "runs" in node:
        return "".join(r.get("text", "") for r in node["runs"])
    return None


def _duration_secs(text):
    """'12:34' or '1:02:03' -> seconds."""
    if not text or not re.fullmatch(r"[\d:]+", text.strip()):
        return None
    parts = [int(p) for p in text.strip().split(":")]
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


# ---------------------------------------------------------------- parsing

def _parse_video_renderer(r):
    vid = r.get("videoId")
    if not vid:
        return None
    owner = (r.get("ownerText") or r.get("longBylineText")
             or r.get("shortBylineText") or {})
    ch_name, ch_id = None, None
    runs = owner.get("runs") or []
    if runs:
        ch_name = runs[0].get("text")
        ch_id = (runs[0].get("navigationEndpoint", {})
                 .get("browseEndpoint", {}).get("browseId"))
    views_txt = _runs_text(r.get("viewCountText") or {}) or \
        _runs_text(r.get("shortViewCountText") or {})
    return {
        "videoId": vid,
        "title": _runs_text(r.get("title") or {}) or "",
        "views": parse_count(views_txt),
        "age_days": parse_age_days(_runs_text(r.get("publishedTimeText") or {})),
        "duration_s": _duration_secs(_runs_text(r.get("lengthText") or {})),
        "channel": ch_name,
        "channelId": ch_id,
    }


def _parse_lockup(l):
    """Newer 'lockupViewModel' layout (rolling out since 2024)."""
    vid = l.get("contentId")
    ctype = l.get("contentType", "")
    if not vid or len(vid) != 11 or ("VIDEO" not in ctype and ctype):
        return None
    title = None
    for t in find_all(l, "title"):
        if isinstance(t, dict) and isinstance(t.get("content"), str):
            title = t["content"]
            break
    views = age = dur = None
    for s in find_strings(l, r"."):
        if views is None and re.search(r"[\d.,]+[KMB]?\s+views", s):
            views = parse_count(s)
        elif age is None and re.search(r"\bago\b", s):
            age = parse_age_days(s)
        elif dur is None and re.fullmatch(r"[\d:]{3,}", s.strip()):
            dur = _duration_secs(s)
    return {
        "videoId": vid, "title": title or "", "views": views,
        "age_days": age, "duration_s": dur,
        "channel": None, "channelId": None,
    }


def _extract_videos(data):
    seen, out = set(), []
    for r in find_all(data, "videoRenderer"):
        v = _parse_video_renderer(r)
        if v and v["videoId"] not in seen:
            seen.add(v["videoId"])
            out.append(v)
    for r in find_all(data, "gridVideoRenderer"):
        v = _parse_video_renderer(r)
        if v and v["videoId"] not in seen:
            seen.add(v["videoId"])
            out.append(v)
    for l in find_all(data, "lockupViewModel"):
        v = _parse_lockup(l)
        if v and v["videoId"] not in seen:
            seen.add(v["videoId"])
            out.append(v)
    return out


# ---------------------------------------------------------------- API

def search_params(sort="relevance", period="all"):
    """Build the protobuf 'params' blob for search filters (videos only)."""
    sort_map = {"relevance": 0, "rating": 1, "date": 2, "views": 3}
    per_map = {"hour": 1, "day": 2, "week": 3, "month": 4, "year": 5}
    payload = b""
    if sort_map.get(sort, 0):
        payload += bytes([0x08, sort_map[sort]])
    filt = bytes([0x10, 0x01])            # type = video
    if per_map.get(period):
        filt = bytes([0x08, per_map[period]]) + filt
    payload += bytes([0x12, len(filt)]) + filt
    return base64.b64encode(payload).decode()


def search(query, sort="relevance", period="all", limit=20):
    data = _post("search", {
        "query": query,
        "params": search_params(sort, period),
    })
    return _extract_videos(data)[:limit]


def autocomplete(query):
    _throttle(0.15)
    url = ("https://suggestqueries.google.com/complete/search"
           f"?client=firefox&ds=yt&hl=en&q={urllib.parse.quote(query)}")
    r = _session.get(url, timeout=15)
    r.raise_for_status()
    try:
        return json.loads(r.content.decode("utf-8", "ignore"))[1]
    except Exception:
        return []


def resolve_channel(ref):
    """Accept UC... id, @handle, youtube.com URL, or bare name -> channel id."""
    ref = ref.strip()
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return ref
    if "youtube.com" in ref:
        url = ref if ref.startswith("http") else "https://" + ref
    else:
        handle = ref if ref.startswith("@") else "@" + ref
        url = "https://www.youtube.com/" + handle
    data = _post("navigation/resolve_url", {"url": url})
    for ep in find_all(data, "browseEndpoint"):
        bid = ep.get("browseId", "")
        if bid.startswith("UC"):
            return bid
    raise ValueError(f"could not resolve channel: {ref}")


def channel_meta(channel_id):
    data = _post("browse", {"browseId": channel_id})
    meta = next(find_all(data, "channelMetadataRenderer"), {}) or {}
    subs = None
    for s in find_strings(data, r"[\d.,]+[KMB]?\s+subscribers"):
        subs = parse_count(s)
        break
    n_videos = None
    for s in find_strings(data, r"^[\d.,]+[KMB]?\s+videos?$"):
        n_videos = parse_count(s)
        break
    return {
        "channelId": channel_id,
        "title": meta.get("title"),
        "description": (meta.get("description") or "")[:300],
        "subs": subs,
        "videoCount": n_videos,
    }


def channel_videos(channel_id, limit=30):
    """Recent uploads from the Videos tab (newest first, ~30 per page)."""
    data = _post("browse", {
        "browseId": channel_id,
        "params": base64.b64encode(b"\x12\x06videos").decode(),
    })
    vids = _extract_videos(data)
    for v in vids:
        v["channelId"] = v["channelId"] or channel_id
    return vids[:limit]


def video_info(video_id):
    data = _post("player", {"videoId": video_id})
    d = data.get("videoDetails", {}) or {}
    micro = (data.get("microformat", {}) or {}).get(
        "playerMicroformatRenderer", {}) or {}
    return {
        "videoId": video_id,
        "title": d.get("title"),
        "views": int(d["viewCount"]) if d.get("viewCount") else None,
        "duration_s": int(d["lengthSeconds"]) if d.get("lengthSeconds") else None,
        "channel": d.get("author"),
        "channelId": d.get("channelId"),
        "tags": d.get("keywords") or [],
        "description": (d.get("shortDescription") or "")[:500],
        "publishDate": micro.get("publishDate"),
        "category": micro.get("category"),
    }


def video_id_from(ref):
    """Accept a video id or any YouTube URL form."""
    ref = ref.strip()
    if re.fullmatch(r"[\w-]{11}", ref):
        return ref
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", ref)
    if m:
        return m.group(1)
    raise ValueError(f"not a video id/url: {ref}")
