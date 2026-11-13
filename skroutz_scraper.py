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
from datetime import date
from pathlib import Path
from statistics import median
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse, quote_plus

from scrapling.fetchers import FetcherSession

BASE = "https://www.skroutz.gr"

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
HISTORY_COLS = ["Verdict", "vs Usual %", "Usual Price", "Lowest Before", "Days Tracked"]


def history_verdict(price, old_price, past):
    """Compare today's price with the prices recorded on earlier days (one per day)."""
    if not past:
        return {"Verdict": "New", "vs Usual %": None, "Usual Price": None, "Lowest Before": None, "Days Tracked": 1}
    usual, low = median(past), min(past)
    if price < low:
        verdict = "All-time low"
    elif price < usual * 0.95:
        verdict = "Below usual"
    elif old_price and len(past) >= 3 and old_price > max(past) * 1.02:
        verdict = "Fake discount?"  # "old price" higher than anything we've actually seen it sell for
    elif price > usual * 1.05:
        verdict = "Above usual"
    else:
        verdict = "Normal"
    return {
        "Verdict": verdict,
        "vs Usual %": round((price - usual) / usual * 100, 1),
        "Usual Price": round(usual, 2),
        "Lowest Before": low,
        "Days Tracked": len(past) + 1,
    }


def open_db(db_path=DB_PATH):
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE IF NOT EXISTS prices (
        sku TEXT, day TEXT, price REAL, old_price REAL, title TEXT, PRIMARY KEY (sku, day))""")
    db.execute("CREATE TABLE IF NOT EXISTS photos (sku TEXT PRIMARY KEY, url TEXT)")
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
            past = [p for (p,) in db.execute("SELECT price FROM prices WHERE sku = ? AND day < ?", (r["SKU"], today))]
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
    rating = re.search(r"(\d+[.,]\d+)", rating_title)
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
        "Specs": card.css("p.specs::text").get(),
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
        "history": lambda r: (r.get("vs Usual %") is None, r.get("vs Usual %") or 0),  # furthest below usual first
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
                         "history = furthest below its own usual price")
    ap.add_argument("--min-discount", type=float, default=0, help="Keep only items at least this %% off the old price")
    ap.add_argument("--only-discounted", action="store_true", help="Keep only items that show a discount")
    ap.add_argument("--top", type=int, help="Keep only the first N after sorting")
    ap.add_argument("--details", action="store_true", help="Also open each product page for brand + full specs")
    ap.add_argument("--delay", type=float, default=2.0, help="Seconds between requests (default 2)")
    ap.add_argument("-o", "--out", default="skroutz.csv", help=".csv or .xlsx")
    args = ap.parse_args()

    if not (args.url or args.search):
        ap.error("give a URL or --search")
    url = f"{BASE}/search?keyphrase={quote_plus(args.search)}" if args.search else args.url
    if args.sort == "price":
        url = with_params(url, order_by="pricevat", order_dir="asc")  # let the site sort, so cheapest come first

    rows = []
    try:
        with FetcherSession(impersonate="chrome", retries=3) as session:
            crawl_listing(session, url, args.pages, args.delay, rows)
            record_and_annotate(rows)
            min_discount = max(args.min_discount, 0.1 if args.only_discounted else 0)
            rows = rank(rows, args.sort, args.top, min_discount)
            if args.details:
                add_details(session, rows, args.delay)
    except KeyboardInterrupt:
        print("\nStopped, saving what we have...")
    finally:
        save(rows, args.out)


if __name__ == "__main__":
    assert parse_price("3.448,89 €") == 3448.89 and parse_price("0,27 €") == 0.27 and parse_price("899 €") == 899
    assert parse_price(None) is None
    assert history_verdict(80, None, [100, 100, 95])["Verdict"] == "All-time low"
    assert history_verdict(100, 150, [100, 101, 99])["Verdict"] == "Fake discount?"
    assert history_verdict(100, None, [])["Verdict"] == "New"
    assert thumb("https://a.scdn.gr/images/sku_main_images/1/2/xlarge_20220913153729_x.jpeg").endswith("/medium_20220913153729_x.jpeg")
    assert thumb("https://a.scdn.gr/images/sku_main_images/1/2/20220913153729_x.jpeg").endswith("/medium_20220913153729_x.jpeg")
    main()
