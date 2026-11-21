"""Skroutz scraper: any category, deals page, or search -> CSV / Excel.

Examples:
  python skroutz_scraper.py https://www.skroutz.gr/c/25/laptop.html --pages 5
  python skroutz_scraper.py https://www.skroutz.gr/deals/c/1269/technology --top 50 --details -o deals.xlsx
  python skroutz_scraper.py --search "gaming mouse" --sort price
  python skroutz_scraper.py https://www.skroutz.gr/deals/c/1269/technology --min-discount 30 --sort discount

Plain HTTP (no browser needed). Polite: one request at a time, delay between requests.
Partial results are saved if it crashes or you press Ctrl+C.
Every run saves prices to skroutz_prices.db, so each product gets compared with its own history
(the "Verdict" column flags all-time lows and discounts whose "old price" was never really charged).
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse, quote_plus

import random

from scrapling.fetchers import FetcherSession
from scrapling.parser import Selector

BASE = "https://www.skroutz.gr"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]


FETCH_JSON_JS = """const done = arguments[arguments.length - 1];
fetch(arguments[0], {headers: {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}})
  .then(r => r.text().then(t => done([r.status, t]))).catch(e => done([0, String(e)]));"""


class BrowserSession:
    """Headless Firefox via Selenium, same .get(url) -> page interface as FetcherSession.
    Each request gets a fresh browser with a random user agent and, if SKROUTZ_PROXIES is set
    (comma-separated, e.g. http://host:port), a random proxy. Images, CSS and JavaScript are off,
    so pages load fast and the HTML is what the server sent.
    Firefox must be installed; Selenium fetches geckodriver itself (or set GECKODRIVER to its path)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _driver(self, javascript=False):
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service

        options = Options()
        options.add_argument("--headless")
        options.set_preference("permissions.default.image", 2)
        options.set_preference("permissions.default.stylesheet", 2)
        options.set_preference("javascript.enabled", javascript)
        options.set_preference("general.useragent.override", random.choice(USER_AGENTS))
        proxies = [p.strip() for p in os.environ.get("SKROUTZ_PROXIES", "").split(",") if p.strip()]
        if proxies:
            proxy = urlparse(random.choice(proxies))
            options.set_preference("network.proxy.type", 1)  # manual; Firefox ignores Chrome's --proxy-server
            for scheme in ("http", "ssl"):
                options.set_preference(f"network.proxy.{scheme}", proxy.hostname)
                options.set_preference(f"network.proxy.{scheme}_port", proxy.port or 80)
        # Selenium sometimes can't find Firefox on Windows (SessionNotCreatedException), so point at it
        binary = os.environ.get("FIREFOX_BINARY") or r"C:\Program Files\Mozilla Firefox\firefox.exe"
        if os.path.exists(binary):
            options.binary_location = binary
        # ponytail: a new Firefox per request (~2 s) for a fresh identity each time; reuse one driver if speed matters more
        driver = webdriver.Firefox(service=Service(os.environ.get("GECKODRIVER")), options=options)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        return driver

    def get(self, url):
        driver = self._driver()
        try:
            driver.get(url)
            page = Selector(driver.page_source, url=driver.current_url)
        finally:
            driver.quit()
        page.status = 200  # Selenium can't see HTTP status; a block page simply has no product cards
        return page

    def price_graph(self, url):
        """A product's price-history JSON. Skroutz only answers it from inside the product page with
        JavaScript on, so this opens the page and asks from there. Raises PermissionError when refused."""
        from selenium.webdriver.common.by import By

        driver = self._driver(javascript=True)
        try:
            driver.get(url)
            path = driver.find_element(By.CSS_SELECTOR, f"[{CHART_ATTR}]").get_attribute(CHART_ATTR)
            status, text = driver.execute_async_script(FETCH_JSON_JS, path)
        finally:
            driver.quit()
        if status in (403, 429):
            raise PermissionError(f"HTTP {status}")
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return json.loads(text)


def open_session(fetcher=None):
    """'browser' (default): Selenium + Firefox. 'http': plain HTTP via Scrapling, no browser needed.
    Pick with the SKROUTZ_FETCHER environment variable or the --fetcher option."""
    fetcher = fetcher or os.environ.get("SKROUTZ_FETCHER", "browser")
    if fetcher == "browser":
        try:
            import selenium  # noqa: F401
            return BrowserSession()
        except ImportError:
            print("Selenium isn't installed, falling back to plain HTTP (pip install selenium)")
    return FetcherSession(impersonate="chrome", retries=3)

# Windows consoles can't show every character (e.g. Skroutz's non-breaking hyphen); a progress print must never crash a scrape
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")
def data_dir():
    """Next to the scripts; for the .exe, %APPDATA%/SkroutzTools (the .exe's own folder is wiped after each run)."""
    if getattr(sys, "frozen", False):
        folder = Path(os.environ.get("APPDATA") or Path.home()) / "SkroutzTools"
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    return Path(__file__).parent


DB_PATH = data_dir() / "skroutz_prices.db"
HISTORY_COLS = ["Verdict", "vs Avg %", "3-Month Avg", "3-Month Low", "Days Tracked"]
HISTORY_DAYS = 90  # verdicts compare with the last 3 months
CHART_ATTR = "data-sku-page--price-history-chart-url-value"  # product page -> its price-history JSON URL


def history_verdict(price, old_price, past):
    """Compare today's price with the prices recorded on earlier days of the last 3 months (one per day)."""
    if not past:
        return {"Verdict": "New", "vs Avg %": None, "3-Month Avg": None, "3-Month Low": None, "Days Tracked": 1}
    avg, low = mean(past), min(past)
    if price < low:
        verdict = "3-month low"
    elif price < avg * 0.95:
        verdict = "Below average"
    elif old_price and len(past) >= 3 and old_price > max(past) * 1.02:
        verdict = "Fake discount?"  # "old price" higher than anything it actually sold for in 3 months
    elif price > avg * 1.05:
        verdict = "Above average"
    else:
        verdict = "Normal"
    return {
        "Verdict": verdict,
        "vs Avg %": round((price - avg) / avg * 100, 1),
        "3-Month Avg": round(avg, 2),
        "3-Month Low": low,
        "Days Tracked": len(past) + 1,
    }


def open_db(db_path=DB_PATH):
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE IF NOT EXISTS prices (
        sku TEXT, day TEXT, price REAL, old_price REAL, title TEXT, PRIMARY KEY (sku, day))""")
    db.execute("CREATE TABLE IF NOT EXISTS photos (sku TEXT PRIMARY KEY, url TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS history_fetched (sku TEXT PRIMARY KEY, day TEXT)")
    return closing(db)


def record_and_annotate(rows, db_path=DB_PATH, today=None):
    """Save today's prices + any known photos, then add history columns and remembered photos to each row."""
    today = today or date.today().isoformat()
    with open_db(db_path) as db:
        # One row per product per day; a later run the same day replaces it
        db.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?)",
                       [(r["SKU"], today, r["Price"], r["Old Price"], r["Title"]) for r in rows])
        db.executemany("INSERT OR REPLACE INTO photos VALUES (?, ?)", [(r["SKU"], r["Photo"]) for r in rows if r.get("Photo")])
        db.commit()
        for r in rows:
            since = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).isoformat()
            past = [p for (p,) in db.execute("SELECT price FROM prices WHERE sku = ? AND day < ? AND day >= ?",
                                             (r["SKU"], today, since))]
            r.update(history_verdict(r["Price"], r["Old Price"], past))
            if not r.get("Photo"):
                saved = db.execute("SELECT url FROM photos WHERE sku = ?", (r["SKU"],)).fetchone()
                r["Photo"] = saved[0] if saved else None
    return rows


def fill_photos(session, rows, delay, limit=40, db_path=DB_PATH):
    """Open product pages for photos the listing didn't include. Each photo is remembered, so it's fetched once ever."""
    missing = [r for r in rows if not r.get("Photo")][:limit]
    if missing:
        print(f"Fetching {len(missing)} photos from product pages (once per product, then remembered)...")
    with open_db(db_path) as db:
        for r in missing:
            time.sleep(delay)
            try:
                r["Photo"] = thumb(session.get(r["URL"]).css("meta[property='og:image']::attr(content)").get())
            except Exception as e:  # a missing photo shouldn't kill the run
                print(f"  photo failed for {r['URL']}: {e}")
                continue
            if r["Photo"]:
                db.execute("INSERT OR REPLACE INTO photos VALUES (?, ?)", (r["SKU"], r["Photo"]))
                db.commit()


def history_points(graph, today):
    """Skroutz price-graph JSON -> {day: lowest price} for the last 3 months, before today."""
    since = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).isoformat()
    points = {}
    for series in graph.get("min_price", {}).get("graphData", {}).values():  # "1_months", "3_months", ...
        for v in series.get("values", []):
            # Timestamps are midnight Greek time; +12 h lands mid-day, so the UTC date is the Greek date
            day = datetime.fromtimestamp(v["timestamp"] + 43200, timezone.utc).date().isoformat()
            if since <= day < today and v.get("value"):
                points.setdefault(day, v["value"])
    return points


def fill_history(session, rows, delay, limit=40, db_path=DB_PATH, today=None):
    """Load Skroutz's own daily lowest-price history (last 3 months) into the price database,
    so verdicts work from the first run. Remembered for a week per product. Browser fetcher only."""
    if not hasattr(session, "price_graph"):
        return
    today = today or date.today().isoformat()
    week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    with open_db(db_path) as db:
        fresh = {sku for (sku,) in db.execute("SELECT sku FROM history_fetched WHERE day > ?", (week_ago,))}
        todo = [r for r in rows if r["SKU"] not in fresh][:limit]
        if todo:
            print(f"Fetching 3-month price history for {len(todo)} products from Skroutz (then remembered for a week)...")
        for i, r in enumerate(todo, 1):
            time.sleep(delay)
            try:
                points = history_points(session.price_graph(r["URL"]), today)
            except PermissionError:  # Skroutz/Cloudflare is limiting these requests: back off for this run
                print("  Skroutz is refusing price-history requests right now; the rest use your own history. Try again later.")
                break
            except Exception as e:  # no history shouldn't kill the run
                print(f"  history failed for {r['URL']}: {e}")
                continue
            # Our own daily records win over Skroutz's for the same day
            db.executemany("INSERT OR IGNORE INTO prices VALUES (?, ?, ?, NULL, ?)",
                           [(r["SKU"], day, price, r["Title"]) for day, price in points.items()])
            db.execute("INSERT OR REPLACE INTO history_fetched VALUES (?, ?)", (r["SKU"], today))
            db.commit()
            print(f"  history {i}/{len(todo)}: {len(points)} days, {r['Title'][:50]}")


def thumb(url):
    """Any Skroutz product image URL -> its small 'medium_' version (~3 KB instead of ~100 KB)."""
    if not url:
        return None
    return re.sub(r"/(?:(?:xlarge|large|medium|small)_)?(\d{14}_[^/]+)$", r"/medium_\1", url)


def jsonld_photos(page):
    """SKU -> photo from the page's JSON-LD product list (category and search pages have it; deals pages don't)."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            m = re.search(r"/s/(\d+)/", str(o.get("url", "")))
            img = o.get("image")
            if m and img:
                found[m.group(1)] = thumb(img[0] if isinstance(img, list) else img)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for script in page.css("script[type='application/ld+json']::text").getall():
        try:
            walk(json.loads(script))
        except ValueError:
            pass
    return found


def parse_price(text):
    """'3.448,89 €' -> 3448.89 (Greek format: '.' thousands, ',' decimals)."""
    m = re.search(r"[\d.]+,\d+|\d+", (text or "").replace("\xa0", ""))
    return float(m.group().replace(".", "").replace(",", ".")) if m else None


def with_params(url, **params):
    p = urlparse(url)
    q = parse_qs(p.query)
    q.update({k: [str(v)] for k, v in params.items()})
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def parse_card(card):
    href = card.css("a.sku-card-title-link::attr(href)").get() or card.css("a.js-sku-link::attr(href)").get() or ""
    rating_title = card.css("a.rating.stars::attr(title)").get() or ""  # "5,0 αστέρια από 42 χρήστες"
    rating = re.search(r"(\d+[.,]\d+)", rating_title or card.css("span[data-testid='star-rating-value']::text").get() or "")
    reviews = re.search(r"(\d+)\s*(?:χρήστ|κριτικ|review|member|user)", rating_title)  # Greek or English site
    # The current price is the link; on deals the old price sits in <strike> before it
    price = parse_price(card.css("div[data-testid='normal-price-container'] > a::text").get())
    old = parse_price(card.css("div[data-testid='normal-price-container'] strike::text").get())
    return {
        "SKU": card.attrib["data-skuid"],
        "Title": card.css("a.sku-card-title-link h2::text").get() or card.css("a.js-sku-link::attr(title)").get(),
        "Photo": thumb(card.css("a.pic img::attr(src)").get()),  # only the first ~7 cards have it; see jsonld_photos
        "Category": card.css("a.category-name::text").get(),
        "Price": price,
        "Old Price": old,
        "Discount %": round((old - price) / old * 100, 1) if old and price and old > price else 0,
        "Rating": float(rating.group(1).replace(",", ".")) if rating else None,
        "Reviews": int(reviews.group(1)) if reviews else 0,
        "Specs": card.css("p.specs::attr(title)").get() or card.css("p.specs::text").get(),  # title holds the untruncated specs
        "Sponsored": "sponsored=" in href,
        "URL": urljoin(BASE, href.split("?")[0]),
    }


def crawl_listing(session, url, max_pages, delay, rows):
    """Appends unique products to `rows` as it goes, so partial results survive a crash."""
    seen = {r["SKU"] for r in rows}
    for n in range(1, max_pages + 1):
        page = session.get(url)
        if page.status != 200:
            print(f"Page {n}: HTTP {page.status}, stopping")
            break
        cards = page.css("li.cf.card[data-skuid]")
        photos = jsonld_photos(page)
        new = 0
        for card in cards:
            if card.attrib["data-skuid"] in seen:  # sponsored cards repeat organic ones
                continue
            seen.add(card.attrib["data-skuid"])
            row = parse_card(card)
            row["Photo"] = row["Photo"] or photos.get(row["SKU"])
            if row["Price"] is not None:
                rows.append(row)
                new += 1
        print(f"Page {n}: {len(cards)} cards, {new} new, {len(rows)} total")
        next_href = page.css(".paginator a.next::attr(href)").get()
        if not cards or not new or not next_href:
            break
        url = urljoin(BASE, next_href)
        time.sleep(delay)


def add_details(session, rows, delay):
    """Visit each product page for brand + full spec table."""
    for i, row in enumerate(rows, 1):
        time.sleep(delay)
        try:
            page = session.get(row["URL"])
            row["Brand"] = page.css("div.manufacturer a::text").get()
            row["Photo"] = row.get("Photo") or thumb(page.css("meta[property='og:image']::attr(content)").get())
            for dl in page.css("div.specs-container dl"):
                for dt, dd in zip(dl.css("dt"), dl.css("dd")):
                    row.setdefault(dt.get_all_text(strip=True), dd.get_all_text(separator=" ", strip=True))
        except Exception as e:  # one bad product shouldn't kill the run
            print(f"  details failed for {row['URL']}: {e}")
        if not row.get("Brand") and row["Title"]:
            row["Brand"] = row["Title"].split()[0]
        print(f"  details {i}/{len(rows)}: {row['Title'][:60]}")


def rank(rows, sort, top, min_discount=0):
    if min_discount:
        rows = [r for r in rows if r["Discount %"] >= min_discount]
    keys = {
        "discount": lambda r: (-r["Discount %"], r["Price"]),
        "history": lambda r: (r.get("vs Avg %") is None, r.get("vs Avg %") or 0),  # furthest below its 3-month average first
        "reviews": lambda r: (-r["Reviews"], -(r["Rating"] or 0), r["Price"]),  # most reviewed, best, cheapest
        "price": lambda r: r["Price"],
        "rating": lambda r: (-(r["Rating"] or 0), -r["Reviews"], r["Price"]),
    }
    if sort in keys:
        rows.sort(key=keys[sort])
    return rows[:top] if top else rows


def save(rows, path):
    if not rows:
        print("Nothing to save")
        return
    first = ["Brand", "Title", "Photo", "Category", "Price", "Old Price", "Discount %", *HISTORY_COLS, "Rating", "Reviews", "Specs", "Sponsored", "SKU", "URL"]
    extra = list(dict.fromkeys(k for r in rows for k in r if k not in first))
    cols = [c for c in first if any(c in r for r in rows)] + extra
    if path.lower().endswith(".xlsx"):
        import pandas as pd
        pd.DataFrame(rows, columns=cols).to_excel(path, index=False)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel shows Greek correctly
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    print(f"Saved {len(rows)} products to {path}")


def main():
    ap = argparse.ArgumentParser(description="Scrape Skroutz listings to CSV/Excel")
    ap.add_argument("url", nargs="?", help="Category or deals URL, e.g. https://www.skroutz.gr/c/25/laptop.html")
    ap.add_argument("--search", help="Search phrase instead of a URL")
    ap.add_argument("--pages", type=int, default=10, help="Max listing pages (default 10)")
    ap.add_argument("--sort", choices=["none", "price", "reviews", "rating", "discount", "history"], default="none",
                    help="reviews = most reviews, then best rating, then cheapest; discount = biggest %% off; "
                         "history = furthest below its 3-month average")
    ap.add_argument("--min-discount", type=float, default=0, help="Keep only items at least this %% off the old price")
    ap.add_argument("--only-discounted", action="store_true", help="Keep only items that show a discount")
    ap.add_argument("--top", type=int, help="Keep only the first N after sorting")
    ap.add_argument("--details", action="store_true", help="Also open each product page for brand + full specs")
    ap.add_argument("--delay", type=float, default=2.0, help="Seconds between requests (default 2)")
    ap.add_argument("--fetcher", choices=["browser", "http"], help="browser = headless Firefox (default), http = no browser")
    ap.add_argument("-o", "--out", default="skroutz.csv", help=".csv or .xlsx")
    args = ap.parse_args()

    if not (args.url or args.search):
        ap.error("give a URL or --search")
    url = f"{BASE}/search?keyphrase={quote_plus(args.search)}" if args.search else args.url
    if args.sort == "price":
        url = with_params(url, order_by="pricevat", order_dir="asc")  # let the site sort, so cheapest come first

    rows = []
    try:
        with open_session(args.fetcher) as session:
            crawl_listing(session, url, args.pages, args.delay, rows)
            record_and_annotate(rows)
            min_discount = max(args.min_discount, 0.1 if args.only_discounted else 0)
            rows = rank(rows, args.sort, args.top, min_discount)
            fill_history(session, rows, args.delay)
            record_and_annotate(rows)
            if args.details:
                add_details(session, rows, args.delay)
    except KeyboardInterrupt:
        print("\nStopped, saving what we have...")
    finally:
        save(rows, args.out)


if __name__ == "__main__":
    assert parse_price("3.448,89 €") == 3448.89 and parse_price("0,27 €") == 0.27 and parse_price("899 €") == 899
    assert parse_price(None) is None
    assert history_verdict(80, None, [100, 100, 95])["Verdict"] == "3-month low"
    assert history_verdict(100, 150, [100, 101, 99])["Verdict"] == "Fake discount?"
    assert history_verdict(100, None, [])["Verdict"] == "New"
    assert history_verdict(100, None, [90, 120])["3-Month Avg"] == 105  # average, not median
    g = {"min_price": {"graphData": {"1_months": {"values": [{"timestamp": 1792616400, "value": 28.35}, {"timestamp": 1792702800, "value": 29}]},
                                     "3_months": {"values": [{"timestamp": 1788210000, "value": 31}, {"timestamp": 1779400000, "value": 99}]}}}}
    assert history_points(g, "2026-11-21") == {"2026-10-22": 28.35, "2026-10-23": 29, "2026-09-01": 31}, history_points(g, "2026-11-21")  # 99 is >90 days old
    assert thumb("https://a.scdn.gr/images/sku_main_images/1/2/xlarge_20220913153729_x.jpeg").endswith("/medium_20220913153729_x.jpeg")
    assert thumb("https://a.scdn.gr/images/sku_main_images/1/2/20220913153729_x.jpeg").endswith("/medium_20220913153729_x.jpeg")
    main()
