"""Odds scraper API.

Endpoints:
    GET /api/search?q=<keywords>        -> {"ok": true, "results": [{title, competition, url}]}
    GET /api/odds?url=<match url>       -> {"ok": true, "snapshot": {...}}
    GET /api/snapshot?q=<keywords>      -> {"ok": true, "snapshot": {...}, "match": {...}}
                                         (search, then scrape the top match)
    GET /api/health                     -> {"ok": true}

Auth: send header X-API-Token matching the API_TOKEN env var.
Set API_TOKEN to "" to disable auth (not recommended on a public VPS).

Run:  uvicorn app:app --host 0.0.0.0 --port 8077
"""
import os
import time
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from scraper import BASE, search_matches, get_match_odds, merge_books, finalize_snapshot

API_TOKEN = os.environ.get("API_TOKEN", "change-me")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))  # seconds
# Other region instances to aggregate bookmakers from, e.g.
# PEERS="http://us-vps:8077,http://uk-vps:8077"  (same API_TOKEN on all)
PEERS = [u.strip().rstrip("/") for u in os.environ.get("PEERS", "").split(",") if u.strip()]
REGION = os.environ.get("REGION", "default")

app = FastAPI(title="Odds Scraper API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_cache: dict = {}


def _check_auth(x_api_token: str | None):
    if API_TOKEN and x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-API-Token")


def _cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _ensure_oddsportal_url(url: str) -> str:
    u = urlparse(url)
    if u.netloc.lower().replace("www.", "") != urlparse(BASE).netloc.replace("www.", ""):
        # allow with/without www
        if "oddsportal.com" not in u.netloc.lower():
            raise HTTPException(status_code=400, detail="url must be an oddsportal.com match page")
    return url


@app.get("/api/health")
def health():
    return {"ok": True, "region": REGION, "peers": len(PEERS)}


def _peer_odds(base, url):
    """Ask a peer region instance for odds on a specific match page.
    Uses /api/odds (never fans out) so peers can't recurse into each other."""
    req_url = base + "/api/odds?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(req_url, headers={"X-API-Token": API_TOKEN} if API_TOKEN else {})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
    except Exception:
        return None
    if not d.get("ok") or not d.get("snapshot"):
        return None
    return d["snapshot"]


def _aggregate(match_url, local_snap):
    """Merge bookmakers from this region + peer regions into one snapshot."""
    snaps = [local_snap]
    regions_ok = 1
    if PEERS:
        with ThreadPoolExecutor(max_workers=len(PEERS)) as ex:
            futs = {ex.submit(_peer_odds, p, match_url): p for p in PEERS}
            for f in as_completed(futs):
                s = f.result()
                if s:
                    snaps.append(s)
                    regions_ok += 1
    books = merge_books([s.get("books") for s in snaps])
    base = snaps[0]
    note = (
        f"共 {len(books)} 間莊家（合併 {regions_ok} 個地區：{REGION}"
        + (f"＋{len(PEERS)} 個 peer" if PEERS else "")
        + "）；只供參考，不構成交易建議。"
    )
    merged = finalize_snapshot(
        base.get("home"), base.get("away"), base.get("comp"),
        base.get("kickoff"), base.get("match_url"), books,
        "OddsPortal（即時搜尋・多地區合併）", note,
    )
    return merged, regions_ok


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=2), x_api_token: str | None = Header(default=None)):
    _check_auth(x_api_token)
    try:
        results = _cached(f"search:{q.lower()}", lambda: search_matches(q))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "results": results}


@app.get("/api/odds")
def api_odds(url: str = Query(...), x_api_token: str | None = Header(default=None)):
    _check_auth(x_api_token)
    url = _ensure_oddsportal_url(url)
    try:
        snap = _cached(f"odds:{url}", lambda: get_match_odds(url))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "snapshot": snap}


@app.get("/api/snapshot")
def api_snapshot(q: str = Query(..., min_length=2), x_api_token: str | None = Header(default=None)):
    _check_auth(x_api_token)
    try:
        results = _cached(f"search:{q.lower()}", lambda: search_matches(q))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not results:
        return {"ok": False, "error": "no matches found"}
    match = results[0]
    try:
        snap = _cached(f"odds:{match['url']}", lambda: get_match_odds(match["url"]))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if PEERS:
        snap, regions_ok = _cached(
            f"aggsnap:{match['url']}", lambda: _aggregate(match["url"], snap)
        )
    else:
        regions_ok = 1
    return {"ok": True, "match": match, "snapshot": snap, "regions": regions_ok}
