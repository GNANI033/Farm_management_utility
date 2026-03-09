#!/usr/bin/env python3
"""
CocoTrack Web UI — Flask wrapper around core.py
Run: python app.py  →  http://localhost:3333
"""
from flask import Flask, render_template, request, jsonify
import sys, os, uuid, re
sys.path.insert(0, os.path.dirname(__file__))

from core import (
    load_data, save_data, generate_harvest_id,
    get_current_season, get_harvest_interval, detect_climate_from_location,
    get_settings, DEFAULT_SETTINGS,
    calculate_sell_as_pieces, calculate_sell_by_weight, calculate_sell_as_copra,
    fetch_weather, rain_advisory,
)
from datetime import datetime, timedelta
import pyotp, qrcode, io, base64
from flask import session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "cocotrack-dev-secret-123")

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

# ─── Pages ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

# ─── Auth ────────────────────────────────────────────────────────────────────
@app.before_request
def check_auth():
    # Public paths
    if request.path == "/" or request.path.startswith("/api/auth/"):
        return
    # Check if setup is needed
    data = load_data()
    if not data.get("totp_secret"):
        if request.path == "/api/auth/setup": return
        return err("Setup required", 401)
    # Check session
    if not session.get("authenticated"):
        return err("Auth required", 401)

@app.route("/api/auth/status")
def auth_status():
    data = load_data()
    return ok({
        "setup_required": not bool(data.get("totp_secret")),
        "authenticated": bool(session.get("authenticated"))
    })

@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    data = load_data()
    if data.get("totp_secret"):
        return err("Setup already completed")
    
    # Generate secret
    secret = pyotp.random_base32()
    data["totp_secret"] = secret
    save_data(data)
    
    # Generate QR Code
    farmer = data.get("farmer", {})
    name = farmer.get("name", "Farmer")
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=name, issuer_name="CocoTrack")
    
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf)
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    
    return ok({"qr_code": f"data:image/png;base64,{qr_b64}", "secret": secret})

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = load_data()
    secret = data.get("totp_secret")
    if not secret:
        return err("Setup required", 401)
    
    code = request.json.get("code")
    if not code:
        return err("Code required")
    
    totp = pyotp.TOTP(secret)
    if totp.verify(code):
        session["authenticated"] = True
        return ok({"authenticated": True})
    return err("Invalid code")

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("authenticated", None)
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
    data["harvests"] = [h for h in data.get("harvests", []) if h.get("farm_id") != farm_id]
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
    harvests = data.get("harvests", [])
    if farm_id:
        harvests = [h for h in harvests if h.get("farm_id") == farm_id]
    return ok(sorted(harvests, key=lambda x: x.get("harvest_date",""), reverse=True))

def _calc_harvest(entry, num_trees, data=None):
    good  = int(entry["nuts_harvested"]) - int(entry["defective_nuts"])
    rev   = good * float(entry["selling_price"])
    exp   = float(entry["labour_cost"]) + float(entry["transport_cost"]) + float(entry["other_expenses"])
    entry.update(good_nuts=good, revenue=round(rev,2),
                 total_expenses=round(exp,2), profit=round(rev-exp,2),
                 nuts_per_tree=round(int(entry["nuts_harvested"])/max(num_trees,1),2))
    try:
        hdate  = datetime.strptime(entry["harvest_date"], "%Y-%m-%d")
        season = get_current_season(hdate.month)
        lo, hi = _get_harvest_interval_from_settings(data, season)
        entry.update(season=season,
                     next_harvest_from=(hdate+timedelta(days=lo)).strftime("%Y-%m-%d"),
                     next_harvest_to  =(hdate+timedelta(days=hi)).strftime("%Y-%m-%d"))
    except:
        entry.setdefault("season","unknown")
        entry.setdefault("next_harvest_from","")
        entry.setdefault("next_harvest_to","")
    return entry

@app.route("/api/harvests", methods=["POST"])
def add_harvest():
    data = load_data()
    body = request.json
    farm = next((f for f in data["farms"] if f["id"] == body.get("farm_id")), None)
    if not farm:
        return err("Farm not found")
    entry = {
        "harvest_id":    generate_harvest_id(data),
        "farm_id":       farm["id"],
        "farm_name":     farm["name"],
        "num_trees":     farm["num_trees"],
        "harvest_date":  body.get("harvest_date", today_str()),
        "nuts_harvested":int(body.get("nuts_harvested", 0)),
        "defective_nuts":int(body.get("defective_nuts", 0)),
        "selling_price": float(body.get("selling_price", 0)),
        "labour_cost":   float(body.get("labour_cost", 0)),
        "transport_cost":float(body.get("transport_cost", 0)),
        "other_expenses":float(body.get("other_expenses", 0)),
        "notes":         body.get("notes", ""),
        "logged_at":     datetime.now().isoformat(),
        "last_edited":   "",
    }
    _calc_harvest(entry, farm["num_trees"], data)
    data["harvests"].append(entry)
    save_data(data)
    return ok(entry)

@app.route("/api/harvests/bulk", methods=["POST"])
def bulk_harvest():
    data = load_data()
    body = request.json
    
    # Global fields
    harvest_date   = body.get("harvest_date", today_str())
    selling_price  = float(body.get("selling_price", 0))
    labour_cost    = float(body.get("labour_cost", 0))
    transport_cost = float(body.get("transport_cost", 0))
    other_expenses = float(body.get("other_expenses", 0))
    notes          = body.get("notes", "")
    
    # Farm-specific data: [{"farm_id": "...", "nuts_harvested": 100, "defective_nuts": 5}, ...]
    farm_entries = body.get("farms", [])
    
    # Filter out farms with 0 harvested nuts
    active_entries = [e for e in farm_entries if int(e.get("nuts_harvested", 0)) > 0]
    if not active_entries:
        return err("No nuts harvested in any farm")
        
    total_nuts = sum(int(e["nuts_harvested"]) for e in active_entries)
    
    results = []
    for e in active_entries:
        farm = next((f for f in data["farms"] if f["id"] == e["farm_id"]), None)
        if not farm:
            continue
            
        nuts = int(e["nuts_harvested"])
        share = nuts / total_nuts
        
        entry = {
            "harvest_id":    generate_harvest_id(data),
            "farm_id":       farm["id"],
            "farm_name":     farm["name"],
            "num_trees":     farm.get("num_trees", 1),
            "harvest_date":  harvest_date,
            "nuts_harvested":nuts,
            "defective_nuts":int(e.get("defective_nuts", 0)),
            "selling_price": selling_price,
            "labour_cost":   round(labour_cost * share, 2),
            "transport_cost":round(transport_cost * share, 2),
            "other_expenses":round(other_expenses * share, 2),
            "notes":         f"{notes} (Bulk Entry)".strip(),
            "logged_at":     datetime.now().isoformat(),
            "last_edited":   "",
        }
        _calc_harvest(entry, int(farm.get("num_trees", 1)), data)
        data["harvests"].append(entry)
        results.append(entry)
        
    save_data(data)
    return ok(results)

@app.route("/api/harvests/<harvest_id>", methods=["PUT"])
def update_harvest(harvest_id):
    """Full edit of any harvest field."""
    data = load_data()
    body = request.json
    for h in data["harvests"]:
        if h.get("harvest_id") == harvest_id:
            farm = next((f for f in data["farms"] if f["id"]==h["farm_id"]),
                        {"num_trees": h.get("num_trees",1)})
            for fld in ["harvest_date","nuts_harvested","defective_nuts",
                        "selling_price","labour_cost","transport_cost","other_expenses","notes"]:
                if fld in body:
                    h[fld] = body[fld]
            _calc_harvest(h, int(farm.get("num_trees", h.get("num_trees",1))), data)
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
            p = float(request.json.get("selling_price", h["selling_price"]))
            g = h.get("good_nuts", int(h["nuts_harvested"])-int(h.get("defective_nuts",0)))
            h["selling_price"] = p
            h["revenue"]       = round(g * p, 2)
            h["profit"]        = round(h["revenue"] - h["total_expenses"], 2)
            h["last_edited"]   = datetime.now().isoformat()
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
    for h in data.get("harvests", []):
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

# ─── Stats / Predictions ──────────────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def get_stats():
    data     = load_data()
    harvests = data.get("harvests",[])
    farms    = data.get("farms",[])
    season   = get_current_season()
    lo, hi   = _get_harvest_interval_from_settings(data, season)
    fs = {}
    for h in harvests:
        fid = h["farm_id"]
        fs.setdefault(fid,{"nuts":[],"profit":[],"revenue":[],"per_tree":[]})
        fs[fid]["nuts"].append(h["nuts_harvested"])
        fs[fid]["profit"].append(h["profit"])
        fs[fid]["revenue"].append(h["revenue"])
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
    return ok({"total_farms":len(farms),
               "total_trees":sum(f.get("num_trees",0) for f in farms),
               "total_revenue":round(sum(h["revenue"] for h in harvests),2),
               "total_profit":round(sum(h["profit"] for h in harvests),2),
               "total_harvests":len(harvests),"season":season,
               "harvest_interval":{"lo":lo,"hi":hi},"predictions":preds})

if __name__ == "__main__":
    print("\nCocoTrack Web UI starting...")
    print("   Open http://localhost:3333 in your browser\n")
    app.run(debug=True, port=3333)


