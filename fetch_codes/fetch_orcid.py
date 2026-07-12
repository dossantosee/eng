import json
import re
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from jsonschema import validate
from playwright.sync_api import sync_playwright

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None


# =============================================================================
# CONFIGURATION
# =============================================================================

ORCID_NUMBER = os.environ.get("ORCID_USER")

# Public ORCID profile URL.
TARGET_URL = f"https://orcid.org/{ORCID_NUMBER}"

# Official ORCID public API. Used only for Countries, Keywords and Keywords_pt.
API_RECORD_URL = f"https://pub.orcid.org/v3.0/{ORCID_NUMBER}/record"

API_HEADERS = {
    "Accept": "application/vnd.orcid+json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/122.0 Safari/537.36 "
        "ORCID-Public-Profile-Extractor/1.0"
    ),
}

# For authenticated /my-orcid extraction:
#
# TARGET_URL = f"https://orcid.org/my-orcid?orcid={ORCID_NUMBER}"
# STORAGE_STATE_FILE = "orcid_storage_state.json"
#
# Public profile extraction does not need it.
STORAGE_STATE_FILE = None

OUTPUT_FILE = "orcid_profile.json"
HEADLESS = True

BLOCKED_DOMAINS = {
    "orcid.org",
    "www.orcid.org",
    "mailinator.com",
    "www.mailinator.com",
}


# =============================================================================
# CSS SELECTORS
# =============================================================================

CSS = {
    # Header
    "profile_name": "#names h1",
    "orcid_id": "#orcid-id h2",

    # Sidebar
    "emails_panel": "#emails-panel",
    "websites_panel": "#websites-panel",
    "other_ids_panel": "#other-identifiers",
    "keywords_panel": "#keywords-panel",
    "countries_panel": "#countries-panel",

    # Activity sections
    "employment_section": "#cy-affiliation-employment",
    "education_qualification_section": "#cy-affiliation-education-and-qualification",
    "works_section": "#cy-works",
    "peer_reviews_section": "#cy-peer-reviews",
    "funding_section": "#cy-fundings",
    "professional_activities_section": "#professional-activities",

    # Affiliation records
    "affiliation_stack": "app-affiliation-stack",
    "affiliation_heading": "h4.orc-font-body",
    "affiliation_general_data": "div.general-data",
    "affiliation_type": "div.type",

    # Work records
    "work_stack": "app-work-stack",
    "work_title": "h4.work-title",
    "work_general_data": "div.general-data",
    "work_doi_link": "a[href*='doi.org']",
}


# =============================================================================
# EXTRACTION SCHEMA
# =============================================================================

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
        "Professional_Activities": {
            "type": "array",
            "items": {"type": "object"},
        },
        "Funding": {
            "type": "array",
            "items": {"type": "object"},
        },
        "Peer_Review": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                },
            },
        },
        "refreshed": {"type": "string"},
    },
    "required": ["Name", "ORCID_number"],
}


# =============================================================================
# GENERAL HELPERS
# =============================================================================

def clean_text(value):
    """Normalize whitespace."""
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def unique(values):
    """Remove empty values and duplicates while preserving original order."""
    output = []
    seen = set()

    for value in values:
        value = clean_text(value)

        if value and value not in seen:
            seen.add(value)
            output.append(value)

    return output


def allowed_url(url):
    """Reject unrelated ORCID and Mailinator URLs."""
    url = clean_text(url)

    if not url:
        return False

    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False

    return hostname not in BLOCKED_DOMAINS


def allowed_domain_or_email(value):
    """Reject orcid.org and mailinator.com from emails/domains."""
    value = clean_text(value).lower()

    if not value:
        return False

    domain = value.rsplit("@", 1)[-1]

    return domain not in BLOCKED_DOMAINS


def locator_text(locator):
    """Read Playwright locator text safely."""
    try:
        return clean_text(locator.inner_text(timeout=2000))
    except Exception:
        return ""


def locator_attribute(locator, attribute):
    """Read a Playwright locator attribute safely."""
    try:
        return clean_text(locator.get_attribute(attribute, timeout=2000))
    except Exception:
        return ""


# =============================================================================
# EDUCATION / QUALIFICATION PARSING
# =============================================================================

def split_top_level_parentheses(text):
    """
    Split only top-level parenthetical groups while preserving nested groups.

    Example:
        Master of Science (MSc) (Process Engineering)
        (The Graduate Program in Process Engineering (PPGEP))

    Returns:
        (
            "Master of Science",
            [
                "MSc",
                "Process Engineering",
                "The Graduate Program in Process Engineering (PPGEP)"
            ]
        )
    """
    text = clean_text(text)

    prefix = []
    groups = []
    current_group = []
    depth = 0

    for char in text:
        if char == "(":
            if depth == 0:
                depth = 1
                current_group = []
            else:
                depth += 1
                current_group.append(char)

        elif char == ")":
            if depth == 0:
                prefix.append(char)
            else:
                depth -= 1

                if depth == 0:
                    groups.append(clean_text("".join(current_group)))
                    current_group = []
                else:
                    current_group.append(char)

        else:
            if depth == 0:
                prefix.append(char)
            else:
                current_group.append(char)

    if current_group:
        groups.append(clean_text("".join(current_group)))

    return clean_text("".join(prefix)), groups


def is_degree_abbreviation(group):
    """
    Accept degree acronyms such as MSc, MBA, B.E., Ph.D.
    Reject descriptive values such as EQF Level 7.
    """
    group = clean_text(group)

    if not group or " " in group:
        return False

    return bool(re.fullmatch(r"[A-Za-zÀ-ÿ.]{2,12}", group))


def parse_date_and_description(general_text):
    """
    Parse ORCID affiliation text:

        2021-10 to present | Asset Management Specialist
        (Lab Equipment Management (Asset Management))
    """
    general_text = clean_text(general_text)

    if "|" not in general_text:
        return "", "", general_text

    date_text, description = general_text.split("|", 1)

    date_text = clean_text(date_text)
    description = clean_text(description)

    match = re.match(
        r"^(.*?)\s+to\s+(present|current|.*?)$",
        date_text,
        flags=re.IGNORECASE,
    )

    if not match:
        return date_text, "", description

    return (
        clean_text(match.group(1)),
        clean_text(match.group(2)),
        description,
    )


def parse_employment_description(description):
    """
    Employment format:
        Job title (Department)
    """
    title, groups = split_top_level_parentheses(description)

    return {
        "title": clean_text(title),
        "department": clean_text(" ".join(groups)),
    }


def parse_education_or_qualification_description(description):
    """
    Required parsing:

    - Text before first parenthesis -> degree.
    - First short acronym -> appended to degree.
    - Next group -> title.
    - Remaining groups -> department.
    """
    degree, groups = split_top_level_parentheses(description)

    if groups and is_degree_abbreviation(groups[0]):
        degree = f"{degree} ({groups.pop(0)})"

    title = ""
    department = ""

    if groups:
        title = groups.pop(0)

    if groups:
        department = " ".join(groups)

    return {
        "degree": clean_text(degree),
        "title": clean_text(title),
        "department": clean_text(department),
    }


# =============================================================================
# KEYWORD PARSING / TRANSLATION
# =============================================================================

def parse_numbered_keywords(keyword_text):
    """
    Parse:

        1. Asset Management, Reliability, and Maintenance Engineering,
        2. Belt Conveying Systems,
        3. Digital twinning and IIoT,
        ...
    """
    keyword_text = clean_text(keyword_text)

    numbered_items = re.findall(
        r"(?:^|,\s*)(\d+)\.\s*(.*?)(?=,\s*\d+\.|$)",
        keyword_text,
    )

    if numbered_items:
        return [
            clean_text(keyword)
            for _, keyword in numbered_items
            if clean_text(keyword)
        ]

    return unique(re.split(r"\n|;|•", keyword_text))


KEYWORD_TRANSLATIONS_PT_BR = {
    "Asset Management, Reliability, and Maintenance Engineering": (
        "Gestão de Ativos, Confiabilidade e Engenharia de Manutenção"
    ),
    "Belt Conveying Systems": "Sistemas de Transporte por Correia",
    "Digital twinning and IIoT": "Gêmeo digital e IIoT",
    "Electronics and Mechanical Prototyping": (
        "Prototipagem Eletrônica e Mecânica"
    ),
    "Automation, Programming and LLMs": (
        "Automação, Programação e LLMs"
    ),
}


def translate_keywords_to_pt_br(keywords):
    """
    Translate each keyword in matching order.

    The fixed mapping guarantees the requested pt-BR values for this profile.
    Unknown values use deep-translator, if available.
    """
    if not keywords:
        return []

    translator = None

    if GoogleTranslator is not None:
        try:
            translator = GoogleTranslator(source="en", target="pt")
        except Exception:
            translator = None

    translations = []

    for keyword in keywords:
        if keyword in KEYWORD_TRANSLATIONS_PT_BR:
            translations.append(KEYWORD_TRANSLATIONS_PT_BR[keyword])
            continue

        if translator is not None:
            try:
                translated_value = clean_text(translator.translate(keyword))

                if translated_value:
                    translations.append(translated_value)
                    continue
            except Exception:
                pass

        translations.append(keyword)

    return translations


# =============================================================================
# ORCID PUBLIC API:
# COUNTRIES, KEYWORDS, KEYWORDS_PT
# =============================================================================

def api_get_public_record():
    """
    Get official structured ORCID public JSON.

    This avoids Angular page metadata contamination:
        Source: ...
        Created: ...
    """
    response = requests.get(
        API_RECORD_URL,
        headers=API_HEADERS,
        timeout=60,
    )

    response.raise_for_status()

    return response.json()


def extract_countries_from_api(record):
    """
    Expected:
        ["Brazil", "Saudi Arabia"]
    """
    addresses = (
        record.get("person", {})
        .get("addresses", {})
        .get("address", [])
    )

    countries = []

    for address in addresses:
        country = address.get("country", {})

        if not isinstance(country, dict):
            continue

        country_name = clean_text(
            country.get("value", "") or country.get("country-code", "")
        )

        if country_name:
            countries.append(country_name)

    return unique(countries)


def extract_keywords_from_api(record):
    """
    Extract keyword values from the structured ORCID public API.

    Supports API records where:
    - each keyword is individual; or
    - one API item contains a numbered keyword sentence.
    """
    keyword_items = (
        record.get("person", {})
        .get("keywords", {})
        .get("keyword", [])
    )

    keywords = []

    for item in keyword_items:
        text = clean_text(item.get("content", ""))

        if not text:
            continue

        parsed_keywords = parse_numbered_keywords(text)

        if parsed_keywords:
            keywords.extend(parsed_keywords)
        else:
            keywords.append(
                re.sub(r"^\s*\d+\.\s*", "", text)
            )

    return unique(keywords)


def extract_person_fields_from_api():
    """
    Extract only Countries, Keywords and Keywords_pt from official ORCID API.
    """
    record = api_get_public_record()

    countries = extract_countries_from_api(record)
    keywords = extract_keywords_from_api(record)

    return {
        "Countries": countries,
        "Keywords": keywords,
        "Keywords_pt": translate_keywords_to_pt_br(keywords),
    }


# =============================================================================
# PLAYWRIGHT PAGE HELPERS
# =============================================================================

def click_expandable_controls(page):
    """Expand ORCID visible panels and detail buttons."""
    for _ in range(15):
        clicked = page.evaluate(
            """
            () => {
                const elements = [
                    ...document.querySelectorAll("button, a[role='button']")
                ];

                const candidate = elements.find(el => {
                    const label = (
                        el.getAttribute("aria-label") ||
                        el.innerText ||
                        el.textContent ||
                        ""
                    ).replace(/\\s+/g, " ").trim().toLowerCase();

                    const visible = !!(
                        el.offsetWidth ||
                        el.offsetHeight ||
                        el.getClientRects().length
                    );

                    if (!visible) return false;

                    return (
                        label.startsWith("expand ") ||
                        label.startsWith("show more detail") ||
                        label.startsWith("show details") ||
                        label.startsWith("expand review activity")
                    );
                });

                if (!candidate) {
                    return false;
                }

                try {
                    candidate.click();
                    return true;
                } catch (_) {
                    return false;
                }
            }
            """
        )

        if not clicked:
            break

        page.wait_for_timeout(300)


def get_section_count(section_locator):
    """Read section counts, e.g. Employment (9)."""
    try:
        text = clean_text(
            section_locator.locator("h3.activity-header").inner_text()
        )
    except Exception:
        return None

    match = re.search(r"\((\d+)", text)

    return int(match.group(1)) if match else None


# =============================================================================
# SIDEBAR EXTRACTION
# =============================================================================

def extract_emails_and_domains(page):
    """
    Extract public emails and visible email domains.

    The current profile displays:
        kaust.edu.sa
    """
    panel = page.locator(CSS["emails_panel"])

    if panel.count() == 0:
        return []

    panel_text = locator_text(panel)

    emails = re.findall(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        panel_text,
    )

    domains = re.findall(
        r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b",
        panel_text,
    )

    values = []

    for email in emails:
        if allowed_domain_or_email(email):
            values.append(email)

    for domain in domains:
        if allowed_domain_or_email(domain):
            values.append(domain)

    return unique(values)


def extract_websites(page):
    """Extract links from Websites & social links sidebar panel."""
    panel = page.locator(CSS["websites_panel"])

    if panel.count() == 0:
        return []

    links = panel.locator("a[href]")
    results = []

    for index in range(links.count()):
        link = links.nth(index)

        title = locator_text(link)
        url = locator_attribute(link, "href")

        if allowed_url(url):
            results.append(
                {
                    "title": title or url,
                    "url": url,
                }
            )

    return results


def extract_other_ids(page):
    """Extract Other IDs such as Scopus Author ID."""
    panel = page.locator(CSS["other_ids_panel"])

    if panel.count() == 0:
        return []

    links = panel.locator("app-panel-element a[href]")
    results = []

    for index in range(links.count()):
        link = links.nth(index)

        text = locator_text(link)
        url = locator_attribute(link, "href")

        if not text:
            continue

        if ":" in text:
            identifier_type, identifier_value = text.split(":", 1)
        else:
            identifier_type = ""
            identifier_value = text

        if url and not allowed_url(url):
            url = ""

        results.append(
            {
                "type": clean_text(identifier_type),
                "value": clean_text(identifier_value),
                "url": clean_text(url),
            }
        )

    return results


# =============================================================================
# AFFILIATION EXTRACTION
# =============================================================================

def extract_affiliation_records(page, section_selector, record_kind):
    """
    Extract affiliation records from:
    - Employment;
    - Education and Qualifications.

    Every app-affiliation-stack is one separate ORCID record.
    """
    section = page.locator(section_selector)

    if section.count() == 0:
        return []

    stacks = section.locator(CSS["affiliation_stack"])
    records = []

    for index in range(stacks.count()):
        stack = stacks.nth(index)

        institution_or_employer = locator_text(
            stack.locator(CSS["affiliation_heading"]).first
        )

        general_data_nodes = stack.locator(
            CSS["affiliation_general_data"]
        )

        if not institution_or_employer or general_data_nodes.count() == 0:
            continue

        primary_general_data = locator_text(
            general_data_nodes.nth(0)
        )

        start, end, description = parse_date_and_description(
            primary_general_data
        )

        affiliation_type = locator_text(
            stack.locator(CSS["affiliation_type"]).first
        ).lower()

        url = ""

        url_links = stack.locator(
            "div.general-data a[href], a[href].underline"
        )

        for link_index in range(url_links.count()):
            link = url_links.nth(link_index)
            href = locator_attribute(link, "href")

            if allowed_url(href):
                url = href
                break

        if record_kind == "employment":
            parsed = parse_employment_description(description)

            records.append(
                {
                    "employer": institution_or_employer,
                    "title": parsed["title"],
                    "department": parsed["department"],
                    "start": start,
                    "end": end,
                    "URL": url,
                }
            )

        else:
            parsed = parse_education_or_qualification_description(
                description
            )

            records.append(
                {
                    "record_type": affiliation_type,
                    "institution": institution_or_employer,
                    "department": parsed["department"],
                    "degree": parsed["degree"],
                    "title": parsed["title"],
                    "start": start,
                    "end": end,
                    "URL": url,
                }
            )

    return records


def extract_employment(page):
    return extract_affiliation_records(
        page=page,
        section_selector=CSS["employment_section"],
        record_kind="employment",
    )


def extract_education_and_qualifications(page):
    raw_records = extract_affiliation_records(
        page=page,
        section_selector=CSS["education_qualification_section"],
        record_kind="education_qualification",
    )

    education = []
    qualifications = []

    for record in raw_records:
        record_type = record.pop("record_type", "").lower()

        if record_type == "qualification":
            qualifications.append(record)
        else:
            education.append(record)

    return education, qualifications


# =============================================================================
# WORK EXTRACTION
# =============================================================================

def extract_works(page):
    """
    Extract standard works from #cy-works.

    The featured-work section is separate and is intentionally ignored because
    it duplicates the same featured work in the normal Works list.
    """
    section = page.locator(CSS["works_section"])

    if section.count() == 0:
        return []

    work_stacks = section.locator(CSS["work_stack"])
    works = []

    for index in range(work_stacks.count()):
        work_stack = work_stacks.nth(index)

        title = locator_text(
            work_stack.locator(CSS["work_title"]).first
        )

        if not title:
            continue

        general_data = work_stack.locator(CSS["work_general_data"])

        publisher = ""
        publication_date = ""
        work_type = ""
        contributors = ""

        if general_data.count() >= 1:
            publisher = locator_text(general_data.nth(0))

        if general_data.count() >= 2:
            date_and_type = locator_text(general_data.nth(1))

            if "|" in date_and_type:
                publication_date, work_type = date_and_type.split("|", 1)
                publication_date = clean_text(publication_date)
                work_type = clean_text(work_type)
            else:
                publication_date = clean_text(date_and_type)

        for general_index in range(general_data.count()):
            value = locator_text(general_data.nth(general_index))

            if value.lower().startswith("contributors"):
                contributors = clean_text(
                    re.sub(
                        r"^contributors\s*:\s*",
                        "",
                        value,
                        flags=re.IGNORECASE,
                    )
                )
                break

        doi = ""
        doi_url = ""

        doi_links = work_stack.locator(CSS["work_doi_link"])

        if doi_links.count() > 0:
            doi_link = doi_links.first
            doi = locator_text(doi_link)
            doi_url = locator_attribute(doi_link, "href")

        if doi and not doi_url:
            doi_url = f"https://doi.org/{doi}"

        works.append(
            {
                "title": title,
                "publisher": publisher,
                "date": publication_date,
                "type": work_type,
                "contributors": contributors,
                "DOI": doi,
                "DOI_URL": doi_url,
            }
        )

    output = []
    seen = set()

    for work in works:
        key = (
            work["title"],
            work["DOI"],
            work["date"],
        )

        if key not in seen:
            seen.add(key)
            output.append(work)

    return output


# =============================================================================
# PEER REVIEW / OPTIONAL SECTIONS
# =============================================================================

def extract_peer_reviews(page):
    section = page.locator(CSS["peer_reviews_section"])

    if section.count() == 0:
        return []

    panels = section.locator("app-panel")
    results = []

    for index in range(panels.count()):
        panel = panels.nth(index)

        header = panel.locator(".header")
        text = locator_text(
            header.first if header.count() else panel
        )

        if not text:
            continue

        if text.lower().strip() in {"peer review", "peer reviews"}:
            continue

        results.append(
            {
                "description": text,
            }
        )

    return results


def extract_professional_activities(page):
    section = page.locator(CSS["professional_activities_section"])

    if section.count() == 0:
        return []

    text = locator_text(section)

    if not text:
        return []

    lines = unique(text.splitlines())

    return [
        {"description": line}
        for line in lines
        if line.lower() not in {
            "professional activities",
            "activities",
        }
    ]


def extract_funding(page):
    section = page.locator(CSS["funding_section"])

    if section.count() == 0:
        return []

    text = locator_text(section)

    if not text:
        return []

    lines = unique(text.splitlines())

    return [
        {"description": line}
        for line in lines
        if line.lower() not in {"funding", "fundings"}
    ]


# =============================================================================
# COUNT VALIDATION
# =============================================================================

def verify_section_counts(page, extracted_data):
    """
    Warn if visible ORCID section counts do not equal extracted totals.
    """
    checks = [
        (
            "Employment",
            CSS["employment_section"],
            len(extracted_data["Employment"]),
        ),
        (
            "Education + Qualifications",
            CSS["education_qualification_section"],
            len(extracted_data["Education"])
            + len(extracted_data["Qualifications"]),
        ),
        (
            "Works",
            CSS["works_section"],
            len(extracted_data["Works"]),
        ),
    ]

    for section_name, selector, actual_count in checks:
        section = page.locator(selector)

        if section.count() == 0:
            continue

        expected_count = get_section_count(section)

        if expected_count is not None and expected_count != actual_count:
            print(
                (
                    f"WARNING: {section_name} count mismatch. "
                    f"ORCID page says {expected_count}; "
                    f"extractor found {actual_count}."
                ),
                file=sys.stderr,
            )


# =============================================================================
# MAIN EXTRACTION
# =============================================================================

def scrape_orcid_profile():
    extracted_data = {
        "Name": "",
        "ORCID_number": ORCID_NUMBER,
        "Countries": [],
        "Keywords": [],
        "Keywords_pt": [],
        "Websites_and_social_links": [],
        "Other_IDs": [],
        "Emails_and_domains": [],
        "Employment": [],
        "Education": [],
        "Qualifications": [],
        "Works": [],
        "Professional_Activities": [],
        "Funding": [],
        "Peer_Review": [],
    }

    # -------------------------------------------------------------------------
    # Extract Countries / Keywords / Keywords_pt through ORCID public API.
    # This avoids Angular UI metadata contaminating these values.
    # -------------------------------------------------------------------------
    try:
        api_person_data = extract_person_fields_from_api()

        extracted_data["Countries"] = api_person_data["Countries"]
        extracted_data["Keywords"] = api_person_data["Keywords"]
        extracted_data["Keywords_pt"] = api_person_data["Keywords_pt"]

    except Exception as error:
        print(
            (
                "WARNING: Could not retrieve Countries / Keywords "
                f"from ORCID API: {error}"
            ),
            file=sys.stderr,
        )

    # -------------------------------------------------------------------------
    # Extract all other fields from rendered ORCID profile HTML.
    # -------------------------------------------------------------------------
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)

        context_args = {
            "viewport": {
                "width": 1600,
                "height": 1800,
            },
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        }

        if STORAGE_STATE_FILE and Path(STORAGE_STATE_FILE).exists():
            context_args["storage_state"] = STORAGE_STATE_FILE

        context = browser.new_context(**context_args)
        page = context.new_page()

        page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=120000,
        )

        # Wait for Angular to render the profile.
        page.wait_for_timeout(5000)

        try:
            page.locator(CSS["profile_name"]).wait_for(
                state="visible",
                timeout=30000,
            )
        except Exception:
            pass

        page.wait_for_timeout(2000)

        body_text = locator_text(page.locator("body"))

        if "Sign in to ORCID" in body_text:
            browser.close()

            raise RuntimeError(
                "ORCID returned a sign-in page instead of the public profile."
            )

        if ORCID_NUMBER not in body_text and ORCID_NUMBER not in page.url:
            browser.close()

            raise RuntimeError(
                "Expected ORCID identifier was not found in the loaded page. "
                f"Loaded URL: {page.url}"
            )

        click_expandable_controls(page)
        page.wait_for_timeout(1500)

        # ---------------------------------------------------------------------
        # Header
        # ---------------------------------------------------------------------
        extracted_data["Name"] = locator_text(
            page.locator(CSS["profile_name"]).first
        )

        displayed_orcid = locator_text(
            page.locator(CSS["orcid_id"]).first
        )

        orcid_match = re.search(
            r"(\d{4}-\d{4}-\d{4}-[\dX]{4})",
            displayed_orcid,
            flags=re.IGNORECASE,
        )

        if orcid_match:
            extracted_data["ORCID_number"] = orcid_match.group(1)

        if not extracted_data["Name"]:
            browser.close()

            raise RuntimeError(
                "The profile page loaded, but Name could not be extracted."
            )

        # ---------------------------------------------------------------------
        # Sidebar
        #
        # Countries / Keywords / Keywords_pt were already extracted through
        # the official ORCID API and are intentionally not overwritten here.
        # ---------------------------------------------------------------------
        extracted_data["Websites_and_social_links"] = extract_websites(page)
        extracted_data["Other_IDs"] = extract_other_ids(page)
        extracted_data["Emails_and_domains"] = extract_emails_and_domains(page)

        # ---------------------------------------------------------------------
        # Employment
        # ---------------------------------------------------------------------
        extracted_data["Employment"] = extract_employment(page)

        # ---------------------------------------------------------------------
        # Education / Qualifications
        # ---------------------------------------------------------------------
        education, qualifications = extract_education_and_qualifications(page)

        extracted_data["Education"] = education
        extracted_data["Qualifications"] = qualifications

        # ---------------------------------------------------------------------
        # Works
        # ---------------------------------------------------------------------
        extracted_data["Works"] = extract_works(page)

        # ---------------------------------------------------------------------
        # Other sections
        # ---------------------------------------------------------------------
        extracted_data["Professional_Activities"] = (
            extract_professional_activities(page)
        )

        extracted_data["Funding"] = extract_funding(page)
        extracted_data["Peer_Review"] = extract_peer_reviews(page)

        verify_section_counts(page, extracted_data)

        browser.close()

    extracted_data["refreshed"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    return extracted_data


# =============================================================================
# EXECUTION
# =============================================================================

if __name__ == "__main__":
    try:
        extracted_data = scrape_orcid_profile()

        validate(instance=extracted_data, schema=schema)

        # Save to the JSON file
        file_name = "fetch_data/ORCID_data.json"

        # Ensure the output directory exists.
        Path(file_name).parent.mkdir(parents=True, exist_ok=True)

        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(
                extracted_data,
                f,
                indent=2,
                ensure_ascii=False,
            )

        print(
            f"Successfully saved ORCID profile for "
            f"{extracted_data['Name']} to {file_name}"
        )

    except Exception as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "target_url": TARGET_URL,
                    "api_record_url": API_RECORD_URL,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise
