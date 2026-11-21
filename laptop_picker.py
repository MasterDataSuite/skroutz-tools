"""Ultimate laptop picker for Skroutz.

Run:  python laptop_picker.py   -> opens http://127.0.0.1:8766 in your browser.

How it ranks:
  CPU / GPU  : real benchmark scores from PassMark (cpubenchmark.net / videocardbenchmark.net),
               downloaded once and cached for 30 days in passmark_cache.json.
  Screen     : resolution + refresh rate + panel type (OLED > Mini LED > IPS > basic).
  RAM, storage, weight (portability).
  Each part gets 0-100, then a use-case profile (Gaming, Work/Study, ...) weights them into one score.
  Value = score per 1000 EUR.

Laptops are listed from Skroutz (English site). Full spec pages are fetched only for the most promising
ones and remembered forever in skroutz_prices.db, so later runs are fast and cover more laptops.
Uses skroutz_scraper.py for crawling, price history and photos.
"""
import json
import math
import os
import re
import time
import webbrowser
from datetime import date
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from skroutz_scraper import BASE, crawl_listing, data_dir, fill_history, fill_photos, open_db, open_session, record_and_annotate, thumb

PORT = int(os.environ.get("LAPTOP_PICKER_PORT", 8766))
LAPTOPS_URL = f"{BASE}/c/25/laptop.html"
BENCH_FILE = data_dir() / "passmark_cache.json"
BENCH_MAX_AGE_DAYS = 30
DELAY = 2.0

# Weights per use case: cpu, gpu, screen, ram, storage, portability
PROFILES = {
    "Balanced":               {"cpu": 25, "gpu": 15, "screen": 20, "ram": 15, "storage": 10, "portability": 15},
    "Gaming":                 {"cpu": 20, "gpu": 40, "screen": 15, "ram": 10, "storage": 10, "portability": 5},
    "Work / Study":           {"cpu": 25, "gpu": 5,  "screen": 20, "ram": 20, "storage": 10, "portability": 20},
    "Creative (photo/video)": {"cpu": 30, "gpu": 20, "screen": 25, "ram": 15, "storage": 10, "portability": 0},
    "Travel / Ultraportable": {"cpu": 20, "gpu": 5,  "screen": 20, "ram": 15, "storage": 10, "portability": 30},
}


# ---------------------------------------------------------------- PassMark benchmarks

# The plain list shows one brand per page (Intel by default), so combine brand lists with the
# high/mid/low charts, which cover every brand incl. Apple M-series and Snapdragon.
BENCH_SOURCES = {
    "cpu": ["https://www.cpubenchmark.net/cpu-list/", "https://www.cpubenchmark.net/cpu-list/amd/",
            "https://www.cpubenchmark.net/high_end_cpus.html", "https://www.cpubenchmark.net/mid_range_cpus.html",
            "https://www.cpubenchmark.net/low_end_cpus.html"],
    "gpu": ["https://www.videocardbenchmark.net/gpu_list.php", "https://www.videocardbenchmark.net/high_end_gpus.html",
            "https://www.videocardbenchmark.net/mid_range_gpus.html", "https://www.videocardbenchmark.net/low_end_gpus.html"],
}


def read_marks(page):
    """name -> score from either PassMark page style: the big table, or the bar charts."""
    pairs = [(tr.css("td")[0].get_all_text(strip=True), tr.css("td")[1].get_all_text(strip=True))
             for tr in page.css("table#cputable tbody tr") if len(tr.css("td")) > 1]
    pairs += [(li.css(".prdname::text").get(), li.css(".count::text").get()) for li in page.css("ul.chartlist li")]
    return {n: int(m.replace(",", "")) for n, m in pairs
            if n and m and m.replace(",", "").isdigit() and "(Virtual)" not in n and " 0 MHz" not in n}


def load_benchmarks(session):
    """{'cpu': {name: mark}, 'gpu': {name: mark}} from PassMark, cached for 30 days."""
    cached = None
    if BENCH_FILE.exists():
        cached = json.loads(BENCH_FILE.read_text(encoding="utf-8"))
        if (date.today() - date.fromisoformat(cached["fetched"])).days < BENCH_MAX_AGE_DAYS:
            return cached
    try:
        data = {"fetched": date.today().isoformat()}
        for kind, urls in BENCH_SOURCES.items():
            print(f"Downloading PassMark {kind.upper()} benchmarks (once a month)...")
            marks = {}
            for url in urls:
                marks.update(read_marks(session.get(url)))
                time.sleep(DELAY)
            if len(marks) < 1000:
                raise RuntimeError(f"PassMark {kind} data looks wrong ({len(marks)} entries)")
            data[kind] = marks
        BENCH_FILE.write_text(json.dumps(data), encoding="utf-8")
        return data
    except Exception as e:
        if cached:  # stale data beats no data
            print(f"PassMark refresh failed ({e}); using the cached list from {cached['fetched']}")
            return cached
        raise


CPU_NOISE = {"intel", "amd", "qualcomm", "mediatek", "core", "processor", "cpu", "w", "mobile"}  # "apple" kept: see find()
GPU_NOISE = {"nvidia", "amd", "intel", "apple", "qualcomm", "graphics", "gpu", "laptop", "mobile", "max", "q", "design"}


def tokens(name, noise):
    name = name.lower().split("@")[0].replace("gf ", "geforce ")
    return [t for t in re.split(r"[\s()/,\-_+]+", name) if t and t not in noise]


class Bench:
    """Fuzzy name -> PassMark score. Every word of the laptop's chip name must appear in the PassMark name."""

    def __init__(self, marks, noise, prefer_laptop=False, need_model=True):
        self.noise, self.prefer_laptop, self.need_model = noise, prefer_laptop, need_model
        self.items = [(frozenset(tokens(n, noise)), n, m) for n, m in marks.items()]
        self.exact = {n.lower(): (n, m) for n, m in marks.items()}  # e.g. "Intel Graphics": all noise words

    @staticmethod
    def has_model(words):  # a real model number like "1334u", "250", "155h", "x1e", or Apple's "m4"
        return any(re.fullmatch(r"(?=.*\d)[a-z0-9]{3,}|m\d", w) for w in words)

    @lru_cache(maxsize=None)
    def find(self, text):
        hit = self.exact.get((text or "").lower()) or self._find(text)
        if not hit and text:  # Snapdragon: Skroutz "X1 26 100" vs PassMark "X126100"
            hit = self._find(re.sub(r"(?<=[A-Za-z0-9])[\s-]+(?=\d)", "", text))
        return hit

    def _find(self, text):
        want = tokens(text or "", self.noise)
        if want and re.fullmatch(r"m\d|a1\d", want[0]):
            want.insert(0, "apple")  # Apple "M5" must not match Intel's old "Core m5-6Y54"
        if not want or (self.need_model and not self.has_model(want)):
            return None  # too vague (e.g. just "Ryzen 5"): refuse rather than guess
        while want:
            hits = [(n, m, ts) for ts, n, m in self.items if set(want) <= ts
                    # No model number ("Iris Xe", "Adreno"): only an exact name, not any phone "Adreno 640"
                    and (self.has_model(want) or len(ts) == len(want))]
            if hits:
                # Laptop GPU variants over desktop ones, then the closest name (fewest extra words)
                n, m, _ = min(hits, key=lambda h: (self.prefer_laptop and not re.search(r"laptop|mobile|max-q", h[0], re.I),
                                                   len(h[2]) - len(want), len(h[0])))
                return n, m
            want = want[:-1]  # e.g. "m5 pro 15" not listed -> try "m5 pro" ...
            if not self.has_model(want):
                return None    # ... but never fall back to a bare family like "ryzen 7"
        return None


# ---------------------------------------------------------------- Laptop specs

def num(text):
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(m.group().replace(",", ".")) if m else None


def parse_spec_page(page):
    """{'Screen / Resolution': '1920x1080', ...} from a product page (first spec block only; the page repeats it)."""
    specs = {}
    groups = page.css("div.spec-groups")
    for det in groups[0].css("div.spec-details") if groups else []:
        head = (det.css("h3::text").get() or "").strip()
        for dt, dd in zip(det.css("dt"), det.css("dd")):
            value = dd.get_all_text(separator=" ", strip=True).replace("‑", "-")  # Skroutz uses non-breaking hyphens
            if value and value != "-":
                specs[f"{head} / {dt.get_all_text(strip=True)}"] = value
    return specs


RES_WORDS = [("4k", 3840, 2160), ("uhd", 3840, 2160), ("3.2k", 3200, 2000), ("3k", 2880, 1920), ("2.8k", 2880, 1800),
             ("2.5k", 2560, 1600), ("qhd+", 2560, 1600), ("wqxga", 2560, 1600), ("qhd", 2560, 1440), ("2k", 2560, 1440),
             ("retina", 2560, 1664), ("wuxga", 1920, 1200), ("fhd+", 1920, 1200), ("fhd", 1920, 1080),
             ("full hd", 1920, 1080), ("hd", 1366, 768)]


def describe(row, specs):
    """Normalized laptop facts from the full spec page when we have it, else from the listing title/spec line."""
    s = lambda key: (specs.get(key) or "").replace("‑", "-") or None  # noqa: E731  (non-breaking hyphens)
    title, line = (row["Title"] or "").replace("‑", "-"), (row.get("Specs") or "").replace("‑", "-")
    paren = re.search(r"\(([^)]*)\)", title)
    parts = [p.strip() for p in paren.group(1).split("/")] if paren else []
    sizes = [p for p in parts if re.fullmatch(r"\d+(?:\.\d+)?\s*(GB|TB)", p, re.I)]
    gb = lambda p: num(p) * (1000 if p.upper().endswith("TB") else 1)  # noqa: E731
    d = {"full": bool(specs)}

    if specs:
        fam, model = s("Processor (CPU) / Family"), s("Processor (CPU) / Model")
        # Different names for the same chip; the first one PassMark recognises wins (see score_row)
        d["cpu_names"] = [n for n in (s("Processor (CPU) / Processor"), fam and model and f"{fam} {model}") if n]
        d["cpu"] = d["cpu_names"][0] if d["cpu_names"] else None
        gpu = s("Graphics Card / Model") or s("Graphics Card / Graphics Card")
        if gpu and re.fullmatch(r"\d+-Core GPU", gpu, re.I):  # Apple: "16-Core GPU" -> "M5 Pro 16-Core GPU"
            gpu = f"{fam} {gpu}"
        d["gpu"] = gpu
        d["ram"] = num(s("RAM Memory / Memory Capacity"))
        d["storage"] = num(s("Hard Drive / Hard Disk Capacity"))
        d["size"] = num(s("Screen / Diagonal"))
        res = re.search(r"(\d{3,4})\s*x\s*(\d{3,4})", s("Screen / Resolution") or "")
        d["res"] = (int(res.group(1)), int(res.group(2))) if res else None
        d["hz"] = num(s("Screen / Refresh Rate"))
        panel = (s("Screen / Panel Type") or "").lower()
        d["panel"] = ("OLED" if s("Screen / OLED") == "Yes" or "oled" in panel else
                      "Mini LED" if "mini" in panel else
                      "IPS" if s("Screen / IPS Panel") == "Yes" or "ips" in panel else
                      "Retina" if "retina" in line.lower() else "Basic")
        d["weight"] = num(s("Main Specifications / Weight"))
    else:
        d["cpu"] = parts[0] if parts and parts[0] not in sizes else None
        d["cpu_names"] = [d["cpu"]] if d["cpu"] else []
        g = re.search(r"((?:GF|GeForce|Radeon|Arc)\s[\w ]+?|\d+-Core GPU)(?:,|$)", line)
        d["gpu"] = g.group(1) if g else None
        if d["gpu"] and "Core GPU" in d["gpu"] and d["cpu"]:
            d["gpu"] = f"{d['cpu']} {d['gpu']}"
        d["storage"] = gb(sizes[-1]) if sizes and (len(sizes) > 1 or gb(sizes[-1]) >= 128) else None
        d["ram"] = num(sizes[0]) if sizes and (len(sizes) > 1 or num(sizes[0]) < 128) else None
        d["size"] = num(re.search(r'(\d+(?:\.\d+)?)"', title).group(1)) if re.search(r'\d+(?:\.\d+)?"', title) else None
        low = line.lower()
        d["res"] = next(((w, h) for word, w, h in RES_WORDS if re.search(rf"(?<![\w.]){re.escape(word)}(?![\w])", low)), None)
        d["hz"] = num(re.search(r"\d{2,3}\s*Hz", line).group()) if re.search(r"\d{2,3}\s*Hz", line) else None
        d["panel"] = "OLED" if "oled" in low else "Mini LED" if "mini led" in low else "IPS" if "ips" in low else \
                     "Retina" if "retina" in low else "Basic"
        d["weight"] = None
    return d


def lognorm(value, lo, hi):
    return round(max(0.0, min(1.0, math.log(value / lo) / math.log(hi / lo))) * 100) if value else None


def screen_score(d):
    w, h = d["res"] or (1920, 1080)  # unknown -> assume Full HD, the most common
    res = max(0, min(100, 40 + 60 * math.log(w * h / (1920 * 1080)) / math.log(4)))  # FHD 40, 4K 100
    hz = max(0, min(1, ((d["hz"] or 60) - 60) / 180)) ** 0.5 * 100                    # 60Hz 0, 120Hz 58, 240Hz 100
    panel = {"OLED": 100, "Mini LED": 95, "Retina": 80, "IPS": 70, "Basic": 40}[d["panel"]]
    return round(0.45 * res + 0.25 * hz + 0.30 * panel)


def weight_estimate(d, gpu_mark):
    size = d["size"] or 15.6
    kg = 1.25 + max(0, size - 13) * 0.3          # ~1.25 kg at 13", ~2 kg at 15.6"
    return kg + (0.4 if gpu_mark and gpu_mark > 9000 else 0)  # gaming GPUs bring heavier chassis/cooling


def score_row(row, specs, cpus, gpus):
    d = describe(row, specs or {})
    cpu = next((hit for name in d["cpu_names"] if (hit := cpus.find(name))), None)
    if d["gpu"] and re.fullmatch(r"(amd )?radeon graphics|(qualcomm )?adreno( gpu)?", d["gpu"], re.I):
        # Generic integrated names: PassMark lists these per CPU ("Ryzen 5 7530U with Radeon Graphics",
        # "Snapdragon X Plus - X1P64100 - Qualcomm Adreno GPU"); its bare "Adreno GPU" entry is an old phone chip
        suffix = "with Radeon Graphics" if "radeon" in d["gpu"].lower() else "Adreno"
        gpu = gpus.find(f"{d['cpu']} {suffix}") if d["cpu"] else None
    else:
        gpu = gpus.find(d["gpu"])
    # No GPU score -> assume a typical integrated GPU (conservative). Listings always name dedicated GPUs,
    # so a listing without one is integrated; PassMark has no Apple GPU scores at all.
    integrated_guess = not gpu
    if not d["gpu"]:
        d["gpu"] = "Integrated (estimated)"
    elif not gpu:
        d["gpu"] += " (no benchmark, estimated)"
    weight = d["weight"] or weight_estimate(d, gpu and gpu[1])
    row.update({
        "CPU": d["cpu"], "CPU Bench": cpu[0] if cpu else None, "CPU Mark": cpu[1] if cpu else None,
        "GPU": d["gpu"], "GPU Bench": gpu[0] if gpu else None, "GPU Mark": gpu[1] if gpu else None,
        "Screen Size": d["size"], "Resolution": "x".join(map(str, d["res"])) if d["res"] else None,
        "Refresh Hz": d["hz"], "Panel": d["panel"], "RAM GB": d["ram"], "Storage GB": d["storage"],
        "Weight kg": round(weight, 2), "Weight Estimated": not d["weight"], "Full Specs": d["full"],
        "scores": {
            "cpu": lognorm(cpu and cpu[1], 2000, 70000),     # PassMark CPU Mark: ~2k (Celeron) .. ~70k (top HX)
            # PassMark G3D Mark: ~0.5k (old iGPU) .. ~35k (RTX 5090); typical modern integrated ~2k until specs are checked
            "gpu": lognorm(2000 if integrated_guess else gpu and gpu[1], 500, 35000),
            "screen": screen_score(d),
            "ram": round(15 + 85 * max(0, min(1, math.log2(d["ram"] / 4) / 4))) if d["ram"] else None,  # 8GB 36, 16GB 57, 32GB 79
            "storage": round(100 * max(0, min(1, math.log2(d["storage"] / 128) / 4))) if d["storage"] else None,  # 512GB 50, 1TB 75
            "portability": round(100 * max(0, min(1, (2.6 - weight) / 1.6))),  # 1.0 kg 100, 2.6 kg 0
        },
    })


def overall(row, profile):
    """Weighted average of the parts we know (unknown parts don't count for or against)."""
    w = PROFILES[profile]
    known = {k: v for k, v in row["scores"].items() if v is not None and w[k]}
    total = sum(w[k] for k in known)
    return sum(w[k] * v for k, v in known.items()) / total if total else 0


# ---------------------------------------------------------------- Spec cache (forever, per SKU)

def load_specs(skus):
    with open_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS laptop_specs (sku TEXT PRIMARY KEY, specs TEXT, fetched TEXT)")
        q = ",".join("?" * len(skus))
        return {sku: json.loads(js) for sku, js in db.execute(f"SELECT sku, specs FROM laptop_specs WHERE sku IN ({q})", skus)} if skus else {}


def fetch_specs(session, rows, cache):
    print(f"Fetching full specs for {len(rows)} laptops (once per laptop, then remembered)...")
    with open_db() as db:
        for i, r in enumerate(rows, 1):
            time.sleep(DELAY)
            try:
                page = session.get(r["URL"])
                specs = parse_spec_page(page)
                r["Photo"] = r.get("Photo") or thumb(page.css("meta[property='og:image']::attr(content)").get())
            except Exception as e:  # one bad page shouldn't kill the run
                print(f"  specs failed for {r['URL']}: {e}")
                continue
            if specs:
                cache[r["SKU"]] = specs
                db.execute("INSERT OR REPLACE INTO laptop_specs VALUES (?, ?, ?)",
                           (r["SKU"], json.dumps(specs, ensure_ascii=False), date.today().isoformat()))
            if r.get("Photo"):
                db.execute("INSERT OR REPLACE INTO photos VALUES (?, ?)", (r["SKU"], r["Photo"]))
            db.commit()
            print(f"  {i}/{len(rows)} {r['Title'][:60]}")


def pick(q):
    lo, hi = int(float(q.get("min") or 0)), int(float(q.get("max") or 0))
    profile = q.get("profile") if q.get("profile") in PROFILES else "Balanced"
    pages = max(1, min(int(q.get("pages") or 10), 50))
    lookups = max(0, min(int(q.get("lookups") or 25), 100))
    params = {"lang": "en", "store_lang": "true"}  # English titles and specs
    if lo:
        params["price_min"] = lo
    if hi:
        params["price_max"] = hi

    rows = []
    with open_session() as session:
        bench = load_benchmarks(session)
        cpus = Bench(bench["cpu"], CPU_NOISE)
        gpus = Bench(bench["gpu"], GPU_NOISE, prefer_laptop=True, need_model=False)  # "Iris Xe" has no number
        crawl_listing(session, f"{LAPTOPS_URL}?{urlencode(params)}", pages, DELAY, rows)
        record_and_annotate(rows)
        cache = load_specs([r["SKU"] for r in rows])
        for r in rows:
            score_row(r, cache.get(r["SKU"]), cpus, gpus)
        # Full specs for the most promising laptops we haven't looked at yet
        todo = sorted((r for r in rows if r["SKU"] not in cache), key=lambda r: -overall(r, profile))[:lookups]
        fetch_specs(session, todo, cache)
        for r in todo:
            score_row(r, cache.get(r["SKU"]), cpus, gpus)
        top = sorted(rows, key=lambda r: -overall(r, profile))[:lookups]
        fill_photos(session, top, DELAY)
        fill_history(session, top, DELAY)
        record_and_annotate(top)  # again, now with Skroutz's 3-month history
    return {"rows": rows, "profiles": PROFILES, "benchFetched": bench["fetched"]}


# ---------------------------------------------------------------- Web page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Laptop Picker</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#667085;--line:#e4e7ec;--accent:#f68b24;--accent-text:#fff;--bar:#f68b24;--barbg:#eef0f3;--good:#0a7d3c;--bad:#c4320a}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--text:#e8eaed;--muted:#9aa1ad;--line:#2d313a;--accent:#f68b24;--accent-text:#111;--barbg:#2d313a;--good:#4ade80;--bad:#fb7c5c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif}
main{padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}
.nav{margin:0 0 12px;color:var(--muted)}
.sub{color:var(--muted);margin:0 0 16px}
form{max-width:1200px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));align-items:end}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
label.check{flex-direction:row;align-items:center;gap:8px;font-size:14px;color:var(--text)}
input,select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)}
button{font:inherit;font-weight:600;padding:9px 16px;border:0;border-radius:6px;background:var(--accent);color:var(--accent-text);cursor:pointer}
button:disabled{opacity:.5;cursor:wait}
.live{grid-column:1/-1;border-top:1px solid var(--line);padding-top:12px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));align-items:end}
.live h2{grid-column:1/-1;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0}
#bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:16px 0 8px}
#status{color:var(--muted)}
#filter{flex:1;min-width:180px;max-width:400px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;white-space:nowrap;user-select:none;font-size:12px}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
td.photo img{width:64px;height:64px;object-fit:contain;background:#fff;border-radius:6px;display:block}
td.title{min-width:240px}
td.part{min-width:150px}
.score{display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums;font-weight:600}
.score i{display:block;height:6px;width:60px;border-radius:3px;background:var(--barbg);overflow:hidden}
.score i b{display:block;height:100%;background:var(--bar)}
.big{font-size:16px}
.muted{color:var(--muted);font-size:12px}
s{color:var(--muted)}
a{color:var(--accent)}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:4px;white-space:nowrap}
.good{color:var(--good);border-color:var(--good)}
.bad{color:var(--bad);border-color:var(--bad)}
</style></head><body><main>
<nav class="nav"><a href="http://127.0.0.1:8765/">Deal Finder</a> · <b>Laptop Picker</b></nav>
<h1>Laptop Picker</h1>
<p class="sub">CPU and GPU ranked by PassMark benchmarks · screen, RAM, storage and weight scored · prices from Skroutz</p>
<form id="f">
  <label>Budget from (€) <input name="min" type="number" min="0" step="50" placeholder="0"></label>
  <label>Budget up to (€) <input name="max" type="number" min="0" step="50" value="1200"></label>
  <label>Pages to scan <input name="pages" type="number" min="1" max="50" value="10"></label>
  <label>Full spec lookups <input name="lookups" type="number" min="0" max="100" value="25"></label>
  <button id="go">Find laptops</button>
  <div class="live"><h2>Instant filters (no re-scan)</h2>
    <label>Use case <select name="profile" id="profile"></select></label>
    <label>Min RAM <select name="minram"><option value="0">Any</option><option>8</option><option selected>16</option><option>32</option></select></label>
    <label>Screen from (") <input name="smin" type="number" step="0.1" placeholder="any"></label>
    <label>Screen up to (") <input name="smax" type="number" step="0.1" placeholder="any"></label>
    <label>Show top N <input name="top" type="number" min="1" value="25" placeholder="all"></label>
    <label class="check"><input name="only_discounted" type="checkbox"> Only discounted</label>
    <label class="check"><input name="full_only" type="checkbox"> Only fully checked specs</label>
  </div>
</form>
<div id="bar"><span id="status">Set a budget and press Find laptops. First run downloads the PassMark lists and checks specs, so it takes a minute or two; later runs are much faster.</span>
  <input id="filter" placeholder="Filter (e.g. Lenovo, RTX, OLED)…" hidden></div>
<div class="wrap" hidden id="wrap"><table><thead id="th"></thead><tbody id="tb"></tbody></table></div>
</main><script>
let rows = [], profiles = {}, sortCol = null, asc = false, info = "";
const $ = id => document.getElementById(id), F = () => $("f");
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const eur = v => v == null ? "" : v.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " €";

function overall(r) {
  const w = profiles[F().profile.value] || {};
  let t = 0, s = 0;
  for (const k in w) if (w[k] && r.scores[k] != null) { t += w[k]; s += w[k] * r.scores[k]; }
  return t ? s / t : 0;
}
const value = r => overall(r) / r.Price * 1000;

function scoreCell(v, big) {
  const d = el("div", "score" + (big ? " big" : ""));
  d.append(el("span", null, v == null ? "?" : Math.round(v)));
  const bar = el("i"), fill = el("b"); fill.style.width = (v || 0) + "%"; bar.append(fill); d.append(bar);
  return d;
}
function partCell(name, score, tip) {
  const td = el("td", "part"); td.append(scoreCell(score)); td.append(el("div", "muted", name || "unknown"));
  if (tip) td.title = tip; return td;
}
const VERDICT = {"3-month low": "good", "Below average": "good", "Fake discount?": "bad", "Price-hike scam?": "bad", "Above average": "bad"};

const COLS = [
  {h: "#", sort: null, cell: (r, i) => el("td", "num", i + 1)},
  {h: "Photo", sort: null, cell: r => { const td = el("td", "photo"); if (String(r.Photo).startsWith("https://")) { const im = el("img"); im.src = r.Photo; im.alt = ""; im.loading = "lazy"; td.append(im); } return td; }},
  {h: "Laptop", sort: r => r.Title, cell: r => {
    const td = el("td", "title");
    const a = el("a", null, r.Title); if (String(r.URL).startsWith("https://www.skroutz.gr/")) { a.href = r.URL; a.target = "_blank"; a.rel = "noopener"; }
    td.append(a);
    if (r.Sponsored) td.append(el("span", "tag", "ad"));
    if (r.Verdict && r.Verdict !== "New" && r.Verdict !== "Normal") td.append(el("span", "tag " + (VERDICT[r.Verdict] || ""), r.Verdict));
    if (!r["Full Specs"]) td.append(el("span", "tag", "estimated specs"));
    return td; }},
  {h: "Price", sort: r => r.Price, cell: r => { const td = el("td", "num"); td.append(el("div", null, eur(r.Price)));
    if (r["Old Price"]) { const s = el("s", null, eur(r["Old Price"])); const d = el("div", "muted"); d.append(s, " -" + r["Discount %"] + "%"); td.append(d); } return td; }},
  {h: "Score", sort: r => overall(r), cell: r => { const td = el("td"); td.append(scoreCell(overall(r), true)); return td; }},
  {h: "Value", sort: r => value(r), cell: r => { const td = el("td", "num"); td.append(el("div", null, value(r).toFixed(1))); td.append(el("div", "muted", "per 1000 €")); return td; }},
  {h: "CPU", sort: r => r.scores.cpu ?? -1, cell: r => partCell(r.CPU, r.scores.cpu, r["CPU Bench"] ? `PassMark: ${r["CPU Bench"]} = ${r["CPU Mark"]}` : "No PassMark match")},
  {h: "GPU", sort: r => r.scores.gpu ?? -1, cell: r => partCell(r.GPU || (r["Full Specs"] ? "" : "integrated?"), r.scores.gpu, r["GPU Bench"] ? `PassMark: ${r["GPU Bench"]} = ${r["GPU Mark"]}` : "No PassMark match")},
  {h: "Screen", sort: r => r.scores.screen, cell: r => partCell([r["Screen Size"] && r["Screen Size"] + '"', r.Resolution, r["Refresh Hz"] && r["Refresh Hz"] + "Hz", r.Panel].filter(Boolean).join(" · "), r.scores.screen)},
  {h: "RAM", sort: r => r["RAM GB"] ?? -1, cell: r => partCell(r["RAM GB"] ? r["RAM GB"] + " GB" : "", r.scores.ram)},
  {h: "Storage", sort: r => r["Storage GB"] ?? -1, cell: r => partCell(r["Storage GB"] ? (r["Storage GB"] >= 1000 ? r["Storage GB"] / 1000 + " TB" : r["Storage GB"] + " GB") : "", r.scores.storage)},
  {h: "Weight", sort: r => -r["Weight kg"], cell: r => partCell((r["Weight Estimated"] ? "~" : "") + r["Weight kg"] + " kg", r.scores.portability, r["Weight Estimated"] ? "Estimated from screen size" : "")},
];

function render() {
  const f = F(), q = $("filter").value.toLowerCase();
  const minram = +f.minram.value, smin = parseFloat(f.smin.value), smax = parseFloat(f.smax.value), n = parseInt(f.top.value);
  let view = rows.filter(r =>
    (!minram || (r["RAM GB"] || 0) >= minram) &&
    (isNaN(smin) || (r["Screen Size"] || 0) >= smin) && (isNaN(smax) || (r["Screen Size"] || 99) <= smax) &&
    (!f.only_discounted.checked || r["Discount %"] > 0) && (!f.full_only.checked || r["Full Specs"]) &&
    (!q || [r.Title, r.CPU, r.GPU, r.Panel].join(" ").toLowerCase().includes(q)));
  const key = sortCol ? sortCol.sort : overall, dir = sortCol ? (asc ? 1 : -1) : -1;
  view.sort((a, b) => { const x = key(a), y = key(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
  const matching = view.length; if (n > 0) view = view.slice(0, n);
  $("status").textContent = `Showing ${view.length} of ${matching} matching · ${info}`;
  $("th").replaceChildren(row(COLS.map(c => { const th = el("th", null, c.h + (c === sortCol ? (asc ? " ▲" : " ▼") : (!sortCol && c.h === "Score" ? " ▼" : "")));
    if (c.sort) th.onclick = () => { asc = sortCol === c ? !asc : false; sortCol = c; render(); }; return th; })));
  $("tb").replaceChildren(...view.map((r, i) => row(COLS.map(c => c.cell(r, i)))));
}
function row(cells) { const t = el("tr"); t.append(...cells); return t; }

F().onsubmit = async e => {
  e.preventDefault();
  $("go").disabled = true;
  const t0 = Date.now(), tick = setInterval(() => $("status").textContent = `Scanning laptops… ${Math.round((Date.now() - t0) / 1000)}s (see the console window for progress)`, 500);
  try {
    const q = new URLSearchParams(new FormData(F()));
    const r = await fetch("/pick?" + q), data = await r.json();
    if (!r.ok) throw new Error(data.error);
    rows = data.rows; sortCol = null;
    const full = rows.filter(r => r["Full Specs"]).length;
    info = `${rows.length} laptops scanned in ${Math.round((Date.now() - t0) / 1000)}s · ${full} with full specs · PassMark data from ${data.benchFetched}`;
    $("filter").hidden = $("wrap").hidden = !rows.length;
    render();
  } catch (err) { $("status").textContent = "Error: " + err.message; }
  finally { clearInterval(tick); $("go").disabled = false; }
};
fetch("/profiles").then(r => r.json()).then(p => { profiles = p;
  $("profile").replaceChildren(...Object.keys(p).map(k => new Option(k, k))); });
for (const name of ["profile", "minram", "smin", "smax", "top", "only_discounted", "full_only"]) F()[name].addEventListener("input", () => rows.length && render());
$("filter").oninput = render;
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if parsed.path == "/profiles":
            return self.send(200, json.dumps(PROFILES).encode(), "application/json")
        if parsed.path == "/pick":
            try:
                body, code = pick({k: v[0] for k, v in parse_qs(parsed.query).items()}), 200
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

    def log_message(self, *args):
        pass


def selfcheck():
    b = Bench({"Intel Core i5-1334U": 13000, "Intel Core i5-13420H": 19000, "AMD Ryzen 7 250": 28000,
               "AMD Ryzen 7 2500U": 6000, "Apple M4 10 Core": 23000}, CPU_NOISE)
    assert b.find("Core i5 1334U") == ("Intel Core i5-1334U", 13000)
    assert b.find("Ryzen 7-250") == ("AMD Ryzen 7 250", 28000)   # not the much older 2500U
    assert b.find("M4 10-Core")[1] == 23000 and b.find("Ryzen 5") is None
    assert Bench({"AMD Ryzen 7 5800H": 21000}, CPU_NOISE).find("Ryzen 7 250") is None  # no bare-family fallback
    a = Bench({"Intel Core m5-6Y54 @ 1.10GHz": 2264, "Apple M5 10 Core": 30000, "Apple M5 Pro 15 Core": 45000,
               "Snapdragon X - X126100 - Qualcomm Oryon": 13000}, CPU_NOISE)
    assert a.find("M5 10-Core")[1] == 30000 and a.find("M5 Pro 15-Core")[1] == 45000  # never the old Intel m5
    assert a.find("Snapdragon-X1 26 100")[1] == 13000
    g = Bench({"GeForce RTX 4060": 19000, "GeForce RTX 4060 Laptop GPU": 10500, "Intel Iris Xe": 2200},
              GPU_NOISE, prefer_laptop=True, need_model=False)
    assert g.find("GF RTX 4060") == ("GeForce RTX 4060 Laptop GPU", 10500)
    assert g.find("Iris Xe Graphics") == ("Intel Iris Xe", 2200)
    assert Bench({"Adreno 640": 900}, GPU_NOISE, need_model=False).find("Adreno") is None
    d = describe({"Title": 'MSI Thin 15 15.6" (i5-12450H/16GB/1TB)', "Specs": "GF RTX 4050, 144Hz, FHD"}, {})
    assert (d["cpu"], d["gpu"], d["ram"], d["storage"], d["size"], d["hz"], d["res"]) == \
        ("i5-12450H", "GF RTX 4050", 16, 1000, 15.6, 144, (1920, 1080))
    assert screen_score({"res": (2880, 1800), "hz": 120, "panel": "OLED"}) > screen_score({"res": None, "hz": None, "panel": "Basic"})


if __name__ == "__main__":
    selfcheck()
    ThreadingHTTPServer.allow_reuse_address = False  # see skroutz_web.py: refuse to share the port with an old copy
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit(f"Port {PORT} is busy: the laptop picker is probably already running. Close it and run this again.")
    url = f"http://127.0.0.1:{PORT}"
    print(f"Laptop Picker running at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped")
