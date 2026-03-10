"""
Coconut Price Scraper
Source: commodityfact.org (single, fresh, consistent source)

Outputs 4 key data points:
  1. Costliest Market Price  (Tamil Nadu)
  2. Lowest Market Price     (Tamil Nadu)
  3. Average Market Price    (Tamil Nadu)
  4. Thanjavur District Price

Requirements:
    pip install requests beautifulsoup4

Usage:
    python coconut_price_scraper.py
    python coconut_price_scraper.py --export csv
    python coconut_price_scraper.py --export json
    python coconut_price_scraper.py --export both
"""

import re
import requests
from bs4 import BeautifulSoup
import json
import csv
import argparse
from datetime import datetime

# ─── URLs ─────────────────────────────────────────────────────────────────────

TN_URL  = "https://commodityfact.org/mandi-prices/coconut/tamil-nadu"
TJ_URL  = "https://commodityfact.org/mandi-prices/coconut/tamil-nadu/thanjavur"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_rupee(val: str):
    """'₹6,500' or '6500' -> 6500.0, returns None if unparseable."""
    clean = re.sub(r"[^\d.]", "", val.strip())
    return float(clean) if clean else None


def to_kg(quintal_price):
    """Convert per-quintal price to per-kg."""
    return round(quintal_price / 100, 2) if quintal_price else None


def fmt(price_per_kg):
    if price_per_kg is None:
        return "N/A"
    return f"Rs.{price_per_kg:.1f}/kg  (Rs.{price_per_kg * 100:,.0f}/quintal)"


def fetch(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.exceptions.RequestException as e:
        print(f"  [!] Failed to fetch {url}: {e}")
        return None


def parse_table(soup, label=""):
    """
    Parse the last <table> on the page (individual market rows).
    Columns: Commodity | Market | District/State | Min | Max | Modal | Date
    Returns list of dicts with market, min_kg, max_kg, modal_kg, date.
    """
    tables = soup.find_all("table")
    if not tables:
        print(f"  [!] No table found {label}")
        return []

    # Use the last table (individual market list, not district summary)
    table = tables[-1]
    rows = table.find_all("tr")
    if len(rows) <= 1:
        return []

    # Map columns
    headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
    col = {}
    for idx, h in enumerate(headers):
        if "market" in h and "col" not in str(col.get("market", "")):
            col["market"] = idx
        if h.startswith("min"):
            col["min"] = idx
        if h.startswith("max"):
            col["max"] = idx
        if "modal" in h:
            col["modal"] = idx
        if "date" in h:
            col["date"] = idx

    results = []
    seen = set()

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        ct = [c.get_text(strip=True) for c in cells]
        if not any(ct):
            continue

        def gc(field):
            idx = col.get(field)
            return ct[idx] if idx is not None and idx < len(ct) else ""

        market = gc("market")
        modal  = parse_rupee(gc("modal"))
        mn     = parse_rupee(gc("min"))
        mx     = parse_rupee(gc("max"))
        date   = gc("date")

        if not market or modal is None:
            continue
        if market in seen:
            continue
        seen.add(market)

        results.append({
            "market":    market,
            "modal_kg":  to_kg(modal),
            "min_kg":    to_kg(mn),
            "max_kg":    to_kg(mx),
            "date":      date,
        })

    return results


# ─── Scrape Tamil Nadu ────────────────────────────────────────────────────────

def scrape_tamil_nadu():
    print("[*] Fetching Tamil Nadu prices (commodityfact.org)...")
    soup = fetch(TN_URL)
    if not soup:
        return None

    result = {}

    # ── Parse State Market Insights block ──
    # The page has a prose block: "average wholesale price ... ₹5,555/quintal"
    # "prices ranging from ₹2,000 to ₹8,000"
    # "146 Mandis"
    text = soup.get_text(" ", strip=True)

    avg_m = re.search(r"average wholesale price.*?[₹Rs\.]+\s*([\d,]+)\s*/quintal", text, re.I)
    low_m = re.search(r"prices ranging from\s*[₹Rs\.]+\s*([\d,]+)", text, re.I)
    hi_m  = re.search(r"prices ranging from.*?to\s*[₹Rs\.]+\s*([\d,]+)", text, re.I)
    upd_m = re.search(r"Updated:\s*(.+?)(?:\n|District)", text, re.I)
    mkt_m = re.search(r"([\d,]+)\s*Mandis", text, re.I)

    result["avg_kg"]       = to_kg(parse_rupee(avg_m.group(1))) if avg_m else None
    result["low_kg"]       = to_kg(parse_rupee(low_m.group(1))) if low_m else None
    result["high_kg"]      = to_kg(parse_rupee(hi_m.group(1)))  if hi_m  else None
    result["updated"]      = upd_m.group(1).strip() if upd_m else "Unknown"
    result["total_mandis"] = mkt_m.group(1) if mkt_m else "?"

    # ── Parse district summary table for costliest/lowest district ──
    tables = soup.find_all("table")
    district_table = tables[0] if tables else None
    district_rows = []

    if district_table:
        drows = district_table.find_all("tr")
        dheaders = [th.get_text(strip=True).lower() for th in drows[0].find_all(["th", "td"])]
        dcol = {}
        for idx, h in enumerate(dheaders):
            if "district" in h: dcol["district"] = idx
            if "min" in h:      dcol["min"] = idx
            if "max" in h:      dcol["max"] = idx
            if "avg" in h:      dcol["avg"] = idx
            if "market" in h and "district" not in h: dcol["markets"] = idx

        for dr in drows[1:]:
            dcells = [c.get_text(strip=True) for c in dr.find_all(["td", "th"])]
            if not any(dcells):
                continue
            def dgc(f):
                i = dcol.get(f)
                return dcells[i] if i is not None and i < len(dcells) else ""
            avg = parse_rupee(dgc("avg"))
            if avg is None:
                continue
            district_rows.append({
                "district": dgc("district"),
                "min_kg":   to_kg(parse_rupee(dgc("min"))),
                "max_kg":   to_kg(parse_rupee(dgc("max"))),
                "avg_kg":   to_kg(avg),
            })

    result["districts"] = district_rows

    # ── Costliest / Lowest from district table ──
    if district_rows:
        costliest_d = max(district_rows, key=lambda r: r["avg_kg"])
        lowest_d    = min(district_rows, key=lambda r: r["avg_kg"])
        result["costliest_district"] = costliest_d["district"]
        result["costliest_avg_kg"]   = costliest_d["avg_kg"]
        result["costliest_max_kg"]   = costliest_d["max_kg"]
        result["lowest_district"]    = lowest_d["district"]
        result["lowest_avg_kg"]      = lowest_d["avg_kg"]
        result["lowest_min_kg"]      = lowest_d["min_kg"]

    # ── Individual market rows ──
    market_rows = parse_table(soup, label="(Tamil Nadu)")
    result["markets"] = market_rows

    if market_rows:
        costliest_m = max(market_rows, key=lambda r: r["modal_kg"])
        lowest_m    = min(market_rows, key=lambda r: r["modal_kg"])
        result["costliest_market"]    = costliest_m["market"]
        result["costliest_market_kg"] = costliest_m["modal_kg"]
        result["lowest_market"]       = lowest_m["market"]
        result["lowest_market_kg"]    = lowest_m["modal_kg"]

    print(f"    Updated: {result['updated']}  |  {result['total_mandis']} mandis  |  {len(market_rows)} markets in response")
    return result


# ─── Scrape Thanjavur ─────────────────────────────────────────────────────────

def scrape_thanjavur():
    print("[*] Fetching Thanjavur prices (commodityfact.org)...")
    soup = fetch(TJ_URL)
    if not soup:
        return None

    rows = parse_table(soup, label="(Thanjavur)")
    if not rows:
        print("  [!] No Thanjavur market data found.")
        return None

    prices = [r["modal_kg"] for r in rows]
    latest_date = sorted([r["date"] for r in rows if r["date"]], reverse=True)

    result = {
        "markets":    sorted(rows, key=lambda r: -r["modal_kg"]),
        "avg_kg":     round(sum(prices) / len(prices), 2),
        "low_kg":     min(prices),
        "high_kg":    max(prices),
        "data_date":  latest_date[0] if latest_date else "Unknown",
        "total":      len(rows),
    }
    print(f"    {result['total']} markets  |  Latest date: {result['data_date']}")
    return result


# ─── Display ──────────────────────────────────────────────────────────────────

def print_summary(tn, tj):
    W = 66
    print(f"\n{'='*W}")
    print(f"  🥥  COCONUT MARKET PRICES  —  {datetime.now().strftime('%d %B %Y')}")
    print(f"{'='*W}")

    # ── Tamil Nadu ──
    print(f"\n  ┌─ TAMIL NADU  (source updated: {tn.get('updated','?')}, {tn.get('total_mandis','?')} mandis)")

    # Costliest — use max from district table if markets list is truncated
    c_kg    = tn.get("costliest_market_kg") or tn.get("costliest_max_kg")
    c_label = tn.get("costliest_market")    or tn.get("costliest_district")

    l_kg    = tn.get("lowest_market_kg")    or tn.get("lowest_min_kg")
    l_label = tn.get("lowest_market")       or tn.get("lowest_district")

    print(f"  │")
    print(f"  │  🔴 Costliest   {fmt(c_kg)}")
    print(f"  │               {c_label}")
    print(f"  │")
    print(f"  │  🟢 Lowest      {fmt(l_kg)}")
    print(f"  │               {l_label}")
    print(f"  │")
    print(f"  │  🟡 Average     {fmt(tn.get('avg_kg'))}")
    print(f"  └               State-wide average across all mandis")

    # ── Thanjavur ──
    print(f"\n  ┌─ THANJAVUR DISTRICT  (data: {tj.get('data_date','?') if tj else 'N/A'})")
    if tj:
        print(f"  │")
        print(f"  │  📍 Avg Price   {fmt(tj['avg_kg'])}")
        print(f"  │  🔴 Costliest   {fmt(tj['high_kg'])}")
        print(f"  │  🟢 Lowest      {fmt(tj['low_kg'])}")
        print(f"  │")
        print(f"  │  Market Breakdown ({tj['total']} markets):")
        print(f"  │  {'─'*52}")
        print(f"  │  {'Market':<38} {'Price/kg':>8}  {'Quintal':>10}")
        print(f"  │  {'─'*52}")
        for r in tj["markets"]:
            label = (r["market"]
                     .replace("(Uzhavar Sandhai )", "")
                     .replace("(Uzhavar Sandhai)", "")
                     .strip())
            print(f"  │  {label:<38} Rs.{r['modal_kg']:>5.1f}/kg  Rs.{r['modal_kg']*100:>7,.0f}")
        print(f"  └")
    else:
        print(f"  └  No data available")

    print(f"\n{'='*W}")
    print(f"  Source: commodityfact.org")
    print(f"  Note : Prices shown are wholesale mandi rates.")
    print(f"{'='*W}\n")


# ─── Export ───────────────────────────────────────────────────────────────────

def export_csv(tn, tj, filename):
    c_kg    = tn.get("costliest_market_kg") or tn.get("costliest_max_kg")
    c_label = tn.get("costliest_market")    or tn.get("costliest_district")
    l_kg    = tn.get("lowest_market_kg")    or tn.get("lowest_min_kg")
    l_label = tn.get("lowest_market")       or tn.get("lowest_district")

    rows = [
        {"section": "Tamil Nadu", "metric": "Costliest Market Price",
         "price_per_kg": c_kg, "price_per_quintal": (c_kg or 0)*100, "market": c_label,
         "data_date": tn.get("updated")},
        {"section": "Tamil Nadu", "metric": "Lowest Market Price",
         "price_per_kg": l_kg, "price_per_quintal": (l_kg or 0)*100, "market": l_label,
         "data_date": tn.get("updated")},
        {"section": "Tamil Nadu", "metric": "Average Market Price",
         "price_per_kg": tn.get("avg_kg"), "price_per_quintal": (tn.get("avg_kg") or 0)*100,
         "market": "State Average", "data_date": tn.get("updated")},
    ]
    if tj:
        rows += [
            {"section": "Thanjavur", "metric": "Average Price",
             "price_per_kg": tj["avg_kg"], "price_per_quintal": tj["avg_kg"]*100,
             "market": "District Average", "data_date": tj["data_date"]},
            {"section": "Thanjavur", "metric": "Costliest Price",
             "price_per_kg": tj["high_kg"], "price_per_quintal": tj["high_kg"]*100,
             "market": "District Costliest", "data_date": tj["data_date"]},
            {"section": "Thanjavur", "metric": "Lowest Price",
             "price_per_kg": tj["low_kg"], "price_per_quintal": tj["low_kg"]*100,
             "market": "District Lowest", "data_date": tj["data_date"]},
        ]
        for r in tj["markets"]:
            rows.append({
                "section": "Thanjavur Markets", "metric": "Market Price",
                "price_per_kg": r["modal_kg"], "price_per_quintal": r["modal_kg"]*100,
                "market": r["market"], "data_date": tj["data_date"],
            })

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"[+] CSV saved -> {filename}")


def export_json(tn, tj, filename):
    c_kg    = tn.get("costliest_market_kg") or tn.get("costliest_max_kg")
    c_label = tn.get("costliest_market")    or tn.get("costliest_district")
    l_kg    = tn.get("lowest_market_kg")    or tn.get("lowest_min_kg")
    l_label = tn.get("lowest_market")       or tn.get("lowest_district")

    out = {
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "commodityfact.org",
        "tamil_nadu": {
            "updated": tn.get("updated"),
            "total_mandis": tn.get("total_mandis"),
            "costliest": {"market": c_label, "price_per_kg": c_kg},
            "lowest":    {"market": l_label, "price_per_kg": l_kg},
            "average":   {"price_per_kg": tn.get("avg_kg")},
        },
        "thanjavur": {
            "data_date":        tj["data_date"] if tj else None,
            "average_per_kg":   tj["avg_kg"]    if tj else None,
            "costliest_per_kg": tj["high_kg"]   if tj else None,
            "lowest_per_kg":    tj["low_kg"]    if tj else None,
            "markets": [
                {"market": r["market"], "price_per_kg": r["modal_kg"],
                 "min_per_kg": r["min_kg"], "max_per_kg": r["max_kg"], "date": r["date"]}
                for r in (tj["markets"] if tj else [])
            ],
        },
    }
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"[+] JSON saved -> {filename}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Coconut price scraper (commodityfact.org)")
    parser.add_argument("--export", choices=["csv", "json", "both"], default=None)
    parser.add_argument("--output", default=None, help="Output filename base (no extension)")
    args = parser.parse_args()

    tn = scrape_tamil_nadu()
    tj = scrape_thanjavur()

    if not tn and not tj:
        print("[!] No data retrieved.")
        return

    print_summary(tn or {}, tj)

    base = args.output or f"coconut_prices_{datetime.now().strftime('%Y%m%d_%H%M')}"
    if args.export == "csv":
        export_csv(tn, tj, f"{base}.csv")
    elif args.export == "json":
        export_json(tn, tj, f"{base}.json")
    elif args.export == "both":
        export_csv(tn, tj, f"{base}.csv")
        export_json(tn, tj, f"{base}.json")


if __name__ == "__main__":
    main()