#!/usr/bin/env python3
"""
MADASS LEAD ENGINE — WEB EDITION
=================================
Mobile-friendly web app for Google Maps lead scraping.
Access from any phone or computer browser.

    pip install flask playwright
    playwright install chromium
    python madass_web.py

Deploy to Render:
    See Dockerfile + render.yaml included.
"""

import asyncio
import csv
import hashlib
import io
import json
import os
import queue
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, render_template_string, request, jsonify, Response, send_file

# ─── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path.home() / "MADASS_Leads"))
DATA_DIR.mkdir(exist_ok=True)
JSON_PATH = DATA_DIR / "leads_master.json"

# Global state
scraper_state = {
    "running": False,
    "stop_requested": False,
    "logs": [],
    "stats": {"captured": 0, "processed": 0, "skipped_web": 0, "skipped_rating": 0, "dupes": 0, "errors": 0},
    "progress": {"current": 0, "total": 0},
    "session_leads": [],
}
log_queue = queue.Queue()

# ─── Config ───────────────────────────────────────────────────────────────────

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", re.IGNORECASE)
PHONE_REGEX = re.compile(r"[\+]?[\d\s\-\(\)]{7,15}")

JUNK_EMAILS = {
    "google.com","gmail.com","gstatic.com","googleapis.com","schema.org",
    "w3.org","example.com","sentry.io","googleusercontent.com","youtube.com",
    "goo.gl","facebook.com","twitter.com","instagram.com","yelp.com",
}

CITIES = [
    "Houston, TX","Dallas, TX","Atlanta, GA","Phoenix, AZ","Miami, FL",
    "Charlotte, NC","San Antonio, TX","Tampa, FL","Orlando, FL","Nashville, TN",
    "Austin, TX","Denver, CO","Las Vegas, NV","Jacksonville, FL","Columbus, OH",
    "Indianapolis, IN","Memphis, TN","Oklahoma City, OK","Raleigh, NC","Louisville, KY",
    "Portland, OR","Seattle, WA","Minneapolis, MN","Detroit, MI","Kansas City, MO",
    "Salt Lake City, UT","Richmond, VA","Birmingham, AL","New Orleans, LA","Tucson, AZ",
]

NICHES = [
    "restaurant","barber shop","auto repair shop","plumber","electrician",
    "dentist","gym","hair salon","spa","landscaping company",
    "cleaning service","roofing company","HVAC company","pet grooming",
    "bakery","tattoo shop","photographer","florist","chiropractor",
    "yoga studio","car wash","nail salon","real estate agent","insurance agent",
    "accounting firm","law firm","veterinarian","moving company","pest control",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

TEMPLATES = {
    "email_no_site": {"label":"Email — No Website","subject":"Quick question about {name}","body":"Hey {owner},\n\nI was looking for {niche}s in {city} and came across {name} — your reviews are incredible. {reviews} reviews at {rating} stars is no joke.\n\nI noticed you don't have a website yet, and I think you're leaving money on the table. People search Google before they call anyone, and right now they can't find you.\n\nI build websites for businesses like yours. Clean, fast, mobile-friendly — the kind that actually brings in calls.\n\nWant to see a quick mockup? No cost, no commitment.\n\nTalk soon,\nGee"},
    "email_ugly": {"label":"Email — Bad Website","subject":"Your website might be turning customers away","body":"Hey {owner},\n\nFound {name} while searching {niche}s in {city}. Love what you've built — {reviews} reviews speak for themselves.\n\nI checked out your website though, and honestly? It's not doing your business justice. It looks dated and isn't great on mobile.\n\nI redesign websites for businesses like yours. Modern, fast, built to convert. Would you be open to seeing what a refresh could look like?\n\nBest,\nGee"},
    "dm_casual": {"label":"DM — Casual","subject":"","body":"Hey! Came across {name} and had to reach out — your reviews are amazing 🔥\n\nI noticed you don't have a website yet. I build them for local businesses and I'd love to put something together for you. Want to see what I had in mind?"},
    "dm_direct": {"label":"DM — Direct","subject":"","body":"Hey {owner}, quick question — have you thought about getting a website for {name}? I build sites for {niche}s and yours would be a perfect fit. Interested?"},
    "followup_1": {"label":"Follow-Up 1 (3 days)","subject":"Re: Quick question about {name}","body":"Hey {owner},\n\nJust bumping this up in case it got buried. Would love to show you what a site could do for {name}.\n\nNo rush — let me know if you're interested.\n\nGee"},
    "followup_2": {"label":"Follow-Up 2 (7 days)","subject":"Re: Quick question about {name}","body":"Hey {owner},\n\nLast thing — I put together a quick concept for {name} and I think you'd really like it.\n\nIf you've got 2 minutes this week I'd love to walk you through it.\n\nGee"},
    "breakup": {"label":"Breakup (14 days)","subject":"Should I close your file?","body":"Hey {owner},\n\nI've reached out a couple times about building a website for {name} and haven't heard back, so I'm guessing the timing isn't right.\n\nNo worries — I'll close out your file. If things change, you know where to find me.\n\nAll the best,\nGee"},
}

CITY_TZ = {"Houston":"America/Chicago","Dallas":"America/Chicago","Atlanta":"America/New_York","Phoenix":"America/Phoenix","Miami":"America/New_York","Denver":"America/Denver","Las Vegas":"America/Los_Angeles","Seattle":"America/Los_Angeles","Portland":"America/Los_Angeles"}


# ─── Utilities ────────────────────────────────────────────────────────────────

def clean_email(e):
    if not e: return ""
    d = e.split("@")[-1].lower()
    if d in JUNK_EMAILS or len(e) > 80 or len(e) < 5: return ""
    return e.lower().strip()

def lead_hash(name, addr):
    return hashlib.md5(f"{name.strip().lower()}|{addr.strip().lower()}".encode()).hexdigest()

def calc_score(rating, reviews, phone, email, has_web):
    s = 35 if not has_web else 5
    if rating >= 4.5: s += 20
    elif rating >= 4.0: s += 15
    elif rating >= 3.5: s += 10
    elif rating >= 3.0: s += 5
    if reviews >= 100: s += 20
    elif reviews >= 50: s += 15
    elif reviews >= 20: s += 10
    elif reviews >= 10: s += 5
    if phone: s += 10
    if email: s += 10
    return min(s, 100)

def tz_for(city):
    for k, v in CITY_TZ.items():
        if k.lower() in city.lower(): return v
    return "America/Chicago"

def load_leads():
    if JSON_PATH.exists():
        try:
            with open(JSON_PATH) as f: return json.load(f)
        except: pass
    return []

def save_leads(leads):
    with open(JSON_PATH, "w") as f:
        json.dump(leads, f, indent=2, ensure_ascii=False)

def existing_hashes():
    return {lead_hash(l.get("name",""), l.get("address","")) for l in load_leads()}


# ─── Extraction Helpers ───────────────────────────────────────────────────────

async def safe_text(page, sels, attr=None, default="", timeout=4000):
    for sel in sels:
        try:
            el = page.locator(sel).first
            if await el.count() == 0: continue
            if attr: v = await el.get_attribute(attr, timeout=timeout)
            else: v = await el.inner_text(timeout=timeout)
            if v and v.strip(): return v.strip()
        except: continue
    return default

async def human_scroll(page, cycles=10):
    for _ in range(cycles):
        await page.mouse.wheel(0, random.randint(600, 3500))
        if random.random() < 0.25: await asyncio.sleep(random.uniform(2, 4))
        else: await asyncio.sleep(random.uniform(0.6, 1.5))
        if random.random() < 0.1:
            await page.mouse.wheel(0, -random.randint(100, 400))
            await asyncio.sleep(random.uniform(0.3, 0.8))

async def detect_website(page):
    for sel in ['a[data-item-id="authority"]','a[aria-label*="Website"]','a[aria-label*="website"]','a[data-tooltip*="website"]','a[data-tooltip="Open website"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                h = await el.get_attribute("href", timeout=3000)
                if h and "google" not in h.lower() and len(h) > 10: return True, h
        except: continue
    return False, ""

async def extract_rating(page):
    try:
        el = page.locator('span[role="img"][aria-label*="star"]').first
        if await el.count() > 0:
            l = await el.get_attribute("aria-label", timeout=3000)
            if l:
                m = re.search(r"([\d.]+)", l)
                if m: return float(m.group(1))
    except: pass
    try:
        for sel in ['div.fontDisplayLarge','span.fontDisplayLarge','div.F7nice span']:
            el = page.locator(sel).first
            if await el.count() > 0:
                t = await el.inner_text(timeout=3000)
                v = float(t.strip().replace(",","."))
                if 0 < v <= 5: return v
    except: pass
    return 0.0

async def extract_reviews(page):
    for sel in ['button[jsaction*="pane.review"]','button[jsaction*="review"]','button[aria-label*="review"]','span[aria-label*="review"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                t = await el.inner_text(timeout=3000)
                c = re.sub(r"[^\d]","",t)
                if c: return int(c)
        except: continue
    return 0

async def extract_phone(page):
    for sel in ['button[data-item-id*="phone"]','button[aria-label*="Phone"]','a[href^="tel:"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                if "href" in sel:
                    h = await el.get_attribute("href", timeout=3000)
                    if h: return h.replace("tel:","").strip()
                t = await el.inner_text(timeout=3000)
                if t and re.search(r"\d",t): return t.strip()
        except: continue
    return ""

async def extract_address(page):
    for sel in ['button[data-item-id="address"]','button[aria-label*="Address"]','div[data-item-id="address"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                t = await el.inner_text(timeout=3000)
                if t and len(t) > 5: return t.strip()
        except: continue
    return ""

async def extract_category(page):
    for sel in ['button[jsaction*="pane.rating.category"]','span.DkEaL']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                t = await el.inner_text(timeout=3000)
                if t and 2 < len(t) < 80: return t.strip()
        except: continue
    return ""

async def extract_emails(page):
    try:
        content = await page.content()
        for e in EMAIL_REGEX.findall(content):
            c = clean_email(e)
            if c: return c
    except: pass
    return ""


# ─── Scraper Core ─────────────────────────────────────────────────────────────

def slog(msg, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    scraper_state["logs"].append(entry)
    log_queue.put(entry)
    # Keep log buffer manageable
    if len(scraper_state["logs"]) > 500:
        scraper_state["logs"] = scraper_state["logs"][-300:]

async def run_scrape(keywords, locations, min_rating, min_reviews, no_web_only, scroll_cycles):
    from playwright.async_api import async_playwright

    state = scraper_state
    state["stats"] = {"captured":0,"processed":0,"skipped_web":0,"skipped_rating":0,"dupes":0,"errors":0}
    state["progress"] = {"current":0,"total": len(keywords)*len(locations)}
    state["session_leads"] = []

    total = state["progress"]["total"]
    cur = 0
    results = []
    seen = existing_hashes()
    session_h = set()
    t0 = time.time()

    slog("MADASS LEAD ENGINE — WEB EDITION", "header")
    slog(f"Queued: {total} search(es)", "config")
    slog(f"Keywords: {', '.join(keywords)}", "config")
    slog(f"Cities: {', '.join(locations)}", "config")
    slog(f"Filters: ★≥{min_rating}  reviews≥{min_reviews}  no-web={no_web_only}", "config")

    tz = tz_for(locations[0] if locations else "Houston, TX")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled","--no-first-run","--no-default-browser-check",
                       "--disable-dev-shm-usage","--no-sandbox","--disable-gpu"],
            )
        except Exception as e:
            slog(f"Browser launch failed: {e}", "error")
            slog("Run: playwright install chromium", "error")
            return

        ctx = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width":1280,"height":720},
            locale="en-US", timezone_id=tz,
        )
        await ctx.add_init_script("""
            try{Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.chrome={runtime:{}};}catch(e){}
        """)

        page = await ctx.new_page()
        page.set_default_timeout(15000)

        # Warmup
        try:
            await page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            for btn in ["Accept all","Accept","Reject all"]:
                try:
                    b = page.locator(f'button:has-text("{btn}")').first
                    if await b.count() > 0: await b.click(); await asyncio.sleep(1); break
                except: pass
            slog("Browser ready", "success")
        except Exception as e:
            slog(f"Warmup: {e}", "warn")

        for keyword in keywords:
            for location in locations:
                if state["stop_requested"]: slog("Stopped by user.", "warn"); break

                cur += 1
                query = f"{keyword} in {location}"
                state["progress"]["current"] = cur
                slog(f"[{cur}/{total}] Searching: {query}", "search")

                try:
                    url = f"https://www.google.com/maps/search/{quote(query, safe='')}"

                    nav_ok = False
                    for attempt in range(3):
                        try:
                            wait = ["load","domcontentloaded","commit"][min(attempt,2)]
                            await page.goto(url, wait_until=wait, timeout=30000)
                            await asyncio.sleep(2)
                            if "google.com/maps" in page.url:
                                nav_ok = True; break
                        except Exception as e:
                            slog(f"Nav retry {attempt+1}/3: {str(e)[:60]}", "warn")
                            await asyncio.sleep(random.uniform(2,5))
                            try: await page.close()
                            except: pass
                            page = await ctx.new_page()
                            page.set_default_timeout(15000)

                    if not nav_ok:
                        state["stats"]["errors"] += 1
                        slog("Could not load Maps.", "error"); continue

                    await asyncio.sleep(random.uniform(2,4))

                    # Consent
                    for btn in ["Accept all","Accept","Reject all","I agree"]:
                        try:
                            b = page.locator(f'button:has-text("{btn}")').first
                            if await b.count() > 0: await b.click(); await asyncio.sleep(1); break
                        except: pass

                    slog(f"Scrolling ({scroll_cycles}x)...", "info")
                    await human_scroll(page, cycles=scroll_cycles)

                    listings = []
                    for sel in ['div[role="article"]','div.Nv2PK','a.hfpxzc']:
                        items = await page.locator(sel).all()
                        if len(items) > 2: listings = items; break

                    if not listings:
                        slog("No listings found.", "warn"); continue

                    slog(f"{len(listings)} listings found", "info")

                    for idx, listing in enumerate(listings):
                        if state["stop_requested"]: break
                        try:
                            state["stats"]["processed"] += 1
                            try: await listing.scroll_into_view_if_needed(timeout=5000)
                            except: pass
                            await asyncio.sleep(random.uniform(0.3,0.7))
                            await listing.click(timeout=5000)
                            await asyncio.sleep(random.uniform(2,3.5))

                            name = await safe_text(page,["h1.fontHeadlineLarge","h1.DUwDvf","h1"])
                            if not name or len(name) < 2: continue

                            address = await extract_address(page)
                            h = lead_hash(name, address)
                            if h in seen or h in session_h:
                                state["stats"]["dupes"] += 1; continue
                            session_h.add(h)

                            rating = await extract_rating(page)
                            reviews = await extract_reviews(page)
                            has_web, web_url = await detect_website(page)

                            if no_web_only and has_web:
                                state["stats"]["skipped_web"] += 1; continue

                            if rating > 0 and rating < min_rating:
                                state["stats"]["skipped_rating"] += 1; continue

                            if reviews < min_reviews: continue

                            phone = await extract_phone(page)
                            email = await extract_emails(page)
                            category = await extract_category(page)
                            score = calc_score(rating, reviews, bool(phone), bool(email), has_web)

                            lead = {
                                "name":name,"phone":phone,"address":address,
                                "rating":rating,"reviews":reviews,"email":email,
                                "has_website":"yes" if has_web else "no",
                                "website":web_url,"maps_url":page.url,
                                "niche":keyword,"city":location,"category":category,
                                "lead_score":score,"scraped_at":datetime.now().isoformat(),
                            }
                            results.append(lead)
                            state["session_leads"].append(lead)
                            state["stats"]["captured"] += 1

                            badge = "🔥" if score >= 75 else "✦" if score >= 50 else "·"
                            slog(f"✓ {name}  ★{rating} ({reviews}) {'📞' if phone else ''} [{badge}{score}]", "found")

                        except Exception as e:
                            state["stats"]["errors"] += 1
                            slog(f"Error on listing {idx+1}: {str(e)[:60]}", "error")

                        await asyncio.sleep(random.uniform(0.2,0.5))

                except Exception as e:
                    state["stats"]["errors"] += 1
                    slog(f"Search error: {str(e)[:60]}", "error")

                if cur < total and not state["stop_requested"]:
                    await asyncio.sleep(random.uniform(3,6))

            if state["stop_requested"]: break

        await browser.close()

    # Save
    if results:
        all_leads = load_leads()
        all_leads.extend(results)
        save_leads(all_leads)
        elapsed = int(time.time() - t0)
        slog(f"✅ DONE — {len(results)} leads in {elapsed}s", "success")
        hot = sum(1 for l in results if l["lead_score"] >= 75)
        slog(f"🔥 Hot: {hot}  Total: {len(results)}", "success")
    else:
        slog("0 leads. Try broader filters.", "warn")

def start_scrape_thread(keywords, locations, min_rating, min_reviews, no_web_only, scroll_cycles):
    scraper_state["running"] = True
    scraper_state["stop_requested"] = False
    scraper_state["logs"] = []
    def run():
        try:
            asyncio.run(run_scrape(keywords, locations, min_rating, min_reviews, no_web_only, scroll_cycles))
        except Exception as e:
            slog(f"Fatal: {e}", "error")
        finally:
            scraper_state["running"] = False
    threading.Thread(target=run, daemon=True).start()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, cities=CITIES, niches=NICHES, templates=TEMPLATES)

@app.route("/api/start", methods=["POST"])
def api_start():
    if scraper_state["running"]:
        return jsonify({"error": "Already running"}), 400

    data = request.json or {}
    niche = data.get("custom_niche","").strip() or data.get("niche","restaurant")
    city = data.get("custom_city","").strip() or data.get("city","Houston, TX")

    keywords = [niche]
    extra_n = data.get("extra_niches","").strip()
    if extra_n: keywords.extend([k.strip() for k in extra_n.split(",") if k.strip()])

    locations = [city]
    extra_c = data.get("extra_cities","").strip()
    if extra_c: locations.extend([c.strip() for c in extra_c.split(",") if c.strip()])

    min_rating = float(data.get("min_rating", 3.5))
    min_reviews = int(data.get("min_reviews", 10))
    no_web_only = data.get("no_web_only", True)
    scroll = int(data.get("scroll_cycles", 10))

    start_scrape_thread(keywords, locations, min_rating, min_reviews, no_web_only, scroll)
    return jsonify({"status": "started"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    scraper_state["stop_requested"] = True
    return jsonify({"status": "stopping"})

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": scraper_state["running"],
        "stats": scraper_state["stats"],
        "progress": scraper_state["progress"],
        "log_count": len(scraper_state["logs"]),
    })

@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    return jsonify(scraper_state["logs"][since:])

@app.route("/api/leads")
def api_leads():
    leads = load_leads()
    q = request.args.get("q","").lower()
    if q:
        leads = [l for l in leads if q in l.get("name","").lower() or q in l.get("city","").lower() or q in l.get("niche","").lower()]
    leads.reverse()  # newest first
    return jsonify(leads[:200])

@app.route("/api/leads/csv")
def api_csv():
    leads = load_leads()
    if not leads:
        return "No leads", 404
    output = io.StringIO()
    fields = ["name","phone","address","rating","reviews","email","has_website","city","niche","category","lead_score","maps_url","scraped_at"]
    w = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(leads)
    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="madass_leads.csv")

@app.route("/api/leads/delete", methods=["POST"])
def api_delete():
    data = request.json or {}
    name = data.get("name","")
    leads = [l for l in load_leads() if l.get("name") != name]
    save_leads(leads)
    return jsonify({"status":"deleted","remaining":len(leads)})

@app.route("/api/template", methods=["POST"])
def api_template():
    data = request.json or {}
    tpl_key = data.get("template","email_no_site")
    lead = data.get("lead",{})
    tpl = TEMPLATES.get(tpl_key, TEMPLATES["email_no_site"])

    vals = {
        "name": lead.get("name","[Business]"),
        "owner": lead.get("owner_name","there") or "there",
        "city": lead.get("city","[City]"),
        "niche": lead.get("niche","business"),
        "rating": str(lead.get("rating","")),
        "reviews": str(lead.get("reviews","")),
    }
    subj = tpl["subject"]
    body = tpl["body"]
    for k, v in vals.items():
        subj = subj.replace("{"+k+"}", v)
        body = body.replace("{"+k+"}", v)

    return jsonify({"subject":subj,"body":body,"label":tpl["label"]})

@app.route("/api/stream")
def api_stream():
    """Server-Sent Events for real-time log streaming."""
    def generate():
        idx = 0
        while True:
            logs = scraper_state["logs"][idx:]
            if logs:
                for entry in logs:
                    yield f"data: {json.dumps(entry)}\n\n"
                idx += len(logs)
            stats = {
                "stats": scraper_state["stats"],
                "progress": scraper_state["progress"],
                "running": scraper_state["running"],
            }
            yield f"event: status\ndata: {json.dumps(stats)}\n\n"
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ─── HTML Template ────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>MADASS Lead Engine</title>
<style>
:root{--bg:#06080f;--s1:#0d1220;--s2:#141c2e;--s3:#1a2540;--brd:#1c2844;--acc:#7c5cfc;--acc2:#9b7dff;--grn:#00e88f;--red:#ff4d6a;--ylw:#ffbe2e;--orn:#ff8c42;--cyn:#00d4ff;--txt:#e8ecf4;--mut:#5a6a8a;--dim:#2e3d5a}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;overflow-x:hidden;min-height:100vh;min-height:100dvh}
input,select,textarea,button{font-family:inherit;font-size:inherit}

/* Header */
.hdr{background:var(--s1);border-bottom:1px solid var(--brd);padding:14px 16px;position:sticky;top:0;z-index:100;backdrop-filter:blur(12px);display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--acc),#6d28d9);display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:900;color:#fff}
.logo-text{font-size:17px;font-weight:800;color:var(--acc2)}
.logo-sub{font-size:8px;color:var(--dim);font-family:'Courier New',monospace;letter-spacing:2px}
.status{font-family:'Courier New',monospace;font-size:11px;font-weight:700}
.status.ready{color:var(--grn)}.status.running{color:var(--ylw)}.status.stopped{color:var(--orn)}

/* Stats ribbon */
.stats{display:flex;overflow-x:auto;gap:2px;padding:8px 12px;background:var(--s2);border-bottom:1px solid var(--brd);-webkit-overflow-scrolling:touch}
.stats::-webkit-scrollbar{display:none}
.stat{flex:1 0 auto;min-width:70px;text-align:center;padding:6px 10px}
.stat-label{font-size:7px;font-weight:700;letter-spacing:1.5px;color:var(--dim);font-family:'Courier New',monospace;text-transform:uppercase}
.stat-val{font-size:20px;font-weight:900;font-family:'Courier New',monospace;line-height:1.2}

/* Tabs */
.tabs{display:flex;gap:4px;padding:10px 12px 0;background:var(--bg)}
.tab{flex:1;padding:10px;text-align:center;font-size:12px;font-weight:700;border-radius:8px 8px 0 0;background:var(--s1);color:var(--mut);cursor:pointer;border:1px solid var(--brd);border-bottom:none;transition:.15s}
.tab.active{background:var(--s2);color:var(--acc2);border-color:var(--acc)44}

/* Panels */
.panel{display:none;padding:12px;animation:fadeIn .2s}
.panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}

/* Form elements */
.field{margin-bottom:10px}
.field label{display:block;font-size:9px;font-weight:700;letter-spacing:1.5px;color:var(--dim);font-family:'Courier New',monospace;margin-bottom:3px;text-transform:uppercase}
.field select,.field input{width:100%;background:var(--s2);border:1px solid var(--brd);border-radius:8px;padding:10px 12px;color:var(--txt);font-size:13px;font-family:'Courier New',monospace;outline:none;-webkit-appearance:none}
.field select:focus,.field input:focus{border-color:var(--acc)}
.row{display:flex;gap:8px}
.row .field{flex:1}

.chk{display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer}
.chk input{width:18px;height:18px;accent-color:var(--acc)}
.chk span{font-size:12px;font-family:'Courier New',monospace}
.chk .green{color:var(--grn)}

/* Buttons */
.btn{width:100%;padding:14px;border:none;border-radius:10px;font-size:15px;font-weight:800;cursor:pointer;transition:.15s;font-family:inherit}
.btn-primary{background:var(--acc);color:#fff}
.btn-primary:active{background:#6d28d9;transform:scale(0.98)}
.btn-primary:disabled{opacity:.5;cursor:wait}
.btn-danger{background:var(--red);color:#fff;margin-top:6px}
.btn-danger:disabled{opacity:.3}
.btn-secondary{background:var(--s2);color:var(--mut);border:1px solid var(--brd);margin-top:6px;font-size:12px;padding:10px}

/* Progress */
.progress{height:4px;background:var(--brd);border-radius:2px;margin:8px 0 4px;overflow:hidden}
.progress-bar{height:100%;background:var(--acc);border-radius:2px;transition:width .3s;width:0%}
.progress-text{font-size:9px;color:var(--dim);font-family:'Courier New',monospace}

/* Log */
.log-box{background:var(--bg);border:1px solid var(--brd);border-radius:10px;padding:10px;height:320px;overflow-y:auto;font-family:'Courier New',monospace;font-size:11px;line-height:1.6;-webkit-overflow-scrolling:touch}
.log-entry{padding:1px 0}
.log-header{color:#b8a0ff}.log-config{color:#7c8fff}.log-search{color:var(--cyn)}.log-found{color:var(--grn)}.log-skip{color:#3a4a66}.log-info{color:#8090aa}.log-warn{color:var(--ylw)}.log-error{color:var(--red)}.log-success{color:var(--grn)}

/* Leads table */
.lead-card{background:var(--s1);border:1px solid var(--brd);border-radius:10px;padding:12px;margin-bottom:8px}
.lead-name{font-size:14px;font-weight:700;color:var(--txt)}
.lead-meta{font-size:11px;color:var(--mut);font-family:'Courier New',monospace;margin-top:3px}
.lead-badges{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.badge{font-size:9px;font-weight:700;padding:3px 8px;border-radius:4px;font-family:'Courier New',monospace}
.badge-score{background:var(--acc)22;color:var(--acc2)}
.badge-hot{background:#ff4d6a22;color:#ff6b81}
.badge-noweb{background:var(--grn)18;color:var(--grn)}
.badge-phone{background:var(--cyn)18;color:var(--cyn)}
.lead-actions{display:flex;gap:6px;margin-top:8px}
.lead-btn{padding:7px 12px;border-radius:6px;font-size:11px;font-weight:600;border:1px solid var(--brd);background:var(--s2);color:var(--mut);cursor:pointer;font-family:'Courier New',monospace}
.lead-btn:active{background:var(--s3)}
.lead-btn.primary{background:var(--acc)22;color:var(--acc2);border-color:var(--acc)44}

/* Template composer */
.tpl-selector{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
.tpl-btn{padding:6px 10px;border-radius:6px;font-size:10px;font-weight:600;border:1px solid var(--brd);background:var(--s1);color:var(--mut);cursor:pointer;font-family:'Courier New',monospace}
.tpl-btn.active{background:var(--acc)22;color:var(--acc2);border-color:var(--acc)44}
.tpl-subject{background:var(--s1);border-radius:6px;padding:8px 10px;font-size:11px;color:var(--mut);font-family:'Courier New',monospace;margin-bottom:6px}
.tpl-body{background:var(--bg);border:1px solid var(--brd);border-radius:8px;padding:12px;font-family:'Courier New',monospace;font-size:12px;color:#cbd5e1;line-height:1.6;white-space:pre-wrap;max-height:300px;overflow-y:auto;-webkit-overflow-scrolling:touch}
.copy-btn{margin-top:8px}

.search-bar{margin-bottom:10px}
.search-bar input{width:100%;background:var(--s1);border:1px solid var(--brd);border-radius:8px;padding:10px 12px;color:var(--txt);font-size:12px;font-family:'Courier New',monospace;outline:none}
.empty{text-align:center;padding:40px 20px;color:var(--dim);font-family:'Courier New',monospace;font-size:12px}

/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(4px)}
.modal-overlay.show{display:flex}
.modal{background:var(--s1);border:1px solid var(--brd);border-radius:16px;padding:20px;width:100%;max-width:440px;max-height:85vh;overflow-y:auto}
.modal h3{font-size:16px;font-weight:800;color:var(--txt);margin-bottom:12px}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
    <div class="logo">
        <div class="logo-icon">M</div>
        <div>
            <div class="logo-text">MADASS</div>
            <div class="logo-sub">LEAD ENGINE · WEB</div>
        </div>
    </div>
    <div id="status" class="status ready">● READY</div>
</div>

<!-- Stats Ribbon -->
<div class="stats">
    <div class="stat"><div class="stat-label">CAPTURED</div><div class="stat-val" id="st-captured" style="color:var(--grn)">0</div></div>
    <div class="stat"><div class="stat-label">SCANNED</div><div class="stat-val" id="st-processed" style="color:var(--cyn)">0</div></div>
    <div class="stat"><div class="stat-label">NO-WEB</div><div class="stat-val" id="st-skipped_web" style="color:var(--ylw)">0</div></div>
    <div class="stat"><div class="stat-label">DUPES</div><div class="stat-val" id="st-dupes" style="color:var(--mut)">0</div></div>
    <div class="stat"><div class="stat-label">ERRORS</div><div class="stat-val" id="st-errors" style="color:var(--red)">0</div></div>
</div>

<!-- Tabs -->
<div class="tabs">
    <div class="tab active" onclick="switchTab('scraper')">⚡ Scraper</div>
    <div class="tab" onclick="switchTab('leads')">📋 Leads</div>
    <div class="tab" onclick="switchTab('compose')">✉ Compose</div>
</div>

<!-- Scraper Panel -->
<div id="panel-scraper" class="panel active">
    <div class="field">
        <label>BUSINESS TYPE</label>
        <select id="niche">{% for n in niches %}<option value="{{n}}">{{n}}</option>{% endfor %}</select>
    </div>
    <div class="field">
        <label>OR CUSTOM NICHE</label>
        <input id="custom_niche" placeholder="Type custom niche...">
    </div>
    <div class="field">
        <label>TARGET CITY</label>
        <select id="city">{% for c in cities %}<option value="{{c}}">{{c}}</option>{% endfor %}</select>
    </div>
    <div class="field">
        <label>OR CUSTOM CITY</label>
        <input id="custom_city" placeholder="Type custom city...">
    </div>
    <div class="field">
        <label>EXTRA NICHES (comma sep)</label>
        <input id="extra_niches" placeholder="plumber, electrician...">
    </div>
    <div class="field">
        <label>EXTRA CITIES (comma sep)</label>
        <input id="extra_cities" placeholder="Dallas TX, Atlanta GA...">
    </div>
    <div class="row">
        <div class="field"><label>MIN RATING</label><input id="min_rating" type="number" value="3.5" step="0.5" min="0" max="5"></div>
        <div class="field"><label>MIN REVIEWS</label><input id="min_reviews" type="number" value="10" min="0"></div>
        <div class="field"><label>SCROLL DEPTH</label><input id="scroll_cycles" type="number" value="10" min="3" max="30"></div>
    </div>
    <label class="chk"><input type="checkbox" id="no_web_only" checked><span class="green">✦ ONLY businesses WITHOUT a website</span></label>

    <button class="btn btn-primary" id="startBtn" onclick="startScrape()">⚡ START SCRAPING</button>
    <button class="btn btn-danger" id="stopBtn" onclick="stopScrape()" disabled>■ STOP</button>

    <div class="progress"><div class="progress-bar" id="pbar"></div></div>
    <div class="progress-text" id="ptext"></div>

    <div class="log-box" id="logbox"></div>

    <button class="btn btn-secondary" onclick="exportCSV()">⬇ Export CSV</button>
</div>

<!-- Leads Panel -->
<div id="panel-leads" class="panel">
    <div class="search-bar"><input id="lead-search" placeholder="Search by name, city, niche..." oninput="loadLeads()"></div>
    <div id="leads-list"></div>
</div>

<!-- Compose Panel -->
<div id="panel-compose" class="panel">
    <div id="compose-lead-info" class="lead-card" style="margin-bottom:12px">
        <div style="color:var(--dim);font-size:12px;font-family:'Courier New',monospace">Select a lead from the Leads tab first, or compose a generic message below.</div>
    </div>
    <div class="tpl-selector" id="tpl-selector"></div>
    <div class="tpl-subject" id="tpl-subject"></div>
    <div class="tpl-body" id="tpl-body"></div>
    <button class="btn btn-primary copy-btn" onclick="copyMessage()">📋 Copy Message</button>
</div>

<!-- Template Modal (for lead cards) -->
<div class="modal-overlay" id="tpl-modal">
    <div class="modal">
        <h3 id="modal-title">Compose Message</h3>
        <div class="tpl-selector" id="modal-tpl-selector"></div>
        <div class="tpl-subject" id="modal-tpl-subject"></div>
        <div class="tpl-body" id="modal-tpl-body"></div>
        <button class="btn btn-primary copy-btn" onclick="copyModal()">📋 Copy Message</button>
        <button class="btn btn-secondary" onclick="closeModal()">Close</button>
    </div>
</div>

<script>
const TEMPLATES = {{ templates | tojson }};
let activeLead = null;
let activeTemplate = 'email_no_site';
let logIdx = 0;
let evtSource = null;

// ── Tabs ──
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['scraper','leads','compose'][i]===tab));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.getElementById('panel-'+tab).classList.add('active');
    if (tab==='leads') loadLeads();
    if (tab==='compose') renderCompose();
}

// ── Scraper ──
function startScrape() {
    const body = {
        niche: document.getElementById('niche').value,
        custom_niche: document.getElementById('custom_niche').value,
        city: document.getElementById('city').value,
        custom_city: document.getElementById('custom_city').value,
        extra_niches: document.getElementById('extra_niches').value,
        extra_cities: document.getElementById('extra_cities').value,
        min_rating: parseFloat(document.getElementById('min_rating').value) || 3.5,
        min_reviews: parseInt(document.getElementById('min_reviews').value) || 10,
        no_web_only: document.getElementById('no_web_only').checked,
        scroll_cycles: parseInt(document.getElementById('scroll_cycles').value) || 10,
    };
    fetch('/api/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    document.getElementById('startBtn').disabled = true;
    document.getElementById('startBtn').textContent = '⏳ Running...';
    document.getElementById('stopBtn').disabled = false;
    document.getElementById('status').className = 'status running';
    document.getElementById('status').textContent = '● SCRAPING';
    document.getElementById('logbox').innerHTML = '';
    logIdx = 0;
    startSSE();
}

function stopScrape() {
    fetch('/api/stop', {method:'POST'});
    document.getElementById('status').className = 'status stopped';
    document.getElementById('status').textContent = '● STOPPING';
}

function startSSE() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource('/api/stream');
    evtSource.onmessage = (e) => {
        const entry = JSON.parse(e.data);
        appendLog(entry);
    };
    evtSource.addEventListener('status', (e) => {
        const d = JSON.parse(e.data);
        for (const [k,v] of Object.entries(d.stats)) {
            const el = document.getElementById('st-'+k);
            if (el) el.textContent = v;
        }
        const p = d.progress;
        if (p.total > 0) {
            document.getElementById('pbar').style.width = (p.current/p.total*100)+'%';
            document.getElementById('ptext').textContent = `Search ${p.current}/${p.total}`;
        }
        if (!d.running) {
            document.getElementById('startBtn').disabled = false;
            document.getElementById('startBtn').textContent = '⚡ START SCRAPING';
            document.getElementById('stopBtn').disabled = true;
            document.getElementById('status').className = 'status ready';
            document.getElementById('status').textContent = '● READY';
            if (evtSource) { evtSource.close(); evtSource = null; }
        }
    });
}

function appendLog(entry) {
    const box = document.getElementById('logbox');
    const div = document.createElement('div');
    div.className = 'log-entry log-' + (entry.level||'info');
    div.textContent = (entry.time ? entry.time+' ' : '') + entry.msg;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
}

// ── Leads ──
async function loadLeads() {
    const q = document.getElementById('lead-search')?.value || '';
    const res = await fetch('/api/leads?q='+encodeURIComponent(q));
    const leads = await res.json();
    const container = document.getElementById('leads-list');
    if (!leads.length) {
        container.innerHTML = '<div class="empty">📭 No leads yet. Run a scrape first!</div>';
        return;
    }
    container.innerHTML = leads.map(l => {
        const score = l.lead_score||0;
        const badge = score>=75?'🔥 HOT':score>=50?'✦ GOOD':'· OK';
        return `<div class="lead-card">
            <div class="lead-name">${esc(l.name)}</div>
            <div class="lead-meta">${esc(l.niche)} · ${esc(l.city)} ${l.phone?'· '+esc(l.phone):''}</div>
            <div class="lead-badges">
                <span class="badge badge-score">${badge} ${score}</span>
                ${l.has_website==='no'?'<span class="badge badge-noweb">NO WEBSITE</span>':''}
                ${l.phone?'<span class="badge badge-phone">📞</span>':''}
                ${l.rating?'<span class="badge" style="background:#ffbe2e18;color:#ffbe2e">★ '+l.rating+'</span>':''}
                ${l.reviews?'<span class="badge" style="background:#ffffff08;color:var(--mut)">('+l.reviews+')</span>':''}
            </div>
            <div class="lead-actions">
                <button class="lead-btn primary" onclick='selectLead(${JSON.stringify(l).replace(/'/g,"&#39;")})'>✉ Compose</button>
                ${l.phone?`<a class="lead-btn" href="tel:${esc(l.phone)}">📞 Call</a>`:''}
                ${l.maps_url?`<a class="lead-btn" href="${esc(l.maps_url)}" target="_blank">📍 Maps</a>`:''}
            </div>
        </div>`;
    }).join('');
}

function selectLead(lead) {
    activeLead = lead;
    openTemplateModal(lead);
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// ── Compose ──
function renderCompose() {
    const sel = document.getElementById('tpl-selector');
    sel.innerHTML = Object.entries(TEMPLATES).map(([k,v]) =>
        `<button class="tpl-btn ${k===activeTemplate?'active':''}" onclick="setTemplate('${k}')">${v.label}</button>`
    ).join('');
    renderTemplateContent('tpl-subject','tpl-body', activeLead);
}

function setTemplate(key) {
    activeTemplate = key;
    document.querySelectorAll('#tpl-selector .tpl-btn').forEach(b => b.classList.toggle('active', b.textContent===TEMPLATES[key].label));
    renderTemplateContent('tpl-subject','tpl-body', activeLead);
}

function renderTemplateContent(subjId, bodyId, lead) {
    const tpl = TEMPLATES[activeTemplate];
    const v = {
        name: lead?.name||'[Business]',
        owner: lead?.owner_name||'there',
        city: lead?.city||'[City]',
        niche: lead?.niche||'business',
        rating: String(lead?.rating||'[rating]'),
        reviews: String(lead?.reviews||'[reviews]'),
    };
    let subj = tpl.subject, body = tpl.body;
    for (const [k,val] of Object.entries(v)) { subj=subj.split('{'+k+'}').join(val); body=body.split('{'+k+'}').join(val); }
    document.getElementById(subjId).textContent = subj ? 'Subject: '+subj : '(DM — no subject)';
    document.getElementById(bodyId).textContent = body;

    if (lead) {
        document.getElementById('compose-lead-info').innerHTML = `
            <div class="lead-name">${esc(lead.name)}</div>
            <div class="lead-meta">${esc(lead.niche)} · ${esc(lead.city)} ${lead.phone?'· '+esc(lead.phone):''}</div>`;
    }
}

function copyMessage() {
    const subj = document.getElementById('tpl-subject').textContent;
    const body = document.getElementById('tpl-body').textContent;
    const text = subj.startsWith('Subject:') ? subj+'\n\n'+body : body;
    navigator.clipboard?.writeText(text).then(() => {
        const btn = document.querySelector('.copy-btn');
        btn.textContent = '✓ Copied!'; btn.style.background = 'var(--grn)';
        setTimeout(() => { btn.textContent = '📋 Copy Message'; btn.style.background = ''; }, 1500);
    });
}

// ── Template Modal ──
let modalTemplate = 'email_no_site';
let modalLead = null;

function openTemplateModal(lead) {
    modalLead = lead;
    modalTemplate = 'email_no_site';
    document.getElementById('modal-title').textContent = '✉ ' + lead.name;
    const sel = document.getElementById('modal-tpl-selector');
    sel.innerHTML = Object.entries(TEMPLATES).map(([k,v]) =>
        `<button class="tpl-btn ${k===modalTemplate?'active':''}" onclick="setModalTpl('${k}')">${v.label}</button>`
    ).join('');
    renderModalContent();
    document.getElementById('tpl-modal').classList.add('show');
}

function setModalTpl(key) {
    modalTemplate = key;
    document.querySelectorAll('#modal-tpl-selector .tpl-btn').forEach(b => b.classList.toggle('active', b.textContent===TEMPLATES[key].label));
    renderModalContent();
}

function renderModalContent() {
    const tpl = TEMPLATES[modalTemplate];
    const lead = modalLead;
    const v = {name:lead?.name||'',owner:lead?.owner_name||'there',city:lead?.city||'',niche:lead?.niche||'',rating:String(lead?.rating||''),reviews:String(lead?.reviews||'')};
    let subj = tpl.subject, body = tpl.body;
    for (const [k,val] of Object.entries(v)) { subj=subj.split('{'+k+'}').join(val); body=body.split('{'+k+'}').join(val); }
    document.getElementById('modal-tpl-subject').textContent = subj ? 'Subject: '+subj : '(DM)';
    document.getElementById('modal-tpl-body').textContent = body;
}

function copyModal() {
    const subj = document.getElementById('modal-tpl-subject').textContent;
    const body = document.getElementById('modal-tpl-body').textContent;
    const text = subj.startsWith('Subject:') ? subj+'\n\n'+body : body;
    navigator.clipboard?.writeText(text).then(() => {
        const btns = document.querySelectorAll('#tpl-modal .copy-btn');
        btns.forEach(b => { b.textContent='✓ Copied!'; b.style.background='var(--grn)'; });
        setTimeout(() => btns.forEach(b => { b.textContent='📋 Copy Message'; b.style.background=''; }), 1500);
    });
}

function closeModal() { document.getElementById('tpl-modal').classList.remove('show'); }

function exportCSV() { window.location.href = '/api/leads/csv'; }

// Auto-render compose on load
document.addEventListener('DOMContentLoaded', renderCompose);
</script>
</body>
</html>"""


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    # Auto-detect local IP for phone access
    local_ip = "localhost"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        pass

    print()
    print("  ╔═══════════════════════════════════════════╗")
    print("  ║   MADASS LEAD ENGINE — WEB EDITION        ║")
    print("  ╚═══════════════════════════════════════════╝")
    print()
    print(f"  📱 Phone (same WiFi):  http://{local_ip}:{port}")
    print(f"  💻 Local:              http://localhost:{port}")
    print()
    print("  Open the phone URL in your mobile browser!")
    print()

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
