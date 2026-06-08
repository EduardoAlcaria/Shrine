"""Parsers: PUCRS portal servlet HTML (ISO-8859-1, legacy tables) -> clean JSON.

Each parser takes the raw servlet HTML and returns structured data. A registry
maps servlet name -> parser so portal_client output can be enriched. Unparsed
servlets fall back to raw visible text.
"""
import html as _html
import re


def _tables(page_html):
    s = re.sub(r"@font-face\s*{[^}]*}", " ", page_html)
    s = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    return re.findall(r"<table.*?</table>", s, re.I | re.S)


def _cell(c):
    c = re.sub(r"<[^>]+>", " ", c)
    return " ".join(_html.unescape(c).replace("\xa0", " ").split())


def _rows(table_html):
    out = []
    for tr in re.findall(r"<tr.*?</tr>", table_html, re.I | re.S):
        cells = [_cell(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        out.append(cells)
    return out


def _nonempty(row):
    return [c for c in row if c != ""]


# ---------------------------------------------------------------- parsers

def parse_historico(page_html):
    """Academic transcript -> list of completed disciplines with grades."""
    tabs = _tables(page_html)
    if not tabs:
        return {"disciplinas": []}
    rows = _rows(tabs[0])
    keys = ["periodo", "codicred", "disciplina", "grau", "obs"]
    out = []
    for r in rows[1:]:
        if len(r) < 4 or not re.match(r"\d{4}/\d", r[0]):
            continue
        out.append({k: (r[i] if i < len(r) else "") for i, k in enumerate(keys)})
    return {"disciplinas": out}


def parse_grade_horarios(page_html):
    """Weekly timetable grid + discipline legend (code -> name)."""
    tabs = _tables(page_html)
    grade, legenda = [], {}
    if tabs:
        rows = _rows(tabs[0])
        days = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado"]
        for r in rows[1:]:
            r = [c for c in r if c is not None]
            if len(r) < 3:
                continue
            # row shape: [turno, hora, seg, ter, qua, qui, sex, sab]
            turno = r[0]
            hora = r[1] if len(r) > 1 else ""
            for i, day in enumerate(days):
                idx = 2 + i
                val = r[idx] if idx < len(r) else ""
                if val and val not in ("-", ""):
                    grade.append({"turno": turno, "hora": hora, "dia": day, "turma": val})
    if len(tabs) > 1:
        flat = _nonempty([c for r in _rows(tabs[1]) for c in r])
        # pairs of (codigo, nome), skipping the "Disciplinas" header
        flat = [c for c in flat if c.lower() != "disciplinas"]
        for i in range(0, len(flat) - 1, 2):
            legenda[flat[i]] = flat[i + 1]
    return {"grade": grade, "disciplinas": legenda}


def parse_extrato(page_html):
    """Financial statement -> per-mensalidade blocks with line items."""
    out = []
    for t in _tables(page_html):
        rows = [r for r in _rows(t) if _nonempty(r)]
        if not rows:
            continue
        head = _nonempty(rows[0])
        if len(head) == 1 and "MENSALIDADE" in head[0].upper():
            block = {"mensalidade": head[0], "lancamentos": [], "saldo": None}
            for r in rows[1:]:
                r = _nonempty(r)
                if not r:
                    continue
                if "SALDO" in r[0].upper():
                    block["saldo"] = r[-1]
                elif len(r) >= 4:
                    block["lancamentos"].append({
                        "tipo": r[0], "periodo": r[1], "data": r[2], "valor": r[-1]})
            out.append(block)
    return {"mensalidades": out}


def parse_publicacoes(page_html):
    """Published grades per enrolled discipline (partial + final grades, absences)."""
    tabs = _tables(page_html)
    if not tabs:
        return {"disciplinas": []}
    out = []
    for r in _rows(tabs[0])[1:]:
        r = _nonempty(r)
        if len(r) >= 2 and re.match(r"\w+-\d+", r[0]):
            out.append({"turma": r[0], "disciplina": r[1], "graus": r[2:]})
    return {"disciplinas": out}


def parse_pendentes(page_html):
    """Pending disciplines -> code/name/status/level (one row per discipline)."""
    out, seen = [], set()
    for t in _tables(page_html):
        for r in _rows(t):
            if len(r) >= 5 and re.match(r"\w+-\d+$", r[0]) and r[1] == "Detalhar":
                if r[0] not in seen:
                    seen.add(r[0])
                    out.append({"codicred": r[0], "disciplina": r[2],
                                "status": r[3], "nivel": r[4]})
    return {"pendentes": out}


def parse_trancometro(page_html):
    """Requirements per pending discipline -> level/code/name/prereqs."""
    out, seen = [], set()
    for t in _tables(page_html):
        for r in _rows(t):
            if len(r) >= 5 and r[1] == "Detalhar" and re.match(r"\w+-\d+$", r[2]):
                if r[2] not in seen:
                    seen.add(r[2])
                    out.append({"nivel": r[0], "codicred": r[2],
                                "disciplina": r[3], "requisitos": r[4]})
    return {"disciplinas": out}


def parse_certificacao(page_html):
    """Study certifications -> disciplines the student may take/skip."""
    out = []
    for t in _tables(page_html):
        for r in _rows(t):
            if len(r) >= 3 and re.match(r"\w+-\d+$", r[0]):
                out.append({"codicred": r[0], "disciplina": r[1], "situacao": r[2]})
    return {"certificacoes": out}


def parse_estacionamento(page_html):
    """Parking balance -> the saldo text."""
    txt = _visible_from(page_html)
    m = re.search(r"saldo no estacionamento[^R\d]*(R?\$?\s*[\d.,]+)", txt, re.I)
    return {"saldo": m.group(1).strip() if m else None, "texto": txt[:200]}


def parse_email(page_html):
    """Academic email address."""
    txt = _visible_from(page_html)
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", txt)
    return {"email": m.group(0) if m else None}


def parse_message(page_html):
    """Servlets that only show a status message (no data) -> the message text."""
    return {"mensagem": _visible_from(page_html)[:300], "items": []}


def _visible_from(page_html):
    s = re.sub(r"@font-face\s*{[^}]*}", " ", page_html)
    s = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    m = re.search(r"<body[^>]*>(.*)</body>", s, re.I | re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", _html.unescape(m.group(1) if m else s)).split())


PARSERS = {
    "Historico": parse_historico,
    "GradeHorarios": parse_grade_horarios,
    "Extrato": parse_extrato,
    "Publicacoes": parse_publicacoes,
    "Pendentes": parse_pendentes,
    "Trancometro": parse_trancometro,
    "CertificacaoAdicional": parse_certificacao,
    "Estacionamento": parse_estacionamento,
    "EmailPUC": parse_email,
    "Rejeitadas": parse_message,
    "AprovIndeferidos": parse_message,
}


def parse_servlet(servlet, page_html):
    """Parse one servlet's HTML if a parser exists, else None."""
    fn = PARSERS.get(servlet)
    return fn(page_html) if fn else None


if __name__ == "__main__":
    import json
    import os
    for servlet in PARSERS:
        path = f"portal_data/{servlet}.html"
        if not os.path.exists(path):
            print(f"!! {path} missing")
            continue
        data = parse_servlet(servlet, open(path, encoding="utf-8").read())
        print(f"\n##### {servlet}")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1400])
