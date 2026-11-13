"""Browser UI for skroutz_scraper.py.

Run:  python skroutz_web.py   -> opens http://127.0.0.1:8765 in your browser.
Only reachable from this PC. Scraping logic lives in skroutz_scraper.py.
"""
import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from scrapling.fetchers import Fetcher, FetcherSession

from skroutz_scraper import BASE, add_details, crawl_listing, fill_photos, rank, record_and_annotate, with_params

PORT = int(os.environ.get("DEAL_FINDER_PORT", 8765))

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deal Finder</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#667085;--line:#e4e7ec;--accent:#f68b24;--accent-text:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--text:#e8eaed;--muted:#9aa1ad;--line:#2d313a;--accent:#f68b24;--accent-text:#111}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif}
main{margin:0 auto;padding:24px 16px}
form{max-width:1200px}
h1{font-size:22px;margin:0 0 16px}
.nav{margin:0 0 12px;color:var(--muted)}
form{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
label.wide{grid-column:1/-1}
label.check{flex-direction:row;align-items:center;gap:8px;font-size:14px;color:var(--text)}
input,select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)}
button{font:inherit;font-weight:600;padding:9px 16px;border:0;border-radius:6px;background:var(--accent);color:var(--accent-text);cursor:pointer}
button:disabled{opacity:.5;cursor:wait}
button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
#bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:16px 0 8px}
#status{color:var(--muted)}
#filter{flex:1;min-width:180px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;white-space:nowrap;user-select:none}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
s{color:var(--muted)}
a{color:var(--accent)}
.verdict{white-space:nowrap;font-size:12px;padding:1px 6px;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.verdict.good{color:#0a7d3c;border-color:#0a7d3c;font-weight:600}
.verdict.bad{color:#c4320a;border-color:#c4320a}
@media (prefers-color-scheme:dark){.verdict.good{color:#4ade80;border-color:#4ade80}.verdict.bad{color:#fb7c5c;border-color:#fb7c5c}}
td.specs{min-width:320px}
td.photo{padding:4px 8px}
td.photo img{width:64px;height:64px;object-fit:contain;background:#fff;border-radius:6px;display:block}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:6px}
</style></head><body><main>
<nav class="nav"><b>Deal Finder</b> · <a href="http://127.0.0.1:8766/">Laptop Picker</a></nav>
<h1>Deal Finder</h1>
<form id="f">
  <label class="wide">Deals category <select id="cat"><option value="">Loading categories…</option></select></label>
  <label class="wide">…or any category / deals URL <input name="url" value="https://www.skroutz.gr/deals/c/1269/technology"></label>
  <label class="wide">…or search <input name="search" placeholder="gaming mouse"></label>
  <label>Max pages <input name="pages" type="number" min="1" max="50" value="3"></label>
  <label>Sort <select name="sort">
    <option value="discount">Biggest discount</option>
    <option value="history">Best vs its usual price</option>
    <option value="none">Site order</option><option value="price">Cheapest first</option>
    <option value="reviews">Most reviews → rating → price</option><option value="rating">Best rating</option></select></label>
  <label>Min % off <input name="min_discount" type="number" min="0" max="99" placeholder="e.g. 30"></label>
  <label>Show top N items <input name="top" type="number" min="1" value="20" placeholder="all"></label>
  <label class="check"><input name="only_discounted" type="checkbox" checked> Display only discounted items</label>
  <label class="check"><input name="details" type="checkbox"> Brand + full specs (slower)</label>
  <button id="go">Scrape</button>
</form>
<div id="bar"><span id="status">Enter a URL or a search, then Scrape. Each page takes about 2 seconds.</span>
  <input id="filter" placeholder="Filter results…" hidden><button class="ghost" id="dl" hidden>Download CSV</button></div>
<div class="wrap" hidden id="wrap"><table><thead id="th"></thead><tbody id="tb"></tbody></table></div>
</main><script>
let rows = [], cols = [], sortKey = null, asc = true, scrapeInfo = "";
const $ = id => document.getElementById(id);
const NUM = new Set(["Price", "Old Price", "Discount %", "vs Usual %", "Usual Price", "Lowest Before", "Days Tracked", "Rating", "Reviews"]);
const EURO = new Set(["Price", "Old Price", "Usual Price", "Lowest Before"]);
const fmt = (k, v) => v == null ? "" : EURO.has(k) ? v.toFixed(2).replace(".", ",") + " €"
  : k === "Discount %" ? (v ? "-" + v + "%" : "")
  : k === "vs Usual %" ? (v > 0 ? "+" : "") + v + "%" : String(v);
const VERDICT = {"All-time low": "good", "Below usual": "good", "Fake discount?": "bad", "Above usual": "bad"};

$("f").onsubmit = async e => {
  e.preventDefault();
  const q = new URLSearchParams(new FormData(e.target));
  if (!q.get("url") && !q.get("search")) { $("status").textContent = "Enter a URL or a search."; return; }
  $("go").disabled = true;
  const t0 = Date.now(), tick = setInterval(() => $("status").textContent = `Scraping… ${Math.round((Date.now()-t0)/1000)}s`, 500);
  try {
    const r = await fetch("/scrape?" + q);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error);
    rows = data.rows; sortKey = null;
    const first = ["Brand","Title","Photo","Category","Price","Old Price","Discount %","Verdict","vs Usual %","Usual Price","Lowest Before","Days Tracked","Rating","Reviews","Specs","Sponsored","SKU","URL"];
    cols = [...new Set(rows.flatMap(Object.keys))].sort((a, b) => (first.indexOf(a)+1 || 99) - (first.indexOf(b)+1 || 99));
    scrapeInfo = `${rows.length} products scraped in ${Math.round((Date.now()-t0)/1000)}s`
      + (rows.length && rows.every(r => r.Verdict === "New") ? " · First time seeing these: price history builds up each day you run it." : "");
    $("filter").hidden = $("dl").hidden = $("wrap").hidden = !rows.length;
    render();
  } catch (err) { $("status").textContent = "Error: " + err.message; }
  finally { clearInterval(tick); $("go").disabled = false; }
};

function render() {
  const f = $("filter").value.toLowerCase();
  let view = rows.filter(r => !f || cols.some(c => String(r[c] ?? "").toLowerCase().includes(f)));
  if (sortKey) view.sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x == null) return 1; if (y == null) return -1;
    return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
  });
  // Live display controls: change them any time, no re-scrape needed
  if ($("f").only_discounted.checked) view = view.filter(r => r["Discount %"] > 0);
  const matching = view.length, n = parseInt($("f").top.value);
  if (n > 0) view = view.slice(0, n);
  if (rows.length) $("status").textContent = `Showing ${view.length} of ${matching} matching · ${scrapeInfo}`;
  const shown = cols.filter(c => !["URL", "SKU", "Sponsored"].includes(c));
  $("th").replaceChildren(tr(shown.map(c => { const th = document.createElement("th");
    th.textContent = c + (c === sortKey ? (asc ? " ▲" : " ▼") : "");
    th.onclick = () => { asc = sortKey === c ? !asc : true; sortKey = c; render(); }; return th; })));
  $("tb").replaceChildren(...view.map(r => tr(shown.map(c => {
    const td = document.createElement("td");
    if (NUM.has(c)) td.className = "num";
    if (c === "Specs") td.className = "specs";
    if (c === "Title" && String(r.URL).startsWith("https://www.skroutz.gr/")) {
      const a = document.createElement("a"); a.href = r.URL; a.target = "_blank"; a.rel = "noopener";
      a.textContent = r.Title; td.append(a);
      if (r.Sponsored) { const s = document.createElement("span"); s.className = "tag"; s.textContent = "ad"; td.append(s); }
    } else if (c === "Photo") {
      td.className = "photo";
      if (String(r.Photo).startsWith("https://")) {
        const img = document.createElement("img"); img.src = r.Photo; img.alt = ""; img.loading = "lazy"; td.append(img);
      }
    } else if (c === "Verdict") {
      const s = document.createElement("span"); s.className = "verdict " + (VERDICT[r[c]] || ""); s.textContent = r[c]; td.append(s);
    } else if (c === "Old Price" && r[c] != null) { const s = document.createElement("s"); s.textContent = fmt(c, r[c]); td.append(s); }
    else td.textContent = fmt(c, r[c]);   // textContent: scraped text can never inject HTML
    return td;
  }))));
}
function tr(cells) { const t = document.createElement("tr"); t.append(...cells); return t; }
$("filter").oninput = $("f").top.oninput = $("f").only_discounted.onchange = render;

// Category dropdown: picking one fills the URL box
fetch("/categories").then(r => r.json()).then(cats => {
  const sel = $("cat");
  sel.replaceChildren(new Option("Technology (all)", "https://www.skroutz.gr/deals/c/1269/technology"),
    ...cats.map(c => new Option(c.name, c.url)));
}).catch(() => $("cat").replaceChildren(new Option("Couldn't load categories — paste a URL below", "")));
$("cat").onchange = e => { if (e.target.value) { $("f").url.value = e.target.value; $("f").search.value = ""; } };

$("dl").onclick = () => {
  const esc = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = [cols.map(esc).join(","), ...rows.map(r => cols.map(c => esc(r[c])).join(","))].join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + csv], {type: "text/csv"}));  // BOM so Excel shows Greek
  a.download = "skroutz.csv"; a.click();
};
</script></body></html>"""


_categories = []


def categories():
    """Deals categories, read once from the deals page's own links."""
    if not _categories:
        page = Fetcher.get(f"{BASE}/deals/c/1269/technology", impersonate="chrome")
        seen = set()
        for a in page.css("a[href*='/deals/c/']"):
            url = urljoin(BASE, a.attrib.get("href", "").split("?")[0])
            name = a.get_all_text(separator="\n", strip=True).split("\n")[0]  # drop the "2.2 K" count line
            if url.endswith("/1269/technology") or url in seen or not name:
                continue
            seen.add(url)
            _categories.append({"name": name, "url": url})
        _categories.sort(key=lambda c: c["name"])
    return _categories


def scrape(q):
    url = q.get("url", "").strip()
    search = q.get("search", "").strip()
    if search:
        url = f"{BASE}/search?keyphrase={quote_plus(search)}"
    elif urlparse(url).netloc not in ("www.skroutz.gr", "skroutz.gr"):
        raise ValueError("URL must be a skroutz.gr page")
    sort = q.get("sort", "none")
    if sort == "price":
        url = with_params(url, order_by="pricevat", order_dir="asc")
    pages = max(1, min(int(q.get("pages") or 3), 50))
    top = int(q["top"]) if q.get("top") else None

    rows = []
    with FetcherSession(impersonate="chrome", retries=3) as session:
        crawl_listing(session, url, pages, 2.0, rows)
        record_and_annotate(rows)
        rows = rank(rows, sort, None, float(q.get("min_discount") or 0))  # all rows; the page trims to top N live
        # Product-page visits (photos, specs) only for the items the page will initially show
        shown = [r for r in rows if r["Discount %"] > 0] if q.get("only_discounted") else rows
        shown = shown[:top] if top else shown
        fill_photos(session, shown, 1.5)
        if q.get("details"):
            add_details(session, shown, 2.0)
    return rows


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if parsed.path == "/categories":
            try:
                body, code = categories(), 200
            except Exception as e:
                body, code = {"error": str(e)}, 502
            return self.send(code, json.dumps(body, ensure_ascii=False).encode(), "application/json")
        if parsed.path == "/scrape":
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                body, code = {"rows": scrape(q)}, 200
            except Exception as e:
                body, code = {"error": str(e)}, 400
            return self.send(code, json.dumps(body, ensure_ascii=False).encode(), "application/json")
        self.send(404, b"Not found", "text/plain")

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the console quiet
        pass


if __name__ == "__main__":
    # On Windows, port reuse lets a second copy share the port and an old copy keeps answering; refuse instead
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # 127.0.0.1 = only this PC can reach it
    except OSError:
        raise SystemExit(f"Port {PORT} is busy: the scraper is probably already running (check IDLE / other windows). "
                         f"Close it and run this again, so you get the latest version.")
    url = f"http://127.0.0.1:{PORT}"
    print(f"Skroutz Scraper running at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped")
