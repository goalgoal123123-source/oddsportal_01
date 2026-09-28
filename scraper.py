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
    return browser, ctx


def _looks_blocked(page):
    txt = (page.title() + " " + (page.content()[:2000] or "")).lower()
    return any(
        k in txt
        for k in ["just a moment", "attention required", "cf-chl", "captcha", "are you a robot"]
    )


def search_matches(query, timeout_ms=30000):
    """Search OddsPortal for matches matching the keyword query.

    OddsPortal's search dropdown returns teams/players as <li> items
    (JS navigation, no hrefs) — so we click the top team result, land on
    the team page, and collect that team's upcoming match links.
    """
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser, ctx = _new_context(p)
        try:
            page = ctx.new_page()
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            if _looks_blocked(page):
                raise RuntimeError("blocked by anti-bot challenge on homepage")

            # OddsPortal search box (no <form>, no name attr)
            search_box = page.query_selector("input#search-input")
            if not search_box or not search_box.is_visible():
                raise RuntimeError("search input not found (site layout may have changed)")

            search_box.click()
            search_box.fill("")  # clear first
            search_box.type(query, delay=80)  # type like a human to trigger AJAX

            # Wait for the autocomplete dropdown (li items, NOT links).
            # If the full query yields nothing (e.g. "Liverpool Man City"),
            # fall back to the first word ("Liverpool").
            queries_to_try = [query]
            first_word = query.strip().split()[0] if query.strip() else ""
            if first_word and first_word.lower() != query.strip().lower():
                queries_to_try.append(first_word)
            dropdown_ok = False
            for qi, q in enumerate(queries_to_try):
                if qi > 0:
                    search_box.click()
                    search_box.fill("")
                    search_box.type(q, delay=80)
                try:
                    page.wait_for_selector(".dropdown-content li", timeout=12000)
                    dropdown_ok = True
                    break
                except Exception:
                    continue
            if not dropdown_ok:
                raise RuntimeError("search dropdown did not appear for query")
            page.wait_for_timeout(1500)  # let all items render

            items = page.query_selector_all(".dropdown-content li")
            if not items:
                raise RuntimeError("search returned no teams/players")
            # Click the top result -> team page (JS navigation)
            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                    items[0].click()
            except Exception:
                # navigation may have happened without the event firing
                page.wait_for_timeout(3000)
            page.wait_for_timeout(2000)
            if _looks_blocked(page):
                raise RuntimeError("blocked by anti-bot challenge on team page")

            # On the team page, collect fixture links.
            # Fixtures use /football/h2h/<home>-<id>/<away>-<id>/#<matchHash>
            links = page.query_selector_all('a[href^="/football/h2h/"], a[href^="/tennis/h2h/"], a[href^="/basketball/h2h/"]')
            seen = set()
            for a in links:
                try:
                    href = a.get_attribute("href") or ""
                    text = (a.inner_text() or "").strip()
                except Exception:
                    continue
                if not href or not text:
                    continue
                # Strip the #hash for a canonical URL, keep it as match id hint
                url = href if href.startswith("http") else BASE + href
                url = url.split("#")[0]
                if url in seen:
                    continue
                seen.add(url)
                # Tidy the title: collapse whitespace
                title = re.sub(r"\s+", " ", text)
                results.append({"title": title, "competition": "", "url": url})
                if len(results) >= 10:
                    break
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
            page.wait_for_timeout(4000)
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
            page.wait_for_timeout(2000)

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
