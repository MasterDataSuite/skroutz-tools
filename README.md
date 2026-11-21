# Skroutz Tools

Two small local web apps for finding good buys on [Skroutz.gr](https://www.skroutz.gr), the Greek price-comparison site:

- **Deal Finder**: scrape any category, deals page or search. Filter by % off, pick a category from a dropdown, and see photos. Each product is compared with **its own last 3 months of prices**: you see its **3-month average**, and it flags 3-month lows and **fake discounts**, where a shop shows a crossed-out "old price" that was never actually charged.
- **Laptop Picker**: ranks laptops for your use case (Gaming, Work/Study, Creative, Travel, Balanced). CPUs and GPUs are scored with real **PassMark** benchmarks. Screen (resolution, refresh rate, OLED/IPS), RAM, storage and weight are scored too, plus a value score (performance per euro).

Everything runs on your own PC. The pages are only reachable from your machine.

## Download (Windows, no Python needed)

1. Go to the **[latest release](https://github.com/MasterDataSuite/skroutz-tools/releases/latest)** and download `SkroutzTools.exe`.
2. Install [Firefox](https://www.mozilla.org/firefox/) if you don't have it; the app uses it, hidden, to load pages.
3. Double-click it. A console window opens (it shows progress, and closing it stops the app), and your browser opens the Deal Finder.
4. Windows may say **"Windows protected your PC"**, because the app isn't code-signed. Click **More info → Run anyway**.

Your price history and benchmark data are kept in `%APPDATA%\SkroutzTools`, so they survive updates. To update, download the new `.exe` and replace the old one.

## Run from source

Requires **Python 3.10+**.

**Windows:** double-click `Start.bat`. The first run installs the requirements.

**Any OS:**
```bash
pip install -r requirements.txt
python app.py
```
Your browser opens the Deal Finder at http://127.0.0.1:8765. The Laptop Picker is at http://127.0.0.1:8766, and each page links to the other.

You can also run each tool on its own: `python skroutz_web.py` or `python laptop_picker.py`.

## Command line

The scraper also works without the web page:
```bash
python skroutz_scraper.py https://www.skroutz.gr/c/25/laptop.html --pages 5
python skroutz_scraper.py https://www.skroutz.gr/deals/c/1269/technology --min-discount 30 --sort discount -o deals.xlsx
python skroutz_scraper.py --search "gaming mouse" --sort price --only-discounted
```
Run `python skroutz_scraper.py --help` for all options.

## How it works

| File | What it does |
|---|---|
| `skroutz_scraper.py` | Core scraper, price history (SQLite), photos, CSV/Excel export |
| `skroutz_web.py` | Deal Finder web page |
| `laptop_picker.py` | Laptop Picker: PassMark matching and scoring |
| `app.py` | Starts both pages together |

- Pages are fetched with **headless Firefox** through Selenium. Each request opens a fresh browser with a random user agent, with images, CSS and JavaScript turned off. Firefox must be installed; Selenium downloads geckodriver itself, or you can set `GECKODRIVER` to its path (and `FIREFOX_BINARY` if Firefox isn't in the default place).
- **Proxies (optional):** set `SKROUTZ_PROXIES` to a comma-separated list such as `http://host1:8080,http://host2:8080`, and each request uses a random one.
- **No browser?** Set `SKROUTZ_FETCHER=http`, or pass `--fetcher http` on the command line, to fetch with plain HTTP using [Scrapling](https://github.com/D4Vinci/Scrapling). If Selenium isn't installed, the tools fall back to this automatically. Skroutz sometimes blocks plain HTTP with a 403.
- **Price history:** `skroutz_prices.db` is created next to the scripts. For the products a page shows (up to 40), the tools also load **Skroutz's own daily lowest-price history** for the last 3 months, so verdicts work from the first run. This takes a few seconds per product, and each product is remembered for a week. Each run also adds today's prices.
- **Verdicts**, all against the last 3 months: **3-month low**; **Below average** / **Above average** (more than 5% off the 3-month average); **Fake discount?**, when the crossed-out "old price" is more than 2% above anything it actually sold for.
- Skroutz sometimes refuses price-history requests (HTTP 403). The tools then stop asking for the rest of that run and fall back to your own saved history; try again later. The plain-HTTP fetcher doesn't load Skroutz's history.
- **Benchmarks:** the PassMark CPU and GPU lists are downloaded once and refreshed every 30 days (`passmark_cache.json`). PassMark has no scores for Apple GPUs, so those, and any other unrecognised GPU, get a conservative estimate that is labelled as such.
- **Laptop specs:** full spec pages are fetched only for the most promising laptops and remembered, so later runs are faster.

## Be polite

- **Pace:** the tools make one request at a time, with a delay between them.
- **robots.txt:** they only visit pages Skroutz's robots.txt allows (listings and product pages).
- **Personal use only:** check the sites' terms before running anything at scale.
- **PassMark data:** it is © PassMark Software and is not included in this repository. Each user downloads it for their own personal use.

## Building the .exe

Build in a clean virtual environment, so unrelated packages don't get bundled:
```bash
python -m venv .venv
.venv\Scripts\pip install "scrapling[fetchers]>=0.5.1" selenium pyinstaller
.venv\Scripts\python -m PyInstaller --onefile --name SkroutzTools --exclude-module playwright --exclude-module tkinter --collect-data apify_fingerprint_datapoints --collect-data browserforge --collect-data scrapling --collect-all selenium app.py
```
The result is `dist\SkroutzTools.exe`, about 36 MB. It includes the Firefox fetcher, so Firefox must be installed on the PC; without Firefox, run it with `SKROUTZ_FETCHER=http`. `--collect-all selenium` matters: without it, Selenium's Firefox driver is left out of the .exe. Ports can be changed with the `DEAL_FINDER_PORT` and `LAPTOP_PICKER_PORT` environment variables.

Scrapers break when sites change their HTML. If results suddenly come back empty, the CSS selectors in `skroutz_scraper.py` probably need updating.
