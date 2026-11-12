#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "websocket-client==1.9.2",
#     "beautifulsoup4==4.15.0",
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Offline tests for impots-cli: HTML parsers, file naming, input checks, CLI errors.

Fixtures are synthetic HTML shaped like the real pages (same ids and classes),
with made-up names, numbers and texts.
"""

import json
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import impots_cli as ic  # noqa: E402

DOCS_HTML = """
<html><body><main id="content">
<div class="fr-tabs"><div class="fr-tabs__panel">
  <a href="Affichage_Document_PDF?idEnsua=AAAA1111">Avis d'impôt 2026 sur les revenus 2025</a>
</div></div>
<div class="fr-accordions-group">
 <section class="fr-accordion"><div class="fr-collapse" id="2026">
  <div class="fr-card"><div class="fr-card__body"><div class="fr-card__content">
   <h3><a href="Affichage_Document_PDF?idEnsua=AAAA1111">Avis d'impôt 2026 sur les revenus 2025</a></h3>
   <p class="fr-card__desc">1 RUE DE L'EXEMPLE, 00000 VILLE<br>Date limite de paiement&nbsp;: 15/12/2026</p>
  </div></div></div>
  <div class="fr-collapse" id="collapseGrid-1-2">
   <div class="fr-card"><h3><a href="Affichage_Document_PDF?idEnsua=AAAA1111">Avis d'impôt 2026 sur les revenus 2025</a></h3></div>
  </div>
 </div></section>
 <section class="fr-accordion"><div class="fr-collapse" id="2024">
  <div class="fr-card"><h3><a href="Affichage_Document_PDF?idEnsua=BBBB2222">Lettre de relance taxes foncières</a></h3></div>
  <div class="fr-card"><h3><a href="Affichage_Document_PDF?idEnsua=CCCC3333">Lettre de relance taxes foncières</a></h3></div>
  <div class="fr-card"><h3><a href="Affichage_Document_PDF?idEnsua=DDDD4444">Impôt sur les revenus 2023 – Montant de l’avance</a></h3></div>
  <div class="fr-collapse" id="collapseGridRwd-3-1">
   <div class="fr-card fr-card--horizontal"><div class="fr-card__desc">
    <p id="titreLieRwd-3-1-1" class="fr-text--lg"> Avis de taxes foncières 2024</p>
    <p class="fr-text--sm">1 RUE DE L'EXEMPLE, 00000 VILLE<br>Date limite de paiement&nbsp;: 15/10/2024</p></div>
    <form action="Affichage_Document_PDF" method="GET"><input type="hidden" name="idEnsua" value="EEEE5555"><button title="Visualiser"></button></form>
   </div>
   <div class="fr-card fr-card--horizontal"><div class="fr-card__desc">
    <p id="titreLieRwd-3-1-2"> Accusé de réception de télédéclaration des revenus 2023</p></div>
    <form action="Affichage_Document_PDF" method="GET"><input type="hidden" name="idEnsua" value="FFFF6666"></form>
   </div>
  </div>
 </div></section>
 <section class="fr-accordion"><div class="fr-collapse" id="2018"></div></section>
</div>
<button class="fr-btn--logout"></button><form action="/enp/j_spring_security_logout.ex"></form>
</main></body></html>
"""

HOME_HTML = """
<a href="messagerie.do" class="fr-btn fr-btn-icon fr-btn--mail" title="Messagerie 2 nouveaux messages">Messagerie</a>
<button id="btnMenuDeroulant" type="button">JEAN EXEMPLE</button>
"""

LIST_HTML = """
<html><body>
<form id="listeDemandesForm">
<table><tbody id="listeDemandesForm:listeDemandes_data">
 <tr data-ri="0" data-rk="1234567890" class="ui-widget-content listeDemandeHeaderDmdWithUnreadMsg">
  <td><a id="listeDemandesForm:listeDemandes:0:numDemande">1234567890</a></td>
  <td><span id="listeDemandesForm:listeDemandes:0:libelledemandezu">Question sur l'impôt sur...</span></td>
  <td> SIP EXEMPLE </td><td>Internet</td><td>Demande en cours</td><td>01/02/2026</td><td>03/02/2026</td>
 </tr>
 <tr class="ui-expanded-row-content ui-widget-content"><td colspan="7">
  <div class="ui-panel detailMessageListeDemande detailMessageListeDemandeLeft" data-widget="panel_detail_Msg000000002">
   <div class="ui-panel-titlebar"><span class="ui-panel-title">
    <div id="IdStOrigineLibel"><label>De : </label><span>SIP EXEMPLE</span></div>
    <div id="IdStLibelleMsg"><span>Réponse de l'administration </span></div>
    <div id="IdStDateCreation">03/02/2026</div>
    <div id="IdStDestinationLibel"><label>A : </label>JEAN EXEMPLE</div>
   </span></div>
   <div class="ui-panel-content">
    <span class="formulaireVerbaimPre"><p>Bonjour,</p><p><br></p><p>Voici le RIB.</p></span>
    <div id="signatureBlock_text"><span class="preformatted">Agent EXEMPLE
SIP EXEMPLE</span></div>
    <a id="listeDemandesForm:listeDemandes:0:msg_000000002:pjAddedScroller:0:downloadFile">RIB exemple.pdf</a>
    <p>Cette demande est terminée, si vous souhaitez y répondre, vous devez déposer une autre demande.</p>
   </div>
  </div>
  <div class="ui-panel detailMessageListeDemande detailMessageListeDemandeRight" data-widget="panel_detail_Msg000000001">
   <div class="ui-panel-titlebar"><span class="ui-panel-title">
    <div id="IdStOrigineLibel"><label>De : </label>JEAN EXEMPLE</div>
    <div id="IdStLibelleMsg">Ma demande N° 1234567890</div>
    <div id="IdStDateCreation">01/02/2026</div>
    <div id="IdStDestinationLibel"><label>A : </label>SIP EXEMPLE</div>
   </span></div>
   <div class="ui-panel-content">
    <div><span class="formulaireVerbaimPre"><p>Bonjour</p><p>Ligne deux</p></span></div>
    <div id="listeDemandesForm:listeDemandes:0:msg_000000001:arBlock">
     <a id="listeDemandesForm:listeDemandes:0:msg_000000001:getARFile" href="#"></a></div>
   </div>
  </div>
 </td></tr>
 <tr data-ri="1" data-rk="1234567891" class="ui-widget-content listeDemandeHeaderDmdWithOutUnreadMsg">
  <td><a id="listeDemandesForm:listeDemandes:1:numDemande">1234567891</a></td>
  <td><span>Mon changement d'adresse postale</span></td>
  <td>SIP EXEMPLE</td><td>Internet</td><td>Demande traitée</td><td>05/01/2020</td><td>06/01/2020</td>
 </tr>
</tbody></table>
</form>
<div id="listeDemandesForm:listeDemandes:0:libelledemandezu:j_idt1" class="ui-tooltip">
 <div class="ui-tooltip-text">Question sur l'impôt sur le revenu</div></div>
<span>N° fiscal : 1234567890123</span>
<form id="menu"><ul>
 <li><a>Questions sur mes impôts</a><ul>
  <li><a>Impôt sur le revenu</a><ul>
   <li><a onclick='PrimeFaces.ab({pa:[{name:"codeDemande",value:"43"},{name:"menu:menuUsager_menuid",value:"aaaa|1_2_0_0"}]});'>Question sur l'impôt sur le revenu</a></li>
  </ul></li>
  <li><a onclick='PrimeFaces.ab({pa:[{name:"codeDemande",value:"14"},{name:"menu:menuUsager_menuid",value:"bbbb|1_2_2"}]});'>Vous ne trouvez pas de sujet</a></li>
 </ul></li>
 <li><a>Paiement</a><ul>
  <li><a onclick='PrimeFaces.ab({pa:[{name:"codeDemande",value:"42"},{name:"menu:menuUsager_menuid",value:"cccc|1_1_3"}]});'>Changement de coordonnées bancaires</a></li>
  <li><a onclick='PrimeFaces.ab({pa:[{name:"codeDemande",value:"42"},{name:"menu:menuUsager_menuid",value:"cccc|1_1_3"}]});'>Changement de coordonnées bancaires</a></li>
 </ul></li>
</ul></form>
</body></html>
"""

RECAP_TEXT = """Courant octobre, une version modernisée…
Vous ne trouvez pas de sujet correspondant à votre demande
Saisie du formulaire > Récapitulatif > Accusé de Réception
Résumé de votre demande

Bonjour,

Test.

Liste des pièces jointes :
test.pdf
Envoyer
Enregistrer un brouillon
"""


@pytest.fixture
def docs():
    return ic.parse_documents(DOCS_HTML)


@pytest.fixture
def demandes():
    return ic.parse_demandes(LIST_HTML)


@pytest.fixture
def thread():
    return ic.parse_thread(LIST_HTML, "1234567890")


@pytest.fixture
def topics():
    return ic.parse_topics(LIST_HTML)


# --- documents -------------------------------------------------------------

def test_documents_deduplicated_across_tabs_and_nested_groups(docs):
    assert len(docs) == 6


def test_documents_year_from_accordion(docs):
    assert [d["year"] for d in docs] == [2026, 2024, 2024, 2024, 2024, 2024]


def test_linked_document_from_form(docs):
    assert docs[4]["file"] == "2024/Avis de taxes foncières 2024 - 1 RUE DE L'EXEMPLE, 00000 VILLE.pdf"


def test_linked_document_flag(docs):
    assert [d["linked"] for d in docs] == [False, False, False, False, True, True]


def test_linked_document_without_description(docs):
    assert docs[5]["label"] == "Accusé de réception de télédéclaration des revenus 2023"


def test_documents_description_joins_lines(docs):
    assert docs[0]["description"] == "1 RUE DE L'EXEMPLE, 00000 VILLE | Date limite de paiement : 15/12/2026"


def test_documents_duplicate_labels_get_numbered_files(docs):
    assert [d["file"] for d in docs[1:3]] == [
        "2024/Lettre de relance taxes foncières.pdf",
        "2024/Lettre de relance taxes foncières (2).pdf",
    ]


@pytest.mark.parametrize(("label", "desc", "name"), [
    ("Déclaration en ligne des revenus 2025 (le 07/05/2026, à 13:27)", "",
     "Déclaration en ligne des revenus 2025 (le 07-05-2026, à 13h27)"),
    ("Déclaration des revenus 2016 : revenus non salariés", "",
     "Déclaration des revenus 2016 - revenus non salariés"),
    ("Avis de taxe d'habitation 2026", "1 RUE X, 00000 VILLE | Date limite de paiement : 15/12/2026",
     "Avis de taxe d'habitation 2026 - 1 RUE X, 00000 VILLE"),
    ("Avis d'impôt 2026", "Date limite de paiement : 15/09/2026", "Avis d'impôt 2026"),
    ('Bad "name"?', "", "Bad name"),
])
def test_readable_name(label, desc, name):
    assert ic.readable_name(label, desc) == name


def test_documents_id_is_short_hash_of_token(docs):
    assert docs[0]["id"] == ic.doc_id("AAAA1111")


def test_documents_keep_token_for_download(docs):
    assert docs[0]["token"] == "AAAA1111"


@pytest.mark.parametrize(("label", "slug"), [
    ("Avis d'impôt 2026", "Avis_d_impot_2026"),
    ("Montant de l’avance", "Montant_de_l_avance"),
    ("Revenus 2023 – Montant", "Revenus_2023_Montant"),
    ("Déclaration (le 07/05/2026, à 13:27)", "Declaration_le_07_05_2026_a_13_27"),
    ("***", "document"),
])
def test_slugify(label, slug):
    assert ic.slugify(label) == slug


def test_logged_in_detects_logout_link():
    assert ic.parse_logged_in(DOCS_HTML) is True


def test_logged_out_page():
    assert ic.parse_logged_in("<html>Particuliers | authentification</html>") is False


# --- header / identity -----------------------------------------------------

def test_home_name():
    assert ic.parse_home(HOME_HTML)["name"] == "JEAN EXEMPLE"


def test_home_unread_count():
    assert ic.parse_home(HOME_HTML)["unread_messages"] == 2


def test_fiscal_number():
    assert ic.parse_fiscal_number(LIST_HTML) == "1234567890123"


# --- demandes list ---------------------------------------------------------

def test_demandes_count(demandes):
    assert len(demandes) == 2


def test_demande_full_subject_from_tooltip(demandes):
    assert demandes[0]["subject"] == "Question sur l'impôt sur le revenu"


def test_demande_subject_without_tooltip(demandes):
    assert demandes[1]["subject"] == "Mon changement d'adresse postale"


def test_demande_dates_iso(demandes):
    assert (demandes[0]["created"], demandes[0]["last_message"]) == ("2026-02-01", "2026-02-03")


@pytest.mark.parametrize(("index", "unread"), [(0, True), (1, False)])
def test_demande_unread_flag(demandes, index, unread):
    assert demandes[index]["unread"] is unread


def test_demande_service_trimmed(demandes):
    assert demandes[0]["service"] == "SIP EXEMPLE"


# --- thread ----------------------------------------------------------------

def test_thread_oldest_first(thread):
    assert [m["id"] for m in thread["messages"]] == ["000000001", "000000002"]


def test_thread_direction(thread):
    assert [m["direction"] for m in thread["messages"]] == ["out", "in"]


def test_thread_from_strips_label(thread):
    assert thread["messages"][1]["from"] == "SIP EXEMPLE"


def test_thread_to_strips_label(thread):
    assert thread["messages"][1]["to"] == "JEAN EXEMPLE"


def test_thread_body_paragraphs(thread):
    assert thread["messages"][1]["body"] == "Bonjour,\n\nVoici le RIB."


def test_thread_signature(thread):
    assert thread["messages"][1]["signature"] == "Agent EXEMPLE SIP EXEMPLE"


def test_thread_attachment_name(thread):
    assert thread["messages"][1]["attachments"][0]["name"] == "RIB exemple.pdf"


def test_thread_receipt_link(thread):
    assert thread["messages"][0]["receipt_link"].endswith(":getARFile")


def test_thread_closed(thread):
    assert thread["closed"] is True


def test_thread_not_expanded_is_an_error():
    with pytest.raises(ValueError, match="not expanded"):
        ic.parse_thread(LIST_HTML, "1234567891")


def test_thread_unknown_demande_is_an_error():
    with pytest.raises(ValueError, match="not in page"):
        ic.parse_thread(LIST_HTML, "9999999999")


# --- topics ----------------------------------------------------------------

def test_topics_codes_deduplicated_by_menu_entry(topics):
    assert [t["code"] for t in topics] == [43, 14, 42]


def test_topics_category_path(topics):
    assert topics[0]["category"] == "Questions sur mes impôts > Impôt sur le revenu"


def test_topics_menu_id(topics):
    assert topics[2]["menu_id"] == "1_1_3"


# --- send helpers ----------------------------------------------------------

def test_recap_keeps_summary_only():
    assert ic.parse_recap(RECAP_TEXT) == "Bonjour,\n\nTest.\n\nListe des pièces jointes :\ntest.pdf"


@pytest.mark.parametrize(("name", "safe"), [
    ("Relevé d'impôt_2026 (v2).pdf", "Relevedimpot2026v2.pdf"),
    ("scan.PDF", "scan.pdf"),
    ("___.png", "document.png"),
])
def test_safe_attachment_name(name, safe):
    assert ic.safe_attachment_name(Path(name)) == safe


def test_attachment_type_refused(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    with pytest.raises(click.BadParameter, match="type not accepted"):
        ic.check_attachments([f])


def test_attachment_missing(tmp_path):
    with pytest.raises(click.BadParameter, match="no such file"):
        ic.check_attachments([tmp_path / "absent.pdf"])


def test_attachment_total_size(tmp_path, monkeypatch):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"0" * 11)
    monkeypatch.setattr(ic, "MAX_ATTACH_BYTES", 20)
    with pytest.raises(click.BadParameter, match="20 MB limit"):
        ic.check_attachments([f, f])


@pytest.mark.parametrize(("cd", "name"), [
    ("attachment;filename=\"RIB%20SIP.pdf\"; filename*=UTF-8''RIB%20SIP.pdf", "RIB SIP.pdf"),
    ('inline;filename=Avis_2026.pdf', "Avis_2026.pdf"),
    (None, "fallback.pdf"),
])
def test_filename_from_content_disposition(cd, name):
    assert ic.filename_from_cd(cd, "fallback.pdf") == name


# --- CLI wiring (no browser) ------------------------------------------------

def test_download_requires_a_selection():
    res = CliRunner().invoke(ic.cli, ["--no-launch", "download"])
    assert "say what to download" in res.output


def test_send_requires_one_body():
    res = CliRunner().invoke(ic.cli, ["--no-launch", "send", "--topic", "14"])
    assert "exactly one of --body or --body-file" in res.output


def test_no_chrome_names_the_fix(monkeypatch):
    monkeypatch.setattr(ic, "_targets_or_none", lambda url: None)
    res = CliRunner().invoke(ic.cli, ["--no-launch", "documents"])
    assert "drop --no-launch" in res.output


class StubTab:
    """Answers the evaluations `documents` makes, from the synthetic pages."""

    def ev(self, expr):
        if "location.href" in expr:
            return ic.ORIGIN + "/enp/accueilensupres.do"
        return json.dumps({"url": ic.ORIGIN + "/enp/accueilensupres.do", "html": DOCS_HTML})

    def fetch_b64(self, url, opts=None):
        import base64
        return {"status": 200, "url": url, "b64": base64.b64encode(DOCS_HTML.encode()).decode()}


def test_documents_json_parses(monkeypatch):
    monkeypatch.setattr(ic, "open_tab", lambda ctx: StubTab())
    res = CliRunner().invoke(ic.cli, ["documents", "--year", "2024", "--json"])
    assert len(json.loads(res.output)) == 5


def test_documents_json_hides_token(monkeypatch):
    monkeypatch.setattr(ic, "open_tab", lambda ctx: StubTab())
    res = CliRunner().invoke(ic.cli, ["documents", "--json"])
    assert all("token" not in d for d in json.loads(res.output))


def test_documents_match_filter(monkeypatch):
    monkeypatch.setattr(ic, "open_tab", lambda ctx: StubTab())
    res = CliRunner().invoke(ic.cli, ["documents", "--match", "relance", "--json"])
    assert len(json.loads(res.output)) == 2


def test_download_unknown_id(monkeypatch):
    monkeypatch.setattr(ic, "open_tab", lambda ctx: StubTab())
    res = CliRunner().invoke(ic.cli, ["download", "ffffffffff"])
    assert "unknown document id(s) ffffffffff" in res.output


# --- send confirmation ------------------------------------------------------

OLD_ROW = {"id": "1111111111", "service": "SIP EXEMPLE", "subject": "Ancienne demande"}
NEW_ROW = {"id": "2222222222", "service": "SIP EXEMPLE", "subject": "Question sur l'impôt sur le revenu"}


def test_new_demande_found():
    assert ic.new_demande({"1111111111"}, [NEW_ROW, OLD_ROW]) == NEW_ROW


def test_new_demande_none_when_list_unchanged():
    assert ic.new_demande({"1111111111"}, [OLD_ROW]) is None


class StubSendTab:
    """Walks the send form: topic menu, empty extra fields, editor, recap, send button.

    The page after the click echoes a 10-digit number from the body, like the real one can.
    """

    def __init__(self, dialog=True):
        self.dialog = dialog
        self.clicked = []

    def ev(self, expr):
        if "offsetParent" in expr and ic.SEND_CONFIRM_BTN in expr:
            return self.dialog
        if ".click()" in expr:
            self.clicked.append(expr)
        if "outerHTML" in expr:
            return LIST_HTML
        if "querySelectorAll('input" in expr:
            return "[]"
        if "innerText" in expr:
            return "Récapitulatif … demande n° 9999999999 …"
        return True

    def wait_ajax(self):
        pass


@pytest.fixture
def send_tab():
    return StubSendTab()


@pytest.fixture
def send_stubs(monkeypatch, send_tab):
    monkeypatch.setattr(ic, "open_tab", lambda ctx: send_tab)
    monkeypatch.setattr(ic, "open_messagerie", lambda tab: None)
    monkeypatch.setattr(ic, "SEND_CHECK_DELAY", 0)
    return monkeypatch


def test_send_unconfirmed_is_an_error(send_stubs):
    send_stubs.setattr(ic, "load_all_demandes", lambda tab: [OLD_ROW])
    res = CliRunner().invoke(ic.cli, ["send", "--topic", "43", "--body", "Bonjour", "--yes"])
    assert "send NOT confirmed" in res.output


def test_send_unconfirmed_exit_code(send_stubs):
    send_stubs.setattr(ic, "load_all_demandes", lambda tab: [OLD_ROW])
    res = CliRunner().invoke(ic.cli, ["send", "--topic", "43", "--body", "Bonjour", "--yes"])
    assert res.exit_code == 1


def test_send_confirmed_reports_new_demande_id(send_stubs):
    lists = iter([[OLD_ROW], [OLD_ROW], [NEW_ROW, OLD_ROW]])
    send_stubs.setattr(ic, "load_all_demandes", lambda tab: next(lists))
    res = CliRunner().invoke(ic.cli, ["send", "--topic", "43", "--body", "Bonjour", "--yes"])
    assert res.output.strip() == "sent\t2222222222"


def test_send_clicks_the_confirmation_yes(send_stubs, send_tab):
    send_stubs.setattr(ic, "load_all_demandes", lambda tab: [OLD_ROW])
    CliRunner().invoke(ic.cli, ["send", "--topic", "43", "--body", "Bonjour", "--yes"])
    assert any(ic.SEND_CONFIRM_BTN in c for c in send_tab.clicked)


def test_send_without_confirmation_dialog_is_an_error(send_stubs, send_tab, monkeypatch):
    send_tab.dialog = False
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)
    send_stubs.setattr(ic, "load_all_demandes", lambda tab: [OLD_ROW])
    res = CliRunner().invoke(ic.cli, ["send", "--topic", "43", "--body", "Bonjour", "--yes"])
    assert "confirmation dialog did not open" in res.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
