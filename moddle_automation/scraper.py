import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

MOODLE_URL = os.getenv("MOODLE_URL", "https://moodle.pucrs.br")
COURSE_URL = os.getenv("COURSE_URL", "https://moodle.pucrs.br/course/view.php?id=92506")
USERNAME = os.getenv("MOODLE_USERNAME")
PASSWORD = os.getenv("MOODLE_PASSWORD")

# Activity types that represent something you have to deliver/submit.
DELIVERABLE_TYPES = {"assign", "quiz", "workshop"}

# Portuguese (and English fallback) month names -> month number.
# Includes abbreviated forms Moodle uses, e.g. "26 jun. 2026".
MONTHS = {
    # Portuguese full
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
    # Portuguese abbreviated
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
    # English full
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    # English abbreviated
    "feb": 2, "apr": 4, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "dec": 12,
}

# e.g. "segunda-feira, 12 de maio de 2025, 23:59" / "12 maio 2025" / "12 May 2025, 11:59 PM"
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(?:de\s+)?([A-Za-zçãéêíóúûÇÃ]+)\.?\s+(?:de\s+)?(\d{4})"
    r"(?:[,\s]+(\d{1,2}):(\d{2})\s*(AM|PM)?)?",
    re.IGNORECASE,
)

# Status values meaning "no submission yet".
_NOT_SUBMITTED_HINTS = (
    "nenhuma tentativa", "no attempt", "não enviado", "nao enviado",
    "nenhum envio", "ainda não", "ainda nao", "no submissions",
    "nada foi enviado",
)
# Text meaning the submission window already closed / is overdue.
_OVERDUE_HINTS = ("atrasad", "overdue", "encerrad", "fechad", "closed")


def get_course_id(url):
    return parse_qs(urlparse(url).query).get("id", [None])[0]


def parse_moodle_date(text):
    """Parse a Moodle PT/EN date string into a datetime, or None."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, month_word, year, hh, mm, ampm = m.groups()
    month = MONTHS.get(month_word.lower().strip("."))
    if not month:
        return None
    hour = int(hh) if hh else 23
    minute = int(mm) if mm else 59
    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
    try:
        return datetime(int(year), month, int(day), hour, minute)
    except ValueError:
        return None


def login(page):
    page.goto(f"{MOODLE_URL}/login/index.php")
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#loginbtn")
    page.wait_for_load_state("load")
    if "login" in page.url:
        raise RuntimeError("Login failed — check your credentials in .env")


def _parse_activity(item, section_name):
    activity = {"section": section_name}

    modtype = item.get_attribute("data-modtype")
    if modtype:
        activity["type"] = modtype

    link = item.query_selector(".activityname a, a.aalink")
    if link:
        name_el = link.query_selector(".instancename")
        activity["name"] = (name_el.inner_text() if name_el else link.inner_text()).strip()
        activity["url"] = link.get_attribute("href")
    else:
        name_div = item.query_selector("div.activity-item[data-activityname]")
        if name_div:
            activity["name"] = name_div.get_attribute("data-activityname") or ""

    date_el = item.query_selector(".date, .info .date")
    if date_el:
        activity["due_date"] = date_el.inner_text().strip()

    return activity if activity.get("name") else None


def _wait_for(page, selector, timeout=10000):
    """Wait for a selector, swallowing timeout (page may legitimately lack it)."""
    try:
        page.wait_for_selector(selector, timeout=timeout)
    except Exception:
        pass


def discover_courses(page):
    """Find every course the logged-in user is enrolled in.

    Dashboard course cards are rendered by JS after load, so we must wait
    for the links to appear before querying.
    """
    courses = []
    seen = set()
    for path in ("/my/courses.php", "/my/"):
        page.goto(f"{MOODLE_URL}{path}", wait_until="domcontentloaded")
        _wait_for(page, "a.aalink.coursename, a[href*='course/view.php?id=']")
        page.wait_for_timeout(2000)  # let the full card grid hydrate
        links = page.query_selector_all("a.aalink.coursename")
        if not links:
            links = page.query_selector_all("a[href*='course/view.php?id=']")
        for link in links:
            href = link.get_attribute("href")
            cid = get_course_id(href)
            if not cid or cid in seen:
                continue
            # inner_text carries an sr-only "Nome do curso" prefix line; drop it.
            lines = [ln.strip() for ln in (link.inner_text() or "").splitlines() if ln.strip()]
            name = lines[-1] if lines else (link.get_attribute("aria-label") or f"course {cid}")
            seen.add(cid)
            courses.append({"id": cid, "name": name, "url": href})
        if courses:
            break
    return courses


def scrape_activities(page, course_url):
    page.goto(course_url, wait_until="domcontentloaded")
    page.wait_for_load_state("load")
    # Tiles / activities hydrate via JS; wait for either to appear.
    _wait_for(page, "li[data-for='cmitem'], a.tile-link")
    page.wait_for_timeout(1500)

    activities = []

    # Section 0 (Geral) is rendered directly on the main page
    sec0_h3 = page.query_selector("#section-0 h3")
    sec0_name = sec0_h3.inner_text().strip() if sec0_h3 else "Geral"
    for item in page.query_selector_all("#section-0 li[data-for='cmitem']"):
        act = _parse_activity(item, sec0_name)
        if act:
            activities.append(act)

    # Tile sections require navigating to each section URL
    sections = []
    seen_urls = set()
    for tile in page.query_selector_all("a.tile-link[href]"):
        href = tile.get_attribute("href")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)
        aria = tile.get_attribute("aria-label") or ""
        name = (aria.split(",")[0].strip() if aria
                else (tile.inner_text() or "").strip()) or "Unknown"
        name = " ".join(name.split())  # collapse \xa0 / newlines
        sections.append((name, href))

    for section_name, section_url in sections:
        page.goto(section_url, wait_until="domcontentloaded")
        page.wait_for_load_state("load")
        _wait_for(page, "li[data-for='cmitem']", timeout=6000)
        page.wait_for_timeout(800)
        for item in page.query_selector_all("li[data-for='cmitem']"):
            act = _parse_activity(item, section_name)
            if act:
                activities.append(act)

    return activities


def _read_status_table(page):
    """Read Moodle's label->value status table into a dict (lowercase keys)."""
    info = {}
    for row in page.query_selector_all("table.generaltable tr, table.table tr"):
        cells = row.query_selector_all("th, td")
        if len(cells) >= 2:
            key = cells[0].inner_text().strip().lower()
            val = cells[1].inner_text().strip()
            if key and key not in info:
                info[key] = val
    return info


def _find_value(info, *keywords):
    """Return the first table value whose label contains any keyword."""
    for key, val in info.items():
        if any(kw in key for kw in keywords):
            return val
    return None


def _is_overdue(text, due_dt, now):
    if text and any(h in text.lower() for h in _OVERDUE_HINTS):
        return True
    return bool(due_dt and due_dt < now)


def _label_date(text, *labels):
    """Find 'Label: <date>' in page text and parse the date. Returns (text, dt)."""
    for label in labels:
        m = re.search(label + r"\s*:?\s*([^\n]+)", text, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            dt = parse_moodle_date(raw)
            if dt:
                return raw, dt
    return None, None


def _enrich_assign(page, now):
    body = page.inner_text("body")

    # Due date lives in a body line, not the status table.
    due_text, due_dt = _label_date(
        body, "Vencimento", "Data de entrega", "Data limite", "Due date")

    # Submission status comes from the generaltable.
    info = _read_status_table(page)
    status = _find_value(info, "status de envio", "submission status")
    submitted = None
    if status is not None:
        s = status.lower()
        submitted = not any(h in s for h in _NOT_SUBMITTED_HINTS)

    remaining = _find_value(info, "tempo restante", "time remaining") or ""
    overdue = _is_overdue(remaining, due_dt, now)
    return due_text, due_dt, submitted, overdue


def _enrich_quiz(page, now):
    body = page.inner_text("body")

    # "Fechado/Fecha/Closed: <date>" is the submission deadline.
    due_text, due_dt = _label_date(
        body, "Fechado", "Fecha o teste", "Fecha", "Closes", "Closed",
        "This quiz closed", "Encerrado")

    # A finished attempt (Situação Finalizada / Concluído) means submitted.
    low = body.lower()
    if "finalizada" in low or "concluíd" in low or "concluid" in low or "finished" in low:
        submitted = True
    elif "nenhuma tentativa" in low or "no attempt" in low or "ainda não" in low:
        submitted = False
    else:
        submitted = None

    # "Fechado" (past tense) or a past close date means the window is shut.
    overdue = bool(re.search(r"\bFechad", body)) or _is_overdue(body, due_dt, now)
    return due_text, due_dt, submitted, overdue


def _enrich_workshop(page, now):
    body = page.inner_text("body")
    due_text, due_dt = _label_date(
        body, "Prazo para entrega", "Data de entrega", "Vencimento",
        "Submission deadline", "Deadline")

    low = body.lower()
    if "já enviou" in low or "submitted" in low:
        submitted = True
    elif "ainda não enviou" in low or "not submitted" in low:
        submitted = False
    else:
        submitted = None

    overdue = _is_overdue(body, due_dt, now)
    return due_text, due_dt, submitted, overdue


_ENRICHERS = {
    "assign": _enrich_assign,
    "quiz": _enrich_quiz,
    "workshop": _enrich_workshop,
}


def scrape_deliverables(page, activities, now=None):
    """Filter to deliverable activities, visit each, keep those still submittable.

    Still submittable = due/close date in the future AND not yet submitted.
    Unknown submission state (quiz/workshop) is kept so nothing is hidden.
    """
    now = now or datetime.now()
    deliverables = []

    candidates = [a for a in activities
                  if a.get("type") in DELIVERABLE_TYPES and a.get("url")]
    print(f"Checking {len(candidates)} deliverable activities...")

    for act in candidates:
        print(f"  {act['type']}: {act.get('name', '?')}")
        try:
            page.goto(act["url"])
            page.wait_for_load_state("load")
            due_text, due_dt, submitted, overdue = _ENRICHERS[act["type"]](page, now)
        except Exception as e:  # noqa: BLE001 - keep going on any single page failure
            print(f"    ! failed to read page: {e}")
            continue

        act = dict(act)
        act["due_date_text"] = due_text
        act["due_date"] = due_dt.isoformat() if due_dt else None
        act["submitted"] = submitted
        act["overdue"] = overdue

        if overdue:
            continue
        if submitted:  # explicitly submitted -> not a TODO
            continue
        if due_dt is None:
            # No date we could parse and not overdue: include but flag it.
            act["note"] = "due date not detected"
        deliverables.append(act)

    return deliverables


def scrape_grades(page, course_id):
    page.goto(f"{MOODLE_URL}/grade/report/user/index.php?id={course_id}")
    page.wait_for_load_state("load")

    grades = []
    for row in page.query_selector_all("table.user-grade tr"):
        cells = row.query_selector_all("td, th")
        if len(cells) < 2:
            continue
        item = cells[0].inner_text().strip()
        grade = cells[1].inner_text().strip()
        if item and item.lower() not in ("item name", "grade item", ""):
            grades.append({"item": item, "grade": grade})

    return grades


def collect(headless=True, verbose=False):
    """Run the full scrape across all enrolled courses and return the result dict.

    Pure return value, no file I/O — this is the function a future API
    (e.g. a Java backend shelling out to this script) consumes. CLI output
    lives in main().
    """
    now = datetime.now()

    def log(msg):
        if verbose:
            print(msg)

    courses = []
    all_pending = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_context().new_page()
        try:
            log("Logging in...")
            login(page)
            log("Login successful.")

            log("Discovering enrolled courses...")
            courses = discover_courses(page)
            log(f"Found {len(courses)} courses.")

            for course in courses:
                log(f"\nCourse: {course['name']}")
                activities = scrape_activities(page, course["url"])
                log(f"  {len(activities)} activities.")
                deliverables = scrape_deliverables(page, activities, now)
                for d in deliverables:
                    d["course"] = course["name"]
                    d["course_id"] = course["id"]
                deliverables.sort(key=lambda a: a.get("due_date") or "9999")
                course["activities_count"] = len(activities)
                course["pending_deliverables"] = deliverables
                all_pending.extend(deliverables)
                log(f"  {len(deliverables)} deliverables still open.")
        finally:
            browser.close()

    # Flat list across all courses, sorted by due date (undated last).
    all_pending.sort(key=lambda a: a.get("due_date") or "9999")

    return {
        "scraped_at": now.isoformat(),
        "moodle_url": MOODLE_URL,
        "courses": courses,
        "pending_deliverables": all_pending,
    }


def main():
    output = collect(verbose=True)
    deliverables = output["pending_deliverables"]

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\nPending deliverables (all courses):")
    if not deliverables:
        print("  (none — you're all caught up)")
    for d in deliverables:
        due = d.get("due_date_text") or d.get("due_date") or "no date"
        course = d.get("course", "?")
        print(f"  [{course}] [{d['type']}] {d.get('name', '?')} — due {due}")

    print("\nDone! Results saved to output.json")


if __name__ == "__main__":
    main()
