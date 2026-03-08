#!/usr/bin/env python3
"""
🌴 CocoTrack - Coconut Farm Management System
Phase 2: Harvest IDs, Edit Sale Prices, Selling Mode Profit Calculator
         (Sell as Pieces / Sell by Weight / Sell as Copra)
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm, IntPrompt, FloatPrompt
    from rich.text import Text
    from rich.columns import Columns
    from rich import box
    from rich.align import Align
    from rich.rule import Rule
except ImportError:
    print("Installing required packages...")
    os.system("pip install rich --break-system-packages -q")
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt, Confirm, IntPrompt, FloatPrompt
    from rich.text import Text
    from rich.columns import Columns
    from rich import box
    from rich.align import Align
    from rich.rule import Rule

console = Console()

# ─── Weather (Open-Meteo — free, no API key) ──────────────────────────────────
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

def fetch_weather(location_name: str) -> dict | None:
    """
    Fetch current weather + 3-day rain for a location using Open-Meteo.
    Free, no API key needed. Returns None on any failure.
    """
    try:
        import urllib.request, urllib.parse, json as _json

        # Step 1: geocode the location name → lat/lon
        geo_params = urllib.parse.urlencode({
            "name": location_name, "count": 1, "language": "en", "format": "json"
        })
        with urllib.request.urlopen(f"{GEOCODE_URL}?{geo_params}", timeout=6) as resp:
            geo = _json.loads(resp.read())
        if not geo.get("results"):
            return None
        loc  = geo["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        loc_label = f"{loc.get('name','')}, {loc.get('admin1','')}, {loc.get('country','')}"

        # Step 2: fetch current conditions + 7-day daily rain
        wx_params = urllib.parse.urlencode({
            "latitude":  lat,
            "longitude": lon,
            "current":   "temperature_2m,relative_humidity_2m,precipitation,rain,wind_speed_10m,weather_code",
            "daily":     "precipitation_sum,rain_sum,temperature_2m_max,temperature_2m_min",
            "forecast_days": 7,
            "timezone":  "Asia/Kolkata"
        })
        with urllib.request.urlopen(f"{WEATHER_URL}?{wx_params}", timeout=6) as resp:
            wx = _json.loads(resp.read())
            
        cur   = wx["current"]
        daily = wx.get("daily", {})

        # WMO weather code → human description
        WMO = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Icy fog",
            51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
            61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
            80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
            95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
        }
        desc = WMO.get(cur.get("weather_code", 0), f"Code {cur.get('weather_code',0)}")

        rain_today = float((daily.get("rain_sum") or [0])[0] or 0)
        rain_7day  = round(sum(float(v or 0) for v in (daily.get("rain_sum") or [0]*7)[:7]), 1)

        return {
            "location":      loc_label,
            "lat":           lat, "lon": lon,
            "temp_c":        cur.get("temperature_2m"),
            "humidity_pct":  cur.get("relative_humidity_2m"),
            "rain_mm":       float(cur.get("rain", 0) or 0),
            "rain_today_mm": rain_today,
            "rain_7day_mm":  rain_7day,
            "wind_kmh":      cur.get("wind_speed_10m"),
            "description":   desc,
            "daily_max":     (daily.get("temperature_2m_max") or [None])[0],
            "daily_min":     (daily.get("temperature_2m_min") or [None])[0],
            "fetched_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    except Exception:
        return None

def rain_advisory(wx: dict) -> str:
    r7 = wx.get("rain_7day_mm", 0)
    rt = wx.get("rain_today_mm", 0)
    if r7 >= 100:
        return "[bold red]🌧 HEAVY RAIN EXPECTED ({r7}mm/7days) — Delay harvest if possible, skip copra drying[/bold red]".format(r7=r7)
    elif r7 >= 40:
        return f"[yellow]🌦 MODERATE RAIN ({r7}mm/7days) — Monitor; protect dried copra[/yellow]"
    elif rt > 5:
        return f"[cyan]🌂 Light rain today ({rt}mm) — Nuts may be wet; factor in drying time[/cyan]"
    else:
        return f"[green]☀  Dry conditions — Good for harvesting and copra drying[/green]"

def show_weather_widget(wx: dict):
    if not wx:
        console.print("  [dim]Weather unavailable (check internet)[/dim]")
        return
    console.print(Panel(
        f"📍 {wx['location']}\n"
        f"🌡  [bold]{wx['temp_c']}°C[/bold]  (min {wx['daily_min']}° / max {wx['daily_max']}°)   "
        f"💧 Humidity: {wx['humidity_pct']}%   💨 Wind: {wx['wind_kmh']} km/h\n"
        f"🌤  {wx['description']}\n"
        f"🌧  Rain now: [bold]{wx['rain_mm']} mm[/bold]  │  Today: [bold]{wx['rain_today_mm']} mm[/bold]  │  7-day total: [bold]{wx['rain_7day_mm']} mm[/bold]\n\n"
        f"{rain_advisory(wx)}\n"
        f"[dim]Last updated: {wx['fetched_at']}[/dim]",
        title="[bold cyan]🌤 Live Weather[/bold cyan]",
        style="cyan"
    ))

def show_weather_for_location(data: dict):
    console.print(Panel("[bold cyan]🌤 Live Weather Check[/bold cyan]", expand=False))
    farmer = data.get("farmer") or {}
    default_loc = farmer.get("district") or farmer.get("village") or "Coimbatore"
    loc = Prompt.ask("  Location to check", default=default_loc)
    console.print("  [dim]Fetching weather from Open-Meteo...[/dim]")
    wx = fetch_weather(loc)
    if wx:
        show_weather_widget(wx)
        # Also show farm-level context if farms exist
        farms = data.get("farms", [])
        if farms:
            console.print("\n  [dim]Tip: Weather is fetched for the location you typed. "
                          "For farm-specific weather, enter your farm's town/district.[/dim]")
    else:
        console.print("[yellow]  Could not fetch weather — check internet connection.[/yellow]")
        console.print("  [dim]Uses Open-Meteo API (free, no key required)[/dim]")

# ─── Data Storage ────────────────────────────────────────────────────────────
DATA_DIR  = Path(__file__).parent
DATA_FILE = DATA_DIR / "data.json"

def load_data() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if DATA_FILE.exists():
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
    else:
        d = {}
    # Ensure all keys exist (backwards-compatible with Phase 1 saves)
    d.setdefault("farmer",      None)
    d.setdefault("farms",       [])
    d.setdefault("harvests",    [])
    d.setdefault("fertilizers", [])
    d.setdefault("expenses",    [])
    d.setdefault("settings",    {})   # Phase 2: persistent copra/selling settings
    return d

def save_data(data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ─── Harvest ID Generator ─────────────────────────────────────────────────────
def generate_harvest_id(data: dict) -> str:
    """Generate unique harvest ID like HRV-2025-001"""
    year    = datetime.now().year
    year_harvests = [h for h in data.get("harvests", [])
                     if h.get("harvest_id", "").startswith(f"HRV-{year}-")]
    seq = len(year_harvests) + 1
    return f"HRV-{year}-{seq:03d}"

# ─── Climate Logic ────────────────────────────────────────────────────────────
MONTH_SEASONS = {
    12: "winter", 1: "winter",  2: "winter",
    3:  "summer", 4: "summer",  5: "summer",  6: "summer",
    7:  "monsoon",8: "monsoon", 9: "monsoon",
    10: "post_monsoon", 11: "post_monsoon"
}
HARVEST_INTERVALS = {
    "summer":       (30, 35),
    "winter":       (45, 60),
    "monsoon":      (38, 45),
    "post_monsoon": (40, 50),
}
FERTILIZER_INTERVALS = {
    "coconut": {"urea": 90, "potash": 120, "compost": 180, "boron": 365}
}

def get_current_season(month: int = None) -> str:
    if month is None:
        month = datetime.now().month
    return MONTH_SEASONS.get(month, "summer")

def get_harvest_interval(season: str) -> tuple:
    return HARVEST_INTERVALS.get(season, (35, 45))

def detect_climate_from_location(location: str) -> str:
    loc = location.lower()
    if any(k in loc for k in ["chennai", "madurai", "coimbatore", "trichy", "salem",
                               "tirunelveli", "kanyakumari", "kerala", "mangalore",
                               "goa", "coastal", "pollachi", "erode", "thanjavur",
                               "dindigul", "nagercoil", "tuticorin", "cuddalore"]):
        return "tropical"
    if any(k in loc for k in ["delhi", "punjab", "haryana", "up", "uttarakhand",
                               "himachal", "kashmir", "ladakh"]):
        return "subtropical"
    return "tropical"

# ─── Banner ───────────────────────────────────────────────────────────────────
def show_banner():
    console.clear()
    banner = """
 ██████╗ ██████╗  ██████╗ ██████╗ ████████╗██████╗  █████╗  ██████╗██╗  ██╗
██╔════╝██╔═══██╗██╔════╝██╔═══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██║ ██╔╝
██║     ██║   ██║██║     ██║   ██║   ██║   ██████╔╝███████║██║     █████╔╝ 
██║     ██║   ██║██║     ██║   ██║   ██║   ██╔══██╗██╔══██║██║     ██╔═██╗ 
╚██████╗╚██████╔╝╚██████╗╚██████╔╝   ██║   ██║  ██║██║  ██║╚██████╗██║  ██╗
 ╚═════╝ ╚═════╝  ╚═════╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝"""
    console.print(banner, style="bold green")
    console.print(Align.center("[bold yellow]🌴 Coconut Farm Management System — Phase 2[/bold yellow]"))
    console.print(Align.center("[dim]Track · Alert · Analyse · Sell Smart[/dim]"))
    console.print()

# ─── Farmer Setup ─────────────────────────────────────────────────────────────
def setup_farmer(data: dict) -> dict:
    console.print(Panel("[bold green]👨‍🌾 Farmer Registration[/bold green]", expand=False))
    farmer = {}
    farmer["name"]    = Prompt.ask("  Full Name")
    farmer["phone"]   = Prompt.ask("  Phone Number")
    farmer["email"]   = Prompt.ask("  Email (optional)", default="")
    farmer["village"] = Prompt.ask("  Village / Town")
    farmer["district"]= Prompt.ask("  District")
    farmer["state"]   = Prompt.ask("  State", default="Tamil Nadu")
    farmer["joined"]  = datetime.now().isoformat()
    farmer["climate_type"] = detect_climate_from_location(farmer["district"])
    console.print(f"\n  [bold cyan]Climate region detected:[/bold cyan] [green]{farmer['climate_type']}[/green]")
    data["farmer"] = farmer
    save_data(data)
    console.print(f"\n  ✅ Welcome, [bold green]{farmer['name']}[/bold green]! Your profile has been saved.\n")
    return data

def show_farmer_profile(data: dict):
    f = data.get("farmer")
    if not f:
        console.print("[red]No farmer profile found.[/red]")
        return
    table = Table(title="👨‍🌾 Farmer Profile", box=box.ROUNDED, style="green")
    table.add_column("Field",   style="cyan",  width=18)
    table.add_column("Details", style="white")
    for k, v in [("Name", f["name"]), ("Phone", f["phone"]), ("Email", f.get("email","—")),
                 ("Village", f["village"]), ("District", f["district"]), ("State", f["state"]),
                 ("Climate", f.get("climate_type","tropical").title()), ("Joined", f["joined"][:10])]:
        table.add_row(k, v)
    console.print(table)

# ─── Farm Management ──────────────────────────────────────────────────────────
def add_farm(data: dict) -> dict:
    console.print(Panel("[bold green]🌱 Add New Farm[/bold green]", expand=False))
    farm = {}
    farm["id"]            = f"FARM{len(data['farms'])+1:03d}"
    farm["name"]          = Prompt.ask("  Farm Name")
    farm["location"]      = Prompt.ask("  Farm Location / Survey No.")
    farm["area_acres"]    = FloatPrompt.ask("  Total Area (in acres)")
    farm["crop_type"]     = "coconut"
    farm["intercropping"] = Confirm.ask("  Intercropping? (other crops alongside coconut)")
    if farm["intercropping"]:
        farm["intercrop_names"] = Prompt.ask("  Crops (comma-separated)", default="banana")
        farm["intercrop_area"]  = FloatPrompt.ask("  Area under intercrop (acres)")
    else:
        farm["intercrop_names"] = ""
        farm["intercrop_area"]  = 0.0
    farm["num_trees"]      = IntPrompt.ask("  Number of Coconut Trees")
    farm["tree_age_years"] = IntPrompt.ask("  Average Tree Age (years)")
    farm["soil_type"]      = Prompt.ask("  Soil Type", default="loamy")
    farm["water_source"]   = Prompt.ask("  Water Source (borewell/canal/rain)", default="borewell")
    farm["added_on"]       = datetime.now().isoformat()
    data["farms"].append(farm)
    save_data(data)
    console.print(f"\n  ✅ Farm [bold green]{farm['name']}[/bold green] (ID: {farm['id']}) added!\n")
    return data

def list_farms(data: dict):
    farms = data.get("farms", [])
    if not farms:
        console.print("[yellow]No farms added yet.[/yellow]")
        return
    table = Table(title="🌴 Your Farms", box=box.ROUNDED)
    table.add_column("ID",       style="cyan",  width=8)
    table.add_column("Name",     style="white", width=16)
    table.add_column("Area",     style="green", width=10)
    table.add_column("Trees",    style="yellow",width=8)
    table.add_column("Intercrop",style="blue",  width=16)
    table.add_column("Water",    style="cyan",  width=12)
    table.add_column("Added",    style="dim",   width=12)
    for f in farms:
        ic = f.get("intercrop_names","—") if f.get("intercropping") else "—"
        table.add_row(f["id"], f["name"], f"{f['area_acres']} ac",
                      str(f["num_trees"]), ic or "—", f.get("water_source","—"), f["added_on"][:10])
    console.print(table)

def select_farm(data: dict):
    farms = data.get("farms", [])
    if not farms:
        console.print("[yellow]No farms found. Please add a farm first.[/yellow]")
        return None
    list_farms(data)
    farm_id = Prompt.ask("\n  Enter Farm ID", default=farms[0]["id"])
    for f in farms:
        if f["id"].upper() == farm_id.upper():
            return f
    console.print("[red]Farm not found.[/red]")
    return None

# ─── Fertilizer Tracking ──────────────────────────────────────────────────────
def log_fertilizer(data: dict) -> dict:
    console.print(Panel("[bold green]🧪 Log Fertilizer Application[/bold green]", expand=False))
    farm = select_farm(data)
    if not farm:
        return data
    entry = {"farm_id": farm["id"], "farm_name": farm["name"]}
    console.print("\n  [dim]1=Urea(90d)  2=Potash(120d)  3=Compost(180d)  4=Boron(365d)  5=Custom[/dim]")
    choice = Prompt.ask("  Select type", choices=["1","2","3","4","5"])
    fert_map = {"1":"urea","2":"potash","3":"compost","4":"boron","5":"custom"}
    entry["fertilizer_type"] = fert_map[choice]
    if entry["fertilizer_type"] == "custom":
        entry["fertilizer_type"] = Prompt.ask("  Fertilizer name")
    entry["quantity_kg"]  = FloatPrompt.ask("  Quantity (kg)")
    entry["cost"]         = FloatPrompt.ask("  Cost (₹)")
    entry["applied_date"] = Prompt.ask("  Date applied (YYYY-MM-DD)", default=datetime.now().strftime("%Y-%m-%d"))
    entry["notes"]        = Prompt.ask("  Notes (optional)", default="")
    entry["logged_at"]    = datetime.now().isoformat()
    interval = FERTILIZER_INTERVALS["coconut"].get(entry["fertilizer_type"], 90)
    try:
        applied_dt = datetime.strptime(entry["applied_date"], "%Y-%m-%d")
        entry["next_due_date"] = (applied_dt + timedelta(days=interval)).strftime("%Y-%m-%d")
    except:
        entry["next_due_date"] = ""
    data["fertilizers"].append(entry)
    save_data(data)
    console.print(f"\n  ✅ Logged! Next due: [bold yellow]{entry.get('next_due_date','N/A')}[/bold yellow]\n")
    return data

def show_fertilizer_alerts(data: dict):
    fertilizers = data.get("fertilizers", [])
    today = datetime.now().date()
    last_applied: dict = {}
    for e in fertilizers:
        key = (e["farm_id"], e["fertilizer_type"])
        if key not in last_applied or e["applied_date"] > last_applied[key]["applied_date"]:
            last_applied[key] = e
    if not last_applied:
        console.print("[yellow]No fertilizer data logged yet.[/yellow]")
        return
    table = Table(title="🔔 Fertilizer Alerts", box=box.ROUNDED)
    table.add_column("Farm",       style="cyan",  width=16)
    table.add_column("Type",       style="white", width=12)
    table.add_column("Last Applied",style="green",width=14)
    table.add_column("Next Due",   style="yellow",width=14)
    table.add_column("Status",     style="bold",  width=18)
    for (_, ftype), e in sorted(last_applied.items()):
        nd_str = e.get("next_due_date", "")
        status = "[dim]—[/dim]"
        if nd_str:
            try:
                nd = datetime.strptime(nd_str, "%Y-%m-%d").date()
                dl = (nd - today).days
                if dl < 0:   status = f"[bold red]⚠ OVERDUE {abs(dl)}d[/bold red]"
                elif dl <= 7: status = f"[bold yellow]⚡ DUE SOON {dl}d[/bold yellow]"
                elif dl <= 14:status = f"[yellow]📅 {dl} days[/yellow]"
                else:         status = f"[green]✓ {dl} days[/green]"
            except: pass
        table.add_row(e["farm_name"], ftype.title(), e["applied_date"], nd_str, status)
    console.print(table)

# ─── Harvest Tracking (Phase 2 — with Harvest ID) ────────────────────────────
def log_harvest(data: dict) -> dict:
    console.print(Panel("[bold green]🥥 Log Harvest[/bold green]", expand=False))
    farm = select_farm(data)
    if not farm:
        return data

    entry = {}
    entry["harvest_id"]   = generate_harvest_id(data)
    entry["farm_id"]      = farm["id"]
    entry["farm_name"]    = farm["name"]
    entry["num_trees"]    = farm["num_trees"]

    entry["harvest_date"]   = Prompt.ask("  Harvest date (YYYY-MM-DD)", default=datetime.now().strftime("%Y-%m-%d"))
    entry["nuts_harvested"] = IntPrompt.ask("  Total nuts harvested")
    entry["defective_nuts"] = IntPrompt.ask("  Defective / dropped nuts", default=0)
    entry["selling_price"]  = FloatPrompt.ask("  Selling price per nut (₹) [can edit later]")
    entry["labour_cost"]    = FloatPrompt.ask("  Labour cost (₹)")
    entry["transport_cost"] = FloatPrompt.ask("  Transport cost (₹)", default=0.0)
    entry["other_expenses"] = FloatPrompt.ask("  Other expenses (₹)", default=0.0)
    entry["notes"]          = Prompt.ask("  Notes / observations", default="")

    good_nuts  = entry["nuts_harvested"] - entry["defective_nuts"]
    revenue    = good_nuts * entry["selling_price"]
    total_exp  = entry["labour_cost"] + entry["transport_cost"] + entry["other_expenses"]
    profit     = revenue - total_exp

    entry["good_nuts"]      = good_nuts
    entry["revenue"]        = round(revenue, 2)
    entry["total_expenses"] = round(total_exp, 2)
    entry["profit"]         = round(profit, 2)
    entry["nuts_per_tree"]  = round(entry["nuts_harvested"] / max(farm["num_trees"], 1), 2)
    entry["logged_at"]      = datetime.now().isoformat()
    entry["last_edited"]    = ""

    # Season & next harvest window
    try:
        hdate  = datetime.strptime(entry["harvest_date"], "%Y-%m-%d")
        season = get_current_season(hdate.month)
        lo, hi = get_harvest_interval(season)
        entry["season"]            = season
        entry["next_harvest_from"] = (hdate + timedelta(days=lo)).strftime("%Y-%m-%d")
        entry["next_harvest_to"]   = (hdate + timedelta(days=hi)).strftime("%Y-%m-%d")
    except:
        entry["season"]            = "unknown"
        entry["next_harvest_from"] = ""
        entry["next_harvest_to"]   = ""

    data["harvests"].append(entry)
    save_data(data)

    console.print(f"""
  ✅ [bold green]Harvest Logged![/bold green]  ID: [bold cyan]{entry['harvest_id']}[/bold cyan]
  ┌──────────────────────────────────────┐
  │  Good Nuts :  [bold]{good_nuts}[/bold] nuts
  │  Revenue   :  [bold green]₹{revenue:,.2f}[/bold green]
  │  Expenses  :  [bold red]₹{total_exp:,.2f}[/bold red]
  │  Profit    :  [bold cyan]₹{profit:,.2f}[/bold cyan]
  │  Per Tree  :  {entry['nuts_per_tree']} nuts/tree
  │  Season    :  {entry['season'].title()}
  │  Next Harvest: [bold yellow]{entry['next_harvest_from']} → {entry['next_harvest_to']}[/bold yellow]
  └──────────────────────────────────────┘
""")
    return data

def edit_harvest_sale_price(data: dict) -> dict:
    """Allow farmer to update the selling price of any past harvest."""
    console.print(Panel("[bold yellow]✏️  Edit Harvest Sale Price[/bold yellow]", expand=False))

    harvests = data.get("harvests", [])
    if not harvests:
        console.print("[yellow]No harvests logged yet.[/yellow]")
        return data

    # Show recent harvests
    table = Table(title="Recent Harvests", box=box.SIMPLE)
    table.add_column("Harvest ID",    style="cyan",  width=14)
    table.add_column("Date",          style="white", width=12)
    table.add_column("Farm",          style="green", width=14)
    table.add_column("Nuts",          style="yellow",width=8)
    table.add_column("Price/nut (₹)", style="white", width=14)
    table.add_column("Revenue (₹)",   style="cyan",  width=12)
    for h in sorted(harvests, key=lambda x: x["harvest_date"], reverse=True)[:15]:
        table.add_row(
            h.get("harvest_id", "—"),
            h["harvest_date"],
            h["farm_name"],
            str(h["nuts_harvested"]),
            f"₹{h['selling_price']:,.2f}",
            f"₹{h['revenue']:,.2f}"
        )
    console.print(table)

    harvest_id = Prompt.ask("\n  Enter Harvest ID to edit").strip().upper()
    target = next((h for h in harvests if h.get("harvest_id","").upper() == harvest_id), None)

    if not target:
        console.print("[red]Harvest ID not found.[/red]")
        return data

    console.print(f"\n  Harvest: [bold]{target['harvest_id']}[/bold] | Farm: [bold]{target['farm_name']}[/bold] | Date: {target['harvest_date']}")
    console.print(f"  Current price: [bold yellow]₹{target['selling_price']:,.2f}[/bold yellow] per nut")

    new_price = FloatPrompt.ask("  New selling price per nut (₹)")

    # Recalculate
    good_nuts  = target.get("good_nuts", target["nuts_harvested"] - target.get("defective_nuts", 0))
    new_rev    = round(good_nuts * new_price, 2)
    new_profit = round(new_rev - target["total_expenses"], 2)

    console.print(f"\n  [dim]Old Revenue: ₹{target['revenue']:,.2f}  →  New Revenue: [bold green]₹{new_rev:,.2f}[/bold green][/dim]")
    console.print(f"  [dim]Old Profit:  ₹{target['profit']:,.2f}  →  New Profit:  [bold cyan]₹{new_profit:,.2f}[/bold cyan][/dim]")

    if Confirm.ask("  Confirm update?"):
        target["selling_price"] = new_price
        target["revenue"]       = new_rev
        target["profit"]        = new_profit
        target["last_edited"]   = datetime.now().isoformat()
        save_data(data)
        console.print("  ✅ [bold green]Harvest record updated![/bold green]\n")
    else:
        console.print("  [dim]No changes made.[/dim]\n")

    return data

def show_harvest_history(data: dict):
    harvests = data.get("harvests", [])
    if not harvests:
        console.print("[yellow]No harvest data logged yet.[/yellow]")
        return
    farms = data.get("farms", [])
    if len(farms) > 1:
        filter_id = Prompt.ask("  Filter by Farm ID (Enter for all)", default="")
        if filter_id:
            harvests = [h for h in harvests if h["farm_id"].upper() == filter_id.upper()]

    table = Table(title="🥥 Harvest History", box=box.ROUNDED)
    table.add_column("Harvest ID",  style="cyan",   width=13)
    table.add_column("Date",        style="white",  width=12)
    table.add_column("Farm",        style="green",  width=13)
    table.add_column("Nuts",        style="yellow", width=7)
    table.add_column("/Tree",       style="green",  width=7)
    table.add_column("Price/nut",   style="white",  width=10)
    table.add_column("Revenue",     style="green",  width=11)
    table.add_column("Expenses",    style="red",    width=11)
    table.add_column("Profit",      style="cyan",   width=11)
    table.add_column("Next Harvest",style="yellow", width=22)

    for h in sorted(harvests, key=lambda x: x["harvest_date"], reverse=True):
        nxt = f"{h.get('next_harvest_from','')} → {h.get('next_harvest_to','')}" if h.get("next_harvest_from") else "—"
        edited_marker = " ✏" if h.get("last_edited") else ""
        table.add_row(
            (h.get("harvest_id","—") or "—") + edited_marker,
            h["harvest_date"],
            h["farm_name"],
            str(h["nuts_harvested"]),
            str(h.get("nuts_per_tree","—")),
            f"₹{h['selling_price']:.2f}",
            f"₹{h['revenue']:,.0f}",
            f"₹{h['total_expenses']:,.0f}",
            f"₹{h['profit']:,.0f}",
            nxt
        )
    console.print(table)
    console.print("  [dim]✏ = price was edited after initial entry[/dim]")

def show_next_harvest_alerts(data: dict):
    harvests = data.get("harvests", [])
    today    = datetime.now().date()
    latest: dict = {}
    for h in harvests:
        fid = h["farm_id"]
        if fid not in latest or h["harvest_date"] > latest[fid]["harvest_date"]:
            latest[fid] = h
    if not latest:
        console.print("[yellow]No harvest data to compute alerts from.[/yellow]")
        return
    season = get_current_season()
    lo, hi = get_harvest_interval(season)
    console.print(Panel(f"[bold yellow]🔔 Harvest Alerts[/bold yellow]\nSeason: [bold]{season.title()}[/bold] | Window: [bold]{lo}–{hi} days[/bold]", expand=False))

    # Live rain check — critical for harvest timing decisions
    farmer_info = data.get("farmer") or {}
    wx_loc = farmer_info.get("district") or farmer_info.get("village") or ""
    if wx_loc:
        wx = fetch_weather(wx_loc)
        if wx:
            console.print(f"\n  [bold]Current conditions for {wx['location']}:[/bold]")
            console.print(f"  🌡 {wx['temp_c']}°C  💧 Humidity: {wx['humidity_pct']}%  🌧 Rain 3-day: [bold]{wx['rain_3day_mm']} mm[/bold]")
            console.print(f"  {rain_advisory(wx)}\n")

    table = Table(box=box.ROUNDED)
    table.add_column("Farm",         style="cyan",   width=16)
    table.add_column("Last Harvest", style="white",  width=14)
    table.add_column("Window Start", style="green",  width=14)
    table.add_column("Window End",   style="yellow", width=14)
    table.add_column("Status",       style="bold",   width=22)

    for fid, h in latest.items():
        try:
            last_dt   = datetime.strptime(h["harvest_date"], "%Y-%m-%d").date()
            win_start = last_dt + timedelta(days=lo)
            win_end   = last_dt + timedelta(days=hi)
            d2start   = (win_start - today).days
            if today > win_end:      status = "[bold red]⚠ OVERDUE![/bold red]"
            elif today >= win_start: status = "[bold green]✅ READY TO HARVEST[/bold green]"
            elif d2start <= 5:       status = f"[yellow]⚡ In {d2start} days[/yellow]"
            else:                    status = f"[dim]In {d2start} days[/dim]"
        except:
            win_start = win_end = "—"; status = "[dim]—[/dim]"
        table.add_row(h["farm_name"], h["harvest_date"], str(win_start), str(win_end), status)
    console.print(table)

# ─── Settings Page ────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    # Fixed / rarely-changing copra processing costs
    "copra_dehusking_charge_per_nut":  0.50,   # ₹ per nut
    "copra_splitting_charge_per_nut":  0.30,
    "copra_removal_charge_per_nut":    0.40,   # removal of kernel from shell
    "copra_transport_charge_per_nut":  0.20,
    "copra_other_charge_per_nut":      0.25,   # any other misc cost per nut
    "husk_selling_price_per_1000":     200.0,  # ₹ per 1000 husks
    # Copra yield ratios (these are typical, can be adjusted)
    "copra_yield_grade1_pct": 60.0,   # % of total copra weight that is grade 1
    "copra_yield_grade2_pct": 30.0,
    "copra_yield_grade3_pct": 10.0,
    # Shell weight as fraction of dehusked coconut weight
    "shell_weight_fraction":  0.15,   # ~15% of dehusked nut weight is shell
    # Copra conversion: kg of dry copra per kg of dehusked coconut kernel
    "copra_conversion_ratio": 0.30,   # ~30% of kernel weight becomes copra
    # Custom dashboard weather location
    "weather_location":       "",
}

def get_settings(data: dict) -> dict:
    s = DEFAULT_SETTINGS.copy()
    s.update(data.get("settings", {}))
    return s

def show_settings(data: dict) -> dict:
    console.print(Panel("[bold blue]⚙️  Settings — Copra Processing Costs & Ratios[/bold blue]", expand=False))
    s = get_settings(data)

    table = Table(box=box.ROUNDED, title="Current Settings")
    table.add_column("Setting",    style="cyan",  width=38)
    table.add_column("Value",      style="yellow",width=14)
    table.add_column("Key",        style="dim",   width=36)

    labels = [
        ("Dehusking charge (₹/nut)",              "copra_dehusking_charge_per_nut"),
        ("Splitting charge (₹/nut)",               "copra_splitting_charge_per_nut"),
        ("Kernel removal charge (₹/nut)",          "copra_removal_charge_per_nut"),
        ("Transport charge (₹/nut)",               "copra_transport_charge_per_nut"),
        ("Other costs (₹/nut)",                    "copra_other_charge_per_nut"),
        ("Husk selling price (₹/1000 nuts)",       "husk_selling_price_per_1000"),
        ("Copra Grade 1 yield (%)",                "copra_yield_grade1_pct"),
        ("Copra Grade 2 yield (%)",                "copra_yield_grade2_pct"),
        ("Copra Grade 3 yield (%)",                "copra_yield_grade3_pct"),
        ("Shell weight fraction (e.g. 0.15=15%)",  "shell_weight_fraction"),
        ("Copra conversion ratio (e.g. 0.30=30%)", "copra_conversion_ratio"),
        ("Dashboard Weather Location (Optional)",  "weather_location"),
    ]
    for label, key in labels:
        table.add_row(label, str(s[key]), key)
    console.print(table)

    if Confirm.ask("\n  Edit a setting?", default=False):
        key = Prompt.ask("  Enter key name to edit")
        if key not in DEFAULT_SETTINGS:
            console.print("[red]Unknown key.[/red]")
            return data
        new_val = FloatPrompt.ask(f"  New value for '{key}'")
        data["settings"][key] = new_val
        save_data(data)
        console.print("  ✅ [green]Setting saved.[/green]\n")
    return data

# ─── Selling Mode Profit Calculator ──────────────────────────────────────────
def _fmt_rupees(v: float) -> str:
    return f"₹{v:,.2f}"

def calculate_sell_as_pieces(num_nuts: int, good_nuts: int, price_per_nut: float,
                              harvest_expenses: float) -> dict:
    """Revenue from selling whole coconuts per piece."""
    revenue = good_nuts * price_per_nut
    profit  = revenue - harvest_expenses
    return {
        "mode":             "Sell as Pieces",
        "revenue":          round(revenue, 2),
        "expenses":         round(harvest_expenses, 2),
        "profit":           round(profit, 2),
        "revenue_per_nut":  round(revenue / max(num_nuts, 1), 4),
    }

def calculate_sell_by_weight(num_nuts: int, avg_weight_10_nuts_kg: float,
                              price_per_ton: float, harvest_expenses: float) -> dict:
    """Revenue from selling dehusked coconuts by weight."""
    avg_weight_per_nut_kg = avg_weight_10_nuts_kg / 10.0
    total_weight_kg       = avg_weight_per_nut_kg * num_nuts
    total_weight_ton      = total_weight_kg / 1000.0
    revenue               = total_weight_ton * price_per_ton
    profit                = revenue - harvest_expenses
    return {
        "mode":               "Sell by Weight",
        "avg_weight_per_nut_g": round(avg_weight_per_nut_kg * 1000, 1),
        "total_weight_kg":    round(total_weight_kg, 2),
        "total_weight_ton":   round(total_weight_ton, 4),
        "price_per_ton":      price_per_ton,
        "revenue":            round(revenue, 2),
        "expenses":           round(harvest_expenses, 2),
        "profit":             round(profit, 2),
        "revenue_per_nut":    round(revenue / max(num_nuts, 1), 4),
    }

def calculate_sell_as_copra(num_nuts: int, avg_weight_10_dehusked_kg: float,
                             price_shell_per_kg: float,
                             price_copra_g1: float, price_copra_g2: float, price_copra_g3: float,
                             harvest_expenses: float, settings: dict) -> dict:
    """Revenue from processing and selling as copra."""
    s = settings

    avg_dehusked_kg_per_nut = avg_weight_10_dehusked_kg / 10.0
    total_dehusked_kg       = avg_dehusked_kg_per_nut * num_nuts

    # Shell weight
    shell_weight_kg  = total_dehusked_kg * s["shell_weight_fraction"]
    shell_revenue    = shell_weight_kg * price_shell_per_kg

    # Kernel weight (dehusked minus shell)
    kernel_weight_kg = total_dehusked_kg - shell_weight_kg

    # Copra weight from kernel (dry copra is ~30% of kernel weight)
    total_copra_kg   = kernel_weight_kg * s["copra_conversion_ratio"]
    g1_kg = total_copra_kg * (s["copra_yield_grade1_pct"] / 100)
    g2_kg = total_copra_kg * (s["copra_yield_grade2_pct"] / 100)
    g3_kg = total_copra_kg * (s["copra_yield_grade3_pct"] / 100)

    copra_revenue    = (g1_kg * price_copra_g1) + (g2_kg * price_copra_g2) + (g3_kg * price_copra_g3)

    # Husk revenue
    husk_revenue     = (num_nuts / 1000.0) * s["husk_selling_price_per_1000"]

    # Processing costs
    proc_cost_per_nut = (s["copra_dehusking_charge_per_nut"] +
                         s["copra_splitting_charge_per_nut"] +
                         s["copra_removal_charge_per_nut"]   +
                         s["copra_transport_charge_per_nut"] +
                         s["copra_other_charge_per_nut"])
    total_proc_cost  = proc_cost_per_nut * num_nuts

    total_revenue    = copra_revenue + shell_revenue + husk_revenue
    total_expenses   = harvest_expenses + total_proc_cost
    profit           = total_revenue - total_expenses

    return {
        "mode":               "Sell as Copra",
        "avg_dehusked_g_per_nut": round(avg_dehusked_kg_per_nut * 1000, 1),
        "total_dehusked_kg":  round(total_dehusked_kg, 2),
        "shell_weight_kg":    round(shell_weight_kg, 2),
        "kernel_weight_kg":   round(kernel_weight_kg, 2),
        "total_copra_kg":     round(total_copra_kg, 2),
        "g1_kg":              round(g1_kg, 2),
        "g2_kg":              round(g2_kg, 2),
        "g3_kg":              round(g3_kg, 2),
        "copra_revenue":      round(copra_revenue, 2),
        "shell_revenue":      round(shell_revenue, 2),
        "husk_revenue":       round(husk_revenue, 2),
        "total_revenue":      round(total_revenue, 2),
        "harvest_expenses":   round(harvest_expenses, 2),
        "processing_cost":    round(total_proc_cost, 2),
        "total_expenses":     round(total_expenses, 2),
        "profit":             round(profit, 2),
        "revenue_per_nut":    round(total_revenue / max(num_nuts, 1), 4),
        "revenue_breakdown": {
            "copra":  round(copra_revenue, 2),
            "shell":  round(shell_revenue, 2),
            "husk":   round(husk_revenue, 2),
        }
    }

def print_result_panel(r: dict, label: str, highlight: bool = False):
    style = "bold green" if highlight else "cyan"
    crown = "👑 BEST OPTION  " if highlight else ""
    console.print(Panel(
        f"[bold]{crown}{r['mode']}[/bold]\n"
        f"  Revenue  : [green]{_fmt_rupees(r['revenue'] if 'revenue' in r else r['total_revenue'])}[/green]\n"
        f"  Expenses : [red]{_fmt_rupees(r['expenses'] if 'expenses' in r else r['total_expenses'])}[/red]\n"
        f"  [bold]Profit   : {_fmt_rupees(r['profit'])}[/bold]\n"
        f"  Per Nut  : {_fmt_rupees(r['revenue_per_nut'])}",
        title=f"[{style}]{label}[/{style}]",
        style=style if highlight else "dim"
    ))

def sell_decision_calculator(data: dict):
    """Main selling mode profit calculator."""
    console.print(Panel("[bold magenta]💰 Selling Mode Profit Calculator[/bold magenta]\n"
                        "[dim]Compare: Sell as Pieces vs By Weight vs As Copra[/dim]", expand=False))

    settings = get_settings(data)

    # ── Weather check upfront — rain heavily affects copra drying viability
    farmer_info = data.get("farmer") or {}
    wx_loc = farmer_info.get("district") or farmer_info.get("village") or ""
    if wx_loc:
        console.print(f"  [dim]Checking rain forecast for {wx_loc}...[/dim]")
        wx = fetch_weather(wx_loc)
        if wx:
            rain7 = wx.get("rain_7day_mm", 0)
            console.print(f"\n  🌧  Rain forecast (7 days): [bold]{'⚠ ' if rain7 >= 40 else ''}{rain7} mm[/bold]")
            console.print(f"  {rain_advisory(wx)}")
            if rain7 >= 40:
                console.print("  [bold yellow]  ⚠  Copra drying requires 4-6 dry days — heavy rain makes copra option risky![/bold yellow]")
            console.print()

    # ── Step 1: Choose harvest or manual entry
    harvests = data.get("harvests", [])
    use_harvest = False
    if harvests:
        use_harvest = Confirm.ask("  Link to a logged harvest? (No = enter nuts manually)")

    num_nuts        = 0
    harvest_expenses = 0.0
    harvest_ref     = None

    if use_harvest:
        table = Table(box=box.SIMPLE, title="Recent Harvests")
        table.add_column("ID",    style="cyan", width=14)
        table.add_column("Date",  style="white",width=12)
        table.add_column("Farm",  style="green",width=14)
        table.add_column("Nuts",  style="yellow",width=8)
        table.add_column("Expenses",style="red",width=12)
        for h in sorted(harvests, key=lambda x: x["harvest_date"], reverse=True)[:10]:
            table.add_row(h.get("harvest_id","—"), h["harvest_date"],
                          h["farm_name"], str(h["nuts_harvested"]),
                          f"₹{h['total_expenses']:,.0f}")
        console.print(table)
        hid = Prompt.ask("  Enter Harvest ID").strip().upper()
        harvest_ref = next((h for h in harvests if h.get("harvest_id","").upper() == hid), None)
        if not harvest_ref:
            console.print("[red]Harvest not found. Switching to manual entry.[/red]")
            use_harvest = False

    if use_harvest and harvest_ref:
        num_nuts         = harvest_ref["nuts_harvested"]
        good_nuts        = harvest_ref.get("good_nuts", num_nuts - harvest_ref.get("defective_nuts", 0))
        harvest_expenses = harvest_ref["total_expenses"]
        console.print(f"\n  Using harvest [bold cyan]{harvest_ref['harvest_id']}[/bold cyan]: "
                      f"[bold]{num_nuts}[/bold] nuts | Good: [bold]{good_nuts}[/bold] | "
                      f"Expenses: [red]₹{harvest_expenses:,.2f}[/red]")
    else:
        num_nuts         = IntPrompt.ask("  Total nuts to sell")
        good_nuts        = IntPrompt.ask("  Good nuts (saleable)", default=num_nuts)
        harvest_expenses = FloatPrompt.ask("  Total harvest expenses (₹)", default=0.0)

    console.print()
    console.print(Rule("[bold]Market Prices[/bold]"))

    # ── Step 2: Price inputs per mode
    # --- Pieces ---
    console.print("\n  [bold cyan]① Sell as Pieces[/bold cyan]")
    price_per_nut = FloatPrompt.ask("  Current selling price per nut (₹)")

    # --- By Weight ---
    console.print("\n  [bold cyan]② Sell by Weight (dehusked)[/bold cyan]")
    console.print("  [dim]Weigh 10 dehusked nuts together and enter the total weight.[/dim]")
    avg_weight_10 = FloatPrompt.ask("  Total weight of 10 dehusked nuts together (kg)")
    console.print(f"  [dim]→ Avg per nut: {avg_weight_10/10*1000:.1f} g[/dim]")
    price_per_ton = FloatPrompt.ask("  Current price per ton of dehusked coconut (₹)")

    # --- As Copra ---
    console.print("\n  [bold cyan]③ Sell as Copra[/bold cyan]")
    console.print("  [dim]These change often — enter today's prices:[/dim]")
    console.print("  [dim]Weigh 10 dehusked nuts together and enter the total weight.[/dim]")
    avg_dehusked_10 = FloatPrompt.ask("  Total weight of 10 dehusked nuts together (kg)", default=avg_weight_10)
    console.print(f"  [dim]→ Avg per nut: {avg_dehusked_10/10*1000:.1f} g[/dim]")
    price_shell     = FloatPrompt.ask("  Shell selling price (₹/kg)")
    price_g1        = FloatPrompt.ask("  Grade 1 Copra price (₹/kg)")
    price_g2        = FloatPrompt.ask("  Grade 2 Copra price (₹/kg)")
    price_g3        = FloatPrompt.ask("  Grade 3 Copra price (₹/kg)")

    # ── Review / update settings
    console.print()
    console.print(Rule("[bold]Processing Costs[/bold] [dim](from Settings)[/dim]"))
    proc_per_nut = (settings["copra_dehusking_charge_per_nut"] +
                    settings["copra_splitting_charge_per_nut"] +
                    settings["copra_removal_charge_per_nut"]   +
                    settings["copra_transport_charge_per_nut"] +
                    settings["copra_other_charge_per_nut"])
    console.print(f"""
  Dehusking  : ₹{settings['copra_dehusking_charge_per_nut']}/nut
  Splitting  : ₹{settings['copra_splitting_charge_per_nut']}/nut
  Removal    : ₹{settings['copra_removal_charge_per_nut']}/nut
  Transport  : ₹{settings['copra_transport_charge_per_nut']}/nut
  Other Costs: ₹{settings['copra_other_charge_per_nut']}/nut
  ─────────────────────────────
  Total/nut  : [bold yellow]₹{proc_per_nut:.2f}[/bold yellow]  →  [bold]₹{proc_per_nut * num_nuts:,.0f}[/bold] for {num_nuts} nuts
  Husk price : ₹{settings['husk_selling_price_per_1000']}/1000 nuts
""")

    update_settings = Confirm.ask("  Update processing costs before calculating?", default=False)
    if update_settings:
        data = show_settings(data)
        settings = get_settings(data)

    # ── Step 3: Calculate all three modes
    r_pieces = calculate_sell_as_pieces(num_nuts, good_nuts, price_per_nut, harvest_expenses)
    r_weight = calculate_sell_by_weight(num_nuts, avg_weight_10, price_per_ton, harvest_expenses)
    r_copra  = calculate_sell_as_copra(num_nuts, avg_dehusked_10, price_shell,
                                        price_g1, price_g2, price_g3,
                                        harvest_expenses, settings)

    # ── Step 4: Display results
    console.print()
    console.print(Rule("[bold magenta]📊 Results[/bold magenta]"))

    results  = [r_pieces, r_weight, r_copra]
    profits  = [r_pieces["profit"], r_weight["profit"], r_copra["profit"]]
    best_idx = profits.index(max(profits))

    for i, (r, label) in enumerate(zip(results, ["① Pieces", "② By Weight", "③ Copra"])):
        print_result_panel(r, label, highlight=(i == best_idx))

    # ── Copra detailed breakdown
    console.print(Panel(
        f"[bold]Copra Breakdown for {num_nuts} nuts:[/bold]\n\n"
        f"  Sample (10 nuts)     : {avg_dehusked_10:.3f} kg total → {avg_dehusked_10/10*1000:.1f} g avg/nut\n"
        f"  Total Dehusked       : {r_copra['total_dehusked_kg']} kg\n"
        f"  Shell Weight        : {r_copra['shell_weight_kg']} kg  → [green]{_fmt_rupees(r_copra['shell_revenue'])}[/green]\n"
        f"  Kernel Weight       : {r_copra['kernel_weight_kg']} kg\n"
        f"  Dry Copra Total     : {r_copra['total_copra_kg']} kg\n"
        f"    Grade 1  ({settings['copra_yield_grade1_pct']:.0f}%) : {r_copra['g1_kg']} kg × ₹{price_g1}/kg = [green]{_fmt_rupees(r_copra['g1_kg']*price_g1)}[/green]\n"
        f"    Grade 2  ({settings['copra_yield_grade2_pct']:.0f}%) : {r_copra['g2_kg']} kg × ₹{price_g2}/kg = [green]{_fmt_rupees(r_copra['g2_kg']*price_g2)}[/green]\n"
        f"    Grade 3  ({settings['copra_yield_grade3_pct']:.0f}%) : {r_copra['g3_kg']} kg × ₹{price_g3}/kg = [green]{_fmt_rupees(r_copra['g3_kg']*price_g3)}[/green]\n"
        f"  Husk Revenue        : [green]{_fmt_rupees(r_copra['husk_revenue'])}[/green]\n"
        f"  Processing Cost     : [red]{_fmt_rupees(r_copra['processing_cost'])}[/red]",
        title="[cyan]Copra Details[/cyan]",
        style="cyan"
    ))

    # ── Comparison summary table
    console.print()
    comp = Table(title="📊 Side-by-Side Comparison", box=box.DOUBLE_EDGE)
    comp.add_column("Metric",    style="cyan",   width=20)
    comp.add_column("Pieces",    style="white",  width=16)
    comp.add_column("By Weight", style="white",  width=16)
    comp.add_column("Copra",     style="white",  width=16)

    def _rev(r): return r.get("revenue") or r.get("total_revenue")
    def _exp(r): return r.get("expenses") or r.get("total_expenses")

    rows = [
        ("Revenue",    [_fmt_rupees(_rev(r)) for r in results]),
        ("Expenses",   [_fmt_rupees(_exp(r)) for r in results]),
        ("Profit",     [_fmt_rupees(r["profit"]) for r in results]),
        ("Per Nut",    [_fmt_rupees(r["revenue_per_nut"]) for r in results]),
    ]
    for label, vals in rows:
        # Highlight best value in green
        best_val = max(vals, key=lambda v: float(v.replace("₹","").replace(",","")))
        colored = [f"[bold green]{v}[/bold green]" if v == best_val else v for v in vals]
        comp.add_row(label, *colored)
    comp.add_row("Recommended", *[
        "[bold green]✅ BEST[/bold green]" if i == best_idx else "—"
        for i in range(3)
    ])
    console.print(comp)

# ─── Predictions ──────────────────────────────────────────────────────────────
def show_predictions(data: dict):
    harvests = data.get("harvests", [])
    farms    = data.get("farms", [])
    console.print(Panel("[bold magenta]📊 Yield Predictions & Insights[/bold magenta]", expand=False))

    if len(harvests) < 2:
        console.print("""
  [yellow]⚠ Prediction engine needs more data![/yellow]
  
  • Add at least [bold]2 harvest records[/bold] per farm to start.
  • With 5+ harvests accuracy improves significantly.
  
  [dim]Keep logging your harvests — the engine learns over time![/dim]
""")
        return

    farm_harvests: dict = {}
    for h in harvests:
        farm_harvests.setdefault(h["farm_id"], []).append(h)

    for farm in farms:
        fid    = farm["id"]
        fharvs = sorted(farm_harvests.get(fid, []), key=lambda x: x["harvest_date"])
        if not fharvs:
            continue

        console.print(f"\n  [bold cyan]🌴 {farm['name']}[/bold cyan] ({farm['num_trees']} trees)")
        console.print(Rule(style="dim"))

        nuts_list   = [h["nuts_harvested"] for h in fharvs]
        profit_list = [h["profit"] for h in fharvs]
        per_tree    = [h.get("nuts_per_tree", 0) for h in fharvs]

        avg_nuts    = sum(nuts_list) / len(nuts_list)
        avg_profit  = sum(profit_list) / len(profit_list)
        avg_pt      = sum(per_tree) / len(per_tree)

        trend_diff  = nuts_list[-1] - nuts_list[0] if len(nuts_list) >= 2 else 0
        trend_label = "📈 Improving" if trend_diff > 0 else ("📉 Declining" if trend_diff < 0 else "➡ Stable")

        season = get_current_season()
        lo, hi = get_harvest_interval(season)

        console.print(f"""
  Harvests Logged : [bold]{len(fharvs)}[/bold]
  Avg Nuts/Harvest: [bold green]{avg_nuts:.0f}[/bold green] nuts
  Avg Per Tree    : [bold green]{avg_pt:.1f}[/bold green] nuts/tree
  Avg Profit      : [bold cyan]₹{avg_profit:,.0f}[/bold cyan]
  Yield Trend     : {trend_label}

  [bold yellow]🔮 Next Harvest Estimate:[/bold yellow]
  • Season    : {season.title()} ({lo}–{hi} day cycle)
  • Nuts Range: {int(avg_nuts*0.95)} – {int(avg_nuts*1.10)} nuts
  • Accuracy  : {"[green]Moderate[/green]" if len(fharvs) >= 4 else "[yellow]Low — need more data[/yellow]"}
""")
        if len(fharvs) >= 4:
            seasonal_data: dict = {}
            for h in fharvs:
                s = h.get("season", "unknown")
                seasonal_data.setdefault(s, []).append(h["nuts_harvested"])
            if len(seasonal_data) > 1:
                console.print("  [bold]Season-wise Average:[/bold]")
                for s, nuts in seasonal_data.items():
                    console.print(f"    {s.title():<14} → {sum(nuts)/len(nuts):.0f} nuts avg")

# ─── Dashboard ────────────────────────────────────────────────────────────────
def show_dashboard(data: dict):
    farms    = data.get("farms", [])
    harvests = data.get("harvests", [])
    fertz    = data.get("fertilizers", [])
    today    = datetime.now().date()
    season   = get_current_season()
    lo, hi   = get_harvest_interval(season)

    console.print(Rule("[bold green]🌴 CocoTrack Dashboard[/bold green]"))

    # Live weather for farmer's district
    farmer_info = data.get("farmer") or {}
    wx_loc = farmer_info.get("district") or farmer_info.get("village") or ""
    if wx_loc:
        console.print(f"  [dim]Fetching weather for {wx_loc}...[/dim]", end="")
        wx = fetch_weather(wx_loc)
        console.print("\r" + " " * 50 + "\r", end="")
        show_weather_widget(wx)
    console.print()

    total_trees  = sum(f["num_trees"] for f in farms)
    total_rev    = sum(h["revenue"] for h in harvests)
    total_profit = sum(h["profit"] for h in harvests)

    stats = Table.grid(padding=1)
    stats.add_column(justify="center", width=20)
    stats.add_column(justify="center", width=20)
    stats.add_column(justify="center", width=20)
    stats.add_column(justify="center", width=20)
    stats.add_row(
        Panel(f"[bold green]{len(farms)}[/bold green]\n[dim]Farms[/dim]",              style="green"),
        Panel(f"[bold yellow]{total_trees}[/bold yellow]\n[dim]Trees[/dim]",           style="yellow"),
        Panel(f"[bold cyan]₹{total_rev:,.0f}[/bold cyan]\n[dim]Total Revenue[/dim]",   style="cyan"),
        Panel(f"[bold magenta]₹{total_profit:,.0f}[/bold magenta]\n[dim]Profit[/dim]", style="magenta"),
    )
    console.print(stats)
    console.print(f"\n  [dim]Today:[/dim] {today}  |  [dim]Season:[/dim] [bold]{season.title()}[/bold]  |  [dim]Harvest cycle:[/dim] [bold]{lo}–{hi} days[/bold]\n")

    alerts = []
    fert_last: dict = {}
    for e in fertz:
        key = (e["farm_id"], e["fertilizer_type"])
        if key not in fert_last or e["applied_date"] > fert_last[key]["applied_date"]:
            fert_last[key] = e
    for (_, ftype), e in fert_last.items():
        if e.get("next_due_date"):
            try:
                nd = datetime.strptime(e["next_due_date"], "%Y-%m-%d").date()
                if (nd - today).days <= 14:
                    alerts.append(f"🧪 [yellow]{e['farm_name']}[/yellow]: {ftype.title()} due {e['next_due_date']}")
            except: pass

    harv_last: dict = {}
    for h in harvests:
        if h["farm_id"] not in harv_last or h["harvest_date"] > harv_last[h["farm_id"]]["harvest_date"]:
            harv_last[h["farm_id"]] = h
    for _, h in harv_last.items():
        if h.get("next_harvest_from"):
            try:
                nf = datetime.strptime(h["next_harvest_from"], "%Y-%m-%d").date()
                if (nf - today).days <= 7:
                    alerts.append(f"🥥 [green]{h['farm_name']}[/green]: Harvest ready from {h['next_harvest_from']}")
            except: pass

    if alerts:
        console.print(Panel("\n".join(alerts), title="[bold red]⚡ Upcoming Alerts[/bold red]", style="red"))
    else:
        console.print(Panel("[green]✓ All clear — no immediate alerts[/green]", title="Alerts", style="dim"))

# ─── Main Menu ────────────────────────────────────────────────────────────────
def main():
    data = load_data()

    if not data.get("farmer"):
        show_banner()
        console.print(Panel(
            "[bold]Welcome to CocoTrack — Phase 2![/bold]\n\n"
            "Features:\n"
            "  🌴 Farm & crop tracking\n"
            "  🧪 Fertilizer alerts\n"
            "  🥥 Harvest records with unique Harvest IDs\n"
            "  ✏️  Edit sale prices after logging\n"
            "  💰 Sell as Pieces / By Weight / As Copra — profit comparison\n"
            "  🌤 Live weather + rain alerts (Open-Meteo, no key needed)\n"
            "  📊 Yield predictions\n\n"
            "Let's start with your farmer profile.",
            title="🌴 First Time Setup", style="green"
        ))
        data = setup_farmer(data)
        Prompt.ask("\n  Press Enter to continue")

    while True:
        show_banner()
        farmer_name = data["farmer"]["name"] if data.get("farmer") else "Farmer"
        console.print(f"  [bold green]Welcome, {farmer_name}[/bold green] 👋\n")

        console.print(Panel(
            "[bold cyan] 1.[/bold cyan] 📊  Dashboard\n"
            "[bold cyan] 2.[/bold cyan] 👨‍🌾  View / Edit Profile\n"
            "[bold cyan] 3.[/bold cyan] 🌱  Add Farm\n"
            "[bold cyan] 4.[/bold cyan] 🗺   View All Farms\n"
            "[bold cyan] 5.[/bold cyan] 🧪  Log Fertilizer Application\n"
            "[bold cyan] 6.[/bold cyan] 🔔  Fertilizer Alerts\n"
            "[bold cyan] 7.[/bold cyan] 🥥  Log Harvest\n"
            "[bold cyan] 8.[/bold cyan] ✏️   Edit Harvest Sale Price\n"
            "[bold cyan] 9.[/bold cyan] 📋  Harvest History\n"
            "[bold cyan]10.[/bold cyan] ⚡  Next Harvest Alerts\n"
            "[bold cyan]11.[/bold cyan] 💰  Selling Mode Calculator\n"
            "[bold cyan]12.[/bold cyan] 🔮  Yield Predictions\n"
            "[bold cyan]13.[/bold cyan] ⚙️   Settings\n"
            "[bold cyan]14.[/bold cyan] 🌤  Live Weather & Rain Check\n"
            "[bold cyan] 0.[/bold cyan] 🚪  Exit",
            title="[bold]Main Menu[/bold]", expand=False
        ))

        choice = Prompt.ask(
            "\n  Choose option",
            choices=["0","1","2","3","4","5","6","7","8","9","10","11","12","13","14"],
            show_choices=False
        )
        console.print()

        if   choice == "1":  show_dashboard(data)
        elif choice == "2":  show_farmer_profile(data)
        elif choice == "3":  data = add_farm(data)
        elif choice == "4":  list_farms(data)
        elif choice == "5":  data = log_fertilizer(data)
        elif choice == "6":  show_fertilizer_alerts(data)
        elif choice == "7":  data = log_harvest(data)
        elif choice == "8":  data = edit_harvest_sale_price(data)
        elif choice == "9":  show_harvest_history(data)
        elif choice == "10": show_next_harvest_alerts(data)
        elif choice == "11": sell_decision_calculator(data)
        elif choice == "12": show_predictions(data)
        elif choice == "13": data = show_settings(data)
        elif choice == "14": show_weather_for_location(data)
        elif choice == "0":
            console.print("[bold green]Goodbye! Happy farming! 🌴[/bold green]\n")
            sys.exit(0)

        if choice != "0":
            Prompt.ask("\n  Press Enter to return to menu")

if __name__ == "__main__":
    main()
