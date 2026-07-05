import os
import re
import json
import requests
from datetime import datetime, timezone

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")
AUTHOR_ID = os.environ.get("SCOPUS_AUTHOR_ID")
TARGET_URL = f"https://www.scopus.com/authid/detail.uri?authorId={AUTHOR_ID}&display=hIndex"
DATA_FILE = "fetch_data/scopus_data.json"


def load_existing_data(filepath: str) -> dict:
    """Load existing JSON from disk, return empty dict if missing or invalid."""
    if not os.path.exists(filepath):
        print(f"  No existing file found at '{filepath}'. Starting fresh.")
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            print(f"  Loaded existing data from '{filepath}'.")
            return data
        print("  Existing file has unexpected format. Starting fresh.")
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  Could not read existing file: {exc}. Starting fresh.")
        return {}


def fetch_markdown() -> str:
    """Call Firecrawl and return the markdown string, or raise on failure."""
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY environment variable is not set.")

    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "url": TARGET_URL,
        "formats": ["markdown"]
    }

    print("Fetching Scopus data via Firecrawl...")
    response = requests.post(
        "https://api.firecrawl.dev/v1/scrape",
        headers=headers,
        json=payload,
        timeout=30
    )
    response.raise_for_status()

    markdown = response.json().get("data", {}).get("markdown", "")
    if not markdown:
        raise ValueError("Firecrawl returned an empty markdown body.")

    return markdown


def parse_markdown(markdown: str) -> dict:
    """Extract Scopus fields from markdown via regex."""
    author_match      = re.search(r'#\s+([^\n]+)', markdown)
    citations_match   = re.search(r'-\s+(\d+)\s+Citations by', markdown, re.IGNORECASE)
    citing_docs_match = re.search(r'Citations by \*\*(\d+)\*\*documents', markdown, re.IGNORECASE)
    author_docs_match = re.search(r'-\s+(\d+)\s+Documents', markdown, re.IGNORECASE)
    h_index_match     = re.search(r'-\s+(\d+)\s+_h_-index', markdown, re.IGNORECASE)

    return {
        "author":    author_match.group(1).strip() if author_match else None,
        "documents": int(author_docs_match.group(1)) if author_docs_match else None,
        "citations": int(citations_match.group(1))   if citations_match   else None,
        "citing":    int(citing_docs_match.group(1)) if citing_docs_match else None,
        "h-index":   int(h_index_match.group(1))     if h_index_match     else None,
    }


def is_valid(parsed: dict) -> bool:
    """
    Reject the payload if every numeric field is zero/None or the author
    name could not be extracted — which strongly suggests a failed scrape
    (login wall, bot block, empty page, etc.).
    """
    if not parsed.get("author"):
        return False

    numeric_fields = ["documents", "citations", "citing", "h-index"]

    # At least one numeric field must be non-None and greater than zero
    return any(
        parsed.get(f) is not None and parsed[f] > 0
        for f in numeric_fields
    )


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":

    # Always load what is already on disk first
    existing_data = load_existing_data(DATA_FILE)

    # Attempt the fetch — catch every possible error gracefully
    try:
        markdown = fetch_markdown()
        parsed   = parse_markdown(markdown)
    except Exception as exc:
        print(f"\n⚠️  Fetch failed: {exc}")
        print("  Keeping existing data unchanged. No file will be written.")
        exit(0)

    # Validate the parsed payload before touching the file
    if not is_valid(parsed):
        print("\n⚠️  Parsed data looks invalid (possible bot-block or login wall):")
        print(f"  {parsed}")
        print("  Keeping existing data unchanged. No file will be written.")
        exit(0)

    # Stamp with current UTC time and persist
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scopus_data = {**parsed, "refresh": now_str}

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(scopus_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅  Successfully saved to '{DATA_FILE}':")
    print(json.dumps(scopus_data, indent=2))
