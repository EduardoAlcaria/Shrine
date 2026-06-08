"""Automated client for the PUCRS student portal (webapp.pucrs.br/consulta).

Full pipeline from credentials alone (no hardcoded ticket):
  1. real browser passes the Cloudflare JS challenge (sets cf_clearance)
  2. POST /appgw/portal/wsauth with username/password -> 307 -> the account ticket `p`
  3. GET ValidaAluno?p -> binds a fresh session
  4. GET MenuNovo -> parse the `var menu = new Array(...)` tree of data servlets
  5. GET each read-only servlet -> raw data HTML (ISO-8859-1)

`collect_portal()` returns a pure dict (no file I/O) for an API to consume; parsing
the per-servlet HTML into clean JSON is a separate layer (TODO). Credentials come
from .env: PORTAL_USERNAME/PORTAL_PASSWORD, falling back to MOODLE_USERNAME/PASSWORD.
"""
import json
import os
import re
from datetime import datetime

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import portal_parsers

load_dotenv()

BASE = "https://webapp.pucrs.br"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

USER = os.getenv("PORTAL_USERNAME") or os.getenv("MOODLE_USERNAME")
PWD = os.getenv("PORTAL_PASSWORD") or os.getenv("MOODLE_PASSWORD")

# Read-only consulta servlets are scraped; write/action ones are skipped.
SKIP_SERVLETS = (
    "Logout", "UploadDoctos", "AlteracaoEndereco", "requerimentoformatura",
    "AproveitamentoIndividual", "CatalogoControl", "SegundaViaDocs",
)

_DENIED = ("Acesso Negado", "o foi iniciada")
_SERVLET_NAME = re.compile(r"consulta\.(?:aluno|sisdoc)\.([A-Za-z]+)")


def _decode(resp):
    return resp.body().decode("latin-1", errors="replace")


def _visible_text(html):
    s = re.sub(r"@font-face\s*{[^}]*}", " ", html)
    s = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    m = re.search(r"<body[^>]*>(.*)</body>", s, re.I | re.S)
    body = m.group(1) if m else s
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def _pass_cloudflare(page):
    """First navigation clears the Cloudflare JS challenge (sets cf_clearance)."""
    try:
        page.goto(f"{BASE}/consulta/servlet/consulta.aluno.ValidaAluno?p=x",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
    except Exception:
        pass


def _mint_ticket(page, retries=6):
    """POST credentials to the auth gateway; the 307 Location carries the ticket p.

    The gateway is slow/flaky (intermittent 504), so retry with a long timeout and
    re-pass the Cloudflare challenge between attempts.
    """
    last = ""
    for attempt in range(retries):
        if attempt:
            _pass_cloudflare(page)
        try:
            r = page.request.post(
                f"{BASE}/appgw/portal/wsauth",
                max_redirects=0,
                timeout=90000,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": BASE, "Referer": f"{BASE}/appgw/auth/portal",
                    "X-Requested-With": "XMLHttpRequest",
                },
                data=f"username={USER}&password={PWD}&acessar=",
            )
            loc = r.headers.get("location", "")
            m = re.search(r"[?&]p=([0-9a-fA-F-]{36})", loc)
            if m:
                return m.group(1)
            last = f"status={r.status} location={loc!r}"
        except Exception as e:  # noqa: BLE001 - slow gateway, retry
            last = e.__class__.__name__
        page.wait_for_timeout(3000)
    raise RuntimeError(f"login failed after {retries} tries: {last}")


def _bind_session(page, ticket):
    """GET ValidaAluno?p with Cloudflare cleared -> binds the session at the origin."""
    html = _decode(page.request.get(
        f"{BASE}/consulta/servlet/consulta.aluno.ValidaAluno?p={ticket}", timeout=90000))
    if any(d in html for d in _DENIED):
        raise RuntimeError("session bind denied after login")
    return html


def _read_menu(page):
    """Parse MenuNovo's `var menu = new Array([...])` into menu nodes."""
    html = _decode(page.request.get(f"{BASE}/consulta/servlet/consulta.aluno.MenuNovo", timeout=90000))
    region = (re.search(r"var\s+menu\s*=\s*new\s+Array\((.*?)\)\s*;", html, re.S) or [None, ""])[1]
    items = []
    for node in re.findall(r"\[(.*?)\]", region, re.S):
        f = [x.strip().strip("'").strip() for x in re.split(r"'\s*,\s*'", node.strip().strip("'"))]
        label = re.sub(r"<[^>]+>", "", f[1]).strip() if len(f) > 1 else ""
        target = f[3].strip() if len(f) > 3 else ""
        frame = f[4].strip() if len(f) > 4 else ""
        if label:
            items.append({"label": label, "target": target, "frame": frame})
    return items


def collect_portal(headless=True, verbose=False):
    """Run the full portal pipeline and return a pure dict (no file I/O)."""
    if not USER or not PWD:
        raise RuntimeError("missing PORTAL/MOODLE credentials in .env")
    now = datetime.now()

    def log(m):
        if verbose:
            print(m)

    menu, data = [], []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless,
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 1366, "height": 768}, locale="pt-BR")
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        page.set_default_navigation_timeout(60000)
        page.set_default_timeout(60000)
        try:
            log("passing Cloudflare...")
            _pass_cloudflare(page)
            log("minting ticket via wsauth...")
            ticket = _mint_ticket(page)
            log(f"ticket p={ticket}")
            _bind_session(page, ticket)
            log("reading menu...")
            menu = _read_menu(page)
            log(f"{len(menu)} menu items")

            for item in menu:
                target, label = item["target"], item["label"]
                if not target.startswith("/consulta/servlet/"):
                    continue
                name_m = _SERVLET_NAME.search(target)
                sname = name_m.group(1) if name_m else target
                if any(s in target for s in SKIP_SERVLETS):
                    continue
                try:
                    r = page.request.get(BASE + target, timeout=90000)
                    ctype = r.headers.get("content-type", "")
                    if "application/pdf" in ctype:
                        cd = r.headers.get("content-disposition", "")
                        fn = (re.search(r"filename=(.+)", cd) or [None, f"{sname}.pdf"])[1].strip().strip('"')
                        raw = r.body()
                        data.append({
                            "label": label, "servlet": sname, "status": r.status,
                            "type": "pdf", "filename": fn, "size": len(raw),
                            "parsed": None, "_pdf": raw,
                        })
                        log(f"  [{r.status}] {label[:34]:36} PDF {len(raw)}B")
                        continue
                    html = _decode(r)
                    denied = any(d in html for d in _DENIED)
                    parsed = None if denied else portal_parsers.parse_servlet(sname, html)
                    data.append({
                        "label": label, "servlet": sname, "status": r.status,
                        "type": "html", "denied": denied,
                        "tables": len(re.findall(r"<table", html, re.I)),
                        "parsed": parsed,
                        "text": _visible_text(html)[:500],
                        "html": html,
                    })
                    log(f"  [{r.status}] {label[:34]:36} {'DENIED' if denied else 'ok'}")
                except Exception as e:  # noqa: BLE001
                    log(f"  [ERR] {label} -> {e.__class__.__name__}")
                    data.append({"label": label, "servlet": sname, "error": e.__class__.__name__})
        finally:
            b.close()

    return {
        "scraped_at": now.isoformat(),
        "source": "portal.pucrs.br",
        "menu": menu,
        "data": data,
    }


def main():
    out = collect_portal(verbose=True)

    # Persist: raw HTML per servlet + a slim catalog (no html) for quick reading.
    os.makedirs("portal_data", exist_ok=True)
    slim = []
    for d in out["data"]:
        if "html" in d:
            with open(f"portal_data/{d['servlet']}.html", "w", encoding="utf-8") as f:
                f.write(d["html"])
        if "_pdf" in d:
            with open(f"portal_data/{d['servlet']}.pdf", "wb") as f:
                f.write(d["_pdf"])
        slim.append({k: v for k, v in d.items() if k not in ("html", "_pdf")})

    with open("portal_output.json", "w", encoding="utf-8") as f:
        json.dump({**out, "data": slim}, f, ensure_ascii=False, indent=2)

    print(f"\n{len(out['data'])} servlets pulled -> portal_output.json, raw -> portal_data/")


if __name__ == "__main__":
    main()
