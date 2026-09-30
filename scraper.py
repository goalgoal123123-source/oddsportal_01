"""OddsPortal scraper using Playwright.

Two public functions:
    search_matches(query) -> list of {title, competition, url}
    get_match_odds(match_url) -> snapshot dict (same shape as the tracker's ODDS_SNAPSHOTS)

Run `python scraper.py "arsenal chelsea"` for a quick manual test.
"""
import re
import sys
import time
from datetime import datetime, timezone, timedelta

BASE = "https://www.oddsportal.com"
HKT = timezone(timedelta(hours=8))

SEARCH_INPUT_SELECTORS = [
    'input[placeholder*="Search" i]',
    'input[type="search"]',
    'input[name="q"]',
    "#search-input",
    ".search-input",
]


def _new_context(p):
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-images",
        ],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1366, "height": 900},
    )
    # Hide the webdriver flag (basic stealth; not a silver bullet vs Cloudflare)
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    # Block resource hogs: images, fonts, media, analytics, ads
    def _route(route):
        if route.request.resource_type in ("image", "font", "media"):
            return route.abort()
        url = route.request.url.lower()
        if any(
            k in url
            for k in (
                "google-analytics", "googletagmanager", "doubleclick",
                "facebook.net", "hotjar", "taboola", "outbrain",
                "criteo", "adsystem", "/ads/", "analytics",
            )
        ):
            return route.abort()
        return route.continue_()
    ctx.route("**/*", _route)
    return browser, ctx


def _looks_blocked(page):
    txt = (page.title() + " " + (page.content()[:2000] or "")).lower()
    return any(
        k in txt
        for k in ["just a moment", "attention required", "cf-chl", "captcha", "are you a robot"]
    )


def _clean_fixture_title(text):
    """Turn '10/Oct Arsenal Arsenal - Leeds Leeds' -> ('Arsenal - Leeds', '10/Oct').

    Team-page fixture links duplicate team names (logo img alt + <p> text),
    so collapse consecutively duplicated words.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    date = ""
    m = re.match(r"(\d{1,2}/[A-Za-z]{3}),?\s+(.*)$", text)
    if m:
        date, text = m.group(1), m.group(2)
    text = re.sub(r"(?i)\b([\w']+)\b(?:\s+\1\b)+", r"\1", text)
    return text.strip(), date


def _is_upcoming_fixture_link(text):
    """True only for upcoming-fixture rows on a team page.

    Upcoming links look like '10/Oct Arsenal Arsenal - Leeds Leeds'
    (date + teams with ' - ', no score). Excludes:
    - 'Last 6 Games Performance' form badges ('W'/'L'/'D', or tooltips
      like 'L3:0 (Brighton - Arsenal) 19.09.2026'),
    - past-result rows ('Finished'/'FIN' + scorelines).
    Both kinds share the same <a> class and /h2h/ URL pattern, so only
    the link text can tell them apart.
    """
    t = (text or "").strip()
    if len(t) <= 3:
        return False
    if " - " not in t:
        return False
    tl = t.lower()
    if "finished" in tl or re.search(r"(^|\s)fin(\s|$)", tl):
        return False
    # form-badge tooltip: result letter glued to a scoreline, e.g. "L3:0"
    if re.search(r"[WLD]\s*\d+\s*:\s*\d+", t):
        return False
    # upcoming fixture links carry a date like "10/Oct"
    if not re.search(r"\d{1,2}/[A-Za-z]{3}", t):
        return False
    return True


def search_matches(query, timeout_ms=30000):
    """Search OddsPortal for matches matching the keyword query.

    Types the query into the homepage search box, clicks the best-matching
    autocomplete suggestion (a team/player page), and collects that page's
    UPCOMING fixtures (the 'Next Matches' tab) as {title, competition, url}.

    Raises RuntimeError with a clear message instead of silently returning
    wrong matches (e.g. homepage fixtures when the suggestion click fails
    to navigate).
    """
    from playwright.sync_api import sync_playwright

    results = []
    query = (query or "").strip()
    first_word = query.split()[0] if query else ""
    with sync_playwright() as p:
        browser, ctx = _new_context(p)
        try:
            page = ctx.new_page()
            homepage = BASE + "/"
            page.goto(homepage, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_selector("input#search-input", timeout=10000)
            except Exception:
                pass
            if _looks_blocked(page):
                raise RuntimeError("blocked by anti-bot challenge on homepage")

            search_box = page.query_selector("input#search-input")
            if not search_box or not search_box.is_visible():
                raise RuntimeError("search input not found (site layout may have changed)")

            def _try_dropdown(q):
                search_box.click()
                search_box.fill("")
                search_box.fill(q)
                search_box.press("End")
                page.wait_for_timeout(400)
                try:
                    page.wait_for_selector(".dropdown-content li", timeout=8000)
                    return True
                except Exception:
                    return False

            queries_to_try = [query]
            if first_word and first_word.lower() != query.lower():
                queries_to_try.append(first_word)
            dropdown_ok = False
            for q in queries_to_try:
                if _try_dropdown(q):
                    dropdown_ok = True
                    break
            if not dropdown_ok:
                raise RuntimeError(f"search dropdown did not appear for query '{query}'")

            # Pick the suggestion whose text best matches the query
            # (dropdown rows are often identical, e.g. five 'Arsenal' rows).
            items = page.query_selector_all(".dropdown-content li")
            if not items:
                raise RuntimeError("search returned no suggestions")
            target = items[0]
            want = first_word.lower()
            for it in items:
                try:
                    t = (it.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if want and want in t:
                    target = it
                    break

            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=12000):
                    target.click()
            except Exception:
                page.wait_for_timeout(2000)
            if _looks_blocked(page):
                raise RuntimeError("blocked by anti-bot challenge after search")
            if page.url.rstrip("/") == homepage.rstrip("/"):
                raise RuntimeError(
                    f"search for '{query}' did not navigate to a team page "
                    "(suggestion click failed)"
                )

            # Collect UPCOMING fixtures from the page's "Upcoming Fixtures"
            # section (h2, with "Next Matches"/"Results" tabs). Scoping to
            # that section keeps form-guide tooltips and past results
            # elsewhere on the page from leaking in.
            # The section is SPA-rendered and may lazy-load: make sure the
            # "Next Matches" tab is active and scroll the section into view
            # before collecting, otherwise we race the render and find nothing.
            def _fixtures_section():
                try:
                    h2 = page.query_selector('h2:has-text("Upcoming Fixtures")')
                except Exception:
                    h2 = None
                if not h2:
                    return None
                try:
                    return h2.evaluate_handle(
                        "el => el.closest('section') || el.parentElement"
                    )
                except Exception:
                    return None

            try:
                page.wait_for_selector(
                    'h2:has-text("Upcoming Fixtures")', timeout=15000
                )
            except Exception:
                pass
            # Activate the "Next Matches" tab if the page exposes one.
            try:
                tab = page.query_selector('button:has-text("Next Matches")')
                if tab:
                    tab.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            # Scroll the section into view to force lazy rendering, then
            # wait until at least one fixture link carries a date (e.g. "10/Oct").
            section = _fixtures_section()
            if section:
                try:
                    section.evaluate("el => el.scrollIntoView({block: 'start'})")
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
            try:
                page.wait_for_function(
                    """() => {
                        const h2 = [...document.querySelectorAll('h2')]
                            .find(e => /upcoming fixtures/i.test(e.innerText || ''));
                        const root = h2 ? (h2.closest('section') || h2.parentElement) : document;
                        return [...root.querySelectorAll('a[href*="/h2h/"]')]
                            .some(a => /\\d{1,2}\\/[A-Za-z]{3}/.test(a.innerText || ''));
                    }""",
                    timeout=15000,
                )
            except Exception:
                pass
            section = _fixtures_section()
            try:
                if section:
                    links = section.query_selector_all('a[href*="/h2h/"]')
                else:
                    links = page.query_selector_all('a[href*="/h2h/"]')
            except Exception:
                links = page.query_selector_all('a[href*="/h2h/"]')
            seen = set()
            for a in links:
                try:
                    href = a.get_attribute("href") or ""
                    text = (a.inner_text() or "").strip()
                except Exception:
                    continue
                if not href or "/h2h/" not in href:
                    continue
                if not _is_upcoming_fixture_link(text):
                    continue
                url = href if href.startswith("http") else BASE + href
                url = url.split("#")[0]
                if url in seen:
                    continue
                seen.add(url)
                title, date = _clean_fixture_title(text)
                if date:
                    title = f"{title} ({date})"
                results.append({"title": title, "competition": "", "url": url})
                if len(results) >= 10:
                    break
            if not results:
                # Debug: count all h2h links and sample texts+hrefs from start/middle/end
                try:
                    all_links = page.query_selector_all('a[href*="/h2h/"]')
                    n_all = len(all_links)
                    def _info(a):
                        try:
                            t = (a.inner_text() or "").strip()[:60]
                            h = (a.get_attribute("href") or "")[:80]
                            return (t, h)
                        except Exception:
                            return ("?", "?")
                    idxs = list(range(0, min(6, n_all)))
                    if n_all > 12:
                        idxs += list(range(n_all//2 - 3, n_all//2 + 3))
                    if n_all > 6:
                        idxs += list(range(max(6, n_all-6), n_all))
                    samples = [(i,) + _info(all_links[i]) for i in idxs if i < n_all]
                except Exception:
                    n_all, samples = -1, []
                raise RuntimeError(
                    f"no upcoming fixtures found for '{query}' "
                    f"(landed on {page.url}; fixtures section found={section is not None}, "
                    f"section-scoped h2h links={len(links)}, samples={samples})"
                )
        finally:
            browser.close()
    return results


def _parse_float(s):
    try:
        return float(str(s).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def get_match_odds(match_url, timeout_ms=45000):
    """Scrape the 1X2 (Full Time) odds table from an OddsPortal match page."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, ctx = _new_context(p)
        try:
            page = ctx.new_page()
            page.goto(match_url, wait_until="domcontentloaded", timeout=timeout_ms)
            if _looks_blocked(page):
                raise RuntimeError("blocked by anti-bot challenge on match page")

            # The H2H page is a SPA — wait for the odds table to render.
            # 1X2 table headers: Bookmakers | 1 | X | 2 | Payout
            try:
                page.wait_for_function(
                    """() => [...document.querySelectorAll('table th')]
                        .map(th => th.innerText.trim()).join('|').includes('1|X|2')""",
                    timeout=20000,
                )
            except Exception:
                pass  # fall through to the parse attempt anyway

            title = page.title() or ""
            # Title is usually "TeamA - TeamB Betting Odds..." -> split teams
            home, away = "", ""
            m = re.match(r"\s*(.+?)\s*[-–]\s*(.+?)\s+(Betting|Odds)", title)
            if m:
                home, away = m.group(1).strip(), m.group(2).strip()

            # Find the 1X2 odds table: real <table> with th headers
            # Bookmakers | 1 | X | 2 | Payout. Odds live in
            # <a class="font-main text-xs underline"> inside the 1/X/2 <td>s.
            books = []
            for tbl in page.query_selector_all("table"):
                try:
                    headers = [(th.inner_text() or "").strip() for th in tbl.query_selector_all("th")]
                except Exception:
                    continue
                h = [x for x in headers if x]
                # must look like a 1X2 table
                if not ("1" in h and "X" in h and "2" in h):
                    continue
                try:
                    idx1, idxX, idx2 = h.index("1"), h.index("X"), h.index("2")
                except ValueError:
                    continue
                for tr in tbl.query_selector_all("tbody tr"):
                    try:
                        tds = tr.query_selector_all("td")
                    except Exception:
                        continue
                    if len(tds) <= max(idx1, idxX, idx2):
                        continue
                    # bookmaker name: first cell text, strip Review/Claim links noise
                    try:
                        name = (tds[0].inner_text() or "").strip().split("\n")[0].strip()
                    except Exception:
                        continue
                    if not name or len(name) > 40:
                        continue
                    odds = []
                    for i in (idx1, idxX, idx2):
                        try:
                            a = tds[i].query_selector("a")
                            txt = (a.inner_text() if a else tds[i].inner_text()) or ""
                            odds.append(_parse_float(txt.strip()))
                        except Exception:
                            odds.append(None)
                    if (
                        all(o and 1.01 <= o <= 500 for o in odds)
                        and not any(b[0].lower() == name.lower() for b in books)
                    ):
                        books.append([name, odds[0], odds[1], odds[2]])
                if books:
                    break  # got the 1X2 table, stop looking
            # Fallback: try a looser scan of the whole odds container
            if not books:
                raise RuntimeError("odds table not parsed (site layout may have changed)")

            # Competition + kickoff from the H2H page's UPCOMING MATCH section.
            # H2H pages show e.g. "England / Premier League" and "Sunday, 11 Oct 2026, 23:30".
            comp = ""
            kickoff = ""
            try:
                body_text = page.inner_text("body") or ""
            except Exception:
                body_text = ""
            try:
                # Find "UPCOMING MATCH" section and grab competition nearby
                um = re.search(r"UPCOMING MATCH\s*([^\n]{2,60})", body_text)
                if um:
                    comp = um.group(1).strip()
                    # clean up: take first line-ish, drop dates
                    comp = re.sub(r"\s{2,}", " ", comp)
            except Exception:
                pass
            if not comp:
                # fallback: breadcrumbs
                try:
                    crumbs = page.query_selector_all("nav a, [class*='breadcrumb'] a")
                    ctexts = [(c.inner_text() or "").strip() for c in crumbs]
                    ctexts = [t for t in ctexts if t and t.lower() not in ("home",)]
                    if len(ctexts) >= 2:
                        comp = " / ".join(ctexts[-2:])
                except Exception:
                    pass
            try:
                km = re.search(r"(\d{1,2} \w{3} \d{4}, \d{2}:\d{2})", body_text)
                if km:
                    kickoff = km.group(1)
            except Exception:
                pass

            return _build_snapshot(home, away, comp, kickoff, match_url, books)
        finally:
            browser.close()


def merge_books(book_lists):
    """Union of bookmaker odds from multiple regions, deduped by name
    (case-insensitive). A bookmaker's odds are the same worldwide; the
    region filter only hides books, so union = (nearly) all books."""
    seen = {}
    for books in book_lists:
        for b in books or []:
            try:
                name = str(b[0]).strip()
                o1, ox, o2 = float(b[1]), float(b[2]), float(b[3])
            except (IndexError, ValueError, TypeError):
                continue
            k = name.lower()
            if k and k not in seen:
                seen[k] = [name, round(o1, 2), round(ox, 2), round(o2, 2)]
    return list(seen.values())


def finalize_snapshot(home, away, comp, kickoff, match_url, books, src, note):
    n = len(books)
    if n == 0:
        raise RuntimeError("no bookmaker odds to build snapshot")
    avg1 = sum(b[1] for b in books) / n
    avgx = sum(b[2] for b in books) / n
    avg2 = sum(b[3] for b in books) / n
    i1, ix, i2 = 1 / avg1, 1 / avgx, 1 / avg2
    tot = i1 + ix + i2
    cons = [
        ["主勝", round(i1 / tot * 100, 1)],
        ["和局", round(ix / tot * 100, 1)],
        ["客勝", round(i2 / tot * 100, 1)],
    ]
    b1 = max(books, key=lambda b: b[1])
    bx = max(books, key=lambda b: b[2])
    b2 = max(books, key=lambda b: b[3])
    now_hkt = datetime.now(HKT).strftime("%Y年%-m月%-d日 %H:%M（香港時間）")
    return {
        "home": home or "主隊",
        "away": away or "客隊",
        "comp": comp,
        "kickoff": kickoff,
        "market": "1X2（全場）",
        "books": books,
        "cons": cons,
        "best": f"主勝 {b1[1]:.2f}（{b1[0]}）・ 和局 {bx[2]:.2f}（{bx[0]}）・ 客勝 {b2[3]:.2f}（{b2[0]}）",
        "snapped": now_hkt,
        "src": src,
        "note": note,
        "match_url": match_url,
    }


def _build_snapshot(home, away, comp, kickoff, match_url, books):
    n = len(books)
    return finalize_snapshot(
        home, away, comp, kickoff, match_url,
        [[b[0], round(b[1], 2), round(b[2], 2), round(b[3], 2)] for b in books],
        "OddsPortal（即時搜尋）",
        f"共 {n} 間莊家（按伺服器地區顯示）；只供參考，不構成交易建議。",
    )


if __name__ == "__main__":
    import json

    q = " ".join(sys.argv[1:]) or "liverpool manchester city"
    print("searching:", q, file=sys.stderr)
    ms = search_matches(q)
    print(json.dumps(ms, ensure_ascii=False, indent=2))
    if ms:
        print("scraping:", ms[0]["url"], file=sys.stderr)
        snap = get_match_odds(ms[0]["url"])
        print(json.dumps(snap, ensure_ascii=False, indent=2))
