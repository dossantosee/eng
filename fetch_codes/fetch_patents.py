import os
import json
import time
import requests
from urllib.parse import quote
from datetime import datetime, timezone
from deep_translator import GoogleTranslator

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")
INVENTOR_NAME     = os.environ.get("INVENTOR_NAME")
DATA_FILE         = "fetch_data/patent_data.json"


def load_existing_data(filepath: str) -> list:
    """Load existing JSON from disk, return empty list if missing or invalid."""
    if not os.path.exists(filepath):
        print(f"  No existing file found at '{filepath}'. Starting fresh.")
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"  Loaded {len(data)} existing records from '{filepath}'.")
            return data
        print("  Existing file has unexpected format. Starting fresh.")
        return []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  Could not read existing file: {exc}. Starting fresh.")
        return []


def fetch_patents(inventor_name: str) -> list:
    """
    Call Firecrawl and return the extracted patent list.
    Raises on any network / API / empty-response error.
    """
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY environment variable is not set.")

    inventor_query = f'FP:("{inventor_name}")'
    url = (
        "https://patentscope.wipo.int/search/en/result.jsf?query="
        + quote(inventor_query, safe="():")
    )
    print(f"Scraping WIPO Patents for: {url}")

    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "url": url,
        "formats": ["extract"],
        "timeout": 180000,
        "actions": [
            {"type": "wait", "milliseconds": 5000}
        ],
        "extract": {
            "prompt": (
                "Extract the list of patents from the WIPO Patents search results "
                "considering distinct application numbers. For each patent: extract "
                "the complete 'title' in pt-BR in sentence case, grab the 4-digit "
                "'year' from the filing or publication date, get the patent or "
                "'application' number (e.g., BR102019023234), and get the 'url' link "
                "(e.g., https://patentscope.wipo.int/search/en/detail.jsf;"
                "jsessionid=8EC6B3BC6C39FD0F6875DE3571580CBC.wapp2nA?"
                "docId=BR325279117&_cid=P20-MR0UGE-89008-1)"
                
            ),
            "schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":       {"type": "string"},
                        "year":        {"type": "integer"},
                        "application": {"type": "string"},
                        "url":         {"type": "string"}
                    },
                    "required": ["title", "year", "application", "url"]
                }
            }
        }
    }

    print("Fetching data from Firecrawl...")
    response = requests.post(
        "https://api.firecrawl.dev/v1/scrape",
        headers=headers,
        json=payload,
        timeout=60
    )
    response.raise_for_status()

    extracted = response.json().get("data", {}).get("extract", [])

    if not extracted:
        raise ValueError("Firecrawl returned an empty extract payload.")

    return extracted


def is_valid(records: list) -> bool:
    """
    Reject the payload if it is empty or if every record is missing
    the required fields — which suggests a failed scrape or bot-block.
    """
    if not records:
        return False

    required = {"title", "year", "application", "url"}

    # At least one record must have all required fields populated
    return any(
        all(record.get(field) for field in required)
        for record in records
    )


def stamp_records(records: list, now_str: str) -> list:
    """Add the refresh timestamp to every record."""
    return [{**record, "refresh": now_str} for record in records]


# ── Rate limiting for the translation API ──────────────────────────────────
# Google's endpoint allows up to 5 requests/second. We throttle to one
# request every 0.4s (2.5 req/s) to stay comfortably under that ceiling
# even with clock jitter, plus retry with backoff on transient errors.
_MIN_INTERVAL = 0.4          # seconds between translate calls
_MAX_RETRIES = 4
_last_call_time = [0.0]      # mutable holder so it can be updated in-place


def _throttle():
    """Block just long enough to keep spacing >= _MIN_INTERVAL since last call."""
    now = time.monotonic()
    elapsed = now - _last_call_time[0]
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call_time[0] = time.monotonic()


def translate_title(text: str, source: str = "pt", target: str = "en") -> str:
    """
    Translate a single title, one request at a time, throttled to stay
    under Google's rate limit. Retries with exponential backoff on
    transient errors (e.g. "too many requests"). Returns an empty string
    only if every attempt fails, so a hiccup never breaks the save step.
    """
    if not text:
        return ""

    for attempt in range(1, _MAX_RETRIES + 1):
        _throttle()
        try:
            return GoogleTranslator(source=source, target=target).translate(text)
        except Exception as exc:
            is_last_attempt = attempt == _MAX_RETRIES
            if is_last_attempt:
                print(f"  ⚠️  Translation failed for '{text[:60]}...': {exc}")
                return ""
            backoff = 2 ** attempt  # 2s, 4s, 8s, 16s
            print(f"  ⚠️  Attempt {attempt}/{_MAX_RETRIES} failed "
                  f"({exc}). Retrying in {backoff}s...")
            time.sleep(backoff)

    return ""  # unreachable, kept for clarity


def add_translations(records: list) -> list:
    """
    Insert 'title_en' right after 'title' for every record, keeping
    the original key order/approach for everything else intact.
    """
    translated = []
    total = len(records)
    for i, record in enumerate(records, 1):
        print(f"  Translating title {i}/{total}...")
        title_en = translate_title(record.get("title", ""))

        new_record = {}
        for key, value in record.items():
            new_record[key] = value
            if key == "title":
                new_record["title_en"] = title_en
        # Fallback in case 'title' key was ever missing
        if "title_en" not in new_record:
            new_record["title_en"] = title_en

        translated.append(new_record)
    return translated


def print_results(records: list):
    print(f"\n  {'='*70}")
    print(f"  Found {len(records)} patent(s):")
    print(f"  {'='*70}")
    for i, r in enumerate(records, 1):
        print(f"\n  [{i:03d}] Title:       {r.get('title')}")
        print(f"        Title (EN): {r.get('title_en')}")
        print(f"        Year:        {r.get('year')}")
        print(f"        Application: {r.get('application')}")
        print(f"        URL:         {r.get('url')}")
        print(f"        Refresh:     {r.get('refresh', 'n/a')}")
    print(f"\n  {'='*70}")
    print(f"  Total: {len(records)} records")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":

    if not INVENTOR_NAME:
        print("⚠️  INVENTOR_NAME environment variable is not set. Exiting.")
        exit(0)

    # Always load what is already on disk first
    existing_data = load_existing_data(DATA_FILE)

    # Attempt the fetch — catch every possible error gracefully
    try:
        raw_records = fetch_patents(INVENTOR_NAME)
    except Exception as exc:
        print(f"\n⚠️  Fetch failed: {exc}")
        print("  Keeping existing data unchanged. No file will be written.")
        exit(0)

    # Validate the parsed payload before touching the file
    if not is_valid(raw_records):
        print("\n⚠️  Extracted data looks invalid (possible bot-block or empty page):")
        print(f"  {raw_records}")
        print("  Keeping existing data unchanged. No file will be written.")
        exit(0)

    # Stamp with current UTC time
    now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    patent_data  = stamp_records(raw_records, now_str)

    # Translate titles pt-BR -> en, adding 'title_en' to each record
    print("\nTranslating titles to English...")
    patent_data = add_translations(patent_data)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(patent_data, f, indent=2, ensure_ascii=False)

    print_results(patent_data)
    print(f"\n✅  Successfully saved {len(patent_data)} patents to '{DATA_FILE}'.")
