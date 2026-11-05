#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "websocket-client==1.9.2",
#     "beautifulsoup4==4.15.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""impots-cli — your impots.gouv.fr "espace particulier" from the terminal.

Lists and downloads every document of the "Documents" page (avis d'impôt,
déclarations, taxes foncières, …) and reads and writes the "messagerie
sécurisée": threads, attachments, acknowledgements of receipt, new requests.

Why a browser: the DGFiP login (cfspart-idp.impots.gouv.fr) sits behind a
Radware bot manager that answers any non-browser HTTP client with a JS
challenge page, and the messagerie is a stateful JSF/PrimeFaces app. So this
CLI attaches to a dedicated Chrome over the DevTools Protocol (CDP), runs the
document fetches inside a logged-in cfspart.impots.gouv.fr tab, and drives the
messagerie's real DOM (clicks, PrimeFaces ajax queue, file inputs).

The dedicated Chrome (profile ~/.impots-chrome, port 9224) is launched on
demand and keeps cookies and device trust between runs; `impots login` opens
it on the login page and waits for the person to sign in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import click
import websocket  # websocket-client
from bs4 import BeautifulSoup

log = logging.getLogger("impots")

ORIGIN = "https://cfspart.impots.gouv.fr"
HOME_URL = f"{ORIGIN}/enp/accueilensupres.do"
DOCS_URL = f"{ORIGIN}/enp/documents.do?n=0"
MSG_URL = f"{ORIGIN}/gaia2-zu-mapi/pages/part/portailpart.xhtml"
DEFAULT_CDP = os.environ.get("IMPOTS_CDP_URL") or "http://localhost:9224"
PROFILE_DIR = Path(os.environ.get("IMPOTS_CHROME_PROFILE") or Path.home() / ".impots-chrome")
DOWNLOAD_DIR = Path.home() / "Downloads" / "impots"
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
# Attachment rules stated by the messagerie form itself.
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".tif", ".tiff", ".png", ".xls", ".xlsx", ".ods", ".odt"}
MAX_ATTACH_BYTES = 20 * 1024 * 1024
# A new demande can take a few seconds to show up in "Mes échanges".
SEND_CHECKS = 5
SEND_CHECK_DELAY = 3.0
NOT_LOGGED = "Not signed in to impots.gouv.fr — run `impots login` and sign in in the Chrome window it opens."

# Resolves once PrimeFaces' ajax queue is drained (the DOM is then up to date).
WAIT_JS = """(async()=>{for(let i=0;i<120;i++){await new Promise(r=>setTimeout(r,250));
 if(window.PrimeFaces && PrimeFaces.ajax.Queue.isEmpty()) return true;} return false;})()"""

# Fetches a URL in the page and returns {status, url, ct, cd, b64}.
FETCH_B64_JS = """(async()=>{
 const r = await fetch(__URL__, __OPTS__);
 const b = new Uint8Array(await r.arrayBuffer());
 let s=''; for (let i=0;i<b.length;i+=0x8000) s+=String.fromCharCode.apply(null, b.subarray(i,i+0x8000));
 return JSON.stringify({status:r.status, url:r.url, ct:r.headers.get('content-type'),
   cd:r.headers.get('content-disposition'), b64:btoa(s)});
})()"""

# Submits the messagerie form with one commandLink param, as PrimeFaces' non-ajax
# download links do, and returns the file like FETCH_B64_JS.
FORM_DOWNLOAD_JS = """(async()=>{
 const f=document.getElementById('listeDemandesForm');
 const fd=new URLSearchParams(new FormData(f)); fd.set(__ID__, __ID__);
 const r=await fetch(f.action,{method:'POST',body:fd});
 const b=new Uint8Array(await r.arrayBuffer());
 let s=''; for (let i=0;i<b.length;i+=0x8000) s+=String.fromCharCode.apply(null, b.subarray(i,i+0x8000));
 return JSON.stringify({status:r.status, url:r.url, ct:r.headers.get('content-type'),
   cd:r.headers.get('content-disposition'), b64:btoa(s)});
})()"""


# ---------------------------------------------------------------------------
# Parsing (pure functions, tested offline)
# ---------------------------------------------------------------------------

def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _iso(ddmmyyyy: str | None) -> str | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", ddmmyyyy or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def slugify(label: str) -> str:
    """ASCII file-system safe name (for uploads and `--ascii` names)."""
    # Typographic apostrophes and dashes separate words; NFKD would drop them.
    s = re.sub(r"[’‘`´–—]", " ", label)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:120] or "document"


def readable_name(label: str, description: str = "") -> str:
    """Human file name: the label, plus the property address when there is one.

    Only characters that break paths on macOS, Windows or Drive are replaced.
    """
    first = description.split(" | ", 1)[0] if description else ""
    if first and not first.lower().startswith("date limite"):
        label = f"{label} - {first}"
    s = re.sub(r"(\d{1,2}):(\d{2})", r"\1h\2", label.replace("/", "-"))
    s = re.sub(r"\s*:\s*", " - ", s)
    s = re.sub(r'[\\*?"<>|]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:150] or "document"


def doc_id(token: str) -> str:
    """Short stable id for a document: the raw idEnsua token is 100+ chars."""
    return hashlib.sha1(token.encode()).hexdigest()[:10]


def parse_documents(html: str) -> list[dict]:
    """Documents of documents.do, all years, deduplicated, page order kept.

    The page renders every year twice (tabs and accordions); the year
    accordions are the `div.fr-collapse` whose id is the year. A main document
    is a link; its linked documents (accusé de réception, annexes, one avis
    per property) are GET forms carrying the `idEnsua` token.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for year_div in soup.select("div.fr-collapse"):
        year = year_div.get("id") or ""
        if not re.fullmatch(r"\d{4}", year):
            continue
        names: dict[str, int] = {}
        for el in year_div.find_all(["a", "form"]):
            if el.name == "a" and "Affichage_Document_PDF?idEnsua=" in (el.get("href") or ""):
                token = el["href"].split("idEnsua=", 1)[1]
                label = _clean(el.get_text(" "))
                card = el.find_parent(class_="fr-card")
                desc_el = card.select_one(".fr-card__desc") if card else None
                linked = False
            elif el.name == "form" and el.get("action") == "Affichage_Document_PDF":
                inp = el.select_one('input[name="idEnsua"]')
                if inp is None:
                    continue
                token = inp["value"]
                card = el.find_parent(class_="fr-card")
                title = card.select_one('p[id^="titreLie"]') if card else None
                label = _clean(title.get_text(" ")) if title else ""
                desc_el = card.select_one("p.fr-text--sm") if card else None
                linked = True
            else:
                continue
            if token in seen or not label:
                continue
            seen.add(token)
            desc = _clean(desc_el.get_text(" | ")) if desc_el else ""
            base = readable_name(label, desc)
            names[base] = names.get(base, 0) + 1
            fname = base if names[base] == 1 else f"{base} ({names[base]})"
            out.append({
                "id": doc_id(token),
                "year": int(year),
                "label": label,
                "description": desc,
                "linked": linked,
                "file": f"{year}/{fname}.pdf",
                "token": token,
            })
    return out


def parse_logged_in(html: str) -> bool:
    return "j_spring_security_logout" in html


def parse_fiscal_number(html: str) -> str | None:
    """From the messagerie's (collapsed) "Mes coordonnées" panel."""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    m = re.search(r"N°\s*fiscal\s*:\s*(\d{13})", text)
    return m.group(1) if m else None


def parse_home(html: str) -> dict:
    """Name and unread-message count from the header of any /enp/ page."""
    soup = BeautifulSoup(html, "html.parser")
    btn = soup.select_one("#btnMenuDeroulant")
    mail = soup.select_one("a.fr-btn--mail")
    m = re.search(r"(\d+)\s+nouveaux?\s+messages?", mail.get("title", "")) if mail else None
    return {"name": _clean(btn.get_text()) if btn else None,
            "unread_messages": int(m.group(1)) if m else None}


def parse_demandes(html: str) -> list[dict]:
    """Rows of the messagerie table ("Mes échanges")."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select('tbody[id="listeDemandesForm:listeDemandes_data"] > tr[data-rk]')
    out = []
    for tr in rows:
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 7:
            continue
        # The full subject lives in a tooltip that PrimeFaces moves to <body>.
        prefix = f"listeDemandesForm:listeDemandes:{tr.get('data-ri')}:libelledemandezu:"
        tip_box = soup.find(id=lambda i, p=prefix: bool(i) and i.startswith(p))
        tip = tip_box.select_one(".ui-tooltip-text") if tip_box else None
        subject = _clean(tip.get_text() if tip else cells[1].get_text())
        classes = " ".join(tr.get("class") or [])
        out.append({
            "id": tr["data-rk"],
            "index": int(tr.get("data-ri", 0)),
            "subject": subject,
            "service": _clean(cells[2].get_text()),
            "channel": _clean(cells[3].get_text()),
            "status": _clean(cells[4].get_text()),
            "created": _iso(cells[5].get_text()),
            "last_message": _iso(cells[6].get_text()),
            "unread": "WithUnreadMsg" in classes.replace("WithOutUnreadMsg", ""),
        })
    return out


def new_demande(before: set[str], after: list[dict]) -> dict | None:
    """The demande listed in `after` but not in `before`: the proof a send went through.

    `after` is in "Mes échanges" order (latest first), so the first fresh row wins.
    """
    return next((d for d in after if d["id"] not in before), None)


def _html_to_text(el) -> str:
    for br in el.find_all("br"):
        br.replace_with("\n")
    parts = [p.get_text() for p in el.find_all("p")] if el.find("p") else [el.get_text()]
    text = "\n".join(p.rstrip() for p in parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_thread(html: str, demande_id: str) -> dict:
    """Messages of one expanded demande, oldest first.

    Each message is a `div.detailMessageListeDemande` panel; `...Left` panels
    come from the administration, `...Right` ones from the user.
    """
    soup = BeautifulSoup(html, "html.parser")
    row = soup.select_one(f'tr[data-rk="{demande_id}"]')
    if row is None:
        raise ValueError(f"demande {demande_id} not in page")
    exp = row.find_next_sibling("tr")
    if exp is None or "ui-expanded-row-content" not in (exp.get("class") or []):
        raise ValueError(f"demande {demande_id} is not expanded")
    messages = []
    for panel in exp.select("div.detailMessageListeDemande"):
        m = re.search(r"Msg(\d+)", panel.get("data-widget") or "")
        msg_id = m.group(1) if m else None
        head = panel.select_one(".ui-panel-titlebar")

        def field(sel, prefix=""):
            el = head.select_one(sel) if head else None
            return _clean(el.get_text(" ")).removeprefix(prefix).strip() if el else ""

        content = panel.select_one(".ui-panel-content")
        body_el = content.select_one(".formulaireVerbaimPre") if content else None
        if body_el is not None:
            body = _html_to_text(body_el)
        elif content is not None:
            for junk in content.select(".ui-confirm-dialog, [id$=':arBlock'], script, button"):
                junk.decompose()
            body = _html_to_text(content)
        else:
            body = ""
        sig = content.select_one("#signatureBlock_text") if content else None
        attachments = [
            {"name": _clean(a.get_text()), "link": a["id"]}
            for a in panel.select('a[id$=":downloadFile"]')
        ]
        ar = panel.select_one('a[id$=":getARFile"]')
        messages.append({
            "id": msg_id,
            "from": field("[id='IdStOrigineLibel']", "De :"),
            "to": field("[id='IdStDestinationLibel']", "A :"),
            "title": field("[id='IdStLibelleMsg']"),
            "date": _iso(field("[id='IdStDateCreation']")),
            "direction": "in" if "detailMessageListeDemandeLeft" in (panel.get("class") or []) else "out",
            "body": body,
            "signature": _clean(sig.get_text(" ")) if sig else "",
            "attachments": attachments,
            "receipt_link": ar["id"] if ar else None,
        })
    messages.reverse()
    closed = "Cette demande est terminée" in exp.get_text()
    return {"id": demande_id, "closed": closed, "messages": messages}


def parse_topics(html: str) -> list[dict]:
    """Subjects of the "Écrire" menu: code, label and category path."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen = set()
    for a in soup.select('form#menu a[onclick*="codeDemande"]'):
        m = re.search(r'name:"codeDemande",value:"(\d+)"', a["onclick"])
        mid = re.search(r'menu:menuUsager_menuid",value:"[^|"]*\|([\d_]+)"', a["onclick"])
        if not m:
            continue
        key = (m.group(1), mid.group(1) if mid else "")
        if key in seen:
            continue
        seen.add(key)
        cats = []
        for li in a.find_parents("li"):
            parent_a = li.find("a", recursive=False)
            if parent_a is not None and parent_a is not a:
                cats.append(_clean(parent_a.get_text(" ")))
        out.append({
            "code": int(m.group(1)),
            "menu_id": key[1],
            "label": _clean(a.get_text(" ")),
            "category": " > ".join(reversed([c for c in cats if c and c != "Écrire"])),
        })
    return out


def parse_recap(text: str) -> str:
    """The "Résumé de votre demande" block of the recap page, without the buttons."""
    m = re.search(r"Résumé de votre demande\s*(.*?)\s*(?:Envoyer\b|$)", text, re.S)
    body = m.group(1) if m else text
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def filename_from_cd(cd: str | None, default: str) -> str:
    if cd:
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
        if m:
            return urllib.parse.unquote(m.group(1))
        m = re.search(r'filename="?([^";]+)', cd)
        if m:
            return urllib.parse.unquote(m.group(1))
    return default


def safe_attachment_name(path: Path) -> str:
    """The messagerie accepts only alphanumeric file names (plus the extension)."""
    stem = unicodedata.normalize("NFKD", path.stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9]", "", stem) or "document"
    return f"{stem[:60]}{path.suffix.lower()}"


def check_attachments(paths: list[Path]) -> None:
    total = 0
    for p in paths:
        if not p.is_file():
            raise click.BadParameter(f"{p}: no such file", param_hint="--attach")
        if p.suffix.lower() not in ALLOWED_EXT:
            raise click.BadParameter(
                f"{p.name}: type not accepted; allowed: {', '.join(sorted(ALLOWED_EXT))}",
                param_hint="--attach")
        total += p.stat().st_size
    if total > MAX_ATTACH_BYTES:
        raise click.BadParameter(f"attachments total {total / 1e6:.1f} MB > 20 MB limit", param_hint="--attach")


# ---------------------------------------------------------------------------
# CDP plumbing
# ---------------------------------------------------------------------------

def _http_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _targets_or_none(cdp_url: str) -> list[dict] | None:
    try:
        return _http_json(cdp_url.rstrip("/") + "/json")
    except Exception:  # noqa: BLE001 — any failure means "no Chrome there"
        return None


def _chrome_binary() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    return None


def _launch_chrome(cdp_url: str, url: str) -> None:
    port = urllib.parse.urlparse(cdp_url).port or 9224
    binary = _chrome_binary()
    if not binary:
        raise click.ClickException(
            "Chrome not found; install Google Chrome or start one with "
            f"--remote-debugging-port={port} --user-data-dir={PROFILE_DIR}")
    log.info("starting the dedicated Chrome (profile %s, port %s)", PROFILE_DIR, port)
    subprocess.Popen(
        [binary, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _targets(cdp_url: str, auto_launch: bool) -> list[dict]:
    targets = _targets_or_none(cdp_url)
    if targets is not None:
        return targets
    host = urllib.parse.urlparse(cdp_url).hostname
    if not auto_launch or host not in ("localhost", "127.0.0.1", "::1"):
        raise click.ClickException(f"No Chrome DevTools at {cdp_url}; drop --no-launch or start Chrome.")
    _launch_chrome(cdp_url, HOME_URL)
    for _ in range(40):
        time.sleep(0.5)
        if (targets := _targets_or_none(cdp_url)) is not None:
            return targets
    raise click.ClickException("Chrome started but its DevTools port never opened.")


class Tab:
    """One Chrome tab driven over its CDP websocket."""

    def __init__(self, ws_url: str):
        # Chrome rejects the handshake when an Origin header is present.
        self.ws = websocket.create_connection(ws_url, timeout=180, suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise click.ClickException(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def ev(self, expression: str):
        res = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"].get("exception", {}).get("description") or res["exceptionDetails"].get("text")
            raise click.ClickException(f"page error: {str(detail)[:300]}")
        return res.get("result", {}).get("value")

    def navigate(self, url: str, timeout: float = 30) -> str:
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        time.sleep(0.5)
        while time.time() < deadline:
            try:
                if self.ev("document.readyState") == "complete":
                    break
            except click.ClickException:
                pass  # context swapped mid-navigation
            time.sleep(0.3)
        return self.ev("location.href")

    def wait_ajax(self) -> None:
        if not self.ev(WAIT_JS):
            raise click.ClickException("the messagerie did not answer within 30 s; retry.")
        time.sleep(0.3)

    def fetch_b64(self, url: str, opts: dict | None = None) -> dict:
        js = FETCH_B64_JS.replace("__URL__", json.dumps(url)).replace("__OPTS__", json.dumps(opts or {}))
        return json.loads(self.ev(js))

    def set_files(self, selector: str, files: list[str]) -> None:
        self.call("DOM.enable")
        root = self.call("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node = self.call("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        if not node:
            raise click.ClickException(f"file input {selector} not found on the page")
        self.call("DOM.setFileInputFiles", {"nodeId": node, "files": files})


def open_tab(ctx: click.Context) -> Tab:
    """Attach to an impots tab of the dedicated Chrome, opening one if needed."""
    o = ctx.obj
    targets = _targets(o["cdp_url"], o["auto_launch"])
    pages = [t for t in targets if t.get("type") == "page"]
    mine = [t for t in pages if "impots.gouv.fr" in (t.get("url") or "")]
    if mine:
        target = mine[0]
    else:
        target = _http_json(o["cdp_url"].rstrip("/") + "/json/new?" + urllib.parse.quote(HOME_URL, safe=""), "PUT")
        time.sleep(1)
    return Tab(target["webSocketDebuggerUrl"])


def ensure_on_site(tab: Tab) -> None:
    """Be on a logged-in cfspart page, so in-page fetches carry the session."""
    href = tab.ev("location.href") or ""
    if not href.startswith(ORIGIN):
        href = tab.navigate(HOME_URL)
    if not href.startswith(ORIGIN):
        raise click.ClickException(NOT_LOGGED)
    probe = tab.ev("(async()=>{const r=await fetch('/enp/accueilensupres.do');"
                   "return JSON.stringify({url:r.url, html:await r.text()});})()")
    data = json.loads(probe)
    if not data["url"].startswith(ORIGIN) or not parse_logged_in(data["html"]):
        raise click.ClickException(NOT_LOGGED)


def open_messagerie(tab: Tab) -> None:
    ensure_on_site(tab)
    href = tab.navigate(MSG_URL)
    if "portailpart" not in href and "gaia2" not in href:
        raise click.ClickException(NOT_LOGGED)
    tab.wait_ajax()


def load_all_demandes(tab: Tab) -> list[dict]:
    """Every demande, walking the paginator when there are more than one page."""
    html = tab.ev("document.documentElement.outerHTML")
    rows = parse_demandes(html)
    pages = tab.ev("(()=>{const w=PF('listeDemandesUsager'); return w&&w.paginator? w.paginator.cfg.pageCount:1})()") or 1
    for page in range(1, min(int(pages), 50)):
        tab.ev(f"PF('listeDemandesUsager').paginator.setPage({page})")
        tab.wait_ajax()
        rows += parse_demandes(tab.ev("document.documentElement.outerHTML"))
    if pages > 50:
        raise click.ClickException("more than 50 pages of demandes; the page cap was reached.")
    return rows


def goto_demande(tab: Tab, demande_id: str) -> None:
    """Show the page holding a demande and expand its row."""
    for _ in range(50):
        found = tab.ev(f"""(()=>{{const tr=document.querySelector('tr[data-rk="{demande_id}"]');
            if(!tr) return null; const a=tr.querySelector('a[id$=":numDemande"]');
            const open = tr.nextElementSibling && tr.nextElementSibling.classList.contains('ui-expanded-row-content');
            if(!open) a.click(); return true;}})()""")
        if found:
            tab.wait_ajax()
            return
        more = tab.ev("""(()=>{const p=PF('listeDemandesUsager').paginator; if(!p) return false;
            const n=p.getCurrentPage()+1; if(n>=p.cfg.pageCount) return false; p.setPage(n); return true;})()""")
        if not more:
            break
        tab.wait_ajax()
    raise click.ClickException(f"demande {demande_id} not found; list ids with `impots messages`.")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def emit(data, as_json: bool, render) -> None:
    if as_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        render(data)


def write_file(dest: Path, b64: str) -> int:
    data = base64.b64decode(b64)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return len(data)


def unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(2, 1000):
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
    raise click.ClickException(f"too many files named like {dest}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group(epilog="\b\nExamples:\n  impots documents\n  impots download --all\n  impots messages\n  impots message 1234567890")
@click.option("--cdp-url", default=DEFAULT_CDP, show_default=True, help="DevTools URL of the dedicated Chrome.")
@click.option("--launch/--no-launch", "auto_launch", default=True, show_default=True,
              help="Start the dedicated Chrome when it is not running.")
@click.option("--debug", is_flag=True, help="Log debug details to stderr.")
@click.pass_context
def cli(ctx: click.Context, cdp_url: str, auto_launch: bool, debug: bool) -> None:
    """Documents and messagerie sécurisée of impots.gouv.fr (espace particulier)."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(message)s", stream=sys.stderr)
    ctx.obj = {"cdp_url": cdp_url, "auto_launch": auto_launch}


@cli.command(epilog="\b\nExamples:\n  impots login\n  impots login --timeout 600")
@click.option("--timeout", default=300, show_default=True, help="Seconds to wait for the sign-in.")
@click.pass_context
def login(ctx: click.Context, timeout: int) -> None:
    """Open the dedicated Chrome on the login page and wait for the sign-in."""
    tab = open_tab(ctx)
    tab.navigate(HOME_URL)
    tab.call("Page.bringToFront")
    log.info("Sign in in the Chrome window (numéro fiscal + mot de passe, or FranceConnect)…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            href = tab.ev("location.href") or ""
            html = (tab.ev("document.documentElement.outerHTML") or "") if href.startswith(ORIGIN) else ""
        except click.ClickException:
            html = ""
        if parse_logged_in(html):
            click.echo("signed in")
            return
        time.sleep(2)
    raise click.ClickException(f"not signed in after {timeout} s; re-run `impots login`.")


@cli.command(epilog="\b\nExamples:\n  impots whoami\n  impots whoami --json")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def whoami(ctx: click.Context, as_json: bool) -> None:
    """Show the signed-in account: name, numéro fiscal, unread messages."""
    tab = open_tab(ctx)
    ensure_on_site(tab)
    home = parse_home(tab.ev("(async()=>await (await fetch('/enp/accueilensupres.do')).text())()"))
    msg_html = tab.ev(f"(async()=>await (await fetch({json.dumps(MSG_URL)})).text())()")
    data = {**home, "fiscal_number": parse_fiscal_number(msg_html)}
    emit(data, as_json, lambda d: click.echo(
        f"{d['name']}\t{d['fiscal_number']}\t{d['unread_messages']} unread message(s)"))


@cli.command(epilog="\b\nExamples:\n  impots documents\n  impots documents --year 2026\n"
                    "  impots documents --match 'avis d.impôt' --json")
@click.option("--year", type=int, multiple=True, help="Only these years (repeatable).")
@click.option("--match", help="Case-insensitive regex on the label (client-side).")
@click.option("--limit", type=int, default=0, help="At most N documents (0 = all).")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def documents(ctx: click.Context, year: tuple[int, ...], match: str | None, limit: int, as_json: bool) -> None:
    """List the documents of the "Documents" page, newest year first."""
    docs = _list_documents(ctx, year, match)
    if limit:
        docs = docs[:limit]
    for d in docs:
        d.pop("token")

    def render(rows):
        for d in rows:
            desc = f" — {d['description']}" if d["description"] else ""
            click.echo(f"{d['year']}  {d['label']}{desc}\t{d['id']}")
    emit(docs, as_json, render)


def _list_documents(ctx: click.Context, years=(), match: str | None = None, tab: Tab | None = None) -> list[dict]:
    tab = tab or open_tab(ctx)
    ensure_on_site(tab)
    res = tab.fetch_b64(DOCS_URL)
    if not res["url"].startswith(ORIGIN):
        raise click.ClickException(NOT_LOGGED)
    docs = parse_documents(base64.b64decode(res["b64"]).decode("utf-8", "replace"))
    if years:
        docs = [d for d in docs if d["year"] in years]
    if match:
        rx = re.compile(match, re.I)
        docs = [d for d in docs if rx.search(d["label"]) or rx.search(d["description"])]
    return docs


@cli.command(epilog="\b\nExamples:\n  impots download --all\n  impots download --year 2025 --year 2026 -o ~/Taxes\n"
                    "  impots download 0123456789 abcdef0123\n  impots download --match 'taxe fonci' --json")
@click.argument("ids", nargs=-1)
@click.option("--all", "all_", is_flag=True, help="Every document of every year.")
@click.option("--year", type=int, multiple=True, help="Only these years (repeatable).")
@click.option("--match", help="Case-insensitive regex on the label (client-side).")
@click.option("-o", "--out", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=DOWNLOAD_DIR, show_default=True, help="Target directory (one sub-folder per year).")
@click.option("--force", is_flag=True, help="Re-download files that already exist.")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def download(ctx, ids, all_, year, match, out_dir: Path, force: bool, as_json: bool) -> None:
    """Download documents as PDF into OUT/<year>/<label>.pdf (existing files are skipped)."""
    if not (ids or all_ or year or match):
        raise click.UsageError("say what to download: IDS, --all, --year or --match "
                               "(e.g. `impots download --all`).")
    tab = open_tab(ctx)
    docs = _list_documents(ctx, year, match, tab)
    if ids:
        known = {d["id"] for d in docs}
        missing = [i for i in ids if i not in known]
        if missing:
            raise click.ClickException(f"unknown document id(s) {', '.join(missing)}; list them with `impots documents`.")
        docs = [d for d in docs if d["id"] in ids]
    out_dir = out_dir.expanduser()
    results = []
    for d in docs:
        dest = out_dir / d["file"]
        if dest.exists() and not force:
            results.append({"id": d["id"], "path": str(dest.resolve()), "status": "exists"})
            continue
        res = tab.fetch_b64(f"{ORIGIN}/enp/Affichage_Document_PDF?idEnsua={d['token']}")
        if res["status"] != 200 or "pdf" not in (res["ct"] or ""):
            if not res["url"].startswith(ORIGIN):
                raise click.ClickException(NOT_LOGGED)
            log.warning("skip %s (%s): HTTP %s %s", d["id"], d["label"], res["status"], res["ct"])
            results.append({"id": d["id"], "path": None, "status": f"error {res['status']}"})
            continue
        size = write_file(dest, res["b64"])
        log.info("%s  %s (%d kB)", d["year"], d["label"], size // 1024)
        results.append({"id": d["id"], "path": str(dest.resolve()), "status": "downloaded", "bytes": size})

    def render(rows):
        for r in rows:
            click.echo(f"{r['status']}\t{r['path']}\t{r['id']}")
    emit(results, as_json, render)


@cli.command(epilog="\b\nExamples:\n  impots messages\n  impots messages --unread\n  impots messages --limit 5 --json")
@click.option("--unread", is_flag=True, help="Only demandes with unread replies.")
@click.option("--limit", type=int, default=0, help="At most N demandes (0 = all).")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def messages(ctx: click.Context, unread: bool, limit: int, as_json: bool) -> None:
    """List the demandes (threads) of the messagerie, latest message first."""
    tab = open_tab(ctx)
    open_messagerie(tab)
    rows = load_all_demandes(tab)
    rows.sort(key=lambda r: (r["last_message"] or "", r["id"]), reverse=True)
    if unread:
        rows = [r for r in rows if r["unread"]]
    if limit:
        rows = rows[:limit]
    for r in rows:
        r.pop("index")

    def render(rs):
        for r in rs:
            flag = "● " if r["unread"] else ""
            click.echo(f"{r['last_message']}  {flag}{r['subject']} — {r['service']} — {r['status']}\t{r['id']}")
    emit(rows, as_json, render)


@cli.command(epilog="\b\nExamples:\n  impots message 1234567890\n  impots message 1234567890 1234567891 --json\n"
                    "  impots message 1234567890 --save -o ~/Taxes/messagerie")
@click.argument("ids", nargs=-1, required=True)
@click.option("--save", is_flag=True, help="Also download attachments and receipts (accusés de réception).")
@click.option("-o", "--out", "out_dir", type=click.Path(file_okay=False, path_type=Path),
              default=DOWNLOAD_DIR / "messagerie", show_default=True, help="Target directory for --save.")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def message(ctx, ids, save: bool, out_dir: Path, as_json: bool) -> None:
    """Read one or more demandes: every message, oldest first, with attachments.

    Opening a demande marks its replies as read on the site.
    """
    tab = open_tab(ctx)
    open_messagerie(tab)
    threads = []
    for did in ids:
        goto_demande(tab, did)
        thread = parse_thread(tab.ev("document.documentElement.outerHTML"), did)
        if save:
            _save_thread_files(tab, thread, out_dir.expanduser() / did)
        for m in thread["messages"]:
            for a in m["attachments"]:
                a.pop("link")
            m["has_receipt"] = bool(m.pop("receipt_link"))
        threads.append(thread)

    def render(ts):
        for t in ts:
            click.echo(f"# demande {t['id']}{' (closed)' if t['closed'] else ''}")
            for m in t["messages"]:
                click.echo(f"\n## {m['date']}  {m['from']} → {m['to']}  [{m['title']}]")
                click.echo(m["body"])
                if m["signature"]:
                    click.echo(f"-- {m['signature']}")
                for a in m["attachments"]:
                    click.echo(f"📎 {a['name']}" + (f"\t{a['path']}" if a.get("path") else ""))
                if m.get("receipt_path"):
                    click.echo(f"accusé de réception\t{m['receipt_path']}")
            click.echo("")
    emit(threads, as_json, render)


def _save_thread_files(tab: Tab, thread: dict, folder: Path) -> None:
    for m in thread["messages"]:
        links = [(a, a["link"], a["name"]) for a in m["attachments"]]
        if m["receipt_link"]:
            links.append((m, m["receipt_link"], f"Accuse_de_reception_{m['date']}_{m['id']}.pdf"))
        for holder, link, name in links:
            js = FORM_DOWNLOAD_JS.replace("__ID__", json.dumps(link))
            res = json.loads(tab.ev(js))
            if res["status"] != 200 or "html" in (res["ct"] or ""):
                log.warning("could not download %s (HTTP %s)", name, res["status"])
                continue
            if holder is not m:
                name = filename_from_cd(res["cd"], name)
            dest = folder / name
            if dest.exists():
                holder_key = "receipt_path" if holder is m else "path"
                holder[holder_key] = str(dest.resolve())
                continue
            write_file(dest, res["b64"])
            holder["receipt_path" if holder is m else "path"] = str(dest.resolve())
            log.info("saved %s", dest)


@cli.command(epilog="\b\nExamples:\n  impots topics\n  impots topics --json")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def topics(ctx: click.Context, as_json: bool) -> None:
    """List the subjects a new demande can be filed under (codes for `send --topic`)."""
    tab = open_tab(ctx)
    open_messagerie(tab)
    rows = parse_topics(tab.ev("document.documentElement.outerHTML"))

    def render(rs):
        for r in rs:
            click.echo(f"{r['code']:>3}  {r['label']}  ({r['category']})\t{r['menu_id']}")
    emit(rows, as_json, render)


@cli.command(epilog="\b\nExamples:\n  impots send --topic 14 --body 'Bonjour, …'\n"
                    "  impots send --topic 14 --body-file demande.txt --attach avis.pdf --yes\n"
                    "  impots send --topic 43 --body-file q.txt --json")
@click.option("--topic", "code", type=int, required=True, help="Subject code from `impots topics`.")
@click.option("--menu-id", help="Pick one entry when a code appears under several categories (see `impots topics`).")
@click.option("--body", help="Message text.")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Read the message text from a file.")
@click.option("--attach", "attach", multiple=True, type=click.Path(path_type=Path),
              help="File to attach (repeatable; PDF/JPEG/TIFF/PNG/XLS/XLSX/ODS/ODT, 20 MB total).")
@click.option("--yes", is_flag=True, help="Really send. Without it: fill the form up to the recap, then abandon.")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
@click.pass_context
def send(ctx, code: int, menu_id: str | None, body: str | None, body_file: Path | None,
         attach: tuple[Path, ...], yes: bool, as_json: bool) -> None:
    """File a new demande in the messagerie (dry run unless --yes).

    The dry run goes through the site's own validation and prints the recap
    the DGFiP would receive, then abandons the form: nothing is sent or saved.
    """
    if bool(body) == bool(body_file):
        raise click.UsageError("give exactly one of --body or --body-file "
                               "(e.g. `impots send --topic 14 --body 'Bonjour…'`).")
    text = body if body is not None else body_file.read_text()
    if not text.strip():
        raise click.BadParameter("the message is empty", param_hint="--body")
    paths = [p.expanduser() for p in attach]
    check_attachments(paths)

    tab = open_tab(ctx)
    open_messagerie(tab)
    # Snapshot taken before sending: a send counts only once a new demande shows up.
    before = {d["id"] for d in load_all_demandes(tab)} if yes else set()
    if yes:
        open_messagerie(tab)
    entries = [t for t in parse_topics(tab.ev("document.documentElement.outerHTML")) if t["code"] == code]
    if menu_id:
        entries = [t for t in entries if t["menu_id"] == menu_id]
    if not entries:
        raise click.ClickException(f"unknown topic {code}; list them with `impots topics`.")
    topic = entries[0]
    clicked = tab.ev(f"""(()=>{{const a=[...document.querySelectorAll('#menu a[onclick*="codeDemande"]')]
        .find(a=>a.getAttribute('onclick').includes('value:"{code}"') && a.getAttribute('onclick').includes('|{topic["menu_id"]}"'));
        if(!a) return false; a.click(); return true;}})()""")
    if not clicked:
        raise click.ClickException(f"topic {code} is not clickable in the menu; check `impots topics`.")
    tab.wait_ajax()
    form = tab.ev("""JSON.stringify((()=>{const f=document.getElementById('formulaire'); if(!f) return null;
        return [...f.querySelectorAll('input:not([type=hidden]):not([type=file]), select, textarea')]
          .filter(e=>e.offsetParent && !e.closest('.ql-tooltip') && !e.closest('.ui-dialog')).map(e=>e.id||e.name);})())""")
    fields = json.loads(form)
    if fields is None:
        raise click.ClickException(f"topic {code} did not open a form; it may need the website.")
    if fields:
        _abandon(tab)
        raise click.ClickException(
            f"topic {code} has extra form fields ({', '.join(fields)}) this CLI does not fill; "
            "use topic 14 or the website.")
    ok = tab.ev(f"""(()=>{{const w=PF('widget_formulaire_texte'); if(!w||!w.editor) return false;
        w.editor.setText({json.dumps(text)}); return true;}})()""")
    if not ok:
        _abandon(tab)
        raise click.ClickException("message editor not found on the form; the site layout changed.")

    uploaded = []
    with tempfile.TemporaryDirectory() as tmp:
        for p in paths:
            safe = Path(tmp) / safe_attachment_name(p)
            shutil.copyfile(p, safe)
            tab.set_files("#formulaire\\:pjUpload_input", [str(safe)])
            time.sleep(0.5)
            tab.wait_ajax()
            listed = tab.ev("document.getElementById('formulaire:zonePJ').innerText") or ""
            if safe.name not in listed:
                err = tab.ev("(document.querySelector('#formulaire .ui-messages-error, #formulaire .ui-message-error')||{}).innerText||''")
                _abandon(tab)
                raise click.ClickException(f"upload of {p.name} was refused: {_clean(err) or 'no reason given'}")
            uploaded.append(safe.name)

    tab.ev("document.getElementById('formulaire:validerFormBtn').click()")
    tab.wait_ajax()
    if not tab.ev("!!document.getElementById('creatDmdBtnForm:creationDmdBtn')"):
        err = tab.ev("(document.querySelector('#formulaire')||document.body).innerText") or ""
        _abandon(tab)
        raise click.ClickException(f"the site rejected the form: {_clean(err)[:300]}")
    recap = parse_recap(tab.ev("(document.querySelector('#contenu')||document.body).innerText") or "")
    result = {"topic": code, "label": topic["label"], "attachments": uploaded, "sent": False,
              "recap": recap}
    if not yes:
        _abandon(tab)
        log.info("dry run: form validated by the site, then abandoned. Add --yes to send.")
    else:
        tab.ev("document.getElementById('creatDmdBtnForm:creationDmdBtn').click()")
        tab.wait_ajax()
        page = tab.ev("(document.querySelector('#contenu')||document.body).innerText") or ""
        log.debug("page after send click: %s", _clean(page)[:600])
        # The page text echoes the message body, so any number read from it is
        # unreliable; the new row in "Mes échanges" is the only confirmation.
        created = None
        for attempt in range(SEND_CHECKS):
            open_messagerie(tab)
            created = new_demande(before, load_all_demandes(tab))
            if created:
                break
            if attempt < SEND_CHECKS - 1:
                time.sleep(SEND_CHECK_DELAY)
        if not created:
            raise click.ClickException(
                "send NOT confirmed: no new demande appeared in « Mes échanges ». It may still "
                "have gone through: check the website before sending again (duplicates are "
                "possible). Run with --debug to see the page shown after the click.")
        result.update({"sent": True, "demande": created["id"], "service": created["service"],
                       "subject": created["subject"]})

    def render(r):
        if r["sent"]:
            click.echo(f"sent\t{r['demande']}")
        else:
            click.echo(f"DRY RUN (nothing sent) — topic {r['topic']}: {r['label']}")
            click.echo(r["recap"])
    emit(result, as_json, render)


def _abandon(tab: Tab) -> None:
    """Leave the compose form without sending or saving a draft."""
    for btn in ("creatDmdBtnForm:abandoonerDmdBtn", "formulaire:abandonnerBrouillonFormBtn"):
        if tab.ev(f"!!document.getElementById('{btn}')"):
            tab.ev(f"document.getElementById('{btn}').click()")
            tab.wait_ajax()
            break
    for confirm in ("dlgFormAbandon:confirmerAbandonDmdBtnDlg", "dlgFormAbandonSaisie:ConfirmerAbandonnerBtnDlg"):
        if tab.ev(f"(()=>{{const b=document.getElementById('{confirm}'); return !!(b && b.offsetParent);}})()"):
            tab.ev(f"document.getElementById('{confirm}').click()")
            tab.wait_ajax()
            break


if __name__ == "__main__":
    cli()
