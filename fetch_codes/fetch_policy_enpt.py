"""
fetch_policy_enpt.py

Reads PRIVACY.md (English) from the repository root, translates the prose
content to Brazilian Portuguese using deep-translator (Google Translate),
and writes/updates PRIVACY_BR.md — while preserving markdown/HTML syntax,
badges, links, and URLs untouched.

Intended to run via GitHub Actions whenever PRIVACY.md changes.
"""

import re
import sys
from pathlib import Path

from deep_translator import GoogleTranslator

# --- Paths (relative to repository root) ---
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILE = REPO_ROOT / "PRIVACY.md"
TARGET_FILE = REPO_ROOT / "PRIVACY_BR.md"

translator = GoogleTranslator(source="en", target="pt")


def protect_markdown(line):
    """
    Replace non-translatable markdown/HTML pieces with placeholders.
    Returns (masked_line, placeholder_map).
    """
    placeholders = {}
    counter = [0]

    def stash(match):
        key = f"§{counter[0]}§"
        placeholders[key] = match.group(0)
        counter[0] += 1
        return key

    # 1. HTML comments
    line = re.sub(r"<!--.*?-->", stash, line, flags=re.DOTALL)

    # 2. HTML tags
    line = re.sub(r"<[^>]+>", stash, line)

    # 3. Badge-style links: [![alt](img_url)](link_url) -> mask ENTIRE thing FIRST
    #    (must run before the plain image/link regexes below)
    line = re.sub(r"\[!\[[^\]]*\]\([^)]+\)\]\([^)]+\)", stash, line)

    # GitHub alert markers: [!NOTE] [!TIP] [!IMPORTANT] [!WARNING] [!CAUTION]
    # These must stay in English exactly or GitHub stops rendering the alert box.
    line = re.sub(
        r"\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]",
        stash,
        line,
        flags=re.IGNORECASE
    )

    # 4. Plain images: ![alt](url) -> translate alt, keep url
    def image_sub(m):
        alt, url = m.group(1), m.group(2)
        alt_translated = translator.translate(alt) if alt.strip() else alt
        key = f"§{counter[0]}§"
        placeholders[key] = f"![{alt_translated}]({url})"
        counter[0] += 1
        return key

    line = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_sub, line)

    # 5. Normal links: [text](url) -> translate text, keep url
    def link_sub(m):
        text, url = m.group(1), m.group(2)
        text_translated = translator.translate(text) if text.strip() else text
        key = f"§{counter[0]}§"
        placeholders[key] = f"[{text_translated}]({url})"
        counter[0] += 1
        return key

    line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_sub, line)

    # 6. Bare URLs
    line = re.sub(r"https?://\S+", stash, line)

    return line, placeholders


def restore_placeholders(text, placeholders):
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def is_pure_syntax(line):
    """Lines we should never send to the translator at all."""
    stripped = line.strip()
    return (
        stripped == ""
        or stripped in ("---", "```")
        or stripped.startswith("<!--")
        or stripped.startswith("-->")
        or stripped.startswith("<meta")
        or stripped.startswith("[TDM-Reservation")
        or stripped.startswith("[NoAI")
        or stripped.startswith("[NoML")
    )


def translate_line(line):
    if is_pure_syntax(line):
        return line

    # Split off leading markdown markers (#, ##, >, *, -, 1., etc.)
    match = re.match(r"^(\s*(?:#{1,6}|>|\*|-|\d+\.)\s*)?(.*)$", line)
    prefix, rest = match.group(1) or "", match.group(2)

    if not rest.strip():
        return line

    masked, placeholders = protect_markdown(rest)

    # Don't call the API on strings that are now empty/whitespace/placeholders only
    if masked.strip() == "" or re.fullmatch(r"(§\d+§\s*)+", masked.strip()):
        translated = masked
    else:
        translated = translator.translate(masked)

    translated = restore_placeholders(translated, placeholders)
    return f"{prefix}{translated}"


def main():
    if not SOURCE_FILE.exists():
        print(f"ERROR: source file not found: {SOURCE_FILE}", file=sys.stderr)
        sys.exit(1)

    markdown_text = SOURCE_FILE.read_text(encoding="utf-8")

    translated_lines = [translate_line(line) for line in markdown_text.split("\n")]
    translated_text = "\n".join(translated_lines)

    TARGET_FILE.write_text(translated_text, encoding="utf-8")
    print(f"Translated '{SOURCE_FILE.name}' -> '{TARGET_FILE.name}'")


if __name__ == "__main__":
    main()
