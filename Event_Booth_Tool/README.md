# FreightFox Freight Health Scorecard

Booth tool for conferences — a tablet-optimised scorecard that benchmarks logistics maturity across 5 dimensions.

## Setup

```bash
cd scorecard
chmod +x start.sh
./start.sh
```

This will:
1. Create a Python virtual environment
2. Install dependencies (FastAPI, uvicorn, requests, beautifulsoup4, httpx)
3. Start the server at `http://localhost:8000`
4. Open the scorecard in your default browser

## Manual Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` on your tablet browser.

## Booth Day Checklist

- [ ] Charge tablet, connect to hotspot/WiFi
- [ ] Run `./start.sh` on the laptop serving the backend
- [ ] Open `http://<laptop-ip>:8000` on the tablet browser
- [ ] Test with a known company name (e.g. "Tata Steel") to verify Screener lookup works
- [ ] Verify Jotform submissions are arriving (check Jotform dashboard)

## Jotform Integration

Submissions go to form ID `261021112289042`. The Jotform field IDs in `index.html` (submission[3], submission[4], etc.) need to be mapped to your actual form fields. To find your field IDs:

1. Open your Jotform form in edit mode
2. Click any field → Settings → look at the field name (e.g. `q3_fullName`)
3. The number after `q` is the field ID

## Architecture

```
scorecard/
├── main.py          # FastAPI backend (company lookup + submission storage)
├── index.html       # Single-file frontend (all screens, CSS, JS)
├── start.sh         # Launch script
├── requirements.txt # Python dependencies
└── README.md        # This file

Runtime files (auto-created):
├── company_cache.json   # Cached company lookups
└── submissions.json     # Local backup of all assessments
```

## Scoring

**Three weighted layers:**
- Layer 1 — ICP Match (40%): Based on company revenue + sector from Screener.in/Tofler.in
- Layer 2 — Financial Health (35%): DSO, DPO, ITO ratios
- Layer 3 — Self-Assessment (25%): 13 questions across 5 dimensions

**Zones:** Reactive (0–40) → Controlled (41–60) → Optimised (61–80) → Algorithmic-Ready (81–100)

## Company Snapshot Card (new)

When the user types a company name on the intro form, a **Company Snapshot** card appears below the input showing 4 metrics in equal columns:

| Revenue (Cr) | DSO | DPO | ITO |

**Behaviour:**
- Hidden by default; appears with a smooth fade-in once data arrives
- 300 ms debounce on input (minimum 3 characters)
- Shimmer loading placeholder while fetching
- Card hides again when the input is cleared
- Stale-response guard (rapid typing won't flicker old results)

**API endpoint (new):**
```
GET /company-snapshot?name={company}
→ { company_name, revenue_cr, dso_days, dpo_days, ito_ratio, source }
```

The endpoint tries the existing Screener.in / Tofler.in Pro lookup first, then falls back to a curated mock dataset (Tata Steel, Reliance, HUL, Thermax, Atul, Infosys, Mahindra, Asian Paints) or a deterministic pseudo-random mock for any other name — so the card always renders something, even with no internet or credentials configured.

**Quick test:**
```bash
curl "http://localhost:8000/company-snapshot?name=Tata%20Steel"
```
