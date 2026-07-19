"""
Price Monitor
-------------
Checks a list of product pages for price drops and emails you when one happens.

How it works, step by step:
1. Read the list of products to watch from config.json
2. For each product, download the page and pull out the price using a CSS selector
3. Compare the new price to the last price we saved (in price_history.json)
4. If the price dropped, add it to a list of "deals" to email about
5. Save the new prices back to price_history.json
6. If there were any deals, send one email listing all of them

Run this manually with:  python price_monitor.py
Later, we'll set your computer to run it automatically on a schedule.
"""

import json
import os
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# --- File locations -----------------------------------------------------
# These are just plain files sitting next to this script.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "price_history.json")


# --- Step 1: load config -------------------------------------------------
def load_config():
    """
    config.json holds the list of products and your email settings.

    For email credentials specifically, environment variables take priority
    over what's in config.json if they're set. This lets you run the script
    two ways:
      - Locally: just fill in config.json directly, nothing else needed.
      - On GitHub Actions: leave the sensitive fields in config.json blank
        (or with placeholder text) and set them as encrypted GitHub Secrets
        instead (SENDER_EMAIL, SENDER_APP_PASSWORD, RECIPIENT_EMAIL) — this
        avoids putting your real email password in a file that gets pushed
        to GitHub, even in a private repo.
    """
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    env_overrides = {
        "sender_email": os.environ.get("SENDER_EMAIL"),
        "sender_app_password": os.environ.get("SENDER_APP_PASSWORD"),
        "recipient_email": os.environ.get("RECIPIENT_EMAIL"),
    }
    for key, value in env_overrides.items():
        if value:  # only override if the env var is actually set
            # recipient_email may be a comma-separated list in the env var
            if key == "recipient_email" and "," in value:
                value = [addr.strip() for addr in value.split(",")]
            config["email"][key] = value

    return config


# --- Step 2: load / save price history -----------------------------------
def load_history():
    """price_history.json remembers the last price we saw for each product.
    If the file doesn't exist yet (first run), we start with an empty dict."""
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# --- Alternative extraction method: JSON-LD structured data --------------
def fetch_price_from_jsonld(soup):
    """
    Many e-commerce sites embed a <script type="application/ld+json"> block
    containing structured product data (used by Google/search engines).
    This is often more reliable than scraping visible text, because:
      - it's present in the raw HTML even if the visible price is filled
        in later by JavaScript (e.g. only after selecting a size)
      - it's a clean number, no currency symbols or discount badges to strip
      - it's less likely to break when the site's CSS/design changes

    Looks for a schema.org Product with an "offers" price and returns it.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        # Some sites wrap multiple objects in a list
        candidates = data if isinstance(data, list) else [data]

        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue

            offers = item.get("offers")
            if not offers:
                continue

            # offers can be a single Offer dict, or an AggregateOffer with
            # lowPrice/highPrice. Prefer the lowest price available.
            if isinstance(offers, dict):
                if "lowPrice" in offers:
                    return float(offers["lowPrice"])
                if "price" in offers:
                    return float(offers["price"])

    raise ValueError("No JSON-LD Product price found on this page.")


# --- Number parsing helper: handles different regional formats -----------
def parse_localized_number(raw_text):
    """
    Converts a price string into a float, correctly handling different
    regional number formats:
      - Hungarian style:  "44.990"     (dot = thousands separator, no decimals)
      - European style:   "1 778,39"   (space = thousands, comma = decimal)
      - Plain style:      "24280"      (just digits)

    The key trick: a comma or dot is treated as a DECIMAL separator only if
    it's followed by exactly 2 digits at the very end of the number (e.g.
    ",39" or ".39"). Otherwise, it's treated as a thousands separator and
    removed. This correctly tells apart "1.778,39" (thousands dot + decimal
    comma) from "44.990" (thousands dot only, no decimal part).
    """
    # Strip currency symbols/words, whitespace variants, and known trailing
    # words some sites append (e.g. Hungarian "-tól"/"-től" = "starting from").
    cleaned = (
        raw_text.strip()
        .replace("€", "")
        .replace("$", "")
        .replace("£", "")
        .replace("Ft", "")
        .replace("FT", "")
        .replace("ft", "")
        .replace("\xa0", "")  # non-breaking space, common on price tags
        .replace(" ", "")
        .replace("-tól", "")
        .replace("-től", "")
        .strip()
    )

    if re.search(r",\d{2}$", cleaned):
        # Comma is the decimal separator (e.g. "1.778,39" or "1778,39").
        # Any remaining dots are thousands separators — remove them.
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.search(r"\.\d{2}$", cleaned):
        # Dot is the decimal separator (e.g. "1,778.39" or "1778.39").
        # Any remaining commas are thousands separators — remove them.
        cleaned = cleaned.replace(",", "")
    else:
        # No clear decimal part (e.g. "44.990" or "24,990") — both dots
        # and commas here are thousands separators, so strip them all.
        cleaned = cleaned.replace(",", "").replace(".", "")

    return float(cleaned)


# --- Step 3: fetch and parse a price from a page --------------------------
def fetch_price(url, selectors, badge_selectors=None):
    """
    Downloads the page at `url` and extracts the price.

    `selectors` can be a single CSS selector string, OR a list of selectors
    tried in order. This lets you handle sites where the price element's
    class changes depending on state — e.g. a "discounted price" class when
    on sale, falling back to the normal "price" class when the sale ends:

        "selector": [
            ".price-t02__actual-price--discounted",
            ".price-t02__actual-price"
        ]

    The first selector that matches something on the page is used.

    `badge_selectors` (optional) is a list of selectors for nested elements
    to strip out before reading the price text — e.g. a discount badge like
    "-18%" sitting inside the price div, which would otherwise get glued
    onto the price text.
    """
    headers = {
        # Some sites block requests that don't look like a real browser.
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()  # raises an error if the request failed

    soup = BeautifulSoup(response.text, "html.parser")

    # Special case: "jsonld" tells us to read structured product data
    # instead of scraping a CSS selector (see fetch_price_from_jsonld above).
    if selectors == "jsonld":
        price = fetch_price_from_jsonld(soup)
        return price, "jsonld"

    # Normalize: allow a single selector string or a list of fallbacks.
    if isinstance(selectors, str):
        selectors = [selectors]

    element = None
    matched_selector = None
    for sel in selectors:
        found = soup.select_one(sel)
        if found is not None:
            element = found
            matched_selector = sel
            break  # stop at the first selector that matches

    if element is None:
        raise ValueError(
            f"Could not find price element on {url}. Tried selectors: {selectors}. "
            "The selectors might be wrong, or the site changed its layout."
        )

    # Remove any nested elements (like a discount badge) so their text
    # doesn't get mixed into the price, e.g. "44.990 FT-18%".
    if badge_selectors:
        for badge_sel in badge_selectors:
            for badge in element.select(badge_sel):
                badge.decompose()

    # Safety net: even without knowing the exact badge class, discount
    # badges almost always contain a "%" sign (e.g. "-18%"), while the
    # actual price text never does. Pull out each separate piece of text
    # inside the element and drop any piece containing "%".
    text_pieces = [t for t in element.stripped_strings if "%" not in t]
    raw_text = " ".join(text_pieces)

    price = parse_localized_number(raw_text)
    return price, matched_selector


# --- Step 4: send an email -------------------------------------------------
def send_email(subject, body, email_config):
    """
    Sends the notification email. Two methods are supported:
    1. Resend (HTTP API) - used automatically if RESEND_API_KEY is present.
    2. Gmail SMTP - fallback if no Resend API key is found.
    """
    resend_api_key = os.environ.get("RESEND_API_KEY")
    sender_email = os.environ.get("SENDER_EMAIL") or email_config.get("sender_email")
    recipient_env = os.environ.get("RECIPIENT_EMAIL")

    if recipient_env:
        recipients = [r.strip() for r in recipient_env.split(",") if r.strip()]
    else:
        recipients = email_config.get("recipient_email")

    if isinstance(recipients, str):
        recipients = [recipients]

    if resend_api_key:
        print("RESEND_API_KEY detected. Routing email through Resend API...")
        _send_email_resend(subject, body, {"sender_email": sender_email}, recipients, resend_api_key)
    else:
        print("No RESEND_API_KEY found. Falling back to SMTP...")
        smtp_config = email_config.copy()
        smtp_config["sender_email"] = sender_email
        smtp_config["sender_app_password"] = os.environ.get("SENDER_APP_PASSWORD") or email_config.get("sender_app_password")
        _send_email_smtp(subject, body, smtp_config, recipients)


def _send_email_resend(subject, body, email_config, recipients, api_key):
    """Send via Resend's HTTP API (https://resend.com) — no SMTP login involved."""
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": email_config["sender_email"],
            "to": recipients,
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Resend API error {response.status_code}: {response.text}")


def _send_email_smtp(subject, body, email_config, recipients):
    """Send via Gmail (or other) SMTP server — fine for local runs."""
    msg = MIMEMultipart()
    msg["From"] = email_config["sender_email"]
    msg["To"] = ", ".join(recipients)  # just for display in the email header
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP(email_config["smtp_server"], email_config["smtp_port"]) as server:
        server.starttls(context=context)
        server.login(email_config["sender_email"], email_config["sender_app_password"])
        server.sendmail(email_config["sender_email"], recipients, msg.as_string())


# --- Formatting helper: guess currency by price magnitude -----------------
def format_price(price):
    """
    Formats a price for display in the email, guessing the currency from
    the magnitude of the number:
      - price > 10000  -> shown in forint (Ft)
      - price <= 10000 -> shown in euros (€)

    NOTE: this is a magnitude-based guess, not read from the page itself —
    double check this matches your actual products. If it's backwards for
    your case, just flip the comparison below.
    """
    if price is None:
        return "price unknown"
    if price > 10000:
        return f"{price:,.0f} Ft".replace(",", " ")
    else:
        return f"{price:,.0f} €".replace(",", " ")


# --- Main logic --------------------------------------------------------
def main():
    config = load_config()
    history = load_history()
    deals = []  # will hold text lines about any price drops we find
    product_lines = []  # name + url + current/last-known price, for every product

    for product in config["products"]:
        name = product["name"]
        url = product["url"]
        selector = product["selector"]  # string OR list of fallback selectors
        badge_selectors = product.get("badge_selectors")  # optional

        print(f"Checking: {name}...")

        try:
            current_price, matched_selector = fetch_price(url, selector, badge_selectors)
        except Exception as e:
            # IMPORTANT: on failure we do NOT touch history[url] at all.
            # The previously saved price stays exactly as it was, so a
            # temporary layout change (e.g. sale ending, page hiccup)
            # never wipes out or corrupts what we last successfully read.
            previous = history.get(url, {}).get("last_price")
            print(f"  Could not read price this run ({e}). Keeping previous price: {previous if previous is not None else 'none saved yet'}")
            product_lines.append(f"{name}\n{url}\n{format_price(previous)}")
            continue

        previous_price = history.get(url, {}).get("last_price")

        print(f"  Current price: {current_price}  (matched selector: {matched_selector})  |  Previous: {previous_price}")

        if previous_price is not None and current_price < previous_price:
            deals.append(
                f"- {name}: {previous_price} -> {current_price}\n  {url}"
            )

        # Only update history when we successfully read a price this run.
        history[url] = {"name": name, "last_price": current_price}
        product_lines.append(f"{name}\n{url}\n{format_price(current_price)}")

    save_history(history)

    # Build the "all watched products" list, shown in every email —
    # now including each product's current (or last-known) price.
    watched_list = "\n\n".join(product_lines)

    if deals:
        subject = f"Price drop alert! {len(deals)} product(s) got cheaper"
        body = (
            "The following products dropped in price:\n\n"
            + "\n\n".join(deals)
            + "\n\n---\n\nAll watched products:\n\n"
            + watched_list
        )
    else:
        subject = "Price check complete — no drops this run"
        body = (
            "No price drops this run.\n\n---\n\nAll watched products:\n\n"
            + watched_list
        )

    send_email(subject, body, config["email"])
    print(f"\nSent email. Price drop(s) this run: {len(deals)}")


if __name__ == "__main__":
    main()
