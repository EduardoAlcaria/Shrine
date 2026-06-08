"""Official Moodle Web Services REST client for moodle.pucrs.br.

No scraping, no browser — Moodle exposes a clean token-based REST API:
  1. POST /login/token.php?service=moodle_mobile_app  -> wstoken
  2. GET  /webservice/rest/server.php?wstoken=..&wsfunction=..&moodlewsrestformat=json

`collect()` returns a pure dict (API-ready). Credentials come from .env
(MOODLE_USERNAME / MOODLE_PASSWORD / MOODLE_URL).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

MOODLE_URL = os.getenv("MOODLE_URL", "https://moodle.pucrs.br").rstrip("/")
USERNAME = os.getenv("MOODLE_USERNAME")
PASSWORD = os.getenv("MOODLE_PASSWORD")
SERVICE = "moodle_mobile_app"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class MoodleError(RuntimeError):
    pass


def _http(url, data=None):
    """GET (data=None) or POST a urlencoded form; return decoded JSON."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise MoodleError(f"non-JSON response from {url}: {raw[:200]}")


class MoodleClient:
    def __init__(self, base=MOODLE_URL, token=None):
        self.base = base.rstrip("/")
        self.token = token

    def login(self, username=USERNAME, password=PASSWORD):
        """Exchange credentials for a web-service token."""
        url = f"{self.base}/login/token.php"
        res = _http(url, {
            "username": username, "password": password, "service": SERVICE,
        })
        if "token" not in res:
            raise MoodleError(f"login failed: {res.get('error', res)}")
        self.token = res["token"]
        return self.token

    def call(self, wsfunction, **params):
        """Invoke a Moodle web-service function, return parsed JSON."""
        if not self.token:
            raise MoodleError("not authenticated — call login() first")
        flat = {
            "wstoken": self.token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        flat.update(_flatten(params))
        url = f"{self.base}/webservice/rest/server.php"
        res = _http(url, flat)
        if isinstance(res, dict) and res.get("exception"):
            raise MoodleError(f"{wsfunction}: {res.get('errorcode')} — {res.get('message')}")
        return res

    # --- high-level helpers ------------------------------------------------

    def site_info(self):
        return self.call("core_webservice_get_site_info")

    def my_courses(self, userid):
        return self.call("core_enrol_get_users_courses", userid=userid)

    def assignments(self, course_ids):
        return self.call("mod_assign_get_assignments", courseids=list(course_ids))

    def assign_status(self, assignid):
        return self.call("mod_assign_get_submission_status", assignid=assignid)

    def quizzes(self, course_ids):
        return self.call("mod_quiz_get_quizzes_by_courses", courseids=list(course_ids))

    def grades(self, courseid, userid):
        return self.call("gradereport_user_get_grade_items",
                         courseid=courseid, userid=userid)

    def calendar_upcoming(self):
        """Action events sorted by time — the unified deadline feed."""
        return self.call("core_calendar_get_action_events_by_timesort",
                         **{"limitnum": 50})


def _flatten(params, prefix=""):
    """Moodle wants nested params as courseids[0]=1&courseids[1]=2 etc."""
    out = {}
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                ik = f"{key}[{i}]"
                if isinstance(item, dict):
                    out.update(_flatten(item, ik))
                else:
                    out[ik] = item
        else:
            out[key] = v
    return out


def collect(verbose=False):
    """Full pull via the API: profile, courses, deadlines, assignments, grades."""
    def log(m):
        if verbose:
            print(m)

    c = MoodleClient()
    log("getting token...")
    c.login()
    log(f"token ok ({c.token[:8]}...)")

    info = c.site_info()
    userid = info["userid"]
    log(f"user: {info.get('fullname')} (id={userid}) site={info.get('sitename')}")

    courses = c.my_courses(userid)
    log(f"{len(courses)} courses")
    course_ids = [crs["id"] for crs in courses]

    # Assignments across all courses in one call.
    assigns = {"courses": []}
    try:
        assigns = c.assignments(course_ids)
    except MoodleError as e:
        log(f"assignments: {e}")

    # Per-assignment submission status (so we know what's still open/unsubmitted).
    assign_detail = []
    for crs in assigns.get("courses", []):
        for a in crs.get("assignments", []):
            entry = {
                "course_id": crs["id"], "course": crs.get("shortname"),
                "id": a["id"], "cmid": a.get("cmid"), "name": a["name"],
                "duedate": _ts(a.get("duedate")), "cutoffdate": _ts(a.get("cutoffdate")),
                "submitted": None, "graded": None,
            }
            try:
                st = c.assign_status(a["id"])
                last = st.get("lastattempt", {})
                sub = (last.get("submission") or {})
                entry["submitted"] = sub.get("status") == "submitted"
                fb = st.get("feedback") or {}
                entry["graded"] = bool(fb.get("grade"))
            except MoodleError:
                pass
            assign_detail.append(entry)
    log(f"{len(assign_detail)} assignments detailed")

    # Quizzes.
    quizzes = []
    try:
        for q in c.quizzes(course_ids).get("quizzes", []):
            quizzes.append({
                "course_id": q.get("course"), "id": q["id"], "cmid": q.get("coursemodule"),
                "name": q["name"], "open": _ts(q.get("timeopen")),
                "close": _ts(q.get("timeclose")),
            })
    except MoodleError as e:
        log(f"quizzes: {e}")
    log(f"{len(quizzes)} quizzes")

    # Upcoming calendar deadlines (unified feed).
    events = []
    try:
        for e in c.calendar_upcoming().get("events", []):
            events.append({
                "id": e["id"], "name": e["name"], "course_id": (e.get("course") or {}).get("id"),
                "modulename": e.get("modulename"), "when": _ts(e.get("timesort")),
                "url": e.get("url"),
            })
    except MoodleError as e:
        log(f"calendar: {e}")
    log(f"{len(events)} upcoming events")

    return {
        "fetched_at": datetime.now().isoformat(),
        "source": "moodle_api",
        "user": {"id": userid, "name": info.get("fullname"),
                 "username": info.get("username"), "email": info.get("useremail")},
        "courses": [{"id": x["id"], "shortname": x.get("shortname"),
                     "fullname": x.get("fullname")} for x in courses],
        "assignments": assign_detail,
        "quizzes": quizzes,
        "upcoming": events,
    }


def _ts(v):
    """Moodle timestamps are unix seconds; 0 means unset."""
    if not v:
        return None
    try:
        return datetime.fromtimestamp(int(v)).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def main():
    out = collect(verbose=True)
    with open("moodle_output.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nuser: {out['user']['name']} <{out['user']['email']}>")
    print(f"courses: {len(out['courses'])}  assignments: {len(out['assignments'])}  "
          f"quizzes: {len(out['quizzes'])}  upcoming: {len(out['upcoming'])}")
    open_now = [a for a in out["assignments"] if a.get("submitted") is False]
    print(f"\nunsubmitted assignments ({len(open_now)}):")
    for a in sorted(open_now, key=lambda x: x.get("duedate") or "9999"):
        print(f"  [{a['course']}] {a['name']} — due {a.get('duedate') or 'no date'}")
    print("\n-> moodle_output.json")


if __name__ == "__main__":
    main()
