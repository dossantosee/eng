import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None


INVENTOR_NAME = os.environ.get("INVENTOR_NAME")
DATA_FILE = Path("fetch_data/patent_data.json")

WIPO_BASE_URL = "https://patentscope.wipo.int/search/en/result.jsf"

# Set WIPO_HEADLESS=false if WIPO blocks headless Chromium in your environment.
HEADLESS_BROWSER = os.environ.get("WIPO_HEADLESS", "true").lower() not in {
    "0",
    "false",
    "no",
}

MIN_TRANSLATION_INTERVAL = 0.4
MAX_TRANSLATION_RETRIES = 4
last_translation_time = 0.0


def load_existing_data(filepath: Path) -> list:
    """Load existing JSON from disk, returning an empty list if unavailable."""
    if not filepath.exists():
        print(f"  No existing file found at '{filepath}'. Starting fresh.")
        return []

    try:
        with filepath.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            print(f"  Loaded {len(data)} existing record(s) from '{filepath}'.")
            return data

        print("  Existing file has an unexpected format. Starting fresh.")
        return []

    except (json.JSONDecodeError, OSError) as exc:
        print(f"  Could not read existing file: {exc}. Starting fresh.")
        return []


def clean_text(value: str) -> str:
    """Normalize whitespace in text extracted from the browser DOM."""
    return " ".join((value or "").split()).strip()


def sentence_case(text: str) -> str:
    """
    Normalize titles returned by WIPO.

    WIPO titles may be stored in uppercase or lowercase depending on the
    original patent-office record.
    """
    text = clean_text(text)

    if not text:
        return ""

    normalized = text.lower()
    return normalized[0].upper() + normalized[1:]


def build_application_number(country: str, number: str) -> str:
    """
    WIPO displays country and application number separately.

    Example:
        country = "BR"
        number = "102019023234"
        result = "BR102019023234"
    """
    country = clean_text(country).upper()
    number = clean_text(number).replace(" ", "")

    if not number:
        return ""

    if country and not number.upper().startswith(country):
        return f"{country}{number}"

    return number


def build_wipo_url(inventor_name: str) -> str:
    """Create a WIPO PATENTSCOPE result URL for the inventor query."""
    query = f'FP:("{inventor_name}")'
    return f"{WIPO_BASE_URL}?{urlencode({'query': query})}"


def set_results_per_page(page, per_page: str = "200") -> None:
    """
    Change WIPO's "Per page" selector and wait for the PrimeFaces AJAX
    request to replace the results container.
    """
    selector = 'select[id="resultListCommandsForm:perPage:input"]'
    per_page_select = page.locator(selector)

    per_page_select.wait_for(state="visible", timeout=45000)

    current_value = per_page_select.input_value()

    if current_value == per_page:
        print(f"  WIPO is already configured for {per_page} results per page.")
        return

    print(f"  Setting WIPO results per page to {per_page}...")

    # Save the current HTML so we can verify that PrimeFaces refreshed results.
    previous_results_html = page.locator("#results-container").inner_html()

    # select_option() triggers the select element's PrimeFaces onchange AJAX call.
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and "result.jsf" in response.url
        ),
        timeout=45000,
    ):
        per_page_select.select_option(per_page)

    # IMPORTANT: Playwright Python requires the second script argument as arg=.
    page.wait_for_function(
        """
        ({ selector, previousHtml, expectedValue }) => {
            const select = document.querySelector(selector);
            const results = document.querySelector("#results-container");

            return Boolean(
                select &&
                select.value === expectedValue &&
                results &&
                results.innerHTML !== previousHtml
            );
        }
        """,
        arg={
            "selector": selector,
            "previousHtml": previous_results_html,
            "expectedValue": per_page,
        },
        timeout=45000,
    )

    page.locator(".ps-patent-result").first.wait_for(
        state="attached",
        timeout=45000,
    )

    result_count = page.locator(".ps-patent-result").count()
    print(f"  WIPO loaded {result_count} result(s) on the current page.")

def fetch_patents(inventor_name: str) -> list:
    """
    Open WIPO PATENTSCOPE with Chromium, set results per page to 200,
    and extract the visible patent records from the rendered DOM.
    """
    search_url = build_wipo_url(inventor_name)

    print(f"Fetching WIPO PATENTSCOPE records for: {inventor_name}")
    print(f"URL: {search_url}")
    print(f"Browser mode: {'headless' if HEADLESS_BROWSER else 'visible'}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS_BROWSER)

        context = browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1200},
        )

        page = context.new_page()
        page.set_default_timeout(45000)

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for initial WIPO records using the default per-page setting.
            page.wait_for_selector(
                ".ps-patent-result",
                state="attached",
                timeout=45000,
            )

            # Change WIPO from its default 10 results per page to 200.
            set_results_per_page(page, "200")

            # Extract directly from the refreshed rendered WIPO DOM.
            raw_records = page.locator(".ps-patent-result").evaluate_all(
                """
                cards => cards.map(card => {
                    const titleElement = card.querySelector(".needTranslation-title");

                    const applicationLink = card.querySelector(
                        ".ps-patent-result--title a"
                    );

                    const applicationElement = card.querySelector(
                        ".ps-patent-result--title--patent-number"
                    );

                    const dateElement = card.querySelector(
                        '[id$="resultListTableColumnPubDate"]'
                    );

                    const countryElements = card.querySelectorAll(
                        ".ps-patent-result--title--ctr-pubdate .notranslate"
                    );

                    let country = "";

                    for (const element of countryElements) {
                        const value = (element.textContent || "").trim();

                        if (/^[A-Z]{2}$/.test(value)) {
                            country = value;
                            break;
                        }
                    }

                    return {
                        title: titleElement ? titleElement.textContent : "",
                        applicationNumber: applicationElement
                            ? applicationElement.textContent
                            : "",
                        country: country,
                        publicationDate: dateElement
                            ? dateElement.textContent
                            : "",
                        url: applicationLink ? applicationLink.href : ""
                    };
                })
                """
            )

            # Check whether there are multiple pages after selecting 200 results.
            # Stop safely instead of writing incomplete output.
            page_count_text = page.locator(
                ".ps-paginator--page--value"
            ).first.text_content()

            if page_count_text:
                parts = page_count_text.replace("\n", " ").split("/")

                if len(parts) == 2:
                    total_pages_match = re.search(r"\d+", parts[1])

                    if total_pages_match:
                        total_pages = int(total_pages_match.group(0))

                        if total_pages > 1:
                            raise ValueError(
                                f"WIPO returned {total_pages} pages even after "
                                "setting 200 results per page. Stopping to avoid "
                                "saving only the first page."
                            )

        except PlaywrightTimeoutError as exc:
            page_title = page.title()

            try:
                body_preview = clean_text(page.locator("body").inner_text())[:500]
            except Exception:
                body_preview = "Unable to read page body."

            raise RuntimeError(
                "Timed out while waiting for WIPO results. "
                f"Page title: {page_title!r}. "
                f"Page preview: {body_preview!r}. "
                "Try setting WIPO_HEADLESS=false and run again."
            ) from exc

        finally:
            browser.close()

    records = []
    seen_applications = set()

    for item in raw_records:
        title = sentence_case(item.get("title", ""))
        country = clean_text(item.get("country", ""))
        application_number = clean_text(item.get("applicationNumber", ""))
        publication_date = clean_text(item.get("publicationDate", ""))
        detail_url = clean_text(item.get("url", ""))

        application = build_application_number(country, application_number)
        year_match = re.search(r"\b(?:19|20)\d{2}\b", publication_date)

        if not year_match:
            print(
                "  Skipping record without a valid publication year: "
                f"{application or application_number}"
            )
            continue

        year = int(year_match.group(0))

        if not title or not application or not detail_url:
            print(
                "  Skipping incomplete WIPO record: "
                f"title={title!r}, application={application!r}, url={detail_url!r}"
            )
            continue

        if application in seen_applications:
            continue

        seen_applications.add(application)

        records.append(
            {
                "title": title,
                "year": year,
                "application": application,
                "url": detail_url,
            }
        )

    if not records:
        raise ValueError(
            "No patent records were extracted from the WIPO result cards."
        )

    print(f"  Extracted {len(records)} distinct patent record(s).")
    return records


def is_valid(records: list) -> bool:
    """Validate extracted data before replacing the JSON file."""
    if not records:
        return False

    required_fields = {"title", "year", "application", "url"}

    for record in records:
        if not isinstance(record, dict):
            return False

        if not required_fields.issubset(record):
            return False

        if not isinstance(record["title"], str) or not record["title"].strip():
            return False

        if not isinstance(record["year"], int):
            return False

        if not isinstance(record["application"], str) or not record["application"]:
            return False

        if not isinstance(record["url"], str) or not record["url"].startswith("https://"):
            return False

    return True


def existing_translation_map(existing_records: list) -> dict:
    """
    Return translations indexed by application number.

    A translation is reused only if the original Portuguese title remains
    exactly the same.
    """
    translations = {}

    for record in existing_records:
        application = record.get("application")
        title = record.get("title")
        title_en = record.get("title_en")

        if application and title and title_en:
            translations[application] = {
                "title": title,
                "title_en": title_en,
            }

    return translations


def throttle_translation_requests() -> None:
    """Keep a safe interval between Google translation requests."""
    global last_translation_time

    elapsed = time.monotonic() - last_translation_time

    if elapsed < MIN_TRANSLATION_INTERVAL:
        time.sleep(MIN_TRANSLATION_INTERVAL - elapsed)

    last_translation_time = time.monotonic()


def translate_title(text: str) -> str:
    """Translate one Portuguese patent title into English."""
    if not text:
        return ""

    if GoogleTranslator is None:
        raise RuntimeError(
            "deep-translator is not installed. Run: pip install deep-translator"
        )

    translator = GoogleTranslator(source="pt", target="en")

    for attempt in range(1, MAX_TRANSLATION_RETRIES + 1):
        throttle_translation_requests()

        try:
            translated = clean_text(translator.translate(text))

            if translated:
                return translated

            raise ValueError("Translation returned an empty result.")

        except Exception as exc:
            if attempt == MAX_TRANSLATION_RETRIES:
                raise RuntimeError(
                    f"Translation failed for {text!r}: {exc}"
                ) from exc

            backoff = 2**attempt

            print(
                f"  Translation attempt {attempt}/{MAX_TRANSLATION_RETRIES} "
                f"failed: {exc}. Retrying in {backoff}s..."
            )

            time.sleep(backoff)

    return ""


def add_translations(records: list, existing_records: list) -> list:
    """
    Add title_en immediately after title.

    Existing saved translations are reused when the corresponding Portuguese
    title did not change.
    """
    previous_translations = existing_translation_map(existing_records)
    translated_records = []

    for index, record in enumerate(records, start=1):
        application = record["application"]
        title = record["title"]
        old_record = previous_translations.get(application)

        if old_record and old_record["title"] == title:
            title_en = old_record["title_en"]
            print(f"  Reusing translation {index}/{len(records)}: {application}")
        else:
            print(f"  Translating title {index}/{len(records)}: {application}")
            title_en = translate_title(title)

        translated_records.append(
            {
                "title": title,
                "title_en": title_en,
                "year": record["year"],
                "application": application,
                "url": record["url"],
            }
        )

    return translated_records


def stamp_records(records: list, refresh_timestamp: str) -> list:
    """Add the current UTC refresh timestamp to every record."""
    return [{**record, "refresh": refresh_timestamp} for record in records]


def save_records(records: list, filepath: Path) -> None:
    """Create the output directory if needed and write JSON."""
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with filepath.open("w", encoding="utf-8") as file:
        json.dump(records, file, indent=2, ensure_ascii=False)
        file.write("\n")


def print_results(records: list) -> None:
    """Print the final patent records."""
    print(f"\n  {'=' * 70}")
    print(f"  Found {len(records)} patent(s):")
    print(f"  {'=' * 70}")

    for index, record in enumerate(records, start=1):
        print(f"\n  [{index:03d}] Title:       {record.get('title')}")
        print(f"        Title (EN): {record.get('title_en')}")
        print(f"        Year:        {record.get('year')}")
        print(f"        Application: {record.get('application')}")
        print(f"        URL:         {record.get('url')}")
        print(f"        Refresh:     {record.get('refresh')}")

    print(f"\n  {'=' * 70}")
    print(f"  Total: {len(records)} record(s)")


if __name__ == "__main__":
    if not INVENTOR_NAME:
        print("⚠️  INVENTOR_NAME environment variable is not set. Exiting.")
        raise SystemExit(0)

    existing_data = load_existing_data(DATA_FILE)

    try:
        raw_records = fetch_patents(INVENTOR_NAME)

        if not is_valid(raw_records):
            raise ValueError("Extracted patent records failed validation.")

        print("\nTranslating titles to English...")
        patent_data = add_translations(raw_records, existing_data)

        refresh_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patent_data = stamp_records(patent_data, refresh_timestamp)

        save_records(patent_data, DATA_FILE)

    except Exception as exc:
        print(f"\n⚠️  Patent update failed: {exc}")
        print("  Existing data was kept unchanged. No file was written.")
        raise SystemExit(0)

    print_results(patent_data)
    print(f"\n✅ Successfully saved {len(patent_data)} patent(s) to '{DATA_FILE}'.")
