#!/usr/bin/env python3
"""
CocoTrack Web UI — Flask wrapper around core.py
Run: python app.py  →  http://localhost:3333
"""
from flask import Flask, render_template, request, jsonify
import sys, os, uuid, re, secrets, json, subprocess, time, threading, webbrowser
sys.path.insert(0, os.path.dirname(__file__))

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

import coconut_price_scraper

from core import (
    load_data, save_data, generate_harvest_id,
    get_current_season, get_harvest_interval, detect_climate_from_location,
    get_settings, DEFAULT_SETTINGS,
    calculate_sell_as_pieces, calculate_sell_by_weight, calculate_sell_as_copra,
    fetch_weather, rain_advisory,
)
from datetime import datetime, timedelta
from flask import session

app = Flask(__name__, template_folder=resource_path('templates'))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_urlsafe(48)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=262_144,
)

# When bundled, we save dynamic/writable data next to the EXE
# Templates are bundled inside the EXE
if getattr(sys, 'frozen', False):
    DATA_DIR = os.path.dirname(sys.executable)
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

COCONUT_PRICE_FILE = os.path.join(DATA_DIR, "coconut_prices_latest.json")
COCONUT_PRICE_SCRIPT = resource_path("coconut_price_scraper.py")
THANJAVUR_ALLOWED_MARKETS = {"tirukattupalli", "kumbakonam", "pattukottai"}

COCONUT_PRICE_SNAPSHOT = {
    "source": "commodityfact.org",
    "tamil_nadu": {
        "updated": "6 March 2026",
        "average_kg": 55.5,
        "average_quintal": 5555,
        "summary": "In Tamil Nadu, the average wholesale price for Coconut is currently Rs.5,555/quintal.",
        "total_mandis": 146,
        "costliest": {
            "market": "Dharmapuri (Uzhavar Sandhai)",
            "price_kg": 78.0,
            "price_quintal": 7800,
        },
        "lowest": {
            "market": "RSPuram (Uzhavar Sandhai)",
            "price_kg": 52.0,
            "price_quintal": 5200,
        },
    },
    "thanjavur": {
        "data_date": "6 March 2026",
        "average_kg": 65.0,
        "average_quintal": 6500,
        "costliest": {
            "market": "Tirukattupalli",
            "price_kg": 70.0,
            "price_quintal": 7000,
        },
        "lowest": {
            "market": "Pattukottai",
            "price_kg": 60.0,
            "price_quintal": 6000,
        },
        "markets": [
            {"market": "Tirukattupalli", "price_kg": 70.0, "price_quintal": 7000},
            {"market": "Kumbakonam", "price_kg": 65.0, "price_quintal": 6500},
            {"market": "Pattukottai", "price_kg": 60.0, "price_quintal": 6000},
        ],
    },
}

# ─── Fertilizer master catalogue ─────────────────────────────────────────────
# key → label, how often to reapply, buying unit, default price
FERT_CATALOGUE = {
    "urea":              {"label": "Urea",                    "interval_days": 90,  "unit": "kg",  "price": 0},
    "dap":               {"label": "DAP",                     "interval_days": 120, "unit": "kg",  "price": 0},
    "mop":               {"label": "MOP / Red Potash",        "interval_days": 120, "unit": "kg",  "price": 0},
    "ssp":               {"label": "SSP",                     "interval_days": 120, "unit": "kg",  "price": 0},
    "fym":               {"label": "FYM (Farm Yard Manure)",  "interval_days": 180, "unit": "kg",  "price": 0},
    "neem_cake":         {"label": "Neem Cake",               "interval_days": 180, "unit": "kg",  "price": 0},
    "vermicompost":      {"label": "Vermicompost",            "interval_days": 180, "unit": "kg",  "price": 0},
    "boron":             {"label": "Boron",                   "interval_days": 365, "unit": "kg",  "price": 0},
    "zinc_sulphate":     {"label": "Zinc Sulphate",           "interval_days": 365, "unit": "kg",  "price": 0},
    "potassium_nitrate": {"label": "Potassium Nitrate",       "interval_days": 90,  "unit": "kg",  "price": 0},
    "custom":            {"label": "Custom / Other",          "interval_days": 90,  "unit": "kg",  "price": 0},
}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def ok(payload=None):
    d = {"ok": True, "data": payload}
    return jsonify(d)

def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors
    if hasattr(e, "code") and hasattr(e, "description"):
        return err(e.description, e.code)
    # Handle non-HTTP exceptions only
    import traceback
    
    # Hide full server paths from the user
    raw_error = str(e)
    error_details = raw_error.replace("\\", "/").split("/")[-1]
    
    # If it's a permission error specifically, give a helpful tip
    if isinstance(e, PermissionError):
        error_details = "Permission Denied: The server cannot write to the data file. Please check file permissions on the server."
    elif isinstance(e, json.JSONDecodeError):
        error_details = "Corrupted data file detected."
    
    # Log the full traceback to console for the developer
    print(traceback.format_exc())
    return err(f"Internal Server Error: {error_details}", 500)


def _normalize_market_name(name):
    base = (name or "").split("(")[0].strip().lower()
    return re.sub(r"\s+", " ", base)


def _normalize_coconut_price_payload(payload):
    payload = payload or {}
    tn = payload.get("tamil_nadu") or {}
    tj = payload.get("thanjavur") or {}
    tj_markets = [
        m for m in (tj.get("markets") or [])
        if _normalize_market_name(m.get("market")) in THANJAVUR_ALLOWED_MARKETS
    ]

    costliest_market = max(tj_markets, key=lambda m: float(m.get("price_per_kg", 0) or 0), default={})
    lowest_market = min(tj_markets, key=lambda m: float(m.get("price_per_kg", 0) or 0), default={}) if tj_markets else {}
    average_market_price = (
        round(sum(float(m.get("price_per_kg", 0) or 0) for m in tj_markets) / len(tj_markets), 2)
        if tj_markets else COCONUT_PRICE_SNAPSHOT["thanjavur"]["average_kg"]
    )

    return {
        "source": payload.get("source") or COCONUT_PRICE_SNAPSHOT.get("source"),
        "tamil_nadu": {
            "updated": tn.get("updated") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["updated"],
            "average_kg": float((tn.get("average") or {}).get("price_per_kg") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["average_kg"]),
            "total_mandis": int(tn.get("total_mandis") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["total_mandis"]),
            "costliest": {
                "market": (tn.get("costliest") or {}).get("market") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["costliest"]["market"],
                "price_kg": float((tn.get("costliest") or {}).get("price_per_kg") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["costliest"]["price_kg"]),
            },
            "lowest": {
                "market": (tn.get("lowest") or {}).get("market") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["lowest"]["market"],
                "price_kg": float((tn.get("lowest") or {}).get("price_per_kg") or COCONUT_PRICE_SNAPSHOT["tamil_nadu"]["lowest"]["price_kg"]),
            },
        },
        "thanjavur": {
            "data_date": tj.get("data_date") or COCONUT_PRICE_SNAPSHOT["thanjavur"]["data_date"],
            "average_kg": average_market_price,
            "costliest": {
                "market": costliest_market.get("market") or COCONUT_PRICE_SNAPSHOT["thanjavur"]["costliest"]["market"],
                "price_kg": float(costliest_market.get("price_per_kg") or COCONUT_PRICE_SNAPSHOT["thanjavur"]["costliest"]["price_kg"]),
            },
            "lowest": {
                "market": lowest_market.get("market") or COCONUT_PRICE_SNAPSHOT["thanjavur"]["lowest"]["market"],
                "price_kg": float(lowest_market.get("price_per_kg") or COCONUT_PRICE_SNAPSHOT["thanjavur"]["lowest"]["price_kg"]),
            },
            "markets": [
                {
                    "market": m.get("market", "—"),
                    "price_kg": float(m.get("price_per_kg") or 0),
                }
                for m in tj_markets
            ] or json.loads(json.dumps(COCONUT_PRICE_SNAPSHOT["thanjavur"]["markets"])),
        },
    }


def _get_coconut_price_snapshot():
    if os.path.exists(COCONUT_PRICE_FILE):
        try:
            with open(COCONUT_PRICE_FILE, "r", encoding="utf-8") as f:
                return _normalize_coconut_price_payload(json.load(f))
        except Exception:
            pass
    return json.loads(json.dumps(COCONUT_PRICE_SNAPSHOT))


    with open(COCONUT_PRICE_FILE, "r", encoding="utf-8") as f:
        return _normalize_coconut_price_payload(json.load(f))


def _refresh_coconut_price_snapshot():
    """
    Refresh prices by calling the scraper logic directly.
    We avoid subprocess.run([sys.executable, ...]) because in a bundled EXE,
    sys.executable is the EXE itself, and running it again spawns a new app instance
    (and a new browser tab).
    """
    try:
        # Mocking sys.argv to pass arguments to the scraper's main
        base = os.path.splitext(COCONUT_PRICE_FILE)[0]
        import argparse
        from unittest.mock import patch
        
        args = ["coconut_price_scraper.py", "--export", "json", "--output", base]
        with patch("sys.argv", args):
            coconut_price_scraper.main()
            
        if not os.path.exists(COCONUT_PRICE_FILE):
            raise RuntimeError("Price file was not generated")
            
        with open(COCONUT_PRICE_FILE, "r", encoding="utf-8") as f:
            return _normalize_coconut_price_payload(json.load(f))
    except Exception as e:
        raise RuntimeError(f"Scraper failed: {e}")


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _payload_has_unsafe_text(payload):
    # Blocklist for common XSS vectors and sensitive tags
    BLOCKLIST = [
        "<script", "javascript:", "data:text/html", "onerror=", "onload=",
        "onmouseover=", "onfocus=", "onclick=", "<img", "<iframe",
        "<svg", "<object", "<embed", "<style", "<link", "<meta", "<base"
    ]
    for s in _iter_strings(payload):
        if not s: continue
        low = s.lower()
        if len(s) > 10000:
            return "Input too long"
        if "\x00" in s:
            return "Invalid characters in input"
        if any(x in low for x in BLOCKLIST):
            return "Potentially unsafe input detected"
        if "<" in s or ">" in s:
            return "HTML tags are not allowed in fields"
    return None


def _is_json_write_request():
    return request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH"}

def _is_mutating_request():
    return request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}



def strip_rich(s):
    return re.sub(r'\[/?[^\]]+\]', '', str(s))

def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def _safe_int(v, fallback):
    try:
        return int(float(v))
    except:
        return fallback

def _get_harvest_interval_from_settings(data, season=None):
    season = season or get_current_season()
    s = get_settings(data)
    if season == "summer":
        lo = _safe_int(s.get("harvest_interval_summer_lo", 30), 30)
        hi = _safe_int(s.get("harvest_interval_summer_hi", 35), 35)
    else:
        lo = _safe_int(s.get("harvest_interval_nonsummer_lo", 40), 40)
        hi = _safe_int(s.get("harvest_interval_nonsummer_hi", 50), 50)
    lo = max(1, lo)
    hi = max(lo, hi)
    return lo, hi

def _get_fertilizer_interval_days(data):
    s = get_settings(data)
    return max(1, _safe_int(s.get("fertilizer_interval_days", 365), 365))

def _normalize_composition(comp):
    # Backward compatibility: old per-farm+type presets are converted to age-based bundles.
    if comp.get("fertilizers"):
        age_min = float(comp.get("age_min_years", 0))
        age_max = float(comp.get("age_max_years", 100))
        normalized = []
        for f in comp.get("fertilizers", []):
            ftype = f.get("fertilizer_type", "custom")
            cat = FERT_CATALOGUE.get(ftype, FERT_CATALOGUE["custom"])
            normalized.append({
                "fertilizer_type": ftype,
                "fertilizer_label": f.get("fertilizer_label") or cat["label"],
                "qty_per_tree": float(f.get("qty_per_tree", 0)),
                # Keep units aligned with catalogue (notably FYM should be kg/tree).
                "unit": cat["unit"],
            })
        c = dict(comp)
        c["fertilizers"] = normalized
        c["farm_name"] = ""
        c["preset_name"] = _clean_preset_name(comp.get("preset_name"), comp.get("farm_name", ""), age_min, age_max)
        c["age_min_years"] = age_min
        c["age_max_years"] = age_max
        return c
    ftype = comp.get("fertilizer_type", "custom")
    label = comp.get("fertilizer_label") or FERT_CATALOGUE.get(ftype, {}).get("label", ftype.title())
    return {
        "id": comp.get("id", str(uuid.uuid4())[:8]),
        "farm_id": comp.get("farm_id", ""),
        "farm_name": comp.get("farm_name", ""),
        "preset_name": comp.get("preset_name") or "Default Program",
        "age_min_years": float(comp.get("age_min_years", 0)),
        "age_max_years": float(comp.get("age_max_years", 100)),
        "fertilizers": [{
            "fertilizer_type": ftype,
            "fertilizer_label": label,
            "qty_per_tree": float(comp.get("qty_per_tree", 0)),
            "unit": FERT_CATALOGUE.get(ftype, FERT_CATALOGUE["custom"])["unit"],
        }],
        "application_method": comp.get("application_method", ""),
        "notes": comp.get("notes", ""),
        "updated_at": comp.get("updated_at", datetime.now().isoformat()),
    }


def _clean_preset_name(raw_name, farm_name, age_min, age_max):
    raw = (raw_name or "").strip()
    farm = (farm_name or "").strip()
    if farm and raw:
        raw = re.sub(rf"^\s*{re.escape(farm)}\s*[-:|]\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(rf"\(\s*{re.escape(farm)}\s*\)", "", raw, flags=re.IGNORECASE)
        raw = re.sub(rf"\b{re.escape(farm)}\b", "", raw, flags=re.IGNORECASE).strip(" -:|")
    return raw or f"Age {int(age_min)}-{int(age_max)} years"
def _ensure_keys(data):
    """Make sure all new top-level keys exist in data dict."""
    for k in ["farms","harvests","fertilizers","expenses","settings",
              "fertilizer_prices","fertilizer_compositions","fertilizer_jobs"]:
        data.setdefault(k, [] if k not in ("settings","fertilizer_prices") else {})
    return data

def _sum_fertilizer_expenses(data):
    total = 0.0
    jobs = data.get("fertilizer_jobs", [])
    seen_job_ids = set()
    for job in jobs:
        total += float(job.get("total_cost", 0) or 0)
        if job.get("job_id"):
            seen_job_ids.add(job["job_id"])
    # Backward compatibility: include legacy fertilizer records not linked to a job.
    for fert in data.get("fertilizers", []):
        job_id = fert.get("job_id")
        if job_id and job_id in seen_job_ids:
            continue
        total += float(fert.get("cost", 0) or 0)
    return round(total, 2)

def _sum_other_expenses(data):
    return round(sum(float(e.get("amount", 0) or 0) for e in data.get("expenses", [])), 2)

def _parse_date_safe(date_text):
    try:
        return datetime.strptime(str(date_text), "%Y-%m-%d").date()
    except:
        return None

def _pct_change(curr, prev):
    curr = float(curr or 0)
    prev = float(prev or 0)
    if prev == 0:
        return 0.0 if curr == 0 else None
    return round(((curr - prev) / prev) * 100.0, 2)

def _sum_for_month(records, year, month):
    return round(sum(v for d, v in records if d and d.year == year and d.month == month), 2)

def _sum_for_year(records, year):
    return round(sum(v for d, v in records if d and d.year == year), 2)

def _build_expense_insights(data):
    today = datetime.now().date()
    cur_year = today.year
    prev_year = cur_year - 1
    if today.month == 1:
        prev_month_year, prev_month = cur_year - 1, 12
    else:
        prev_month_year, prev_month = cur_year, today.month - 1

    total_records = []
    fertilizer_records = []
    harvest_records = []

    for h in data.get("harvests", []):
        d = _parse_date_safe(h.get("harvest_date"))
        amt = float(h.get("total_expenses", 0) or 0)
        if d and amt:
            total_records.append((d, amt))
            harvest_records.append((d, amt))

    jobs = data.get("fertilizer_jobs", [])
    job_ids = {j.get("job_id") for j in jobs if j.get("job_id")}
    for job in jobs:
        sessions = job.get("sessions", []) or []
        consumed = 0.0
        for s in sessions:
            d = _parse_date_safe(s.get("date"))
            amt = float(s.get("expense", 0) or 0)
            if d and amt:
                total_records.append((d, amt))
                fertilizer_records.append((d, amt))
                consumed += amt
        remaining = round(float(job.get("total_cost", 0) or 0) - consumed, 2)
        if remaining > 0:
            d = _parse_date_safe(job.get("completed_date") or job.get("start_date"))
            if d:
                total_records.append((d, remaining))
                fertilizer_records.append((d, remaining))

    for f in data.get("fertilizers", []):
        if f.get("job_id") and f.get("job_id") in job_ids:
            continue
        d = _parse_date_safe(f.get("applied_date"))
        amt = float(f.get("cost", 0) or 0)
        if d and amt:
            total_records.append((d, amt))
            fertilizer_records.append((d, amt))

    for e in data.get("expenses", []):
        d = _parse_date_safe(e.get("date"))
        amt = float(e.get("amount", 0) or 0)
        if d and amt:
            total_records.append((d, amt))

    monthly = _sum_for_month(total_records, today.year, today.month)
    prev_month_val = _sum_for_month(total_records, prev_month_year, prev_month)
    yearly = _sum_for_year(total_records, cur_year)
    prev_year_val = _sum_for_year(total_records, prev_year)

    fert_cur_year = _sum_for_year(fertilizer_records, cur_year)
    fert_prev_year = _sum_for_year(fertilizer_records, prev_year)

    sorted_harvest = sorted(harvest_records, key=lambda x: x[0])
    harvest_latest = sorted_harvest[-1][1] if sorted_harvest else 0.0
    harvest_prev = sorted_harvest[-2][1] if len(sorted_harvest) >= 2 else 0.0

    return {
        "monthly_expense": monthly,
        "yearly_expense": yearly,
        "overall_mom_pct": _pct_change(monthly, prev_month_val),
        "overall_yoy_pct": _pct_change(yearly, prev_year_val),
        "fertilizer_yoy_pct": _pct_change(fert_cur_year, fert_prev_year),
        "harvest_latest_expense": round(harvest_latest, 2),
        "harvest_previous_expense": round(harvest_prev, 2),
        "harvest_vs_prev_pct": _pct_change(harvest_latest, harvest_prev),
    }

def _build_harvest_projection(harvests):
    ordered = sorted(harvests, key=lambda h: h.get("harvest_date", ""))
    nuts = [float(h.get("nuts_harvested", 0) or 0) for h in ordered]
    prev_nuts = int(nuts[-2]) if len(nuts) >= 2 else 0
    current_nuts = int(nuts[-1]) if nuts else 0

    if nuts:
        recent = nuts[-3:] if len(nuts) >= 3 else nuts
        baseline = sum(recent) / len(recent)
        if len(nuts) >= 2:
            raw_step = (nuts[-1] - nuts[0]) / max(len(nuts) - 1, 1)
            cap = abs(baseline * 0.12)
            trend_step = max(-cap, min(cap, raw_step))
        else:
            trend_step = 0.0
        predicted = [max(0, int(round(baseline + trend_step * i))) for i in (1, 2, 3)]
    else:
        predicted = [0, 0, 0]

    return {
        "labels": ["Previous", "Current", "Predicted +1", "Predicted +2", "Predicted +3"],
        "values": [prev_nuts, current_nuts] + predicted,
    }


def _iter_harvest_farm_entries(harvests):
    for h in harvests:
        farms = h.get("farms") or []
        total_good = sum(int(f.get("good_nuts", 0) or 0) for f in farms) or max(int(h.get("good_nuts", 0) or 0), 1)
        total_nuts = sum(int(f.get("nuts_harvested", 0) or 0) for f in farms) or max(int(h.get("nuts_harvested", 0) or 0), 1)
        if farms:
            for farm in farms:
                merged = dict(farm)
                merged["harvest_id"] = h.get("harvest_id", "")
                merged["harvest_date"] = h.get("harvest_date", "")
                merged["selling_price"] = h.get("selling_price", 0)
                merged["sale_mode"] = h.get("sale_mode", "yet_to_decide")
                merged["revenue_share"] = round(float(h.get("revenue", 0) or 0) * (int(farm.get("good_nuts", 0) or 0) / total_good), 2)
                merged["profit_share"] = round(float(h.get("profit", 0) or 0) * (int(farm.get("nuts_harvested", 0) or 0) / total_nuts), 2)
                yield merged
            continue

        fid = h.get("farm_id")
        if not fid:
            continue
        yield {
            "harvest_id": h.get("harvest_id", ""),
            "harvest_date": h.get("harvest_date", ""),
            "selling_price": h.get("selling_price", 0),
            "sale_mode": h.get("sale_mode", "yet_to_decide"),
            "farm_id": fid,
            "farm_name": h.get("farm_name", ""),
            "num_trees": h.get("num_trees", 0),
            "nuts_harvested": h.get("nuts_harvested", 0),
            "defective_nuts": h.get("defective_nuts", 0),
            "good_nuts": h.get("good_nuts", 0),
            "nuts_per_tree": h.get("nuts_per_tree", 0),
            "revenue_share": h.get("revenue", 0),
            "profit_share": h.get("profit", 0),
        }


HARVEST_SALE_MODES = {"yet_to_decide", "sold_per_pcs", "sold_in_kgs", "copra"}


def _coerce_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _coerce_int(value, default=0):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _normalize_sale_mode(value):
    mode = str(value or "yet_to_decide").strip().lower()
    aliases = {
        "yet_to_decide": "yet_to_decide",
        "undecided": "yet_to_decide",
        "sold_per_pcs": "sold_per_pcs",
        "pieces": "sold_per_pcs",
        "pcs": "sold_per_pcs",
        "sold_in_kgs": "sold_in_kgs",
        "weight": "sold_in_kgs",
        "kgs": "sold_in_kgs",
        "kg": "sold_in_kgs",
        "copra": "copra",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in HARVEST_SALE_MODES else "yet_to_decide"


def _normalize_harvest_sale(entry):
    mode = _normalize_sale_mode(entry.get("sale_mode"))
    details = dict(entry.get("sale_details") or {})
    good_nuts = _coerce_int(entry.get("good_nuts", entry.get("nuts_harvested", 0)))

    if mode == "yet_to_decide" and _coerce_float(entry.get("selling_price")) > 0:
        mode = "sold_per_pcs"
        details.setdefault("price_per_nut", _coerce_float(entry.get("selling_price")))
        details.setdefault("actual_nuts_sold", good_nuts)

    if mode == "sold_per_pcs":
        price = _coerce_float(details.get("price_per_nut", entry.get("selling_price", 0)))
        actual = max(_coerce_int(details.get("actual_nuts_sold", good_nuts)), 0)
        details = {
            "price_per_nut": price,
            "actual_nuts_sold": actual,
        }
        entry["selling_price"] = price
    elif mode == "sold_in_kgs":
        details = {
            "total_tons_sold": _coerce_float(details.get("total_tons_sold")),
            "rate_per_ton": _coerce_float(details.get("rate_per_ton")),
        }
        entry["selling_price"] = 0.0
    elif mode == "copra":
        details = {
            "copra_grade1_kg": _coerce_float(details.get("copra_grade1_kg")),
            "copra_grade1_income": _coerce_float(details.get("copra_grade1_income")),
            "copra_grade2_kg": _coerce_float(details.get("copra_grade2_kg")),
            "copra_grade2_income": _coerce_float(details.get("copra_grade2_income")),
            "copra_grade3_kg": _coerce_float(details.get("copra_grade3_kg")),
            "copra_grade3_income": _coerce_float(details.get("copra_grade3_income")),
            "shells_kg": _coerce_float(details.get("shells_kg")),
            "shells_income": _coerce_float(details.get("shells_income")),
            "husk_income": _coerce_float(details.get("husk_income")),
            "processing_cost": _coerce_float(details.get("processing_cost")),
        }
        entry["selling_price"] = 0.0
    else:
        details = {}
        entry["selling_price"] = 0.0

    entry["sale_mode"] = mode
    entry["sale_details"] = details
    return mode, details


def _compute_harvest_revenue(entry):
    mode, details = _normalize_harvest_sale(entry)
    if mode == "sold_per_pcs":
        return round(max(_coerce_int(details.get("actual_nuts_sold")), 0) * _coerce_float(details.get("price_per_nut")), 2)
    if mode == "sold_in_kgs":
        return round(_coerce_float(details.get("total_tons_sold")) * _coerce_float(details.get("rate_per_ton")), 2)
    if mode == "copra":
        return round(
            _coerce_float(details.get("copra_grade1_income"))
            + _coerce_float(details.get("copra_grade2_income"))
            + _coerce_float(details.get("copra_grade3_income"))
            + _coerce_float(details.get("shells_income"))
            + _coerce_float(details.get("husk_income")),
            2,
        )
    return 0.0


def _compute_harvest_sale_expenses(entry):
    mode, details = _normalize_harvest_sale(entry)
    if mode == "copra":
        return round(_coerce_float(details.get("processing_cost")), 2)
    return 0.0


def _refresh_harvests(data, persist=False):
    changed = False
    for harvest in data.get("harvests", []):
        before = json.dumps(harvest, sort_keys=True, default=str)
        _recalculate_harvest(harvest, data)
        after = json.dumps(harvest, sort_keys=True, default=str)
        changed = changed or before != after
    if changed and persist:
        save_data(data)
    return data.get("harvests", [])


def _build_farm_performance(harvests, farms):
    farm_map = {f.get("id"): f for f in farms}
    farm_totals = {}
    current_year = datetime.now().year
    farm_totals_year = {}

    for h in _iter_harvest_farm_entries(harvests):
        fid = h.get("farm_id")
        if not fid:
            continue
        nuts = float(h.get("nuts_harvested", 0) or 0)
        farm_totals[fid] = farm_totals.get(fid, 0.0) + nuts

        hd = _parse_date_safe(h.get("harvest_date"))
        if hd and hd.year == current_year:
            farm_totals_year[fid] = farm_totals_year.get(fid, 0.0) + nuts

    ranked = []
    total_nuts = 0.0
    total_trees = 0
    for fid, nuts in farm_totals.items():
        farm = farm_map.get(fid, {})
        trees = int(farm.get("num_trees", 0) or 0)
        if trees <= 0:
            continue
        ranked.append({
            "farm_id": fid,
            "farm_name": farm.get("name") or "Unknown Farm",
            "total_nuts": int(round(nuts)),
            "total_trees": trees,
            "nuts_per_tree": round(nuts / trees, 2),
        })
        total_nuts += nuts
        total_trees += trees

    total_nuts_year = 0.0
    total_trees_year = 0
    for fid, nuts in farm_totals_year.items():
        farm = farm_map.get(fid, {})
        trees = int(farm.get("num_trees", 0) or 0)
        if trees <= 0:
            continue
        total_nuts_year += nuts
        total_trees_year += trees

    ranked.sort(key=lambda x: x["nuts_per_tree"], reverse=True)
    return {
        "top_farm": ranked[0] if ranked else None,
        "worst_farm": ranked[-1] if ranked else None,
        "average_nuts_per_tree": round(total_nuts / total_trees, 2) if total_trees > 0 else 0.0,
        "average_nuts_per_tree_year": round(total_nuts_year / total_trees_year, 2) if total_trees_year > 0 else 0.0,
        "year": current_year,
    }


def _recalculate_harvest(entry, data=None):
    farms = entry.get("farms") or []
    labour_cost = float(entry.get("labour_cost", 0) or 0)
    transport_cost = float(entry.get("transport_cost", 0) or 0)
    other_expenses = float(entry.get("other_expenses", 0) or 0)

    if farms:
        total_nuts = 0
        total_defective = 0
        total_good = 0
        total_trees = 0
        farm_names = []
        for farm in farms:
            nuts = int(farm.get("nuts_harvested", 0) or 0)
            defective = int(farm.get("defective_nuts", 0) or 0)
            good = max(nuts - defective, 0)
            trees = int(farm.get("num_trees", 0) or 0)
            farm["nuts_harvested"] = nuts
            farm["defective_nuts"] = defective
            farm["good_nuts"] = good
            farm["nuts_per_tree"] = round(nuts / max(trees, 1), 2)
            total_nuts += nuts
            total_defective += defective
            total_good += good
            total_trees += trees
            if farm.get("farm_name"):
                farm_names.append(farm["farm_name"])

        entry.update(
            nuts_harvested=total_nuts,
            defective_nuts=total_defective,
            good_nuts=total_good,
            nuts_per_tree=round(total_nuts / max(total_trees, 1), 2),
            farm_count=len(farms),
            farm_names=", ".join(farm_names),
        )
    else:
        num_trees = int(entry.get("num_trees", 0) or 0)
        good = int(entry["nuts_harvested"]) - int(entry["defective_nuts"])
        entry.update(
            good_nuts=good,
            nuts_per_tree=round(int(entry["nuts_harvested"]) / max(num_trees, 1), 2),
            farm_count=1 if entry.get("farm_id") else 0,
            farm_names=entry.get("farm_name", ""),
        )

    revenue = _compute_harvest_revenue(entry)
    expenses = labour_cost + transport_cost + other_expenses + _compute_harvest_sale_expenses(entry)
    entry.update(
        revenue=round(revenue, 2),
        total_expenses=round(expenses, 2),
        profit=round(revenue - expenses, 2),
    )

    try:
        hdate = datetime.strptime(entry["harvest_date"], "%Y-%m-%d")
        season = get_current_season(hdate.month)
        lo, hi = _get_harvest_interval_from_settings(data, season)
        entry.update(
            season=season,
            next_harvest_from=(hdate + timedelta(days=lo)).strftime("%Y-%m-%d"),
            next_harvest_to=(hdate + timedelta(days=hi)).strftime("%Y-%m-%d"),
        )
    except:
        entry.setdefault("season", "unknown")
        entry.setdefault("next_harvest_from", "")
        entry.setdefault("next_harvest_to", "")
    return entry

# ─── Pages ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ─── Auth ────────────────────────────────────────────────────────────────────
@app.before_request
def security_checks():
    if _is_json_write_request():
        if not request.is_json:
            return err("JSON body required", 415)
        body = request.get_json(silent=True)
        if body is None:
            return err("Invalid JSON payload", 400)
        unsafe = _payload_has_unsafe_text(body)
        if unsafe:
            return err(unsafe, 400)


@app.after_request
def apply_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Cache-Control"] = "no-store"
    # Keep CSP compatible with current inline scripts/styles while blocking remote script injection.
    resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resp

@app.route("/api/auth/csrf", methods=["GET"])
def auth_csrf():
    return ok({"token": "disabled"})

@app.route("/api/auth/status")
def auth_status():
    return ok({"setup_required": False, "authenticated": True})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    return ok({"authenticated": False})

# ─── Farmer ───────────────────────────────────────────────────────────────────
@app.route("/api/farmer", methods=["GET"])
def get_farmer():
    return ok(load_data().get("farmer"))

@app.route("/api/farmer", methods=["POST"])
def save_farmer():
    data = load_data()
    body = request.json
    for f in ["name","phone","village","district","state"]:
        if not body.get(f):
            return err(f"Field '{f}' is required")
    body["climate_type"] = detect_climate_from_location(body["district"])
    body["joined"] = body.get("joined") or datetime.now().isoformat()
    data["farmer"] = body
    save_data(data)
    return ok(body)

# ─── Farms ────────────────────────────────────────────────────────────────────
@app.route("/api/farms", methods=["GET"])
def get_farms():
    return ok(load_data().get("farms", []))

@app.route("/api/farms", methods=["POST"])
def add_farm():
    data = load_data()
    body = request.json
    if not body.get("name"):
        return err("Farm name is required")
    body["id"]        = f"FARM{len(data['farms'])+1:03d}"
    body["crop_type"] = "coconut"
    body["added_on"]  = datetime.now().isoformat()
    body.setdefault("intercropping", False)
    body.setdefault("intercrop_names", "")
    body.setdefault("intercrop_area", 0.0)
    data["farms"].append(body)
    save_data(data)
    return ok(body)

@app.route("/api/farms/<farm_id>", methods=["PUT"])
def update_farm(farm_id):
    data = load_data()
    for i, f in enumerate(data["farms"]):
        if f["id"] == farm_id:
            upd = request.json
            upd["id"]        = farm_id           # never overwrite
            upd["crop_type"] = f.get("crop_type","coconut")
            upd["added_on"]  = f.get("added_on","")
            data["farms"][i].update(upd)
            save_data(data)
            return ok(data["farms"][i])
    return err("Farm not found", 404)

@app.route("/api/farms/<farm_id>", methods=["DELETE"])
def delete_farm(farm_id):
    data = load_data()
    # Remove the farm
    f_count = len(data.get("farms", []))
    data["farms"] = [f for f in data.get("farms", []) if f.get("id") != farm_id]
    
    if len(data.get("farms", [])) == f_count:
        return err("Farm not found", 404)
        
    # Cascading deletes
    updated_harvests = []
    for h in data.get("harvests", []):
        farms = h.get("farms") or []
        if farms:
            kept_farms = [fh for fh in farms if fh.get("farm_id") != farm_id]
            if not kept_farms:
                continue
            if len(kept_farms) != len(farms):
                h["farms"] = kept_farms
                _recalculate_harvest(h, data)
        elif h.get("farm_id") == farm_id:
            continue
        updated_harvests.append(h)
    data["harvests"] = updated_harvests
    data["fertilizer_jobs"] = [j for j in data.get("fertilizer_jobs", []) if j.get("farm_id") != farm_id]
    data["fertilizer_compositions"] = [c for c in data.get("fertilizer_compositions", []) if c.get("farm_id") != farm_id]
    data["fertilizers"] = [f for f in data.get("fertilizers", []) if f.get("farm_id") != farm_id]
    
    save_data(data)
    return ok({"deleted": farm_id})

# ─── Harvests ─────────────────────────────────────────────────────────────────
@app.route("/api/harvests", methods=["GET"])
def get_harvests():
    data     = load_data()
    farm_id  = request.args.get("farm_id")
    harvests = _refresh_harvests(data, persist=True)
    if farm_id:
        harvests = [
            h for h in harvests
            if h.get("farm_id") == farm_id or any(f.get("farm_id") == farm_id for f in (h.get("farms") or []))
        ]
    return ok(sorted(harvests, key=lambda x: x.get("harvest_date",""), reverse=True))

@app.route("/api/harvests", methods=["POST"])
def add_harvest():
    data = load_data()
    body = request.json
    farm = next((f for f in data["farms"] if f["id"] == body.get("farm_id")), None)
    if not farm:
        return err("Farm not found")
    entry = {
        "harvest_id": generate_harvest_id(data),
        "harvest_date": body.get("harvest_date", today_str()),
        "farms": [{
            "farm_id": farm["id"],
            "farm_name": farm["name"],
            "num_trees": farm["num_trees"],
            "nuts_harvested": int(body.get("nuts_harvested", 0)),
            "defective_nuts": int(body.get("defective_nuts", 0)),
        }],
        "sale_mode": _normalize_sale_mode(body.get("sale_mode")),
        "sale_details": body.get("sale_details") or {},
        "selling_price": float(body.get("selling_price", 0)),
        "labour_cost": float(body.get("labour_cost", 0)),
        "transport_cost": float(body.get("transport_cost", 0)),
        "other_expenses": float(body.get("other_expenses", 0)),
        "notes": body.get("notes", ""),
        "logged_at": datetime.now().isoformat(),
        "last_edited": "",
    }
    _recalculate_harvest(entry, data)
    data["harvests"].append(entry)
    save_data(data)
    return ok(entry)
@app.route("/api/harvests/bulk", methods=["POST"])
def bulk_harvest():
    data = load_data()
    body = request.json
    harvest_date = body.get("harvest_date", today_str())
    labour_cost = float(body.get("labour_cost", 0))
    transport_cost = float(body.get("transport_cost", 0))
    other_expenses = float(body.get("other_expenses", 0))
    notes = body.get("notes", "")
    farm_entries = body.get("farms", [])
    active_entries = [e for e in farm_entries if int(e.get("nuts_harvested", 0)) > 0]
    if not active_entries:
        return err("No nuts harvested in any farm")
    farm_details = []
    for e in active_entries:
        farm = next((f for f in data["farms"] if f["id"] == e["farm_id"]), None)
        if not farm:
            continue
        farm_details.append({
            "farm_id": farm["id"],
            "farm_name": farm["name"],
            "num_trees": int(farm.get("num_trees", 1) or 1),
            "nuts_harvested": int(e.get("nuts_harvested", 0) or 0),
            "defective_nuts": int(e.get("defective_nuts", 0) or 0),
        })
    if not farm_details:
        return err("No valid farms found for this harvest")
    entry = {
        "harvest_id": generate_harvest_id(data),
        "harvest_date": harvest_date,
        "farms": farm_details,
        "sale_mode": _normalize_sale_mode(body.get("sale_mode")),
        "sale_details": body.get("sale_details") or {},
        "selling_price": float(body.get("selling_price", 0)),
        "labour_cost": labour_cost,
        "transport_cost": transport_cost,
        "other_expenses": other_expenses,
        "notes": f"{notes} (Bulk Entry)".strip(),
        "logged_at": datetime.now().isoformat(),
        "last_edited": "",
    }
    _recalculate_harvest(entry, data)
    data["harvests"].append(entry)
    save_data(data)
    return ok(entry)
@app.route("/api/harvests/<harvest_id>", methods=["PUT"])
def update_harvest(harvest_id):
    """Full edit of any harvest field."""
    data = load_data()
    body = request.json
    farm_map = {f.get("id"): f for f in data.get("farms", [])}
    for h in data["harvests"]:
        if h.get("harvest_id") == harvest_id:
            for fld in ["harvest_date", "selling_price", "labour_cost", "transport_cost", "other_expenses", "notes"]:
                if fld in body:
                    h[fld] = body[fld]
            if "sale_mode" in body:
                h["sale_mode"] = _normalize_sale_mode(body.get("sale_mode"))
            if "sale_details" in body:
                h["sale_details"] = body.get("sale_details") or {}
            if "farms" in body and isinstance(body["farms"], list):
                updated_farms = []
                for farm_entry in body["farms"]:
                    fid = farm_entry.get("farm_id")
                    farm = farm_map.get(fid, {})
                    updated_farms.append({
                        "farm_id": fid,
                        "farm_name": farm_entry.get("farm_name") or farm.get("name", ""),
                        "num_trees": int(farm_entry.get("num_trees", farm.get("num_trees", 0)) or 0),
                        "nuts_harvested": int(farm_entry.get("nuts_harvested", 0) or 0),
                        "defective_nuts": int(farm_entry.get("defective_nuts", 0) or 0),
                    })
                h["farms"] = updated_farms
                for legacy_key in ["farm_id", "farm_name", "num_trees"]:
                    h.pop(legacy_key, None)
            elif not h.get("farms"):
                for fld in ["nuts_harvested", "defective_nuts"]:
                    if fld in body:
                        h[fld] = body[fld]
            _recalculate_harvest(h, data)
            h["last_edited"] = datetime.now().isoformat()
            save_data(data)
            return ok(h)
    return err("Harvest not found", 404)
@app.route("/api/harvests/<harvest_id>", methods=["PATCH"])
def patch_harvest_price(harvest_id):
    """Quick price-only patch."""
    data = load_data()
    for h in data["harvests"]:
        if h.get("harvest_id") == harvest_id:
            details = dict(h.get("sale_details") or {})
            details["price_per_nut"] = float(request.json.get("selling_price", h.get("selling_price", 0)))
            details.setdefault("actual_nuts_sold", int(h.get("good_nuts", h.get("nuts_harvested", 0)) or 0))
            h["sale_mode"] = "sold_per_pcs"
            h["sale_details"] = details
            _recalculate_harvest(h, data)
            h["last_edited"] = datetime.now().isoformat()
            save_data(data)
            return ok(h)
    return err("Harvest not found", 404)
@app.route("/api/harvests/<harvest_id>/apply-sale", methods=["POST"])
def apply_harvest_sale(harvest_id):
    data = load_data()
    body = request.json or {}
    mode = (body.get("mode") or "").strip().lower()
    if mode not in {"pieces", "weight", "copra"}:
        return err("Invalid sale option")

    for h in data.get("harvests", []):
        if h.get("harvest_id") != harvest_id:
            continue

        settings = get_settings(data)
        num_nuts = max(_coerce_int(body.get("num_nuts", h.get("nuts_harvested", 0))), 0)
        good_nuts = max(_coerce_int(body.get("good_nuts", h.get("good_nuts", num_nuts))), 0)
        harvest_expenses = (
            _coerce_float(h.get("labour_cost"))
            + _coerce_float(h.get("transport_cost"))
            + _coerce_float(h.get("other_expenses"))
        )
        requested_expenses = _coerce_float(body.get("harvest_expenses"), harvest_expenses)
        if abs(requested_expenses - harvest_expenses) > 0.009:
            h["other_expenses"] = round(_coerce_float(h.get("other_expenses")) + (requested_expenses - harvest_expenses), 2)
            harvest_expenses = requested_expenses

        if mode == "pieces":
            price_per_nut = _coerce_float(body.get("price_per_nut"))
            calculate_sell_as_pieces(num_nuts, good_nuts, price_per_nut, harvest_expenses)
            h["sale_mode"] = "sold_per_pcs"
            h["sale_details"] = {
                "actual_nuts_sold": good_nuts,
                "price_per_nut": price_per_nut,
            }
        elif mode == "weight":
            avg_weight_10 = _coerce_float(body.get("avg_weight_10"), 1.0)
            price_per_ton = _coerce_float(body.get("price_per_ton"))
            result = calculate_sell_by_weight(num_nuts, avg_weight_10, price_per_ton, harvest_expenses)
            h["sale_mode"] = "sold_in_kgs"
            h["sale_details"] = {
                "total_tons_sold": _coerce_float(result.get("total_weight_ton")),
                "rate_per_ton": price_per_ton,
            }
        else:
            avg_dehusked_10 = _coerce_float(body.get("avg_dehusked_10"), 1.0)
            price_shell = _coerce_float(body.get("price_shell"))
            price_g1 = _coerce_float(body.get("price_g1"))
            price_g2 = _coerce_float(body.get("price_g2"))
            price_g3 = _coerce_float(body.get("price_g3"))
            result = calculate_sell_as_copra(
                num_nuts,
                avg_dehusked_10,
                price_shell,
                price_g1,
                price_g2,
                price_g3,
                harvest_expenses,
                settings,
            )
            h["sale_mode"] = "copra"
            h["sale_details"] = {
                "copra_grade1_kg": _coerce_float(result.get("g1_kg")),
                "copra_grade1_income": round(_coerce_float(result.get("g1_kg")) * price_g1, 2),
                "copra_grade2_kg": _coerce_float(result.get("g2_kg")),
                "copra_grade2_income": round(_coerce_float(result.get("g2_kg")) * price_g2, 2),
                "copra_grade3_kg": _coerce_float(result.get("g3_kg")),
                "copra_grade3_income": round(_coerce_float(result.get("g3_kg")) * price_g3, 2),
                "shells_kg": _coerce_float(result.get("shell_weight_kg")),
                "shells_income": _coerce_float(result.get("shell_revenue")),
                "husk_income": _coerce_float(result.get("husk_revenue")),
                "processing_cost": _coerce_float(result.get("processing_cost")),
            }

        _recalculate_harvest(h, data)
        h["last_edited"] = datetime.now().isoformat()
        save_data(data)
        return ok(h)

    return err("Harvest not found", 404)

@app.route("/api/harvests/<harvest_id>", methods=["DELETE"])
def delete_harvest(harvest_id):
    data = load_data()
    initial_count = len(data["harvests"])
    data["harvests"] = [h for h in data["harvests"] if h.get("harvest_id") != harvest_id]
    if len(data["harvests"]) == initial_count:
        return err("Harvest not found", 404)
    save_data(data)
    return ok({"deleted": harvest_id})

# ─── Fertilizer catalogue & saved prices ─────────────────────────────────────
@app.route("/api/fertilizer-catalogue", methods=["GET"])
def get_fert_catalogue():
    data   = load_data()
    prices = data.get("fertilizer_prices", {})
    interval = _get_fertilizer_interval_days(data)
    result = {k: {**v, "interval_days": interval, "saved_price": prices.get(k, v["price"])}
              for k, v in FERT_CATALOGUE.items()}
    return ok(result)

@app.route("/api/fertilizer-prices", methods=["POST"])
def save_fert_prices():
    data   = load_data()
    prices = data.get("fertilizer_prices", {})
    for k, v in request.json.items():
        if k in FERT_CATALOGUE:
            prices[k] = float(v)
    data["fertilizer_prices"] = prices
    save_data(data)
    return ok(prices)

# ─── Fertilizer compositions (age-based dosage presets) ───────────────────────
@app.route("/api/fertilizer-compositions", methods=["GET"])
def get_compositions():
    data    = load_data()
    comps   = [_normalize_composition(c) for c in data.get("fertilizer_compositions", [])]
    comps = sorted(comps, key=lambda x: float(x.get("age_min_years", 0)))
    return ok(comps)

@app.route("/api/fertilizer-compositions", methods=["POST"])
def save_composition():
    data = load_data()
    body = request.json
    comps = [_normalize_composition(c) for c in data.get("fertilizer_compositions", [])]

    incoming_ferts = body.get("fertilizers") or []
    if not incoming_ferts and body.get("fertilizer_type"):
        # Legacy payload compatibility
        incoming_ferts = [{
            "fertilizer_type": body.get("fertilizer_type"),
            "qty_per_tree": float(body.get("qty_per_tree", 0)),
            "unit": body.get("unit", "kg"),
        }]

    norm_ferts = []
    for f in incoming_ferts:
        ftype = f.get("fertilizer_type", "custom")
        cat = FERT_CATALOGUE.get(ftype, FERT_CATALOGUE["custom"])
        norm_ferts.append({
            "fertilizer_type": ftype,
            "fertilizer_label": cat["label"],
            "qty_per_tree": float(f.get("qty_per_tree", 0)),
            # Enforce catalogue units (FYM is kg/tree).
            "unit": cat["unit"],
        })

    if not norm_ferts:
        return err("At least one fertilizer entry is required")

    age_min = float(body.get("age_min_years", 0))
    age_max = float(body.get("age_max_years", 100))
    if age_max < age_min:
        return err("Age max must be greater than or equal to age min")

    idx = next((i for i, c in enumerate(comps)
                if float(c.get("age_min_years", 0)) == age_min
                and float(c.get("age_max_years", 100)) == age_max), None)

    comp = {
        "id": comps[idx]["id"] if idx is not None else str(uuid.uuid4())[:8],
        "farm_name": "",
        "preset_name": _clean_preset_name(body.get("preset_name"), "", age_min, age_max),
        "age_min_years": age_min,
        "age_max_years": age_max,
        "fertilizers": norm_ferts,
        "application_method": body.get("application_method", ""),
        "notes": body.get("notes", ""),
        "updated_at": datetime.now().isoformat(),
    }

    if idx is not None:
        comps[idx] = comp
    else:
        comps.append(comp)

    data["fertilizer_compositions"] = comps
    save_data(data)
    return ok(comp)

@app.route("/api/fertilizer-compositions/<comp_id>", methods=["DELETE"])
def delete_composition(comp_id):
    data = load_data()
    data["fertilizer_compositions"] = [c for c in data.get("fertilizer_compositions",[])
                                        if c.get("id") != comp_id]
    save_data(data)
    return ok({"deleted": comp_id})

# ─── Fertilizer application jobs (multi-day sessions) ────────────────────────
@app.route("/api/fertilizer-jobs", methods=["GET"])
def get_fert_jobs():
    data    = load_data()
    farm_id = request.args.get("farm_id")
    jobs    = data.get("fertilizer_jobs", [])
    if farm_id:
        jobs = [j for j in jobs if j.get("farm_id") == farm_id]
    return ok(sorted(jobs, key=lambda x: x.get("start_date",""), reverse=True))

@app.route("/api/fertilizer-jobs", methods=["POST"])
def create_fert_job():
    """
    Start a fertilizer application job.
    Supports both legacy and age-preset payloads.
    """
    data = load_data()
    body = request.json
    farm = next((f for f in data["farms"] if f["id"] == body.get("farm_id")), None)
    if not farm:
        return err("Farm not found")

    ftype = body.get("fertilizer_type", "custom")
    cat = FERT_CATALOGUE.get(ftype, FERT_CATALOGUE["custom"])
    unit = body.get("unit", cat["unit"])
    interval = _get_fertilizer_interval_days(data)

    preset_name = (body.get("preset_name") or "").strip()
    label = preset_name or body.get("custom_name", "").strip() or cat["label"]

    total_qty = float(body.get("total_qty", 0))
    price_per_u = float(body.get("price_per_unit", 0))
    total_cost_override = body.get("total_cost")
    if total_cost_override is not None:
        total_cost = float(total_cost_override)
        if total_qty <= 0:
            total_qty = 1
        if price_per_u <= 0:
            price_per_u = total_cost
    else:
        total_cost = round(total_qty * price_per_u, 2)

    start_date = body.get("start_date", today_str())
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        next_due = (start_dt + timedelta(days=interval)).strftime("%Y-%m-%d")
    except:
        next_due = ""

    initial_progress = float(body.get("progress_pct", 0))
    progress_type = body.get("progress_type", "set")
    if progress_type == "delta":
        progress_total = min(100.0, initial_progress)
    else:
        progress_total = min(100.0, initial_progress)

    sessions = []
    if initial_progress > 0:
        sessions.append({
            "session_id": str(uuid.uuid4())[:8],
            "date": body.get("session_date", start_date),
            "pct_this_day": initial_progress,
            "progress_type": progress_type,
            "total_after": progress_total,
            "area_covered": body.get("area_covered", ""),
            "notes": body.get("notes", ""),
            "expense": float(body.get("session_expense", total_cost)),
            "logged_at": datetime.now().isoformat(),
        })

    job_id = "FRTJ-" + str(uuid.uuid4())[:8].upper()
    job = {
        "job_id": job_id,
        "farm_id": farm["id"],
        "farm_name": farm["name"],
        "fertilizer_type": ftype,
        "fertilizer_label": label,
        "unit": unit,
        "composition_id": body.get("composition_id", ""),
        "composition_snapshot": body.get("composition_snapshot", []),
        "total_qty": total_qty,
        "price_per_unit": price_per_u,
        "total_cost": round(total_cost, 2),
        "start_date": start_date,
        "next_due_date": next_due,
        "notes": body.get("notes", ""),
        "status": "completed" if progress_total >= 100 else "in_progress",
        "progress_pct": progress_total,
        "sessions": sessions,
        "created_at": datetime.now().isoformat(),
    }
    if job["status"] == "completed":
        job["completed_date"] = sessions[0]["date"] if sessions else start_date

    data.setdefault("fertilizer_jobs", []).append(job)

    data.setdefault("fertilizers", []).append({
        "farm_id": farm["id"],
        "farm_name": farm["name"],
        "fertilizer_type": ftype,
        "quantity_kg": total_qty,
        "cost": job["total_cost"],
        "applied_date": start_date,
        "next_due_date": next_due,
        "logged_at": datetime.now().isoformat(),
        "notes": body.get("notes", ""),
        "job_id": job_id,
    })

    prices = data.get("fertilizer_prices", {})
    if price_per_u > 0 and ftype in FERT_CATALOGUE:
        prices[ftype] = price_per_u
    data["fertilizer_prices"] = prices

    save_data(data)
    return ok(job)

@app.route("/api/fertilizer-jobs/<job_id>/session", methods=["POST"])
def add_session(job_id):
    """
    Log a daily progress update.
    {date?, progress_pct, progress_type ('delta'|'set'), area_covered?, notes?, expense?}
    delta mode: adds to current progress.  set mode: sets absolute %.
    """
    data = load_data()
    job  = next((j for j in data.get("fertilizer_jobs",[]) if j["job_id"]==job_id), None)
    if not job:
        return err("Job not found", 404)

    body          = request.json
    mode          = body.get("progress_type", "delta")
    new_pct       = float(body.get("progress_pct", 0))
    prev_total    = float(job.get("progress_pct", 0))

    if mode == "delta":
        job["progress_pct"] = min(100.0, prev_total + new_pct)
    else:
        job["progress_pct"] = min(100.0, new_pct)

    expense = float(body.get("expense", 0))
    session = {
        "session_id":    str(uuid.uuid4())[:8],
        "date":          body.get("date", today_str()),
        "pct_this_day":  new_pct,
        "progress_type": mode,
        "total_after":   job["progress_pct"],
        "area_covered":  body.get("area_covered",""),
        "notes":         body.get("notes",""),
        "expense":       expense,
        "logged_at":     datetime.now().isoformat(),
    }
    job.setdefault("sessions", []).append(session)
    job["total_cost"] = round(float(job.get("total_cost", 0)) + expense, 2)

    if job["progress_pct"] >= 100:
        job["status"]         = "completed"
        job["completed_date"] = body.get("date", today_str())

    save_data(data)
    return ok(job)

@app.route("/api/fertilizer-jobs/<job_id>", methods=["PATCH"])
def patch_fert_job(job_id):
    data = load_data()
    job  = next((j for j in data.get("fertilizer_jobs",[]) if j["job_id"]==job_id), None)
    if not job:
        return err("Job not found", 404)
    for k in ["status","notes","next_due_date","price_per_unit","total_qty"]:
        if k in request.json:
            job[k] = request.json[k]
    if job.get("price_per_unit") and job.get("total_qty"):
        job["total_cost"] = round(float(job["price_per_unit"])*float(job["total_qty"]),2)
    save_data(data)
    return ok(job)

@app.route("/api/fertilizer-jobs/<job_id>", methods=["DELETE"])
def delete_fert_job(job_id):
    data = load_data()
    # Remove from fertilizer_jobs
    j_count = len(data.get("fertilizer_jobs", []))
    data["fertilizer_jobs"] = [j for j in data.get("fertilizer_jobs", []) if j.get("job_id") != job_id]
    
    if len(data.get("fertilizer_jobs", [])) == j_count:
        return err("Job not found", 404)
        
    # Also remove from legacy fertilizers array
    data["fertilizers"] = [f for f in data.get("fertilizers", []) if f.get("job_id") != job_id]
    
    save_data(data)
    return ok({"deleted": job_id})

# ─── Other Expenses ───────────────────────────────────────────────────────────
@app.route("/api/expenses", methods=["GET"])
def get_expenses():
    data = load_data()
    farm_id = request.args.get("farm_id")
    expenses = data.get("expenses", [])
    if farm_id:
        expenses = [e for e in expenses if e.get("farm_id") == farm_id]
    return ok(sorted(expenses, key=lambda x: x.get("date", ""), reverse=True))

@app.route("/api/expenses", methods=["POST"])
def add_expense():
    data = load_data()
    body = request.json or {}

    farm_id = (body.get("farm_id") or "").strip()
    farm_name = ""
    if farm_id:
        farm = next((f for f in data.get("farms", []) if f.get("id") == farm_id), None)
        if not farm:
            return err("Farm not found")
        farm_name = farm.get("name", "")

    amount = float(body.get("amount", 0) or 0)
    if amount <= 0:
        return err("Expense amount must be greater than 0")

    entry = {
        "expense_id": "EXP-" + str(uuid.uuid4())[:8].upper(),
        "farm_id": farm_id,
        "farm_name": farm_name,
        "date": body.get("date", today_str()),
        "category": (body.get("category") or "general").strip() or "general",
        "description": (body.get("description") or "").strip(),
        "amount": round(amount, 2),
        "notes": (body.get("notes") or "").strip(),
        "logged_at": datetime.now().isoformat(),
    }
    data.setdefault("expenses", []).append(entry)
    save_data(data)
    return ok(entry)

@app.route("/api/expenses/<expense_id>", methods=["DELETE"])
def delete_expense(expense_id):
    data = load_data()
    old_len = len(data.get("expenses", []))
    data["expenses"] = [e for e in data.get("expenses", []) if e.get("expense_id") != expense_id]
    if len(data["expenses"]) == old_len:
        return err("Expense not found", 404)
    save_data(data)
    return ok({"deleted": expense_id})
# ─── Alerts ───────────────────────────────────────────────────────────────────
@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    data  = load_data()
    today = datetime.now().date()
    alerts = []

    # Fertilizer due alerts (from legacy list, deduplicated by most recent)
    fert_last = {}
    for e in data.get("fertilizers", []):
        key = (e["farm_id"], e["fertilizer_type"])
        if key not in fert_last or e.get("applied_date","") > fert_last[key].get("applied_date",""):
            fert_last[key] = e
        fert_interval = _get_fertilizer_interval_days(data)
    for (_, ftype), e in fert_last.items():
        nd = e.get("next_due_date", "")
        if not nd and e.get("applied_date"):
            try:
                ap = datetime.strptime(e.get("applied_date"), "%Y-%m-%d")
                nd = (ap + timedelta(days=fert_interval)).strftime("%Y-%m-%d")
            except:
                nd = ""
        if nd:
            try:
                dl = (datetime.strptime(nd, "%Y-%m-%d").date() - today).days
                if dl <= 14:
                    lbl = FERT_CATALOGUE.get(ftype, {}).get("label", (ftype or "Fertilizer").title())
                    alerts.append({"type": "fertilizer", "farm": e["farm_name"],
                                   "message": f"{lbl} due on {nd}", "days_left": dl,
                                   "severity": "danger" if dl < 0 else ("warning" if dl <= 7 else "info")})
            except:
                pass

    # In-progress job alerts
    for job in data.get("fertilizer_jobs", []):
        if job.get("status") == "in_progress":
            pct = job.get("progress_pct", 0)
            alerts.append({"type":"fertilizer_job","farm":job["farm_name"],
                           "message":f"{job['fertilizer_label']} in progress — {pct:.0f}% done",
                           "days_left":0,"severity":"info"})

    # Harvest window alerts
    harv_last = {}
    for h in _iter_harvest_farm_entries(data.get("harvests", [])):
        fid = h["farm_id"]
        if fid not in harv_last or h.get("harvest_date","") > harv_last[fid].get("harvest_date",""):
            harv_last[fid] = h
    season = get_current_season()
    lo, hi = _get_harvest_interval_from_settings(data, season)
    for fid, h in harv_last.items():
        try:
            last_dt   = datetime.strptime(h["harvest_date"],"%Y-%m-%d").date()
            win_start = last_dt + timedelta(days=lo)
            win_end   = last_dt + timedelta(days=hi)
            d2s       = (win_start - today).days
            if today > win_end:
                alerts.append({"type":"harvest","farm":h["farm_name"],
                                "message":f"OVERDUE — last harvest {h['harvest_date']}",
                                "days_left":(today-win_end).days*-1,"severity":"danger"})
            elif today >= win_start:
                alerts.append({"type":"harvest","farm":h["farm_name"],
                                "message":f"Ready to harvest! Window: {win_start} → {win_end}",
                                "days_left":0,"severity":"success"})
            elif d2s <= 7:
                alerts.append({"type":"harvest","farm":h["farm_name"],
                                "message":f"Harvest window in {d2s} days ({win_start})",
                                "days_left":d2s,"severity":"warning"})
        except: pass

    alerts.sort(key=lambda x: x["days_left"])
    return ok(alerts)

# ─── Selling Calculator ───────────────────────────────────────────────────────
@app.route("/api/calculator", methods=["POST"])
def selling_calculator():
    data     = load_data()
    body     = request.json
    settings = get_settings(data)
    n   = int(body.get("num_nuts",0))
    g   = int(body.get("good_nuts", n))
    exp = float(body.get("harvest_expenses",0))
    rp  = calculate_sell_as_pieces(n, g, float(body.get("price_per_nut",0)), exp)
    rw  = calculate_sell_by_weight(n, float(body.get("avg_weight_10",1)), float(body.get("price_per_ton",0)), exp)
    rc  = calculate_sell_as_copra(n, float(body.get("avg_dehusked_10",1)),
              float(body.get("price_shell",0)), float(body.get("price_g1",0)),
              float(body.get("price_g2",0)), float(body.get("price_g3",0)), exp, settings)
    profits = [rp["profit"], rw["profit"], rc["profit"]]
    modes   = ["pieces","weight","copra"]
    return ok({"pieces":rp,"weight":rw,"copra":rc,
               "best":modes[profits.index(max(profits))],"settings":settings})

# ─── Weather ──────────────────────────────────────────────────────────────────
@app.route("/api/weather", methods=["GET"])
def get_weather():
    loc = request.args.get("location","")
    if not loc:
        sd = load_data()
        loc = sd.get("settings", {}).get("weather_location", "")
        if not loc:
            farmer = sd.get("farmer") or {}
            loc = farmer.get("district") or farmer.get("village") or "Chennai"
    wx = fetch_weather(loc)
    if wx:
        wx["advisory"] = strip_rich(rain_advisory(wx))
        return ok(wx)
    return err("Could not fetch weather", 503)

# ─── Settings ─────────────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def get_settings_api():
    return ok(get_settings(load_data()))

@app.route("/api/settings", methods=["POST"])
def save_settings_api():
    data = load_data()
    data.setdefault("settings", {})
    for k, v in request.json.items():
        if k in DEFAULT_SETTINGS:
            if k == "weather_location":
                data["settings"][k] = str(v)
            else:
                data["settings"][k] = float(v)
    save_data(data)
    return ok(get_settings(data))

# ─── Project Copra ────────────────────────────────────────────────────────────
@app.route("/api/copra-projects", methods=["GET"])
def get_copra_projects():
    data = load_data()
    projects = data.get("copra_projects", [])
    
    # Calculate stats
    total_rev = 0
    total_profit = 0
    total_nuts = 0
    
    current_date = datetime.now()
    cur_month = current_date.month
    cur_year = current_date.year
    prev_month = (current_date.replace(day=1) - timedelta(days=1)).month
    prev_month_year = (current_date.replace(day=1) - timedelta(days=1)).year
    prev_year = cur_year - 1
    
    m_cur_rev = 0; m_prev_rev = 0
    m_cur_profit = 0; m_prev_profit = 0
    m_cur_nuts = 0; m_prev_nuts = 0
    
    y_cur_rev = 0; y_prev_rev = 0
    y_cur_profit = 0; y_prev_profit = 0
    y_cur_nuts = 0; y_prev_nuts = 0
    
    for p in projects:
        rev = float(p.get("revenue", 0))
        prof = float(p.get("profit", 0))
        nuts = int(p.get("nuts_bought", 0))
        
        total_rev += rev
        total_profit += prof
        total_nuts += nuts
        
        try:
            p_date = datetime.strptime(p["date"], "%Y-%m-%d")
            # Monthly
            if p_date.year == cur_year and p_date.month == cur_month:
                m_cur_rev += rev; m_cur_profit += prof; m_cur_nuts += nuts
            elif p_date.year == prev_month_year and p_date.month == prev_month:
                m_prev_rev += rev; m_prev_profit += prof; m_prev_nuts += nuts
                
            # Yearly
            if p_date.year == cur_year:
                y_cur_rev += rev; y_cur_profit += prof; y_cur_nuts += nuts
            elif p_date.year == prev_year:
                y_prev_rev += rev; y_prev_profit += prof; y_prev_nuts += nuts
        except: pass

    def get_diff(cur, prev):
        if prev == 0: return 100 if cur > 0 else 0
        return round(((cur - prev) / prev) * 100, 1)

    stats = {
        "total_revenue": round(total_rev, 2),
        "total_profit": round(total_profit, 2),
        "total_nuts": total_nuts,
        "mom": {
            "revenue_diff": get_diff(m_cur_rev, m_prev_rev),
            "profit_diff": get_diff(m_cur_profit, m_prev_profit),
            "nuts_diff": get_diff(m_cur_nuts, m_prev_nuts)
        },
        "yoy": {
            "revenue_diff": get_diff(y_cur_rev, y_prev_rev),
            "profit_diff": get_diff(y_cur_profit, y_prev_profit),
            "nuts_diff": get_diff(y_cur_nuts, y_prev_nuts)
        }
    }
    
    return ok({"projects": projects, "stats": stats})

@app.route("/api/copra-projects", methods=["POST"])
def add_copra_project():
    data = load_data()
    body = request.json
    
    # Validation
    if not body.get("date") or not body.get("nuts_bought"):
        return err("Date and Nuts Bought are required", 400)
        
    project = {
        "id": str(uuid.uuid4())[:8],
        "date": body.get("date"),
        "nuts_bought": int(body.get("nuts_bought") or 0),
        "type": body.get("type", "dehusked"), # dehusked or whole
        "dehusking_cost": float(body.get("dehusking_cost") or 0),
        "husk_income": float(body.get("husk_income") or 0),
        "shell_weight": float(body.get("shell_weight") or 0),
        "shell_price": float(body.get("shell_price") or 0),
        "g1_weight": float(body.get("g1_weight") or 0),
        "g1_price": float(body.get("g1_price") or 0),
        "g2_weight": float(body.get("g2_weight") or 0),
        "g2_price": float(body.get("g2_price") or 0),
        "g3_weight": float(body.get("g3_weight") or 0),
        "g3_price": float(body.get("g3_price") or 0),
        "purchase_cost": float(body.get("purchase_cost") or 0),
        "logged_at": datetime.now().isoformat()
    }
    
    # Calculations
    income_shell = project["shell_weight"] * project["shell_price"]
    income_g1 = project["g1_weight"] * project["g1_price"]
    income_g2 = project["g2_weight"] * project["g2_price"]
    income_g3 = project["g3_weight"] * project["g3_price"]
    
    total_revenue = income_shell + income_g1 + income_g2 + income_g3 + project["husk_income"]
    total_expenses = project["purchase_cost"] + project["dehusking_cost"]
    project["revenue"] = round(total_revenue, 2)
    project["expenses"] = round(total_expenses, 2)
    project["profit"] = round(total_revenue - total_expenses, 2)
    
    data.setdefault("copra_projects", []).append(project)
    save_data(data)
    return ok(project)

@app.route("/api/copra-projects/<project_id>", methods=["DELETE"])
def delete_copra_project(project_id):
    data = load_data()
    projects = data.get("copra_projects", [])
    new_projects = [p for p in projects if p["id"] != project_id]
    if len(new_projects) == len(projects):
        return err("Project not found", 404)
    data["copra_projects"] = new_projects
    save_data(data)
    return ok({"deleted": project_id})

# ─── Stats / Predictions ──────────────────────────────────────────────────────
@app.route("/api/coconut-prices/refresh", methods=["POST"])
def refresh_coconut_prices():
    try:
        return ok(_refresh_coconut_price_snapshot())
    except subprocess.TimeoutExpired:
        return err("Price refresh timed out", 504)
    except Exception as e:
        return err(f"Price refresh failed: {e}", 500)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    data     = load_data()
    harvests = _refresh_harvests(data, persist=True)
    farms    = data.get("farms",[])
    season   = get_current_season()
    lo, hi   = _get_harvest_interval_from_settings(data, season)
    fs = {}
    for h in _iter_harvest_farm_entries(harvests):
        fid = h["farm_id"]
        revenue = float(h.get("revenue_share", 0) or 0)
        profit = float(h.get("profit_share", revenue) or 0)
        fs.setdefault(fid,{"nuts":[],"profit":[],"revenue":[],"per_tree":[]})
        fs[fid]["nuts"].append(h["nuts_harvested"])
        fs[fid]["profit"].append(profit)
        fs[fid]["revenue"].append(revenue)
        fs[fid]["per_tree"].append(h.get("nuts_per_tree",0))

    preds = []
    for farm in farms:
        fid = farm["id"]
        if fid in fs:
            ns = fs[fid]["nuts"]; ps = fs[fid]["profit"]
            an = sum(ns)/len(ns); ap = sum(ps)/len(ps)
            trend = "improving" if len(ns)>=2 and ns[-1]>ns[0] else \
                    ("declining" if len(ns)>=2 and ns[-1]<ns[0] else "stable")
            preds.append({"farm_id":fid,"farm_name":farm["name"],
                "harvests_count":len(ns),"avg_nuts":round(an,1),"avg_profit":round(ap,2),
                "avg_per_tree":round(sum(fs[fid]["per_tree"])/len(fs[fid]["per_tree"]),2),
                "trend":trend,"next_nuts_lo":int(an*0.95),"next_nuts_hi":int(an*1.10),
                "accuracy":"moderate" if len(ns)>=4 else "low"})

    total_revenue = round(sum(float(h.get("revenue", 0) or 0) for h in harvests), 2)
    harvest_expenses = round(sum(float(h.get("total_expenses", 0) or 0) for h in harvests), 2)
    fertilizer_expenses = _sum_fertilizer_expenses(data)
    other_expenses = _sum_other_expenses(data)
    
    # Optionally include Copra Projects
    settings = get_settings(data)
    include_copra = settings.get("include_copra_projects_in_dashboard", 0.0) > 0.5
    
    copra_revenue = 0
    copra_expenses = 0
    if include_copra:
        for p in data.get("copra_projects", []):
            copra_revenue += float(p.get("revenue", 0))
            copra_expenses += float(p.get("expenses", 0))
            
    total_revenue = round(total_revenue + copra_revenue, 2)
    total_expenses = round(harvest_expenses + fertilizer_expenses + other_expenses + copra_expenses, 2)
    total_profit = round(total_revenue - total_expenses, 2)
    net_return_pct = round((total_profit / total_revenue) * 100, 2) if total_revenue > 0 else 0.0
    expense_insights = _build_expense_insights(data)
    farm_performance = _build_farm_performance(harvests, farms)

    return ok({"total_farms":len(farms),
               "total_trees":sum(f.get("num_trees",0) for f in farms),
               "total_revenue":total_revenue,
               "total_expenses":total_expenses,
               "harvest_expenses":harvest_expenses,
               "fertilizer_expenses":fertilizer_expenses,
               "other_expenses":other_expenses,
               "total_profit":total_profit,
               "net_return_pct":net_return_pct,
               "total_harvests":len(harvests),"season":season,
               "harvest_interval":{"lo":lo,"hi":hi},
               "harvest_projection":_build_harvest_projection(harvests),
               "expense_insights":expense_insights,
               "farm_performance":farm_performance,
               "predictions":preds,
               "coconut_prices":_get_coconut_price_snapshot()})

# Track active sessions to auto-shutdown when browser is closed
LAST_HEARTBEAT = time.time()
SHUTDOWN_TIMEOUT = 300  # 5 minutes - very safe buffer
IDLE_SHUTDOWN_ENABLED = True

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    global LAST_HEARTBEAT
    LAST_HEARTBEAT = time.time()
    # print("Heartbeat received") # Debug log
    return jsonify({"ok": True})


def shutdown_watchdog():
    """Background thread that shuts down the app if no heartbeat is received."""
    global LAST_HEARTBEAT
    # Giving plenty of time (1 minute) at startup
    time.sleep(60) 
    while IDLE_SHUTDOWN_ENABLED:
        time.sleep(15)
        current_idle = time.time() - LAST_HEARTBEAT
        if current_idle > SHUTDOWN_TIMEOUT:
            print(f"\n[!] No active browser tab detected for {int(current_idle)}s. Shutting down...")
            os._exit(0)

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    def open_browser():
        # Wait a moment for server to start
        time.sleep(1.5)
        webbrowser.open("http://127.0.0.1:3333")

    print("\nCocoTrack Web UI starting...")
    target_host = "127.0.0.1"
    target_port = 3333
    
    # Start browser thread
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Start shutdown watchdog thread
    threading.Thread(target=shutdown_watchdog, daemon=True).start()
    
    print(f"   Opening http://{target_host}:{target_port} in your browser...\n")
    app.run(debug=False, host=target_host, port=target_port)

