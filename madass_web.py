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

@app.route("/health")
def health():
    """Lightweight health check for UptimeRobot / cron-job.org keep-alive pings."""
    lead_count = len(load_leads())
    return jsonify({
        "status": "ok",
        "engine": "MADASS Lead Engine v3.5 Web",
        "leads_on_file": lead_count,
        "scraper_running": scraper_state["running"],
        "uptime": "alive",
    })

@app.route("/ping")
def ping():
    """Ultra-minimal keep-alive — returns 200 with no processing."""
    return "pong", 200

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
<link href="https://fonts.googleapis.com/css2?family=Anybody:wght@700;800;900&family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#04060e;--s1:#0a0f1e;--s2:#10162a;--s3:#161f38;
  --brd:#1a2545;--brd2:#243260;
  --acc:#635bff;--acc2:#8b83ff;--acc3:#b0abff;--accG:linear-gradient(135deg,#635bff,#00c6fb);
  --grn:#00f0a0;--grn2:#00d48d;--grnG:linear-gradient(135deg,#00f0a0,#00c6fb);
  --red:#ff3d71;--ylw:#ffc244;--orn:#ff8a5c;--cyn:#00c6fb;
  --txt:#edf0f7;--txt2:#c4c9d9;--mut:#5b6689;--dim:#2a3358;
  --glass:rgba(10,15,30,0.6);--glassBrd:rgba(255,255,255,0.04);
  --radius:12px;--radiusL:16px;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--txt);font-family:'Outfit',sans-serif;font-size:14px;overflow-x:hidden;min-height:100dvh}
input,select,textarea,button{font-family:inherit;font-size:inherit}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-thumb{background:var(--dim);border-radius:9px}
::placeholder{color:var(--dim)}

/* ── Header ── */
.hdr{
  background:linear-gradient(180deg,var(--s1) 0%,var(--bg) 100%);
  border-bottom:1px solid var(--brd);
  padding:16px 18px;position:sticky;top:0;z-index:100;
  backdrop-filter:blur(20px);
  display:flex;align-items:center;justify-content:space-between;
}
.logo{display:flex;align-items:center;gap:11px}
.logo-icon{
  width:38px;height:38px;border-radius:10px;
  background:var(--accG);
  display:flex;align-items:center;justify-content:center;
  font-family:'Anybody',sans-serif;font-size:20px;font-weight:900;color:#fff;
  box-shadow:0 4px 20px rgba(99,91,255,0.3);
  position:relative;overflow:hidden;
}
.logo-icon::after{
  content:'';position:absolute;inset:0;
  background:linear-gradient(45deg,transparent 40%,rgba(255,255,255,0.15) 50%,transparent 60%);
  animation:shimmer 3s infinite;
}
@keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
.logo-text{font-family:'Anybody',sans-serif;font-size:19px;font-weight:900;color:#fff;letter-spacing:-.5px}
.logo-sub{font-size:8px;color:var(--acc2);font-family:'DM Mono',monospace;letter-spacing:3px;font-weight:500;margin-top:-1px}
.status{font-family:'DM Mono',monospace;font-size:11px;font-weight:500;display:flex;align-items:center;gap:5px}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.status.ready .status-dot{background:var(--grn);box-shadow:0 0 8px var(--grn)}
.status.running .status-dot{background:var(--ylw);box-shadow:0 0 8px var(--ylw);animation:pulse 1.2s infinite}
.status.stopped .status-dot{background:var(--orn)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── Stats ── */
.stats{
  display:flex;overflow-x:auto;gap:1px;padding:0;
  background:var(--s1);border-bottom:1px solid var(--brd);
  -webkit-overflow-scrolling:touch;
}
.stats::-webkit-scrollbar{display:none}
.stat{
  flex:1 0 auto;min-width:68px;text-align:center;
  padding:10px 12px;background:var(--bg);
  position:relative;
}
.stat::after{content:'';position:absolute;right:0;top:20%;height:60%;width:1px;background:var(--brd)}
.stat:last-child::after{display:none}
.stat-label{font-size:7px;font-weight:500;letter-spacing:1.8px;color:var(--dim);font-family:'DM Mono',monospace;text-transform:uppercase}
.stat-val{font-size:22px;font-weight:700;font-family:'Anybody',sans-serif;line-height:1.3}

/* ── Tabs ── */
.tabs{display:flex;gap:0;padding:0;background:var(--s1);border-bottom:1px solid var(--brd)}
.tab{
  flex:1;padding:13px 10px;text-align:center;
  font-size:11px;font-weight:600;letter-spacing:.5px;
  color:var(--mut);cursor:pointer;
  border-bottom:2px solid transparent;
  transition:.2s;font-family:'DM Mono',monospace;
}
.tab:active{background:var(--s2)}
.tab.active{color:var(--acc2);border-bottom-color:var(--acc);background:var(--bg)}

/* ── Panels ── */
.panel{display:none;padding:14px;animation:fadeUp .25s ease}
.panel.active{display:block}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* ── Forms ── */
.field{margin-bottom:10px}
.field label{
  display:block;font-size:9px;font-weight:500;
  letter-spacing:1.8px;color:var(--mut);
  font-family:'DM Mono',monospace;margin-bottom:4px;text-transform:uppercase;
}
.field select,.field input{
  width:100%;background:var(--s1);
  border:1px solid var(--brd);border-radius:var(--radius);
  padding:11px 13px;color:var(--txt);font-size:13px;
  font-family:'DM Mono',monospace;outline:none;
  transition:border .15s;-webkit-appearance:none;
}
.field select:focus,.field input:focus{border-color:var(--acc)}
.field select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%235b6689' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}
.row{display:flex;gap:8px}
.row .field{flex:1}

.chk{display:flex;align-items:center;gap:9px;padding:8px 0;cursor:pointer}
.chk input[type="checkbox"]{
  width:18px;height:18px;accent-color:var(--acc);
  border-radius:4px;cursor:pointer;
}
.chk span{font-size:12px;font-family:'DM Mono',monospace;font-weight:500}
.chk .hl{color:var(--grn)}

/* ── Buttons ── */
.btn{
  width:100%;padding:14px;border:none;border-radius:var(--radius);
  font-size:14px;font-weight:700;cursor:pointer;
  transition:all .15s;letter-spacing:.3px;
}
.btn:active{transform:scale(.98)}
.btn-start{
  background:var(--accG);color:#fff;
  box-shadow:0 4px 20px rgba(99,91,255,0.25);
  font-family:'Anybody',sans-serif;font-size:15px;letter-spacing:.5px;
}
.btn-start:disabled{opacity:.5;cursor:wait;box-shadow:none}
.btn-stop{background:var(--red);color:#fff;margin-top:6px;font-weight:600}
.btn-stop:disabled{opacity:.25}
.btn-ghost{
  background:transparent;color:var(--mut);
  border:1px solid var(--brd);margin-top:6px;
  font-size:12px;padding:11px;font-family:'DM Mono',monospace;font-weight:500;
}
.btn-ghost:active{background:var(--s2)}
.btn-row{display:flex;gap:6px;margin-top:6px}
.btn-row .btn-ghost{flex:1;margin-top:0}

/* ── Progress ── */
.pbar-wrap{height:3px;background:var(--s2);border-radius:2px;margin:10px 0 4px;overflow:hidden}
.pbar{height:100%;background:var(--accG);border-radius:2px;transition:width .4s ease;width:0%}
.pbar-text{font-size:9px;color:var(--dim);font-family:'DM Mono',monospace}

/* ── Log ── */
.log-wrap{
  background:var(--bg);
  border:1px solid var(--brd);border-radius:var(--radiusL);
  padding:2px;margin-top:10px;overflow:hidden;
}
.log-box{
  padding:12px;height:300px;overflow-y:auto;
  font-family:'DM Mono',monospace;font-size:10.5px;line-height:1.65;
  -webkit-overflow-scrolling:touch;
}
.log-entry{padding:1px 0}
.log-header{color:var(--acc3)}.log-config{color:#7c8fff}.log-search{color:var(--cyn)}.log-found{color:var(--grn)}.log-skip{color:#2a3358}.log-info{color:#6b7ba0}.log-warn{color:var(--ylw)}.log-error{color:var(--red)}.log-success{color:var(--grn)}

/* ── Lead Cards ── */
.leads-toolbar{display:flex;gap:8px;margin-bottom:10px;align-items:center}
.leads-toolbar input{
  flex:1;background:var(--s1);border:1px solid var(--brd);border-radius:var(--radius);
  padding:10px 13px;color:var(--txt);font-size:12px;
  font-family:'DM Mono',monospace;outline:none;
}
.leads-toolbar input:focus{border-color:var(--acc)}
.dl-btn{
  padding:10px 14px;border-radius:var(--radius);
  background:var(--s1);border:1px solid var(--brd);
  color:var(--mut);font-size:11px;font-weight:600;
  font-family:'DM Mono',monospace;cursor:pointer;
  white-space:nowrap;transition:.15s;display:flex;align-items:center;gap:5px;
}
.dl-btn:active{background:var(--s2);border-color:var(--acc)}

.lead-card{
  background:var(--s1);border:1px solid var(--glassBrd);
  border-radius:var(--radiusL);padding:14px;margin-bottom:8px;
  transition:border .15s;
}
.lead-card:active{border-color:var(--brd2)}
.lead-name{font-size:15px;font-weight:700;color:#fff;font-family:'Outfit',sans-serif}
.lead-meta{font-size:11px;color:var(--mut);font-family:'DM Mono',monospace;margin-top:4px;line-height:1.5}
.lead-badges{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}
.badge{
  font-size:9px;font-weight:600;padding:3px 9px;
  border-radius:6px;font-family:'DM Mono',monospace;
  letter-spacing:.3px;
}
.badge-hot{background:linear-gradient(135deg,rgba(255,61,113,0.15),rgba(255,138,92,0.1));color:#ff6b8a;border:1px solid rgba(255,61,113,0.15)}
.badge-score{background:rgba(99,91,255,0.1);color:var(--acc2);border:1px solid rgba(99,91,255,0.12)}
.badge-noweb{background:rgba(0,240,160,0.08);color:var(--grn);border:1px solid rgba(0,240,160,0.1)}
.badge-phone{background:rgba(0,198,251,0.08);color:var(--cyn);border:1px solid rgba(0,198,251,0.1)}
.badge-star{background:rgba(255,194,68,0.08);color:var(--ylw);border:1px solid rgba(255,194,68,0.1)}
.badge-rev{background:rgba(255,255,255,0.03);color:var(--mut);border:1px solid var(--glassBrd)}

.lead-actions{display:flex;gap:6px;margin-top:10px}
.lead-btn{
  padding:8px 13px;border-radius:8px;font-size:11px;font-weight:600;
  border:1px solid var(--brd);background:var(--s2);
  color:var(--txt2);cursor:pointer;font-family:'DM Mono',monospace;
  text-decoration:none;display:inline-flex;align-items:center;gap:4px;
  transition:.15s;
}
.lead-btn:active{background:var(--s3)}
.lead-btn.accent{background:rgba(99,91,255,0.12);color:var(--acc2);border-color:rgba(99,91,255,0.2)}

/* ── Compose / Templates ── */
.tpl-selector{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px}
.tpl-btn{
  padding:7px 11px;border-radius:8px;font-size:10px;font-weight:500;
  border:1px solid var(--brd);background:var(--s1);
  color:var(--mut);cursor:pointer;font-family:'DM Mono',monospace;transition:.15s;
}
.tpl-btn.active{background:rgba(99,91,255,0.12);color:var(--acc2);border-color:rgba(99,91,255,0.25)}
.tpl-subject{background:var(--s1);border-radius:8px;padding:10px 12px;font-size:11px;color:var(--mut);font-family:'DM Mono',monospace;margin-bottom:8px;border:1px solid var(--glassBrd)}
.tpl-body{
  background:var(--bg);border:1px solid var(--brd);border-radius:var(--radius);
  padding:14px;font-family:'DM Mono',monospace;font-size:11.5px;
  color:var(--txt2);line-height:1.7;white-space:pre-wrap;
  max-height:320px;overflow-y:auto;-webkit-overflow-scrolling:touch;
}

/* ── Modal ── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(4,6,14,0.85);z-index:200;align-items:flex-end;justify-content:center;padding:0;backdrop-filter:blur(6px)}
.modal-overlay.show{display:flex}
.modal{
  background:var(--s1);border:1px solid var(--brd);
  border-radius:20px 20px 0 0;padding:24px 18px;
  width:100%;max-width:500px;max-height:88vh;
  overflow-y:auto;animation:slideUp .25s ease;
}
@keyframes slideUp{from{transform:translateY(30px);opacity:0}to{transform:translateY(0);opacity:1}}
.modal h3{font-size:17px;font-weight:800;color:#fff;margin-bottom:14px;font-family:'Outfit',sans-serif}
.modal .close-x{position:absolute;top:16px;right:18px;background:none;border:none;color:var(--mut);font-size:20px;cursor:pointer}

.empty{text-align:center;padding:50px 20px;color:var(--dim);font-family:'DM Mono',monospace;font-size:12px}
.empty-icon{font-size:36px;margin-bottom:8px;opacity:.3}
.lead-count{font-size:11px;color:var(--mut);font-family:'DM Mono',monospace;padding:4px 0 10px}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
    <div class="logo">
        <div class="logo-icon">M</div>
        <div>
            <div class="logo-text">MADASS</div>
            <div class="logo-sub">LEAD ENGINE</div>
        </div>
    </div>
    <div id="status" class="status ready"><span class="status-dot"></span> READY</div>
</div>

<!-- Stats -->
<div class="stats">
    <div class="stat"><div class="stat-label">LEADS</div><div class="stat-val" id="st-captured" style="color:var(--grn)">0</div></div>
    <div class="stat"><div class="stat-label">SCANNED</div><div class="stat-val" id="st-processed" style="color:var(--cyn)">0</div></div>
    <div class="stat"><div class="stat-label">FILTERED</div><div class="stat-val" id="st-skipped_web" style="color:var(--ylw)">0</div></div>
    <div class="stat"><div class="stat-label">DUPES</div><div class="stat-val" id="st-dupes" style="color:var(--dim)">0</div></div>
    <div class="stat"><div class="stat-label">ERR</div><div class="stat-val" id="st-errors" style="color:var(--red)">0</div></div>
</div>

<!-- Tabs -->
<div class="tabs">
    <div class="tab active" onclick="switchTab('scraper')">⚡ Scraper</div>
    <div class="tab" onclick="switchTab('leads')">📋 Leads</div>
    <div class="tab" onclick="switchTab('compose')">✉ Compose</div>
</div>

<!-- Scraper -->
<div id="panel-scraper" class="panel active">
    <div class="field"><label>BUSINESS TYPE</label><select id="niche">{% for n in niches %}<option value="{{n}}">{{n}}</option>{% endfor %}</select></div>
    <div class="field"><label>CUSTOM NICHE</label><input id="custom_niche" placeholder="Or type a custom niche..."></div>
    <div class="field"><label>TARGET CITY</label><select id="city">{% for c in cities %}<option value="{{c}}">{{c}}</option>{% endfor %}</select></div>
    <div class="field"><label>CUSTOM CITY</label><input id="custom_city" placeholder="Or type a custom city..."></div>
    <div class="field"><label>EXTRA NICHES</label><input id="extra_niches" placeholder="plumber, electrician, nail salon..."></div>
    <div class="field"><label>EXTRA CITIES</label><input id="extra_cities" placeholder="Dallas TX, Atlanta GA, Memphis TN..."></div>
    <div class="row">
        <div class="field"><label>MIN ★</label><input id="min_rating" type="number" value="3.5" step="0.5" min="0" max="5"></div>
        <div class="field"><label>MIN REVIEWS</label><input id="min_reviews" type="number" value="10" min="0"></div>
        <div class="field"><label>SCROLL</label><input id="scroll_cycles" type="number" value="10" min="3" max="30"></div>
    </div>
    <label class="chk"><input type="checkbox" id="no_web_only" checked><span class="hl">Only businesses WITHOUT a website</span></label>

    <button class="btn btn-start" id="startBtn" onclick="startScrape()">⚡ START SCRAPING</button>
    <button class="btn btn-stop" id="stopBtn" onclick="stopScrape()" disabled>■ STOP</button>

    <div class="pbar-wrap"><div class="pbar" id="pbar"></div></div>
    <div class="pbar-text" id="ptext"></div>

    <div class="log-wrap"><div class="log-box" id="logbox"></div></div>

    <button class="btn btn-ghost" onclick="exportCSV()">⬇ Download CSV</button>
</div>

<!-- Leads -->
<div id="panel-leads" class="panel">
    <div class="leads-toolbar">
        <input id="lead-search" placeholder="Search leads..." oninput="loadLeads()">
        <button class="dl-btn" onclick="exportCSV()">⬇ CSV</button>
        <button class="dl-btn" onclick="downloadTable()">⬇ Table</button>
    </div>
    <div class="lead-count" id="lead-count"></div>
    <div id="leads-list"></div>
</div>

<!-- Compose -->
<div id="panel-compose" class="panel">
    <div id="compose-lead-info" class="lead-card" style="margin-bottom:14px">
        <div style="color:var(--dim);font-size:12px;font-family:'DM Mono',monospace">Select a lead from the Leads tab to auto-fill, or compose a generic message.</div>
    </div>
    <div class="tpl-selector" id="tpl-selector"></div>
    <div class="tpl-subject" id="tpl-subject"></div>
    <div class="tpl-body" id="tpl-body"></div>
    <button class="btn btn-start copy-btn" onclick="copyMessage()" style="margin-top:10px;font-size:13px">📋 Copy Message</button>
</div>

<!-- Modal -->
<div class="modal-overlay" id="tpl-modal" onclick="if(event.target===this)closeModal()">
    <div class="modal">
        <h3 id="modal-title">Compose</h3>
        <div class="tpl-selector" id="modal-tpl-selector"></div>
        <div class="tpl-subject" id="modal-tpl-subject"></div>
        <div class="tpl-body" id="modal-tpl-body"></div>
        <button class="btn btn-start copy-btn" onclick="copyModal()" style="margin-top:10px;font-size:13px">📋 Copy Message</button>
        <button class="btn btn-ghost" onclick="closeModal()">Close</button>
    </div>
</div>

<script>
const TEMPLATES = {{ templates | tojson }};
let activeLead = null;
let activeTemplate = 'email_no_site';
let evtSource = null;
let allLeadsCache = [];

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
        niche: $('niche').value, custom_niche: $('custom_niche').value,
        city: $('city').value, custom_city: $('custom_city').value,
        extra_niches: $('extra_niches').value, extra_cities: $('extra_cities').value,
        min_rating: parseFloat($('min_rating').value)||3.5,
        min_reviews: parseInt($('min_reviews').value)||10,
        no_web_only: $('no_web_only').checked,
        scroll_cycles: parseInt($('scroll_cycles').value)||10,
    };
    fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('startBtn').disabled=true; $('startBtn').textContent='⏳ Running...';
    $('stopBtn').disabled=false;
    setStatus('running','SCRAPING');
    $('logbox').innerHTML='';
    startSSE();
}
function stopScrape() { fetch('/api/stop',{method:'POST'}); setStatus('stopped','STOPPING'); }

function setStatus(cls,text) {
    const el=$('status'); el.className='status '+cls;
    el.innerHTML=`<span class="status-dot"></span> ${text}`;
}

function startSSE() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource('/api/stream');
    evtSource.onmessage = e => appendLog(JSON.parse(e.data));
    evtSource.addEventListener('status', e => {
        const d=JSON.parse(e.data);
        for (const [k,v] of Object.entries(d.stats)) { const el=$('st-'+k); if(el) el.textContent=v; }
        if (d.progress.total>0) {
            $('pbar').style.width=(d.progress.current/d.progress.total*100)+'%';
            $('ptext').textContent=`Search ${d.progress.current}/${d.progress.total}`;
        }
        if (!d.running) {
            $('startBtn').disabled=false; $('startBtn').textContent='⚡ START SCRAPING';
            $('stopBtn').disabled=true; setStatus('ready','READY');
            if(evtSource){evtSource.close();evtSource=null;}
        }
    });
}

function appendLog(entry) {
    const box=$('logbox'), div=document.createElement('div');
    div.className='log-entry log-'+(entry.level||'info');
    div.textContent=(entry.time?entry.time+' ':'')+entry.msg;
    box.appendChild(div); box.scrollTop=box.scrollHeight;
}

// ── Leads ──
async function loadLeads() {
    const q=($('lead-search')?.value||'').toLowerCase();
    const res=await fetch('/api/leads?q='+encodeURIComponent(q));
    allLeadsCache=await res.json();
    const c=document.getElementById('leads-list');
    const ct=$('lead-count');
    if(!allLeadsCache.length){
        c.innerHTML='<div class="empty"><div class="empty-icon">📭</div>No leads yet — run a scrape!</div>';
        ct.textContent=''; return;
    }
    ct.textContent=allLeadsCache.length+' leads';
    c.innerHTML=allLeadsCache.map(l=>{
        const s=l.lead_score||0;
        const badge=s>=75?'🔥 HOT':s>=50?'✦ GOOD':'· OK';
        const bclass=s>=75?'badge-hot':'badge-score';
        return `<div class="lead-card">
            <div class="lead-name">${esc(l.name)}</div>
            <div class="lead-meta">${esc(l.niche)} · ${esc(l.city)}${l.phone?' · '+esc(l.phone):''}${l.email?' · '+esc(l.email):''}</div>
            <div class="lead-badges">
                <span class="badge ${bclass}">${badge} ${s}</span>
                ${l.has_website==='no'?'<span class="badge badge-noweb">NO WEBSITE</span>':''}
                ${l.phone?'<span class="badge badge-phone">📞 Phone</span>':''}
                ${l.rating?'<span class="badge badge-star">★ '+l.rating+'</span>':''}
                ${l.reviews?'<span class="badge badge-rev">'+l.reviews+' rev</span>':''}
                ${l.category?'<span class="badge badge-rev">'+esc(l.category)+'</span>':''}
            </div>
            <div class="lead-actions">
                <button class="lead-btn accent" onclick='selectLead(${JSON.stringify(l).replace(/'/g,"&#39;")})'>✉ Compose</button>
                ${l.phone?`<a class="lead-btn" href="tel:${esc(l.phone)}">📞 Call</a>`:''}
                ${l.maps_url?`<a class="lead-btn" href="${esc(l.maps_url)}" target="_blank" rel="noreferrer">📍 Maps</a>`:''}
            </div>
        </div>`;
    }).join('');
}

function selectLead(lead) { activeLead=lead; openTemplateModal(lead); }
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function downloadTable(){
    if(!allLeadsCache.length) return;
    const headers=['Name','Phone','Email','Address','Rating','Reviews','Niche','City','Category','Score','Website','Maps URL'];
    const keys=['name','phone','email','address','rating','reviews','niche','city','category','lead_score','has_website','maps_url'];
    let csv=headers.join(',')+'\n';
    allLeadsCache.forEach(l=>{
        csv+=keys.map(k=>`"${String(l[k]||'').replace(/"/g,'""')}"`).join(',')+'\n';
    });
    const blob=new Blob([csv],{type:'text/csv'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='madass_leads_table.csv';a.click();
}

// ── Compose ──
function renderCompose(){
    $('tpl-selector').innerHTML=Object.entries(TEMPLATES).map(([k,v])=>
        `<button class="tpl-btn ${k===activeTemplate?'active':''}" onclick="setTpl('${k}')">${v.label}</button>`
    ).join('');
    fillTpl('tpl-subject','tpl-body',activeLead);
}
function setTpl(k){
    activeTemplate=k;
    document.querySelectorAll('#tpl-selector .tpl-btn').forEach(b=>b.classList.toggle('active',b.textContent===TEMPLATES[k].label));
    fillTpl('tpl-subject','tpl-body',activeLead);
}
function fillTpl(sId,bId,lead){
    const t=TEMPLATES[activeTemplate];
    const v={name:lead?.name||'[Business]',owner:lead?.owner_name||'there',city:lead?.city||'[City]',niche:lead?.niche||'business',rating:String(lead?.rating||''),reviews:String(lead?.reviews||'')};
    if(!v.owner)v.owner='there';
    let s=t.subject,b=t.body;
    for(const[k,val]of Object.entries(v)){s=s.split('{'+k+'}').join(val);b=b.split('{'+k+'}').join(val);}
    $(sId).textContent=s?'Subject: '+s:'(DM — no subject)';
    $(bId).textContent=b;
    if(lead){$('compose-lead-info').innerHTML=`<div class="lead-name">${esc(lead.name)}</div><div class="lead-meta">${esc(lead.niche)} · ${esc(lead.city)}${lead.phone?' · '+esc(lead.phone):''}</div>`;}
}
function copyMessage(){
    const s=$('tpl-subject').textContent,b=$('tpl-body').textContent;
    const text=s.startsWith('Subject:')?s+'\n\n'+b:b;
    navigator.clipboard?.writeText(text).then(()=>{
        const btn=document.querySelector('#panel-compose .copy-btn');
        btn.textContent='✓ Copied!';btn.style.background='var(--grn)';btn.style.color='#000';
        setTimeout(()=>{btn.textContent='📋 Copy Message';btn.style.background='';btn.style.color='';},1500);
    });
}

// ── Modal ──
let modalTpl='email_no_site',modalLead=null;
function openTemplateModal(lead){
    modalLead=lead;modalTpl='email_no_site';
    $('modal-title').textContent='✉ '+lead.name;
    $('modal-tpl-selector').innerHTML=Object.entries(TEMPLATES).map(([k,v])=>
        `<button class="tpl-btn ${k===modalTpl?'active':''}" onclick="setModalTpl('${k}')">${v.label}</button>`
    ).join('');
    fillModal();$('tpl-modal').classList.add('show');
}
function setModalTpl(k){
    modalTpl=k;
    document.querySelectorAll('#modal-tpl-selector .tpl-btn').forEach(b=>b.classList.toggle('active',b.textContent===TEMPLATES[k].label));
    fillModal();
}
function fillModal(){
    const t=TEMPLATES[modalTpl],l=modalLead;
    const v={name:l?.name||'',owner:l?.owner_name||'there',city:l?.city||'',niche:l?.niche||'',rating:String(l?.rating||''),reviews:String(l?.reviews||'')};
    if(!v.owner)v.owner='there';
    let s=t.subject,b=t.body;
    for(const[k,val]of Object.entries(v)){s=s.split('{'+k+'}').join(val);b=b.split('{'+k+'}').join(val);}
    $('modal-tpl-subject').textContent=s?'Subject: '+s:'(DM)';
    $('modal-tpl-body').textContent=b;
}
function copyModal(){
    const s=$('modal-tpl-subject').textContent,b=$('modal-tpl-body').textContent;
    const text=s.startsWith('Subject:')?s+'\n\n'+b:b;
    navigator.clipboard?.writeText(text).then(()=>{
        document.querySelectorAll('#tpl-modal .copy-btn').forEach(btn=>{
            btn.textContent='✓ Copied!';btn.style.background='var(--grn)';btn.style.color='#000';
            setTimeout(()=>{btn.textContent='📋 Copy Message';btn.style.background='';btn.style.color='';},1500);
        });
    });
}
function closeModal(){$('tpl-modal').classList.remove('show')}
function exportCSV(){window.location.href='/api/leads/csv'}
function $(id){return document.getElementById(id)}

document.addEventListener('DOMContentLoaded',renderCompose);
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
