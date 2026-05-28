import json
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

MOODLE_URL = os.getenv("MOODLE_URL", "https://moodle.pucrs.br")
COURSE_URL = os.getenv("COURSE_URL", "https://moodle.pucrs.br/course/view.php?id=92506")
USERNAME = os.getenv("MOODLE_USERNAME")
PASSWORD = os.getenv("MOODLE_PASSWORD")


def get_course_id(url):
    return parse_qs(urlparse(url).query).get("id", [None])[0]


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


def scrape_activities(page):
    page.goto(COURSE_URL)
    page.wait_for_load_state("load")

    activities = []

    # Section 0 (Geral) is rendered directly on the main page
    sec0_h3 = page.query_selector("#section-0 h3")
    sec0_name = sec0_h3.inner_text().strip() if sec0_h3 else "Geral"
    for item in page.query_selector_all("#section-0 li[data-for='cmitem']"):
        act = _parse_activity(item, sec0_name)
        if act:
            activities.append(act)

    # Tile sections require navigating to each section URL
    tiles = page.query_selector_all("a.tile-link[href]")
    sections = []
    for tile in tiles:
        href = tile.get_attribute("href")
        aria = tile.get_attribute("aria-label") or ""
        name = aria.split(",")[0].strip() if aria else "Unknown"
        sections.append((name, href))

    for section_name, section_url in sections:
        print(f"  Scraping: {section_name}")
        page.goto(section_url)
        page.wait_for_load_state("load")
        for item in page.query_selector_all("li[data-for='cmitem']"):
            act = _parse_activity(item, section_name)
            if act:
                activities.append(act)

    return activities


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


def main():
    course_id = get_course_id(COURSE_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        print("Logging in...")
        login(page)
        print("Login successful.")

        print("Scraping activities...")
        activities = scrape_activities(page)
        print(f"Found {len(activities)} activities.")

        print("Scraping grades...")
        grades = scrape_grades(page, course_id)
        print(f"Found {len(grades)} grade items.")

        browser.close()

    output = {
        "scraped_at": datetime.now().isoformat(),
        "course_url": COURSE_URL,
        "activities": activities,
        "grades": grades,
    }

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("Done! Results saved to output.json")


if __name__ == "__main__":
    main()
