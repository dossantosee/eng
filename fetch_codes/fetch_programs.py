import os
import json
import time
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

BASE = "https://busca.inpi.gov.br/pePI"
DELAY = 3
DATA_FILE = "fetch_data/program_data.json"


def create_session():
    """Step 1: GET login page to receive session cookie, then wait 3s."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    print("Step 1: Getting session cookie from login page...")
    session.get(f"{BASE}/servlet/LoginController?action=login", timeout=15)
    print(f"  Waiting {DELAY}s...")
    time.sleep(DELAY)

    print("Step 2: Loading computer programs search page...")
    session.get(f"{BASE}/jsp/programas/ProgramaSearchBasico.jsp", timeout=15)
    print(f"  Waiting {DELAY}s...")
    time.sleep(DELAY)

    return session


def search_programs(
    session,
    query: str,
    column: str = "AutorPrograma",
    forma: str = "todasPalavras",
    per_page: int = 100
):
    print(f"\nSearching programs: '{query}' | column={column} | mode={forma}")

    payload = (
        "Action=SearchBasico"
        "&NumPedido="
        "&NumGru="
        "&NumProtocolo="
        f"&FormaPesquisa={forma}"
        f"&ExpressaoPesquisa={requests.utils.quote(query)}"
        f"&Coluna={column}"
        f"&RegisterPerPage={per_page}"
        "&botao=+pesquisar+%BB+"
    )

    response = session.post(
        f"{BASE}/servlet/ProgramaServletController",
        data=payload.encode("latin-1"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60
    )
    response.raise_for_status()
    response.encoding = "ISO-8859-1"

    print(f"  Response: {response.status_code} | {len(response.text)} chars")
    print(f"  Waiting {DELAY}s...")
    time.sleep(DELAY)

    return response.text


def get_pagination_info(html: str):
    soup = BeautifulSoup(html, "html.parser")
    pagination_font = None
    for font in soup.find_all("font", class_="normal"):
        if "Páginas de Resultados" in font.get_text():
            pagination_font = font
            break

    if not pagination_font:
        return None, None

    bold = pagination_font.find("b")
    current_page = int(bold.get_text(strip=True)) if bold else 1

    page_numbers = []
    for a in pagination_font.find_all("a", href=True):
        text = a.get_text(strip=True)
        if text.isdigit():
            page_numbers.append(int(text))

    page_numbers.append(current_page)
    max_page = max(page_numbers) if page_numbers else current_page

    return current_page, max_page


def fetch_page(session, page_number: int):
    url = f"{BASE}/servlet/ProgramaServletController?Action=nextPage&Page={page_number}"
    print(f"\n  Fetching page {page_number}: {url}")

    response = session.get(url, timeout=15)
    response.raise_for_status()
    response.encoding = "ISO-8859-1"

    print(f"  Response: {response.status_code} | {len(response.text)} chars")
    print(f"  Waiting {DELAY}s...")
    time.sleep(DELAY)

    return response.text


def parse_results(html: str):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    table = None
    for t in soup.find_all("table"):
        if t.find("td", string=lambda s: s and "Pedido" in s):
            table = t
            break
        headers = [th.get_text(strip=True) for th in t.find_all("td")]
        if "Pedido" in headers and "Título" in headers:
            table = t
            break

    if not table:
        return results

    rows = table.find_all("tr")[1:]
    for row in rows:
        cols = row.find_all("td")
        if not cols or len(cols) < 3:
            continue

        anchor = cols[0].find("a")
        cod_pedido = ""
        if anchor:
            href = anchor.get("href", "")
            if "CodPedido=" in href:
                cod_pedido = href.split("CodPedido=")[1].split("&")[0]

        results.append({
            "application": cols[0].get_text(strip=True),
            "date":        cols[1].get_text(strip=True),
            "title":       cols[2].get_text(strip=True),
            "code":        cod_pedido
        })

    return results


def fetch_all_results(session, query, column="AutorPrograma",
                      forma="todasPalavras", per_page=100):
    print("\n── Page 1 (initial POST search) ────────────────────────────────────")
    html = search_programs(session, query, column, forma, per_page)
    all_results = parse_results(html)
    print(f"  Page 1 → {len(all_results)} records.")

    current_page, max_page = get_pagination_info(html)

    if max_page is None or max_page <= 1:
        print("\n  Single page of results. Done.")
        return all_results

    print(f"\n  Pagination detected: current={current_page}, max={max_page}")
    print(f"  Will fetch pages 2 → {max_page}.")

    for page_num in range(2, max_page + 1):
        print(f"\n── Page {page_num} of {max_page} ──────────────────────────────────────────")
        html = fetch_page(session, page_num)
        page_results = parse_results(html)

        if not page_results:
            print(f"  Page {page_num} returned no records. Stopping early.")
            break

        all_results.extend(page_results)
        print(f"  Page {page_num} → {len(page_results)} new records | "
              f"{len(all_results)} total so far.")

    return all_results


def load_existing_data(filepath: str) -> list:
    """Load existing JSON data from disk, return empty list if missing or invalid."""
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


def merge_refresh_timestamps(new_results: list, existing_data: list) -> list:
    """
    Stamp every new record with the current UTC datetime.
    If a record already existed before, carry its previous refresh value
    only as a fallback — here we always overwrite since the fetch succeeded.
    """
    # Build a lookup of previous refresh values keyed by application number
    previous_refresh: dict = {
        item["application"]: item.get("refresh", "")
        for item in existing_data
        if isinstance(item, dict) and "application" in item
    }

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stamped = []
    for record in new_results:
        record["refresh"] = now_str          # always mark as freshly fetched
        _ = previous_refresh.get(record["application"])  # available if needed
        stamped.append(record)

    return stamped


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


def print_results(results: list):
    if not results:
        print("  No results found.")
        return
    print(f"\n  {'='*70}")
    print(f"  Found {len(results)} result(s):")
    print(f"  {'='*70}")
    for i, r in enumerate(results, 1):
        print(f"\n  [{i:03d}] Application: {r['application']}")
        print(f"        Date:        {r['date']}")
        print(f"        Title:       {r['title']}")
        print(f"        Title (EN): {r.get('title_en')}")
        print(f"        Code:        {r['code']}")
        print(f"        Refresh:     {r.get('refresh', 'n/a')}")
    print(f"\n  {'='*70}")
    print(f"  Total: {len(results)} records")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":

    query = os.environ.get("USERNUMBER", "")
    if not query:
        print("Warning: USERNUMBER environment variable is not set. Exiting.")
        # Exit 0 so the workflow does not fail
        exit(0)

    # ── Always load what is already on disk first ─────────────────────────────
    existing_data = load_existing_data(DATA_FILE)

    # ── Attempt the fetch — catch every possible error gracefully ─────────────
    try:
        session = create_session()
        new_results = fetch_all_results(
            session,
            query=query,
            column="CpfCnpjAutorPrograma",
            forma="todasPalavras",
            per_page=20
        )
    except Exception as exc:
        # Network error, timeout, HTTP error, parse crash — anything
        print(f"\n⚠️  Fetch failed: {exc}")
        print("  Keeping existing data unchanged. No file will be written.")
        # Exit 0: workflow stays green, file is untouched
        exit(0)

    # ── Only proceed if we actually got something back ────────────────────────
    if not new_results:
        print("\n⚠️  Fetch returned zero records.")
        print("  Keeping existing data unchanged. No file will be written.")
        exit(0)

    # ── Stamp records ──────────────────────────────────────────────────────────
    stamped_results = merge_refresh_timestamps(new_results, existing_data)

    # ── Translate titles pt-BR -> en, adding 'title_en' to each record ────────
    print("\nTranslating titles to English...")
    stamped_results = add_translations(stamped_results)

    print_results(stamped_results)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(stamped_results, f, indent=4, ensure_ascii=False)

    print(f"\n✅  Successfully saved {len(stamped_results)} records to '{DATA_FILE}'.")
