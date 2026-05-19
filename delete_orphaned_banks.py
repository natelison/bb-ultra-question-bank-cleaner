import time
import csv
import re
import os
import json
from datetime import datetime
from getpass import getpass
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Config — loads from secrets.toml if present, otherwise prompts
# ---------------------------------------------------------------------------
SECRETS_FILE = "secrets.toml"

def load_config():
    if os.path.exists(SECRETS_FILE):
        import tomllib
        with open(SECRETS_FILE, "rb") as f:
            secrets = tomllib.load(f)
        return (
            secrets["blackboard_admin"]["base_url"],
            secrets["blackboard_admin"]["username"],
            secrets["blackboard_admin"]["password"],
        )
    else:
        print("\nNo secrets.toml found — enter credentials manually.")
        base_url = input("  Blackboard base URL (e.g. https://learn.example.edu): ").strip().rstrip("/")
        username = input("  Username: ").strip()
        password = getpass("  Password: ")
        return base_url, username, password

BB_BASE, USERNAME, PASSWORD = load_config()

DRY_RUN          = False   # Set False to actually click Delete
HEADLESS         = True    # Set True once confirmed working
DELAY_SECONDS    = 0.5     # Pause between actions
LOG_FILE         = "bank_deletion_log.csv"
SKIP_ASSOCIATED  = True    # Skip banks that are linked to a test (can't delete anyway)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FIELDS = ["timestamp", "course_pk", "bank_name", "action", "note"]

def append_log(log_rows: list[dict]):
    write_header = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(log_rows)

def log_entry(course_pk, bank_name, action, note=""):
    return {
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "course_pk":  course_pk,
        "bank_name":  bank_name,
        "action":     action,
        "note":       note,
    }

# ---------------------------------------------------------------------------
# Bank page helpers
# ---------------------------------------------------------------------------

def login(page):
    print(f"\n[Login] Navigating to {BB_BASE}/webapps/login/login.jsp")
    page.goto(f"{BB_BASE}/webapps/login/login.jsp")
    page.wait_for_load_state("domcontentloaded")

    try:
        page.wait_for_selector("#agree_button", timeout=5000)
        page.click("#agree_button")
        page.wait_for_load_state("domcontentloaded")
        print("    Cookie consent dismissed.")
    except PlaywrightTimeoutError:
        pass

    page.fill("input#user_id", USERNAME)
    page.fill("input#password", PASSWORD)
    page.click("input[type='submit']")
    page.wait_for_load_state("domcontentloaded", timeout=15000)

    if "/webapps/login" in page.url.lower():
        raise RuntimeError("Login failed — check credentials in secrets.toml")
    print("    Logged in successfully.")


def wait_for_banks_page(page, course_pk):
    """
    Wait for the banks page to be ready using stable DOM landmarks.
    - #question-banks-list-view-table  → table is present (has banks)
    - .js-pagination-container         → pagination bar
    - .js-items-empty-state            → no banks at all
    - [aria-live='polite'][role='status'] → fallback
    """
    try:
        page.wait_for_selector(
            "#question-banks-list-view-table, "
            ".js-pagination-container, "
            ".js-items-empty-state, "
            "[aria-live='polite'][role='status']",
            timeout=15000
        )
    except PlaywrightTimeoutError:
        print(f"    Warning: Banks page landmarks not found for {course_pk} — continuing anyway.")


def is_empty_state(page) -> bool:
    """Return True if the page is showing the 'no question banks' empty state."""
    return page.query_selector(".js-items-empty-state") is not None


def goto_banks_page(page, course_pk, page_num=1):
    """Navigate to the Question Banks page for a course."""
    url = f"{BB_BASE}/ultra/courses/{course_pk}/outline/banks"
    page.goto(url)
    # Do NOT use wait_for_load_state("networkidle") — Ultra never idles
    wait_for_banks_page(page, course_pk)

    # Pagination is handled externally via click_next_page()


def click_next_page(page) -> bool:
    """
    Click the Next Page button and wait for the row content to change.
    The table element itself never detaches (React swaps rows in-place,
    no XHR — it's all client-side from already-loaded data), so we detect
    the page change by watching the aria-label of the first action button.
    """
    try:
        btn = page.wait_for_selector(
            "button.js-pagination-page-up-button[aria-label='Next Page']",
            timeout=4000
        )
        if not btn:
            return False

        # Snapshot the first row's bank name before clicking
        first_buttons = page.query_selector_all(
            "[data-analytics-id='questionBanks.table.actions']"
        )
        old_first_label = (
            first_buttons[0].get_attribute("aria-label") if first_buttons else None
        )

        btn.click()

        # Poll until the first button's aria-label changes (new page rendered)
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.25)
            new_buttons = page.query_selector_all(
                "[data-analytics-id='questionBanks.table.actions']"
            )
            if not new_buttons:
                continue
            new_first_label = new_buttons[0].get_attribute("aria-label")
            if new_first_label != old_first_label:
                time.sleep(DELAY_SECONDS)
                return True

        # Timed out — but we did click, so return True and let collect handle it
        print("    Warning: Could not confirm page content changed after Next click.")
        return True

    except PlaywrightTimeoutError:
        pass
    return False


def get_total_bank_count(page) -> int:
    """
    Read the total bank count from the '1-25 of 151' info element.
    Class: MuiTypographysubtitle2  inside makeStylespagingHeader
    Much faster than paginating through all pages to count rows.
    Returns 0 if not found.
    """
    try:
        el = page.query_selector(
            "[class*='MuiTypographysubtitle2'][class*='MuiTypographyroot']"
        )
        if el:
            text = el.inner_text().strip()  # e.g. "1-25 of 151"
            m = re.search(r'of\s+(\d+)', text)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0


def get_total_pages(page) -> int:
    """
    Read total pages from the pagination container.
    Your screenshot shows 'Page [1▼] of 7' — parse the 'of N' text.
    """
    try:
        text = page.inner_text(".js-pagination-container")
        m = re.search(r'of\s+(\d+)', text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    # Fallback: count <option> elements in the pagination select
    try:
        options = page.query_selector_all(".js-pagination-container select option")
        if options:
            return len(options)
    except Exception:
        pass
    return 1


def collect_bank_names(page) -> list[str]:
    """
    Return bank names visible on the current page.
    Names only — no stored element handles (they go stale after any re-render).
    """
    buttons = page.query_selector_all("[data-analytics-id='questionBanks.table.actions']")
    names = []
    for btn in buttons:
        aria = btn.get_attribute("aria-label") or ""
        name = re.sub(r"^More options for\s+", "", aria).strip()
        if name:
            names.append(name)
    return names

def css_escape_attr(value: str) -> str:
    """Escape a string for use inside a double-quoted CSS attribute selector value."""
    return (value
            .replace("\\", "\\\\")
            .replace('"',  '\\"')
            .replace("\n", "\\n")
            .replace("]",  "\\]"))


def try_delete_bank(page, bank_name: str, course_pk: str, dry_run: bool) -> dict:
    """
    Finds the '...' button for bank_name fresh from the DOM (no stale handles),
    opens the menu, and clicks Delete + confirms.
    """
    # Use double-quoted attr selector with escaped bank name to handle apostrophes
    safe_label = css_escape_attr(f"More options for {bank_name}")
    btn = page.query_selector(
        f'[data-analytics-id="questionBanks.table.actions"]'
        f'[aria-label="{safe_label}"]'
    )
    if not btn:
        # Fallback: just grab the first available button on the page
        btns = page.query_selector_all("[data-analytics-id='questionBanks.table.actions']")
        btn = btns[0] if btns else None

    if not btn:
        return log_entry(course_pk, bank_name, "SKIPPED", "Button not found in DOM")

    # Open the context menu
    btn.click()
    time.sleep(0.3)

    # Wait for the Delete menu item
    try:
        delete_item = page.wait_for_selector(
            "li[role='menuitem']:has-text('Delete'), "
            "[class*='MuiMenuItem']:has-text('Delete'), "
            "span.primary-text:text-is('Delete')",
            timeout=4000
        )
    except PlaywrightTimeoutError:
        page.keyboard.press("Escape")
        time.sleep(0.2)
        return log_entry(course_pk, bank_name, "SKIPPED", "No delete option in menu (associated with test?)")

    if dry_run:
        page.keyboard.press("Escape")
        time.sleep(0.2)
        return log_entry(course_pk, bank_name, "DRY_RUN", "Delete option present")

    # Click Delete
    delete_item.click()
    time.sleep(0.4)

    # Confirm dialog — two possible modals:
    #   A) "Delete Question Bank?" with a Delete button  → deletable
    #   B) "You can't delete question banks that contain linked questions." with Close only → linked
    try:
        # Wait for whichever modal button appears first
        modal_btn = page.wait_for_selector(
            "[data-analytics-id='questionBanks.table.actions.delete.confirmation.delete.close'], "
            "button:has-text('Delete'):not([data-analytics-id='questionBanks.table.actions'])",
            timeout=5000
        )
    except PlaywrightTimeoutError:
        page.keyboard.press("Escape")
        return log_entry(course_pk, bank_name, "ERROR", "Confirmation dialog not found")

    # Check which modal we got by inspecting the button
    analytics_id = modal_btn.get_attribute("data-analytics-id") or ""
    btn_text = (modal_btn.inner_text() or "").strip()

    if "close" in analytics_id or btn_text == "Close":
        # Linked-questions modal — can't delete this one
        modal_btn.click()
        time.sleep(0.3)
        return log_entry(course_pk, bank_name, "SKIPPED", "Has linked questions — cannot delete")

    # Normal delete confirmation
    modal_btn.click()
    time.sleep(1.0)
    return log_entry(course_pk, bank_name, "DELETED")


# ---------------------------------------------------------------------------
# Per-course processing
# ---------------------------------------------------------------------------

def process_course(page, course_pk: str, global_counter: list) -> list[dict]:
    """
    global_counter is a two-element list [current, total] shared across all courses.
    """
    print(f"\n{'='*55}")
    print(f"  Course: {course_pk}")
    print(f"{'='*55}")

    goto_banks_page(page, course_pk, page_num=1)

    # Check for empty state
    if is_empty_state(page):
        print("    No question banks found — skipping.")
        return []

    total_pages = get_total_pages(page)
    print(f"    Pages: {total_pages}")

    # Read total count directly from the '1-25 of N' element — no pagination needed
    course_total = get_total_bank_count(page)
    if course_total == 0:
        # Fallback: if count element not found, estimate from pages
        course_total = total_pages * 25
        print(f"    Total banks in course: ~{course_total} (estimated)")
    else:
        print(f"    Total banks in course: {course_total}")
    global_counter[1] += course_total

    logs = []
    current_page = 1
    course_idx = 0
    skip_names = []  # names already tried and not deleted on the current page

    while True:
        page_names = collect_bank_names(page)
        if not page_names:
            # Page is empty — advance
            skip_names = []
            current_page += 1
            if current_page > total_pages:
                break
            if not click_next_page(page):
                print(f"    Could not advance to page {current_page} — stopping.")
                break
            continue

        # Find first name not yet skipped on this page
        remaining = [n for n in page_names if n not in skip_names]

        if not remaining:
            # Every bank on this page is skipped — advance
            skip_names = []
            current_page += 1
            total_pages = get_total_pages(page)
            if current_page > total_pages:
                break
            if not click_next_page(page):
                print(f"    Could not advance to page {current_page} — stopping.")
                break
            continue

        bank_name = remaining[0]
        course_idx += 1
        global_counter[0] += 1

        entry = try_delete_bank(page, bank_name, course_pk, dry_run=DRY_RUN)
        logs.append(entry)
        time.sleep(DELAY_SECONDS)

        action_label = {
            "DELETED": "Deleted",
            "DRY_RUN": "Dry run",
            "SKIPPED": "Skipped",
            "ERROR":   "Error  ",
        }.get(entry["action"], entry["action"])
        print(f"  [{action_label}] [{course_idx}/{course_total}] Bank: '{bank_name}'")

        if entry["action"] in ("DRY_RUN", "SKIPPED", "ERROR"):
            # Not deleted — skip past it so we don't retry the same bank
            skip_names.append(bank_name)
        elif entry["action"] == "DELETED":
            # Row removed — check if course is now fully empty
            if is_empty_state(page):
                print("  All banks deleted.")
                break
            # Otherwise re-scrape same page (next item shifts to top)
            total_pages = get_total_pages(page)

    return logs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    course_pk = input("\nEnter the course PK1 (e.g. _128468_1): ").strip()
    if not re.match(r"^_\d+_1$", course_pk):
        print(f"  Error: '{course_pk}' doesn't look like a valid PK1. Expected format: _XXXXXX_1")
        return

    print(f"\nBank Deletion Script")
    print(f"  DRY_RUN  : {DRY_RUN}")
    print(f"  HEADLESS : {HEADLESS}")
    print(f"  Course   : {course_pk}")
    print(f"  Log file : {LOG_FILE}")

    if DRY_RUN:
        print("\n  *** DRY RUN — no banks will actually be deleted ***\n")

    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=HEADLESS)
    context = browser.new_context()
    page    = context.new_page()

    try:
        login(page)

        global_counter = [0, 0]
        course_logs = process_course(page, course_pk, global_counter)
        if course_logs:
            append_log(course_logs)

        # Summary
        print(f"\n{'='*55}")
        print(f"  SUMMARY")
        print(f"{'='*55}")
        from collections import Counter
        counts = Counter(e["action"] for e in course_logs)
        for action, n in sorted(counts.items()):
            print(f"  {action:<12}: {n}")
        print(f"  Total log entries: {len(course_logs)}")
        print(f"  Log written to   : {LOG_FILE}")

    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()