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



def _matchstat_get(path):
    """GET from Matchstat Tennis API via RapidAPI. Returns parsed JSON or None."""
    if not MATCHSTAT_API_KEY:
        return None
    url = f"https://tennis-api-atp-wta-itf.p.rapidapi.com/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "X-RapidAPI-Key": MATCHSTAT_API_KEY,
            "X-RapidAPI-Host": "tennis-api-atp-wta-itf.p.rapidapi.com",
            "User-Agent": "odds-scraper/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None


def _fetch_matchstat_tennis(q):
    """Search Matchstat for tennis matches by player name.
    Returns a snapshot dict, or None if no match found / no key.
    """
    if not MATCHSTAT_API_KEY:
        return None
    words = [w.lower() for w in q.strip().split() if len(w) > 2]
    if not words:
        return None
    events = []
    # Upcoming matches with pre-match odds
    data = _matchstat_get("tennis/v2/upcoming/matches?page=1&limit=100")
    if data:
        batch = data.get("matches", []) if isinstance(data, dict) else []
        if isinstance(batch, list):
            events.extend(batch)
    if not events:
        return None
    # Find best matching event
    best = None
    best_score = 0
    for ev in events:
        p1_obj = ev.get("player1", {}) or {}
        p2_obj = ev.get("player2", {}) or {}
        p1 = str(p1_obj.get("name", "") if isinstance(p1_obj, dict) else "").lower()
        p2 = str(p2_obj.get("name", "") if isinstance(p2_obj, dict) else "").lower()
        combined = p1 + " " + p2
        score = sum(1 for w in words if w in combined)
        need = 1 if len(words) == 1 else 2
        if score >= need and score > best_score:
            best = ev
            best_score = score
    if not best:
        return None
    # Extract names and odds
    p1_obj = best.get("player1", {}) or {}
    p2_obj = best.get("player2", {}) or {}
    p1 = str(p1_obj.get("name", "Player 1") if isinstance(p1_obj, dict) else "Player 1")
    p2 = str(p2_obj.get("name", "Player 2") if isinstance(p2_obj, dict) else "Player 2")
    try:
        o1 = float(p1_obj.get("odd", 0) if isinstance(p1_obj, dict) else 0)
        o2 = float(p2_obj.get("odd", 0) if isinstance(p2_obj, dict) else 0)
    except (ValueError, TypeError):
        return None
    if not (o1 > 1 and o2 > 1):
        return None
    # Devig to get implied probabilities
    ih, ia = 1 / o1, 1 / o2
    tot = ih + ia
    ph = round(ih / tot * 100, 1)
    pa = round(ia / tot * 100, 1)
    import datetime
    snapped = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y年%-m月%-d日 %H:%M（香港時間）")
    tour_obj = best.get("tournament", {}) or {}
    tour = str(tour_obj.get("name", "Tennis") if isinstance(tour_obj, dict) else "Tennis")
    return {
        "home": p1,
        "away": p2,
        "comp": tour,
        "kickoff": str(best.get("date", "")),
        "market": "h2h（網球）",
        "books": [["Matchstat", round(o1, 2), None, round(o2, 2)]],
        "cons": [["主勝", ph], ["客勝", pa]],
        "best": f"主勝 {o1}（Matchstat）・ 客勝 {o2}（Matchstat）",
        "snapped": snapped,
        "src": "Matchstat（RapidAPI）",
        "note": "網球賽前賠率（免費版）；只供參考，不構成交易建議。",
    }


# Cache for active tennis tournament keys (refresh every hour)
_tennis_keys_cache = {"keys": [], "ts": 0}

def _get_active_tennis_keys():
    """Get active tennis tournament keys from The Odds API."""
    import time
    now = time.time()
    # Cache for 1 hour
    if now - _tennis_keys_cache["ts"] < 3600 and _tennis_keys_cache["keys"]:
        return _tennis_keys_cache["keys"]
    if not THE_ODDS_API_KEY:
        return []
    try:
        url = f"https://api.the-odds-api.com/v4/sports/?apiKey={THE_ODDS_API_KEY}&all=true"
        req = urllib.request.Request(url, headers={"User-Agent": "odds-scraper/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            sports = json.load(r)
        keys = []
        for s in sports:
            key = s.get("key", "")
            # Active tennis tournaments only
            if key.startswith("tennis_atp") or key.startswith("tennis_wta"):
                if s.get("active", False):
                    keys.append(key)
        _tennis_keys_cache["keys"] = keys
        _tennis_keys_cache["ts"] = now
        return keys
    except Exception:
        return []


def _fetch_tennis_the_odds_api(q):
    """Search tennis matches via The Odds API per-tournament keys.
    Returns a snapshot dict, or None if no match found.
    """
    if not THE_ODDS_API_KEY:
        return None
    words = [w.lower() for w in q.strip().split() if len(w) > 2]
    if not words:
        return None
    keys = _get_active_tennis_keys()
    if not keys:
        return None
    for tkey in keys[:5]:  # Check up to 5 active tournaments
        try:
            url = (f"https://api.the-odds-api.com/v4/sports/{tkey}/odds/"
                   f"?apiKey={THE_ODDS_API_KEY}&regions=us&markets=h2h&oddsFormat=decimal")
            req = urllib.request.Request(url, headers={"User-Agent": "odds-scraper/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                events = json.load(r)
            for ev in events:
                home = ev.get("home_team", "")
                away = ev.get("away_team", "")
                combined = f"{home} {away}".lower()
                score = sum(1 for w in words if w in combined)
                need = 1 if len(words) == 1 else 2
                if score >= need:
                    # Found! Build snapshot from bookmakers
                    books = []
                    for bm in ev.get("bookmakers", []):
                        bname = bm.get("title", bm.get("key", ""))
                        for mkt in bm.get("markets", []):
                            if mkt.get("key") == "h2h":
                                outs = {o.get("name"): o.get("price") for o in mkt.get("outcomes", [])}
                                if home in outs and away in outs:
                                    books.append([bname, outs[home], None, outs[away]])
                    if not books:
                        continue
                    # Devig using best odds
                    best_h = max(b[1] for b in books if b[1])
                    best_a = max(b[3] for b in books if b[3])
                    ih, ia = 1 / best_h, 1 / best_a
                    tot = ih + ia
                    ph = round(ih / tot * 100, 1)
                    pa = round(ia / tot * 100, 1)
                    import datetime
                    snapped = datetime.datetime.now(
                        datetime.timezone(datetime.timedelta(hours=8))
                    ).strftime("%Y年%-m月%-d日 %H:%M（香港時間）")
                    return {
                        "home": home,
                        "away": away,
                        "comp": ev.get("sport_title", tkey),
                        "kickoff": ev.get("commence_time", ""),
                        "market": "h2h（網球）",
                        "books": books,
                        "cons": [["主勝", ph], ["客勝", pa]],
                        "best": f"主勝 {best_h}・ 客勝 {best_a}",
                        "snapped": snapped,
                        "src": "The Odds API",
                        "note": "網球賽前賠率；只供參考，不構成交易建議。",
                    }
        except Exception:
            continue
    return None


def _frac_to_dec(frac_str):
    """Convert fractional odds (e.g. '8/15') to decimal."""
    try:
        from fractions import Fraction
        return round(float(Fraction(frac_str)) + 1, 2)
    except Exception:
        return None


def _sofascore_get(path):
    """GET from SofaScore unofficial API. Returns parsed JSON or None."""
    url = f"https://api.sofascore.com/api/v1{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:
        return None


def _fetch_sofascore_tennis_live(q):
    """Search SofaScore for LIVE tennis matches by player name.
    Returns a snapshot dict with in-play odds, or None.
    """
    words = [w.lower() for w in q.strip().split() if len(w) > 2]
    if not words:
        return None
    # Get live tennis events
    data = _sofascore_get("/sport/tennis/events/live")
    if not data:
        return None
    events = data.get("events", [])
    if not events:
        return None
    # Find best matching event
    best = None
    best_score = 0
    for ev in events:
        home = ev.get("homeTeam", {}).get("name", "")
        away = ev.get("awayTeam", {}).get("name", "")
        combined = f"{home} {away}".lower()
        score = sum(1 for w in words if w in combined)
        need = 1 if len(words) == 1 else 2
        if score >= need and score > best_score:
            best = ev
            best_score = score
    if not best:
        return None
    # Get odds for this event
    event_id = best.get("id")
    if not event_id:
        return None
    odds_data = _sofascore_get(f"/event/{event_id}/odds/1/all")
    if not odds_data:
        return None
    # Parse full-time winner market
    markets = odds_data.get("markets", [])
    home_odds = None
    away_odds = None
    for mkt in markets:
        if mkt.get("marketName") == "Full time" or "winner" in str(mkt.get("marketName", "")).lower():
            choices = mkt.get("choices", [])
            if len(choices) >= 2:
                # choices[0] = home, choices[1] = away typically
                c0 = choices[0]
                c1 = choices[1]
                home_odds = _frac_to_dec(c0.get("fractionalValue", ""))
                away_odds = _frac_to_dec(c1.get("fractionalValue", ""))
                if home_odds and away_odds:
                    break
    if not (home_odds and away_odds):
        return None
    home = best.get("homeTeam", {}).get("name", "Home")
    away = best.get("awayTeam", {}).get("name", "Away")
    tour = best.get("tournament", {}).get("name", "Tennis")
    # Devig
    ih, ia = 1 / home_odds, 1 / away_odds
    tot = ih + ia
    ph = round(ih / tot * 100, 1)
    pa = round(ia / tot * 100, 1)
    import datetime
    snapped = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y年%-m月%-d日 %H:%M（香港時間）")
    # Live score if available
    hs = best.get("homeScore", {})
    aws = best.get("awayScore", {})
    score_str = ""
    if hs and aws:
        score_str = f" (比數 {hs.get('current', '?')}-{aws.get('current', '?')})"
    return {
        "home": home,
        "away": away,
        "comp": tour,
        "kickoff": "LIVE in-play" + score_str,
        "market": "h2h（網球・即時）",
        "books": [["SofaScore", home_odds, None, away_odds]],
        "cons": [["主勝", ph], ["客勝", pa]],
        "best": f"主勝 {home_odds}・ 客勝 {away_odds}",
        "snapped": snapped,
        "src": "SofaScore",
        "note": "網球即時 in-play 賠率（SofaScore 綜合盤）；只供參考，不構成交易建議。",
    }

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

        # 1a) Try SofaScore LIVE tennis first (true in-play, ~1s, no key needed)
        try:
            snap = _fetch_sofascore_tennis_live(q)
            if snap:
                return {"ok": True, "snapshot": snap, "source": "sofascore-live"}
        except Exception:
            pass

        # 1b) Try tennis via The Odds API (per-tournament keys)
        try:
            snap = _fetch_tennis_the_odds_api(q)
            if snap:
                return {"ok": True, "snapshot": snap, "source": "the-odds-api-tennis"}
        except Exception:
            pass

        # 1b) Matchstat temporarily disabled (Render->RapidAPI connectivity issue)
        # try:
        #     snap = _fetch_matchstat_tennis(q)
        #     if snap:
        #         return {"ok": True, "snapshot": snap, "source": "matchstat"}
        # except Exception:
        #     pass

        # 1c) If tennis sources were tried but found nothing, fail fast
        # (don't waste 60s on football scraper for tennis queries)
        # Heuristic: single-word queries are likely tennis player names
        if len(q.strip().split()) == 1:
            return {"ok": True, "snapshot": None, "source": "none",
                    "note": "Tennis: no live match found on SofaScore, no pre-match on The Odds API"}

        # 2) Fallback: OddsPortal scraper (football only) (slow, ~2min on first hit)
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
