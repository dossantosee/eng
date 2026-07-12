import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

AUTHOR_ID = os.environ.get("SCOPUS_AUTHOR_ID")
URL = f"https://www.scopus.com/authid/detail.uri?authorId={AUTHOR_ID}"

DATA_FILE = "fetch_data/scopus_data.json"

DEBUG_HTML_FILE = Path("scopus_debug.html")
DEBUG_SCREENSHOT_FILE = Path("scopus_debug.png")


def clean_number(value):
    """Convert values such as '1,234' into integer 1234."""
    if value is None:
        raise ValueError("A required Scopus metric was not found.")

    digits = re.sub(r"[^\d]", "", str(value))

    if not digits:
        raise ValueError(f"Could not convert metric to a number: {value!r}")

    return int(digits)


def first_text(page, selectors):
    """Return text from the first selector that exists and has content."""
    for selector in selectors:
        locator = page.locator(selector)

        try:
            if locator.count() > 0:
                value = locator.first.text_content(timeout=3000)

                if value and value.strip():
                    return value.strip()
        except Exception:
            pass

    return None


def validate_scopus_data(scopus_data):
    """Ensure the result has all required fields and is valid JSON."""

    required_fields = [
        "author",
        "documents",
        "citations",
        "citing",
        "h-index",
        "refreshed",
    ]

    missing_or_empty = [
        field
        for field in required_fields
        if field not in scopus_data
        or scopus_data[field] is None
        or (isinstance(scopus_data[field], str) and not scopus_data[field].strip())
    ]

    if missing_or_empty:
        raise ValueError(
            "Invalid Scopus data. Missing or empty field(s): "
            + ", ".join(missing_or_empty)
        )

    # Ensure numeric fields really are numeric and non-negative.
    for field in ["documents", "citations", "citing", "h-index"]:
        if not isinstance(scopus_data[field], int) or scopus_data[field] < 0:
            raise ValueError(
                f"Invalid Scopus data: '{field}' must be a non-negative integer."
            )

    # Raises an error if the data cannot be serialized to JSON.
    json.loads(json.dumps(scopus_data, ensure_ascii=False))


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        slow_mo=100,
    )

    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )

    page = context.new_page()
    page.set_default_timeout(10000)

    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)

        cookie_selectors = [
            "#onetrust-accept-btn-handler",
            'button:has-text("Accept All")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("I agree")',
        ]

        for selector in cookie_selectors:
            try:
                page.locator(selector).first.click(timeout=2500)
                break
            except Exception:
                pass

        print("Current page title:", page.title())
        print("Current page URL:", page.url)
        print("\nWaiting for profile metrics...")

        page.locator(
            '[data-testid="metrics-section-citations-count"]'
        ).wait_for(state="visible", timeout=120000)

        citations = first_text(
            page,
            [
                '[data-testid="metrics-section-citations-count"] '
                '[data-testid="unclickable-count"]',
            ],
        )

        documents = first_text(
            page,
            [
                '[data-testid="metrics-section-document-count"] '
                '[data-testid="unclickable-count"]',
            ],
        )

        h_index = first_text(
            page,
            [
                '[data-testid="metrics-section-h-index"] '
                '[data-testid="unclickable-count"]',
            ],
        )

        citing = first_text(
            page,
            [
                '[data-testid="metrics-section-citations-count"] strong',
            ],
        )

        author = first_text(
            page,
            [
                '[data-testid="author-name"]',
                '[data-testid="author-profile-name"]',
                '[data-testid="author-details-name"]',
                "h1",
            ],
        )

        scopus_data = {
            "author": author,
            "documents": clean_number(documents),
            "citations": clean_number(citations),
            "citing": clean_number(citing),
            "h-index": clean_number(h_index),
            "refreshed": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
        }

        # Checks required fields, non-empty values, numeric metrics, and JSON validity.
        validate_scopus_data(scopus_data)

        # Create fetch_data/ automatically if it does not exist.
        Path(DATA_FILE).parent.mkdir(parents=True, exist_ok=True)

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(scopus_data, f, indent=2, ensure_ascii=False)

        print(f"\n✅  Successfully saved to '{DATA_FILE}':")
        print(json.dumps(scopus_data, indent=2, ensure_ascii=False))

    except (PlaywrightTimeoutError, ValueError) as error:
        DEBUG_HTML_FILE.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_SCREENSHOT_FILE), full_page=True)

        print("\n❌ Could not extract or validate Scopus metrics.")
        print("Reason:", error)
        print("Page title:", page.title())
        print("Page URL:", page.url)
        print(f"Saved HTML for inspection: {DEBUG_HTML_FILE.resolve()}")
        print(f"Saved screenshot for inspection: {DEBUG_SCREENSHOT_FILE.resolve()}")

    finally:
        browser.close()
