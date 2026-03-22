# MADASS Lead Engine — Web Edition

Mobile-friendly Google Maps lead scraper. Run from your phone browser.

## Quick Start (Local — access from phone on same WiFi)

```bash
pip install flask playwright
playwright install chromium
python madass_web.py
```

Open the phone URL printed in the terminal on your mobile browser.

## Deploy to Render

1. Push all files to a GitHub repo
2. Render → New → Web Service → connect repo
3. Runtime: **Docker**
4. Plan: **Free**
5. Deploy

## Deploy to Railway

1. Push all files to a GitHub repo
2. Railway → New Project → Deploy from GitHub repo
3. It auto-detects the Dockerfile
4. Add env var: `PORT=5000`
5. Deploy

## Deploy to Fly.io

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# From the project directory
fly launch    # follow prompts
fly deploy
```

## Files

- `madass_web.py` — Main app (Flask + Playwright)
- `Dockerfile` — Container build config
- `requirements.txt` — Python dependencies
- `render.yaml` — Render deploy config
- `Procfile` — Railway deploy config
- `fly.toml` — Fly.io deploy config

## Notes

- Leads save to `~/MADASS_Leads/` locally or `/app/data/` in Docker
- Web version runs headless (no visible browser)
- Scroll depth of 8-12 works best for free tier servers
- Desktop version (`madass_engine_v35.py`) is better for heavy batch scrapes
