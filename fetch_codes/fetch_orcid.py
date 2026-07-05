"""
ORCID Profile Scraper -- Firecrawl-only
========================================
Firecrawl's own headless browser renders the page, clicks every
"Show more detail" toggle (actions -> click -> all: true), waits for
the DOM to settle, then returns BOTH:
    - the fully-expanded page as Markdown (formats: "markdown")
    - a structured JSON object matching your schema (formats: "extract")
...all in a single /v1/scrape call. No local browser driver needed.

Environment variables expected:
    ORCID_USER        formatted as 0000-0000-0000-0000
    FIRECRAWL_API_KEY your Firecrawl API key
"""

import os
import re
import json
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
ORCID_USER = os.environ.get("ORCID_USER")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")

if not FIRECRAWL_API_KEY or not ORCID_USER:
    print("Error: Missing FIRECRAWL_API_KEY or ORCID_USER environment variables.")
    exit(1)

TARGET_URL = f"https://orcid.org/{ORCID_USER}"
print(f"Scraping ORCID profile: {TARGET_URL}")

# ORCID renders each toggle as: <a role="button" class="underline" ...>Show more detail</a>
SHOW_MORE_CSS_SELECTOR = "a.underline[role='button']"

headers = {
    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
    "Content-Type": "application/json",
}

# --------------------------------------------------------------------------
# Extraction schema
# --------------------------------------------------------------------------
schema = {
    "type": "object",
    "properties": {
        "Name": {"type": "string"},
        "ORCID_number": {"type": "string"},
        "Countries": {"type": "array", "items": {"type": "string"}},
        "Keywords": {"type": "array", "items": {"type": "string"}},
        "Keywords_pt": {"type": "array", "items": {"type": "string"}},
        "Websites_and_social_links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                },
            },
        },
        "Other_IDs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                    "url": {"type": "string"},
                },
            },
        },
        "Emails_and_domains": {"type": "array", "items": {"type": "string"}},
        "Employment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "employer": {"type": "string"},
                    "title": {"type": "string"},
                    "department": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "URL": {"type": "string"},
                },
            },
        },
        "Education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "institution": {"type": "string"},
                    "department": {"type": "string"},
                    "degree": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "URL": {"type": "string"},
                },
            },
        },
        "Qualifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "institution": {"type": "string"},
                    "department": {"type": "string"},
                    "degree": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "URL": {"type": "string"},
                },
            },
        },
        "Works": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "publisher": {"type": "string"},
                    "date": {"type": "string"},
                    "type": {"type": "string"},
                    "contributors": {"type": "string"},
                    "DOI": {"type": "string"},
                    "DOI_URL": {"type": "string"},
                },
            },
        },
        "Professional_Activities": {"type": "array", "items": {"type": "object"}},
        "Funding": {"type": "array", "items": {"type": "object"}},
        "Peer_Review": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
            },
        },
    },
    "required": ["Name", "ORCID_number"],
}

# --------------------------------------------------------------------------
# Payload: Firecrawl renders, clicks every "Show more detail" button, then
# returns markdown + schema-based extract in one shot.
# --------------------------------------------------------------------------
payload = {
    "url": TARGET_URL,
    "formats": ["markdown", "html", "extract"],
    "timeout": 180000,
    "onlyMainContent": False,
    "actions": [
        {"type": "wait", "milliseconds": 2000},
        # "all": true clicks every element matching the selector, so we
        # don't need to know how many "Show more detail" links exist.
        {"type": "click", "selector": SHOW_MORE_CSS_SELECTOR, "all": True},
        {"type": "wait", "milliseconds": 1500},
    ],
    "extract": {
    "prompt": (
                "Extract the comprehensive ORCID profile data strictly based on "
                "the schema. IMPORTANT DISTINCTION: Under 'Education and "
                "qualifications', each entry has a label identifying it as "
                "either 'Education' or 'Qualification'. You must use this "
                "label to separate the entries into their respective "
                "'Education' or 'Qualifications' arrays in the JSON. Check the "
                "sidebar for Links, Countries, and IDs. If a section is "
                "missing, return an empty list. "
                "The Education or Qualification type is rendered as a label "
                "(originally an <div _ngcontent-ng-c3333035839=\"\" "
                "class=\"type\"> element). "
                "\n\nQUALIFICATION TITLE/DEGREE RULE: For entries labeled "
                "'Qualification', the heading text is formatted as "
                "'<TITLE> (<DEGREE>)' -- the title is the text BEFORE the "
                "parentheses, and the degree/credential is the text INSIDE "
                "the parentheses. For example, given the heading text "
                "'CSAM - Certified Senior Practitioner in Asset Management "
                "(Credential 12312)', you must set "
                "title = 'CSAM - Certified Senior Practitioner in Asset "
                "Management' and degree = 'Credential 12312'. Do not include "
                "the parentheses themselves in either field, and do not swap "
                "the two fields. This rule applies only to Qualification "
                "entries; Education entries do not follow this pattern."
                "You also MUST NOT include the "
                "domains orcid.org or mailinator.com in the extraction as "
                "these are unrelated to the user profile. "
                "\n\nKEYWORDS RULE: If Keywords appear with suffix numbers "
                "(e.g. '1. Asset Management, Reliability, and Maintenance "
                "Engineering, 2. Belt Conveying Systems, ...'), you MUST "
                "consider each numbered sentence as a single keyword, and "
                "strip the leading number and punctuation (e.g. '1.') from "
                "it. You must populate the JSON field 'Keywords' with these "
                "original keywords exactly as found on the page, in English. "
                "You must then populate the JSON field 'Keywords_pt' with the "
                "equivalent translation of each of those same keywords into "
                "Brazilian Portuguese (pt-BR), in the same order, so that "
                "Keywords[i] and Keywords_pt[i] are translations of each "
                "other. Do not invent keywords that are not present on the "
                "page, and do not translate any other field in the schema -- "
                "this translation rule applies only to Keywords_pt. "
                "\n\nWORKS TITLE RULE: For each entry under 'Works', the "
                "publication title is rendered as a heading (originally an "
                "<h4 class=\"work-title\"> element) that appears ABOVE that "
                "work's metadata block (publisher, date, type, contributors, "
                "DOI), not inside it. Treat each heading as the start of a new "
                "Work entry and pair it with the metadata block that "
                "immediately follows it, until the next such heading begins. "
                "Do not leave 'title' blank just because the heading text is "
                "structurally separate from the rest of the fields -- always "
                "walk upward to the nearest preceding heading to find it. If "
                "you see repeated phrases like 'Show more details for work "
                "<TITLE>' or 'Show all sources for work <TITLE>', you may use "
                "the <TITLE> portion as a fallback/cross-check for the title "
                "if the heading itself is ambiguous or missing."
                "\n\nEMPLOYMENT RULE: The employment section lives inside a "
                "container with id=\"cy-affiliation-employment\". Within that "
                "container, EVERY <h4 class=\"orc-font-body\"> heading marks "
                "the start of one separate Employment entry (employer name, "
                "role title, department, start/end dates, URL). Do NOT stop "
                "after the first or most recent heading -- walk through the "
                "ENTIRE cy-affiliation-employment container from top to bottom "
                "and create one Employment array entry for every such h4 you "
                "encounter, even if there are many. Each heading's employer "
                "block ends where the next h4 heading in the same container "
                "begins. Return ALL employment entries found, not just the "
                "current/most recent one."
            ),
        "schema": schema,
    },
}

print("Fetching data from Firecrawl...")
response = requests.post(
    "https://api.firecrawl.dev/v1/scrape", headers=headers, json=payload
)
response.raise_for_status()

result = response.json()
data = result.get("data", {})

# --- Save the expanded Markdown ---
markdown_text = data.get("markdown")
if markdown_text:
    md_path = f"orcid_{ORCID_USER}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown_text)
    print(f"Saved expanded Markdown to {md_path} ({len(markdown_text)} characters)")
else:
    print("Warning: no markdown returned by Firecrawl.")

# --- Validate and print the structured extraction ---
extracted_data = data.get("extract", None)

if not extracted_data or "Name" not in extracted_data:
    print("Error: Invalid or empty data returned from Firecrawl.")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])
    exit(1)


def backfill_work_titles(extracted, raw_html):
    """The LLM-based `extract` sometimes fails to associate a work's
    title (rendered as a separate heading ABOVE the metadata block)
    with the right entry, especially when there are many works in a
    row. Rather than trust that inference, pull every work-title
    heading straight out of the DOM, in document order, and backfill
    any missing/blank titles by position.
    """
    works = extracted.get("Works")
    if not works or not raw_html:
        return

    soup = BeautifulSoup(raw_html, "html.parser")
    # ORCID renders each work title as: <h4 class="work-title orc-font-body">TITLE</h4>
    heading_els = soup.select("h4[class*='work-title']")
    dom_titles = [h.get_text(strip=True) for h in heading_els if h.get_text(strip=True)]

    if not dom_titles:
        print("[backfill] No work-title headings found in HTML -- skipping.")
        return

    if len(dom_titles) != len(works):
        print(
            f"[backfill] Warning: found {len(dom_titles)} title heading(s) in HTML "
            f"but {len(works)} Works entr(y/ies) in the extraction -- "
            "backfilling by position anyway, please spot-check the result."
        )

    filled = 0
    for i, work in enumerate(works):
        current = (work.get("title") or "").strip()
        if not current and i < len(dom_titles):
            work["title"] = dom_titles[i]
            filled += 1

    if filled:
        print(f"[backfill] Filled in {filled} missing Works title(s) from the HTML headings.")


backfill_work_titles(extracted_data, data.get("html"))

extracted_data["refreshed"] = datetime.now(timezone.utc).strftime(
    "%Y-%m-%d %H:%M:%S UTC"
)

# Save to the JSON file
file_name = "fetch_data/ORCID_data.json"
with open(file_name, "w", encoding="utf-8") as f:
    json.dump(extracted_data, f, indent=2, ensure_ascii=False)

print(f"Successfully saved ORCID profile for {extracted_data['Name']} to {file_name}")

