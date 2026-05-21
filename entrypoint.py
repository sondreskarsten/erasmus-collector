"""Erasmus+ collector using Playwright for JS-rendered pages.

Two collection targets:

1. Project lists page (erasmus-plus.ec.europa.eu/projects/projects-lists)
   — JS-rendered table with per-call-year XLSX download links.
   Playwright renders the page, extracts all download URLs, fetches each.

2. Search results page with country=NO filter
   — Paginated project search. Extracts project cards with metadata.
   Stores as JSONL.

Both are stored as immutable versioned snapshots on GCS.
"""

import os
import sys
import json
import hashlib
import re
import requests
from datetime import date, datetime, timezone
from google.cloud import storage as gcs_lib
from playwright.sync_api import sync_playwright

GCS_BUCKET = os.environ.get("GCS_BUCKET", "sondre_brreg_data")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "erasmus")
RUN_MODE = os.environ.get("RUN_MODE", "daily")
SNAPSHOT_DATE = os.environ.get("SNAPSHOT_DATE", date.today().isoformat())
MAX_SEARCH_PAGES = int(os.environ.get("MAX_SEARCH_PAGES", "50"))


def fetch_download_links(page):
    """Navigate to project-lists page, extract XLSX/CSV download links."""
    print("  Fetching project-lists page...", flush=True)
    page.goto("https://erasmus-plus.ec.europa.eu/projects/projects-lists",
              wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(5000)

    links = page.evaluate("""() => {
        const anchors = document.querySelectorAll('a[href]');
        const results = [];
        for (const a of anchors) {
            const href = a.href;
            const text = a.textContent.trim();
            if (href.match(/\\.(xlsx|xls|csv|zip)$/i) ||
                (href.includes('sites/default/files') && !href.includes('.css'))) {
                results.push({href: href, text: text.substring(0, 200)});
            }
        }
        return results;
    }""")

    body_text = page.evaluate("() => document.body.innerText")
    return links, body_text


def fetch_search_page(page, page_num):
    """Fetch one page of search results filtered by country=NO."""
    url = f"https://erasmus-plus.ec.europa.eu/projects/search/results?isAdvancedSearch=false&countryId%5B%5D=NO&page={page_num}"
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    data = page.evaluate("""() => {
        const cards = document.querySelectorAll('.ecl-content-block, [class*="result"], [class*="card"], .views-row');
        const results = [];
        for (const card of cards) {
            const links = card.querySelectorAll('a[href]');
            const text = card.innerText.trim();
            const cardLinks = Array.from(links).map(a => ({href: a.href, text: a.textContent.trim()}));
            if (text.length > 10) {
                results.push({text: text.substring(0, 500), links: cardLinks});
            }
        }
        
        // Check for pagination info
        const paginationText = document.querySelector('[class*="pager"], [class*="pagination"]');
        const totalText = document.body.innerText.match(/(\\d[\\d,]+)\\s*results?/i);
        
        return {
            cards: results,
            totalResults: totalText ? totalText[1] : null,
            pageText: paginationText ? paginationText.innerText.trim() : null,
            bodySnippet: document.body.innerText.substring(0, 2000)
        };
    }""")
    return data


def main():
    snapshot = SNAPSHOT_DATE if RUN_MODE == "backfill" else date.today().isoformat()

    print(f"{'='*60}", flush=True)
    print(f"  erasmus-collector — mode: {RUN_MODE}", flush=True)
    print(f"  {date.today().isoformat()}", flush=True)
    print(f"  GCS: gs://{GCS_BUCKET}/{GCS_PREFIX}/", flush=True)
    print(f"  Snapshot: {snapshot}", flush=True)
    print(f"{'='*60}", flush=True)

    gcs_client = gcs_lib.Client()
    bucket = gcs_client.bucket(GCS_BUCKET)

    manifest_path = f"{GCS_PREFIX}/raw/{snapshot}/manifest.json"
    if bucket.blob(manifest_path).exists():
        print("  Manifest exists, skipping", flush=True)
        return

    fetched_at = datetime.now(timezone.utc).isoformat()
    manifest = {"snapshot_date": snapshot, "fetched_at_utc": fetched_at, "downloads": [], "search": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Registrum/1.0",
        })

        # Phase 1: Download links
        print("\n  Phase 1: Project lists page", flush=True)
        links, body_text = fetch_download_links(page)
        print(f"  Found {len(links)} download links", flush=True)

        # Save the page text
        blob = bucket.blob(f"{GCS_PREFIX}/raw/{snapshot}/project_lists_page.txt")
        blob.upload_from_string(body_text, content_type="text/plain")

        # Download each linked file
        for link in links:
            href = link["href"]
            filename = href.split("/")[-1].split("?")[0]
            if not filename:
                continue
            print(f"  Downloading: {filename[:60]}...", flush=True)
            r = requests.get(href, timeout=120)
            if r.status_code == 200:
                path = f"{GCS_PREFIX}/raw/{snapshot}/downloads/{filename}"
                blob = bucket.blob(path)
                blob.upload_from_string(r.content)
                manifest["downloads"].append({
                    "filename": filename,
                    "url": href,
                    "text": link["text"],
                    "size_bytes": len(r.content),
                    "gcs_path": path,
                    "sha256": hashlib.sha256(r.content).hexdigest(),
                })
                print(f"    → {len(r.content):,} bytes", flush=True)
            else:
                print(f"    → HTTP {r.status_code}", flush=True)
                manifest["downloads"].append({"filename": filename, "url": href, "status": f"http_{r.status_code}"})

        # Phase 2: Search results for Norway
        print(f"\n  Phase 2: Search results (country=NO)", flush=True)
        all_cards = []
        for pg in range(1, MAX_SEARCH_PAGES + 1):
            data = fetch_search_page(page, pg)
            cards = data.get("cards", [])
            total = data.get("totalResults")

            if pg == 1:
                print(f"  Total results: {total}", flush=True)
                manifest["search"]["total_results"] = total
                manifest["search"]["body_snippet"] = data.get("bodySnippet", "")[:500]

            all_cards.extend(cards)
            print(f"  Page {pg}: {len(cards)} cards (cumulative: {len(all_cards)})", flush=True)

            if len(cards) == 0:
                break

        # Save search results
        if all_cards:
            jsonl = "\n".join(json.dumps(c, ensure_ascii=False) for c in all_cards)
            path = f"{GCS_PREFIX}/raw/{snapshot}/search_results_NO.jsonl"
            bucket.blob(path).upload_from_string(jsonl, content_type="application/jsonl")
            manifest["search"]["cards_saved"] = len(all_cards)
            manifest["search"]["gcs_path"] = path
            print(f"  Saved {len(all_cards)} search result cards", flush=True)

        browser.close()

    # Write manifest
    bucket.blob(manifest_path).upload_from_string(
        json.dumps(manifest, indent=2, ensure_ascii=False), content_type="application/json")
    print(f"\n  Manifest written: {len(manifest['downloads'])} downloads, {manifest['search'].get('cards_saved', 0)} search cards", flush=True)


if __name__ == "__main__":
    main()
