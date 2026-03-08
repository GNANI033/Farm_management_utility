#!/usr/bin/env python3
"""
CocoTrack Web UI — Flask wrapper around core.py
Run: python app.py  →  http://localhost:5000
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
    "fym":               {"label": "FYM (Farm Yard Manure)",  "interval_days": 180, "unit": "ton", "price": 0},
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

def _calc_harvest(entry, num_trees):
    good  = int(entry["nuts_harvested"]) - int(entry["defective_nuts"])
    rev   = good * float(entry["selling_price"])
    exp   = float(entry["labour_cost"]) + float(entry["transport_cost"]) + float(entry["other_expenses"])
    entry.update(good_nuts=good, revenue=round(rev,2),
                 total_expenses=round(exp,2), profit=round(rev-exp,2),
                 nuts_per_tree=round(int(entry["nuts_harvested"])/max(num_trees,1),2))
    try:
        hdate  = datetime.strptime(entry["harvest_date"], "%Y-%m-%d")
        season = get_current_season(hdate.month)
        lo, hi = get_harvest_interval(season)
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
    _calc_harvest(entry, farm["num_trees"])
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
        _calc_harvest(entry, int(farm.get("num_trees", 1)))
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
            _calc_harvest(h, int(farm.get("num_trees", h.get("num_trees",1))))
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
    result = {k: {**v, "saved_price": prices.get(k, v["price"])}
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

# ─── Fertilizer compositions (per-farm dosage presets) ───────────────────────
@app.route("/api/fertilizer-compositions", methods=["GET"])
def get_compositions():
    data    = load_data()
    farm_id = request.args.get("farm_id")
    comps   = data.get("fertilizer_compositions", [])
    if farm_id:
        comps = [c for c in comps if c.get("farm_id") == farm_id]
    return ok(comps)

@app.route("/api/fertilizer-compositions", methods=["POST"])
def save_composition():
    data = load_data()
    body = request.json
    farm = next((f for f in data["farms"] if f["id"] == body.get("farm_id")), None)
    if not farm:
        return err("Farm not found")
    comps = data.get("fertilizer_compositions", [])
    # upsert: same farm + same fertilizer type → replace
    idx = next((i for i,c in enumerate(comps)
                if c["farm_id"]==body["farm_id"] and c["fertilizer_type"]==body.get("fertilizer_type")), None)
    comp = {
        "id":                 comps[idx]["id"] if idx is not None else str(uuid.uuid4())[:8],
        "farm_id":            farm["id"],
        "farm_name":          farm["name"],
        "fertilizer_type":    body.get("fertilizer_type"),
        "fertilizer_label":   FERT_CATALOGUE.get(body.get("fertilizer_type","custom"),{}).get("label",""),
        "qty_per_tree":       float(body.get("qty_per_tree", 0)),
        "unit":               body.get("unit", "g"),          # g per tree or kg per tree
        "application_method": body.get("application_method",""),
        "notes":              body.get("notes",""),
        "updated_at":         datetime.now().isoformat(),
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
    {farm_id, fertilizer_type, total_qty, price_per_unit,
     composition_id?, start_date?, notes?, custom_name?}
    """
    data = load_data()
    body = request.json
    farm = next((f for f in data["farms"] if f["id"] == body.get("farm_id")), None)
    if not farm:
        return err("Farm not found")

    ftype    = body.get("fertilizer_type", "urea")
    cat      = FERT_CATALOGUE.get(ftype, FERT_CATALOGUE["custom"])
    interval = cat["interval_days"]
    unit     = body.get("unit", cat["unit"])
    label    = body.get("custom_name","").strip() or cat["label"]

    total_qty   = float(body.get("total_qty", 0))
    price_per_u = float(body.get("price_per_unit", 0))

    try:
        start_dt = datetime.strptime(body.get("start_date", today_str()), "%Y-%m-%d")
        next_due = (start_dt + timedelta(days=interval)).strftime("%Y-%m-%d")
    except:
        next_due = ""

    job_id = "FRTJ-" + str(uuid.uuid4())[:8].upper()
    job = {
        "job_id":           job_id,
        "farm_id":          farm["id"],
        "farm_name":        farm["name"],
        "fertilizer_type":  ftype,
        "fertilizer_label": label,
        "unit":             unit,
        "composition_id":   body.get("composition_id",""),
        "total_qty":        total_qty,
        "price_per_unit":   price_per_u,
        "total_cost":       round(total_qty * price_per_u, 2),
        "start_date":       body.get("start_date", today_str()),
        "next_due_date":    next_due,
        "notes":            body.get("notes",""),
        "status":           "in_progress",   # in_progress | completed
        "progress_pct":     0.0,
        "sessions":         [],
        "created_at":       datetime.now().isoformat(),
    }

    data.setdefault("fertilizer_jobs", []).append(job)

    # Mirror into legacy fertilizers list so alerts work
    data.setdefault("fertilizers", []).append({
        "farm_id":         farm["id"],
        "farm_name":       farm["name"],
        "fertilizer_type": ftype,
        "quantity_kg":     total_qty,
        "cost":            job["total_cost"],
        "applied_date":    job["start_date"],
        "next_due_date":   next_due,
        "logged_at":       datetime.now().isoformat(),
        "notes":           body.get("notes",""),
        "job_id":          job_id,
    })

    # Save price for next time
    prices = data.get("fertilizer_prices", {})
    if price_per_u > 0:
        prices[ftype] = price_per_u
    data["fertilizer_prices"] = prices

    save_data(data)
    return ok(job)

@app.route("/api/fertilizer-jobs/<job_id>/session", methods=["POST"])
def add_session(job_id):
    """
    Log a daily progress update.
    {date?, progress_pct, progress_type ('delta'|'set'), area_covered?, notes?}
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

    session = {
        "session_id":    str(uuid.uuid4())[:8],
        "date":          body.get("date", today_str()),
        "pct_this_day":  new_pct,
        "progress_type": mode,
        "total_after":   job["progress_pct"],
        "area_covered":  body.get("area_covered",""),
        "notes":         body.get("notes",""),
        "logged_at":     datetime.now().isoformat(),
    }
    job.setdefault("sessions", []).append(session)

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
    for (_, ftype), e in fert_last.items():
        nd = e.get("next_due_date","")
        if nd:
            try:
                dl = (datetime.strptime(nd,"%Y-%m-%d").date() - today).days
                if dl <= 14:
                    lbl = FERT_CATALOGUE.get(ftype,{}).get("label", ftype.title())
                    alerts.append({"type":"fertilizer","farm":e["farm_name"],
                                   "message":f"{lbl} due on {nd}","days_left":dl,
                                   "severity":"danger" if dl<0 else ("warning" if dl<=7 else "info")})
            except: pass

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
    lo, hi = get_harvest_interval(season)
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
    lo, hi   = get_harvest_interval(season)
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
    print("   Open http://localhost:5000 in your browser\n")
    app.run(debug=True, port=5000)
