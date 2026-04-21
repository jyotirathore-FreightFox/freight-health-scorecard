"""
FreightFox Freight Health Scorecard — Backend
FastAPI server for company lookup (Screener.in / Tofler.in Pro) and assessment submission.

Tofler Pro lookup uses Playwright (headless browser) to:
  1. Search tofler.in/finder for the company
  2. Log in with Pro credentials (from ../.env)
  3. Scrape exact revenue, DSO, DPO, ITO from the company page
"""

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Tofler Pro credentials from parent .env
# ---------------------------------------------------------------------------
ENV_PATH = Path(__file__).parent.parent / ".env"
env = dotenv_values(str(ENV_PATH))
TOFLER_EMAIL = env.get("TOFLER_EMAIL", "")
TOFLER_PASSWORD = env.get("TOFLER_PASSWORD", "")

# ---------------------------------------------------------------------------
# Playwright browser (lazy-init, reused across requests)
# ---------------------------------------------------------------------------
_browser = None
_tofler_page = None
_tofler_logged_in = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Cleanup on shutdown
    global _browser
    if _browser:
        await _browser.close()
        _browser = None


app = FastAPI(title="FreightFox Scorecard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Serve static assets (logo, images, etc.) from ./images/
IMAGES_DIR = Path(__file__).parent / "images"
if IMAGES_DIR.exists():
    app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
CACHE_FILE = Path(__file__).parent / "company_cache.json"
company_cache: dict = {}

if CACHE_FILE.exists():
    try:
        company_cache = json.loads(CACHE_FILE.read_text())
    except Exception:
        company_cache = {}

SECTOR_DISQUALIFIERS = {
    "services", "it", "software", "government", "healthcare",
    "hospitals", "ngo", "banking", "financial services",
    "information technology", "finance", "insurance",
}


def _save_cache():
    CACHE_FILE.write_text(json.dumps(company_cache, indent=2, default=str))


def _parse_number(text: str) -> Optional[float]:
    """Parse Indian number formats: '4,500', '4500.23', '₹ 4,500 Cr' etc."""
    if not text:
        return None
    text = text.replace(",", "").replace("₹", "").strip()
    m = re.search(r"([\d.]+)", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Screener.in lookup
# ---------------------------------------------------------------------------
SCREENER_SEARCH = "https://www.screener.in/api/company/search/?q={q}"
SCREENER_BASE = "https://www.screener.in"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _try_screener(name: str) -> Optional[dict]:
    """Search Screener.in for company data."""
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)

        # Search
        resp = sess.get(SCREENER_SEARCH.format(q=name), timeout=10)
        if resp.status_code != 200:
            return None

        results = resp.json()
        if not results:
            return None

        # Pick first result
        first = results[0]
        company_url = first.get("url", "")
        company_name = first.get("name", name)

        if not company_url:
            return None

        # Fetch company page
        page_url = SCREENER_BASE + company_url
        page = sess.get(page_url, timeout=15)
        if page.status_code != 200:
            return None

        soup = BeautifulSoup(page.text, "html.parser")

        # Parse key ratios from the "ratios" or "top-ratios" section
        data = {
            "found": True,
            "source": "screener",
            "company_name": company_name,
            "revenue_cr": None,
            "dso_days": None,
            "dpo_days": None,
            "ito_ratio": None,
            "sector": None,
            "is_listed": True,
        }

        # Try to get sector from company info
        sector_el = soup.find("a", class_="button-primary")
        if sector_el:
            data["sector"] = sector_el.get_text(strip=True)

        # Look for sector in the page text
        company_info = soup.find("div", class_="company-info")
        if company_info:
            info_text = company_info.get_text(" ", strip=True)
            data["sector"] = info_text.split("|")[0].strip() if "|" in info_text else None

        # Parse top ratios list
        ratio_list = soup.find("ul", id="top-ratios")
        if ratio_list:
            items = ratio_list.find_all("li")
            for item in items:
                label_el = item.find("span", class_="name")
                value_el = item.find("span", class_="number")
                if not label_el or not value_el:
                    continue
                label = label_el.get_text(strip=True).lower()
                value = _parse_number(value_el.get_text(strip=True))

                if "revenue" in label or "sales" in label:
                    data["revenue_cr"] = value
                elif "debtor" in label or "receivable" in label:
                    data["dso_days"] = value
                elif "inventory turnover" in label:
                    data["ito_ratio"] = value

        # Parse quarterly/annual results table for revenue if not found
        if data["revenue_cr"] is None:
            # Try the profit-loss section
            pl_section = soup.find("section", id="profit-loss")
            if pl_section:
                table = pl_section.find("table")
                if table:
                    rows = table.find_all("tr")
                    for row in rows:
                        cells = row.find_all("td")
                        header = row.find("td", class_="text")
                        if header and "sales" in header.get_text(strip=True).lower():
                            # Last cell is TTM or latest
                            if cells:
                                last_val = cells[-1].get_text(strip=True)
                                data["revenue_cr"] = _parse_number(last_val)
                                break

        # Parse balance sheet for DSO/DPO if not found from ratios
        # Try "Ratios" section
        ratios_section = soup.find("section", id="ratios")
        if ratios_section:
            table = ratios_section.find("table")
            if table:
                rows = table.find_all("tr")
                for row in rows:
                    header = row.find("td", class_="text")
                    if not header:
                        continue
                    h = header.get_text(strip=True).lower()
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue
                    last_val = _parse_number(cells[-1].get_text(strip=True))
                    if "debtor" in h or "receivable days" in h:
                        data["dso_days"] = last_val
                    elif "payable" in h or "creditor" in h:
                        data["dpo_days"] = last_val
                    elif "inventory turnover" in h:
                        data["ito_ratio"] = last_val

        return data

    except Exception as e:
        print(f"[screener] Error for '{name}': {e}")
        return None


# ---------------------------------------------------------------------------
# Tofler.in Pro lookup (Playwright-based, reuses your existing approach)
# ---------------------------------------------------------------------------

async def _ensure_tofler_browser():
    """Lazy-init Playwright browser and log into Tofler Pro."""
    global _browser, _tofler_page, _tofler_logged_in

    if _browser and _tofler_page and _tofler_logged_in:
        return _tofler_page

    from playwright.async_api import async_playwright

    if not _browser:
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
        print("[tofler] Browser launched")

    if not _tofler_page or _tofler_page.is_closed():
        _tofler_page = await _browser.new_page()

    if not _tofler_logged_in and TOFLER_EMAIL and TOFLER_PASSWORD:
        print(f"[tofler] Logging in as {TOFLER_EMAIL}...")
        await _tofler_page.goto("https://www.tofler.in/login", wait_until="domcontentloaded", timeout=30000)
        await _tofler_page.wait_for_timeout(2000)

        # Fill login form
        email_input = await _tofler_page.query_selector(
            'input[type="email"], input[name="email"], #email, '
            'input[placeholder*="mail"], input[placeholder*="Email"]'
        )
        if not email_input:
            inputs = await _tofler_page.query_selector_all('input[type="text"], input:not([type])')
            email_input = inputs[0] if inputs else None

        password_input = await _tofler_page.query_selector(
            'input[type="password"], input[name="password"], #password'
        )

        if email_input and password_input:
            await email_input.fill(TOFLER_EMAIL)
            await password_input.fill(TOFLER_PASSWORD)

            login_btn = await _tofler_page.query_selector(
                'button[type="submit"], input[type="submit"], '
                'button:has-text("Login"), button:has-text("Sign in"), button:has-text("Log in")'
            )
            if login_btn:
                await login_btn.click()
            else:
                await _tofler_page.keyboard.press("Enter")

            await _tofler_page.wait_for_timeout(3000)
            body = await _tofler_page.inner_text("body")
            if any(kw in body.lower() for kw in ["logout", "my account", "dashboard", "pro"]):
                print("[tofler] Logged in successfully")
                _tofler_logged_in = True
            else:
                print("[tofler] Login status uncertain — proceeding anyway")
                _tofler_logged_in = True
        else:
            print("[tofler] Could not find login form fields")

    # Navigate to finder for search
    await _tofler_page.goto("https://www.tofler.in/finder", wait_until="domcontentloaded", timeout=30000)
    await _tofler_page.wait_for_timeout(1000)

    return _tofler_page


async def _tofler_search(page, company_name: str) -> Optional[dict]:
    """Search tofler.in/finder for a company, return best match."""
    responses = []

    async def capture(response):
        if "cnamesearch" in response.url:
            try:
                body = await response.json()
                responses.append(body)
            except Exception:
                pass

    page.on("response", capture)

    search_box = await page.query_selector("#searchbox")
    if not search_box:
        page.remove_listener("response", capture)
        return None

    await search_box.click()
    await search_box.fill("")
    await search_box.type(company_name, delay=30)
    await page.wait_for_timeout(2500)

    page.remove_listener("response", capture)

    if not responses:
        return None

    results = responses[-1]
    if not results or not isinstance(results, list):
        return None

    name_upper = company_name.upper()
    # Prefer close name match
    for r in results:
        if r.get("subtype") != "companyinfo":
            continue
        label = r.get("label", "").upper()
        if name_upper in label or label in name_upper:
            return {"label": r["label"], "cin": r["value"], "url": r["url"]}

    # Fallback: first company result
    for r in results:
        if r.get("subtype") == "companyinfo":
            return {"label": r["label"], "cin": r["value"], "url": r["url"]}

    return None


async def _tofler_scrape_company(page, tofler_url: str) -> Optional[dict]:
    """Visit a Tofler company page and extract revenue + financial ratios."""
    try:
        full_url = tofler_url if tofler_url.startswith("http") else f"https://www.tofler.in{tofler_url}"
        work_page = await _browser.new_page()
        await work_page.goto(full_url, wait_until="domcontentloaded", timeout=25000)
        await work_page.wait_for_timeout(3000)

        body = await work_page.inner_text("body")

        data = {
            "found": True,
            "source": "tofler",
            "company_name": None,
            "revenue_cr": None,
            "dso_days": None,
            "dpo_days": None,
            "ito_ratio": None,
            "sector": None,
            "is_listed": False,
        }

        # Financial year
        fy_match = re.search(r'Based on (\w+ \d{4}) numbers', body)
        if not fy_match:
            all_fy = re.findall(r'Mar(?:ch)?\s*(\d{4})', body)
            data["_fy"] = f"Mar {max(all_fy)}" if all_fy else None
        else:
            data["_fy"] = fy_match.group(1)

        # Extract revenue (Pro exact value)
        exact_data = await work_page.evaluate("""
            () => {
                const body = document.body.innerText;
                const m = body.match(/Total Revenue\\n[\\d.%+-]+\\n₹\\s*([\\d,]+\\.?\\d*)/);
                if (m && !m[1].includes('GET')) return { value: m[1], source: 'total_revenue' };

                const s = body.match(/Sales \\+\\t([\\d,\\t.]+)/);
                if (s) {
                    const v = s[1].split('\\t').filter(v => v.trim());
                    if (v.length) return { value: v[v.length-1], source: 'sales_pl' };
                }

                const r = body.match(/Revenue from Operations\\n[\\d.%+-]*\\n?₹\\s*([\\d,]+\\.?\\d*)/);
                if (r && !r[1].includes('GET')) return { value: r[1], source: 'revenue_ops' };

                const lines = body.split('\\n');
                for (let i = 0; i < lines.length; i++) {
                    if (/revenue|sales/i.test(lines[i])) {
                        for (let j = i+1; j < Math.min(i+5, lines.length); j++) {
                            const vm = lines[j].match(/₹\\s*([\\d,]+\\.?\\d*)\\s*(?:Cr|Lakh|crore)?/i);
                            if (vm && !vm[1].includes('GET') && parseFloat(vm[1].replace(/,/g,'')) > 0) {
                                return { value: vm[1], source: 'nearby_revenue' };
                            }
                        }
                    }
                }
                return null;
            }
        """)

        if exact_data:
            val = exact_data["value"].replace(",", "").strip()
            try:
                data["revenue_cr"] = float(val)
            except ValueError:
                pass

        # Fallback: revenue bucket
        if data["revenue_cr"] is None:
            rev_match = re.search(r'Revenue\s*\n\s*₹\s*([^\n]+)', body)
            if rev_match:
                bucket = rev_match.group(1).strip()
                if "GET" not in bucket.upper():
                    m_range = re.search(r'([\d.]+)\s*-\s*([\d.]+)', bucket)
                    m_gt = re.search(r'>\s*([\d.]+)', bucket)
                    if m_range:
                        data["revenue_cr"] = (float(m_range.group(1)) + float(m_range.group(2))) / 2
                    elif m_gt:
                        data["revenue_cr"] = float(m_gt.group(1))

        # Extract DPO, Days Inventory, CCC, ITO from the Efficiency section
        # Tofler format: "Days Payable\t87.0\t85.0\t63.0\t80.0\t78.0" (last = latest FY)
        ratio_data = await work_page.evaluate("""
            () => {
                const body = document.body.innerText;
                const lines = body.split('\\n');
                const result = {};

                const getLastNum = (line) => {
                    const parts = line.split('\\t').filter(v => v.trim());
                    // Walk backwards to find last numeric value
                    for (let i = parts.length - 1; i >= 1; i--) {
                        const v = parseFloat(parts[i].replace(/,/g, ''));
                        if (!isNaN(v)) return v;
                    }
                    return null;
                };

                for (const line of lines) {
                    const l = line.trim();
                    if (/^Days\\s*Payable\\t/i.test(l)) result.dpo = getLastNum(l);
                    if (/^Days\\s*Inventory\\t/i.test(l)) result.days_inventory = getLastNum(l);
                    if (/^Cash\\s*Conversion\\s*Cycle\\t/i.test(l)) result.ccc = getLastNum(l);
                    if (/^Inventory\\s*Turnover\\t/i.test(l)) result.ito = getLastNum(l);
                    // Some pages show Days Receivable directly
                    if (/^Days\\s*Receivable\\t/i.test(l) || /^Debtor\\s*Days\\t/i.test(l)) result.dso = getLastNum(l);
                }

                // Derive DSO from CCC if not directly available
                // CCC = DSO + Days Inventory - DPO => DSO = CCC - Days Inventory + DPO
                if (!result.dso && result.ccc != null && result.days_inventory != null && result.dpo != null) {
                    result.dso = Math.round((result.ccc - result.days_inventory + result.dpo) * 10) / 10;
                }

                // Derive ITO from Days Inventory if not available: ITO ≈ 365 / Days Inventory
                if (!result.ito && result.days_inventory && result.days_inventory > 0) {
                    result.ito = Math.round((365 / result.days_inventory) * 10) / 10;
                }

                return result;
            }
        """)

        if ratio_data:
            data["dso_days"] = ratio_data.get("dso")
            data["dpo_days"] = ratio_data.get("dpo")
            data["ito_ratio"] = ratio_data.get("ito")

        # Sector
        sector_match = re.search(r'(?:Industry|Sector|Classification)\s*[:\n]\s*([^\n]+)', body)
        if sector_match:
            data["sector"] = sector_match.group(1).strip()

        await work_page.close()
        return data

    except Exception as e:
        print(f"[tofler] Scrape error: {e}")
        try:
            await work_page.close()
        except Exception:
            pass
        return None


async def _try_tofler(name: str) -> Optional[dict]:
    """Search and scrape company data from Tofler.in using Pro login."""
    if not TOFLER_EMAIL or not TOFLER_PASSWORD:
        print("[tofler] No credentials configured — skipping")
        return None

    try:
        page = await _ensure_tofler_browser()
        match = await _tofler_search(page, name)

        if not match:
            print(f"[tofler] No match for '{name}'")
            return None

        print(f"[tofler] Found: {match['label']} (CIN: {match['cin']})")

        data = await _tofler_scrape_company(page, match["url"])
        if data:
            data["company_name"] = match["label"]
            data["is_listed"] = match["cin"].startswith("L")

        # Reset finder page for next search
        await page.goto("https://www.tofler.in/finder", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(500)

        return data

    except Exception as e:
        print(f"[tofler] Error for '{name}': {e}")
        return None


# ---------------------------------------------------------------------------
# ICP check
# ---------------------------------------------------------------------------
def _compute_icp(data: dict) -> bool:
    """Check if company matches ICP based on revenue and sector."""
    sector = (data.get("sector") or "").lower()
    for disq in SECTOR_DISQUALIFIERS:
        if disq in sector:
            return False

    revenue = data.get("revenue_cr")
    if revenue is not None and revenue >= 1000:
        return True
    return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/company")
async def lookup_company(name: str = Query(..., min_length=1)):
    """Look up company financial data from Screener.in or Tofler.in Pro."""
    cache_key = name.strip().lower()

    if cache_key in company_cache:
        return company_cache[cache_key]

    # Try Screener first (listed companies)
    result = _try_screener(name.strip())

    # Fallback to Tofler Pro (unlisted companies)
    if result is None or not result.get("found"):
        result = await _try_tofler(name.strip())

    # Not found anywhere
    if result is None or not result.get("found"):
        result = {
            "found": False,
            "source": None,
            "company_name": name.strip(),
            "revenue_cr": None,
            "dso_days": None,
            "dpo_days": None,
            "ito_ratio": None,
            "sector": None,
            "is_listed": False,
            "icp_match": None,
        }
    else:
        result["icp_match"] = _compute_icp(result)

    # Cache
    company_cache[cache_key] = result
    _save_cache()

    return result


# ---------------------------------------------------------------------------
# Lightweight /company-snapshot endpoint (for Company Snapshot card)
# Returns only the 4 fields needed by the frontend card, with mock fallback.
# ---------------------------------------------------------------------------
_MOCK_SNAPSHOTS = {
    "tata steel":      {"revenue_cr": 243353, "dso_days": 28, "dpo_days": 95, "ito_ratio": 4.8},
    "reliance":        {"revenue_cr": 964693, "dso_days": 18, "dpo_days": 88, "ito_ratio": 9.2},
    "hul":             {"revenue_cr":  62707, "dso_days": 14, "dpo_days": 76, "ito_ratio": 13.1},
    "thermax":         {"revenue_cr":   9323, "dso_days": 92, "dpo_days": 84, "ito_ratio": 5.6},
    "atul":            {"revenue_cr":   4908, "dso_days": 71, "dpo_days": 62, "ito_ratio": 4.3},
    "infosys":         {"revenue_cr": 153670, "dso_days": 71, "dpo_days": 20, "ito_ratio": None},
    "mahindra":        {"revenue_cr": 138279, "dso_days": 32, "dpo_days": 68, "ito_ratio": 11.5},
    "asian paints":    {"revenue_cr":  35495, "dso_days": 28, "dpo_days": 54, "ito_ratio": 5.1},
}


def _mock_snapshot(name: str) -> dict:
    """Return mock data for a company (used when upstream lookup fails/is unavailable)."""
    key = name.strip().lower()
    # Exact-ish match against mock dict
    for k, v in _MOCK_SNAPSHOTS.items():
        if k in key or key in k:
            return {"company_name": name.strip(), **v, "source": "mock"}
    # Generic fallback — deterministic-looking numbers derived from name length
    seed = sum(ord(c) for c in key) or 1
    return {
        "company_name": name.strip(),
        "revenue_cr":  round(1500 + (seed * 37) % 8000, 0),
        "dso_days":    30 + (seed % 60),
        "dpo_days":    40 + ((seed * 3) % 60),
        "ito_ratio":   round(3 + (seed % 90) / 10, 1),
        "source": "mock",
    }


@app.get("/company-snapshot")
async def company_snapshot(name: str = Query(..., min_length=1)):
    """
    Lightweight endpoint for the Company Snapshot card on the intro form.
    Returns { company_name, revenue_cr, dso_days, dpo_days, ito_ratio }.
    Falls back to mock data if live scraping fails.
    """
    cache_key = name.strip().lower()

    # Reuse main cache if present
    if cache_key in company_cache:
        c = company_cache[cache_key]
        if c.get("found"):
            return {
                "company_name": c.get("company_name", name),
                "revenue_cr":   c.get("revenue_cr"),
                "dso_days":     c.get("dso_days"),
                "dpo_days":     c.get("dpo_days"),
                "ito_ratio":    c.get("ito_ratio"),
                "source":       c.get("source", "cache"),
            }

    # Try live lookup (Screener → Tofler)
    try:
        result = _try_screener(name.strip())
        if result is None or not result.get("found"):
            result = await _try_tofler(name.strip())
    except Exception as e:
        print(f"[company-snapshot] live lookup failed: {e}")
        result = None

    if result and result.get("found"):
        return {
            "company_name": result.get("company_name", name),
            "revenue_cr":   result.get("revenue_cr"),
            "dso_days":     result.get("dso_days"),
            "dpo_days":     result.get("dpo_days"),
            "ito_ratio":    result.get("ito_ratio"),
            "source":       result.get("source", "live"),
        }

    # Mock fallback
    return _mock_snapshot(name)


class AssessmentPayload(BaseModel):
    full_name: str
    designation: str
    company_name: str
    contact_number: Optional[str] = None
    total_score: float
    zone: str
    icp_match: Optional[bool] = None
    dimension_scores: dict
    weakest_dimension: str
    key_recommendation: str
    company_data: Optional[dict] = None
    answers: Optional[dict] = None
    layer_scores: Optional[dict] = None


SUBMISSIONS_FILE = Path(__file__).parent / "submissions.json"


@app.post("/api/submit")
def submit_assessment(payload: AssessmentPayload):
    """Save assessment result locally as backup."""
    entry = payload.dict()
    entry["submitted_at"] = datetime.now().isoformat()

    # Append to local file
    submissions = []
    if SUBMISSIONS_FILE.exists():
        try:
            submissions = json.loads(SUBMISSIONS_FILE.read_text())
        except Exception:
            submissions = []

    submissions.append(entry)
    SUBMISSIONS_FILE.write_text(json.dumps(submissions, indent=2, default=str))

    return {"status": "saved", "count": len(submissions)}


# Serve frontend
@app.get("/")
def serve_frontend():
    html_path = Path(__file__).parent / "index.html"
    return FileResponse(html_path, media_type="text/html")
