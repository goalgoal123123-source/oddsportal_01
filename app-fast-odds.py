"""Odds scraper API.

Endpoints:
    GET /api/search?q=<keywords>        -> {"ok": true, "results": [{title, competition, url}]}
    GET /api/odds?url=<match url>       -> {"ok": true, "snapshot": {...}}
    GET /api/snapshot?q=<keywords>      -> {"ok": true, "snapshot": {...}, "match": {...}}
                                         (search, then scrape the top match)
    GET /api/fast-odds?q=<keywords>    -> {"ok": true, "snapshot": {...}, "source": ...}
                                         (The Odds API first, OddsPortal scraper fallback)
    GET /api/health                     -> {"ok": true}

Auth: send header X-API-Token matching the API_TOKEN env var.
Set API_TOKEN to "" to disable auth (not recommended on a public VPS).

Env:
    THE_ODDS_API_KEY  API key for the-odds-api.com (free 500/month). If empty,
                      /api/fast-odds skips straight to the scraper fallback.

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
# The Odds API key for fast odds (free tier 500/month). If empty, /api/fast-odds
# will skip straight to the scraper fallback.
THE_ODDS_API_KEY = os.environ.get("THE_ODDS_API_KEY", "")
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
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "results": results}


@app.get("/api/odds")
def api_odds(url: str = Query(...), x_api_token: str | None = Header(default=None)):
    _check_auth(x_api_token)
    url = _ensure_oddsportal_url(url)
    try:
        snap = _cached(f"odds:{url}", lambda: get_match_odds(url))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "snapshot": snap}


@app.get("/api/snapshot")
def api_snapshot(q: str = Query(..., min_length=2), x_api_token: str | None = Header(default=None)):
    _check_auth(x_api_token)
    try:
        results = _cached(f"search:{q.lower()}", lambda: search_matches(q))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not results:
        return {"ok": False, "error": "no matches found"}
    # Pick the best match: if the query names two teams (e.g. "Liverpool Man City"),
    # prefer a result whose title contains both; otherwise take the top result.
    match = results[0]
    words = [w for w in q.strip().split() if len(w) > 2]
    if len(words) >= 2:
        for r in results:
            t = r["title"].lower()
            if all(w.lower() in t for w in words):
                match = r
                break
    try:
        snap = _cached(f"odds:{match['url']}", lambda: get_match_odds(match["url"]))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if PEERS:
        snap, regions_ok = _cached(
            f"aggsnap:{match['url']}", lambda: _aggregate(match["url"], snap)
        )
    else:
        regions_ok = 1
    return {"ok": True, "match": match, "snapshot": snap, "regions": regions_ok}


# ---------- The Odds API fast path (with scraper fallback) ----------

# Sports to try in order for a team-name query. Each costs ~1 credit per call.
_FAST_SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "basketball_nba",
    "americanfootball_nfl",
    "baseball_mlb",
    "icehockey_nhl",
]


def _the_odds_api_get(path, params):
    """GET from The Odds API, returns (data, headers). Raises on error."""
    qs = urllib.parse.urlencode(params)
    url = f"https://api.the-odds-api.com/v4/{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "odds-scraper/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r), dict(r.headers)


def _devig_three_way(home_odds, draw_odds, away_odds):
    """Convert decimal odds to devigged probabilities (percentages)."""
    try:
        ih, ix, ia = 1 / home_odds, 1 / draw_odds, 1 / away_odds
        tot = ih + ix + ia
        return [round(ih / tot * 100, 1), round(ix / tot * 100, 1), round(ia / tot * 100, 1)]
    except (ZeroDivisionError, TypeError):
        return None


def _fetch_the_odds_api(q):
    """Search The Odds API for a match by team names.

    Returns a snapshot dict in the same shape as the scraper, or None if
    no match found. Raises RuntimeError("quota_exceeded") when the API
    key has no credits left.
    """
    if not THE_ODDS_API_KEY:
        return None
    words = [w.lower() for w in q.strip().split() if len(w) > 2]
    if not words:
        return None
    for sport in _FAST_SPORTS:
        try:
            data, headers = _the_odds_api_get(
                f"sports/{sport}/odds",
                {
                    "apiKey": THE_ODDS_API_KEY,
                    "regions": "us",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("the-odds-api key invalid")
            if e.code == 429:
                raise RuntimeError("quota_exceeded")
            continue
        except Exception:
            continue
        # Find the event whose team names best match the query words
        best, best_score = None, 0
        for ev in data or []:
            ht = (ev.get("home_team") or "").lower()
            at = (ev.get("away_team") or "").lower()
            combined = ht + " " + at
            score = sum(1 for w in words if w in combined)
            # Require at least 2 word hits, or 1 hit when query is a single team
            need = 2 if len(words) >= 2 else 1
            if score >= need and score > best_score:
                best, best_score = ev, score
        if not best:
            continue
        # Build books list: [name, home_odds, draw_odds, away_odds]
        home = best["home_team"]
        away = best["away_team"]
        books = []
        for bm in best.get("bookmakers", []):
            title = bm.get("title") or bm.get("key")
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                outs = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                # Soccer h2h has 3 outcomes; US sports have 2 (no draw)
                if home in outs and away in outs:
                    draw = outs.get("Draw")
                    books.append([title, outs[home], draw, outs[away]])
        if not books:
            continue
        # Consensus: average devigged probabilities across bookmakers
        cons_acc = [0.0, 0.0, 0.0]
        cons_n = 0
        has_draw = any(b[2] is not None for b in books)
        for b in books:
            if has_draw and b[2] is None:
                continue
            if has_draw:
                p = _devig_three_way(b[1], b[2], b[3])
                if p:
                    for i in range(3):
                        cons_acc[i] += p[i]
                    cons_n += 1
            else:
                # Two-way: devig home/away only
                try:
                    ih, ia = 1 / b[1], 1 / b[3]
                    tot = ih + ia
                    cons_acc[0] += ih / tot * 100
                    cons_acc[2] += ia / tot * 100
                    cons_n += 1
                except (ZeroDivisionError, TypeError):
                    pass
        if cons_n:
            labels = ["主勝", "和局", "客勝"] if has_draw else ["主勝", "—", "客勝"]
            cons = [[labels[i], round(cons_acc[i] / cons_n, 1)] for i in range(3) if cons_n]
            if not has_draw:
                cons = [cons[0], cons[2]]
        else:
            cons = []
        # Best odds per outcome
        def _best(idx):
            vals = [(b[idx], b[0]) for b in books if b[idx]]
            if not vals:
                return None
            v, n = max(vals, key=lambda x: x[0])
            return f"{v}（{n}）"
        parts = []
        bh = _best(1)
        if bh:
            parts.append(f"主勝 {bh}")
        if has_draw:
            bd = _best(2)
            if bd:
                parts.append(f"和局 {bd}")
        ba = _best(3)
        if ba:
            parts.append(f"客勝 {ba}")
        import datetime
        snapped = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%Y年%-m月%-d日 %H:%M（香港時間）")
        comp = (best.get("sport_title") or sport).upper()
        kickoff = best.get("commence_time", "")
        snap = {
            "home": home,
            "away": away,
            "comp": comp,
            "kickoff": kickoff,
            "market": "h2h",
            "books": books,
            "cons": cons,
            "best": "・ ".join(parts),
            "snapped": snapped,
            "src": "The Odds API",
            "note": f"共 {len(books)} 間美國莊家即時數據；只供參考，不構成交易建議。",
        }
        return snap
    return None


@app.get("/api/fast-odds")
def api_fast_odds(q: str = Query(..., min_length=2), x_api_token: str | None = Header(default=None)):
    """Fast odds via The Odds API, falling back to the OddsPortal scraper.

    Returns {"ok": true, "snapshot": {...}, "source": "the-odds-api"|"oddsportal-scraper"}.
    """
    _check_auth(x_api_token)

    def _do():
        # 1) Try The Odds API (fast, ~3s)
        try:
            snap = _fetch_the_odds_api(q)
            if snap:
                return {"ok": True, "snapshot": snap, "source": "the-odds-api"}
        except RuntimeError as e:
            if "quota_exceeded" not in str(e) and "invalid" not in str(e):
                pass  # fall through to scraper on unexpected errors too
            # quota_exceeded / invalid key -> fall through to scraper
        except Exception:
            pass  # any other error -> fall through to scraper

        # 2) Fallback: OddsPortal scraper (slow, ~2min on first hit)
        results = search_matches(q)
        if not results:
            return {"ok": False, "error": "no matches found"}
        match = results[0]
        words = [w for w in q.strip().split() if len(w) > 2]
        if len(words) >= 2:
            for r in results:
                t = r["title"].lower()
                if all(w.lower() in t for w in words):
                    match = r
                    break
        snap = get_match_odds(match["url"])
        if PEERS:
            snap, _ = _aggregate(match["url"], snap)
        return {"ok": True, "match": match, "snapshot": snap, "source": "oddsportal-scraper"}

    try:
        return _cached(f"fastodds:{q.lower()}", _do)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
