"""Fünf Härtungen, gemessen statt behauptet.

Alle fünf haben gemeinsam, dass sie nichts kaputtmachen, solange niemand sie
sucht — und dass sie den Container umbringen oder die Tür öffnen, sobald es doch
jemand tut:

* Die **Login-Drossel** hing an einem Header, den der Absender selbst setzt. Mit
  einer neuen Zufallszahl pro Versuch griff sie nie, und ein sechsstelliger
  PIN-Raum stand ohne Bremse offen.
* Ihr **Speicher** wuchs mit jedem erfundenen Absender weiter.
* Die **DTD-Sperre** im CAMT-Import sah nur die ersten 8 KB. Neun Kilobyte
  Kommentar davor, und aus 9'797 Byte Datei wurden 300'000 Zeichen.
* Der **Upload** landete vollständig im Speicher, BEVOR die Grössenprüfung ihn
  ablehnte — gemessen 209 MB für ein versehentlich gewähltes Video.
* Das **PDF-Rendern** hatte weder Seiten- noch Pixelbudget. Eine 520-Byte-Datei
  mit einer übergrossen Seite ergab eine Spitze von 1091 MB gegen ein Limit von
  1024 MB.

Alle Daten hier sind erfunden.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from moneten.routers import auth_pin
from moneten.services import receipt_ocr
from moneten.services.camt053_parser import parse_camt053_all


@pytest.fixture(autouse=True)
def _drossel_leeren():
    """Der Zähler ist Modulzustand — er darf nicht in andere Tests lecken."""
    auth_pin._clear_failures()
    yield
    auth_pin._clear_failures()


# ---------------------------------------------------------------------------
# Login-Drossel
# ---------------------------------------------------------------------------
def test_drossel_laesst_sich_nicht_ueber_einen_header_umgehen(client: TestClient) -> None:
    """Der gemessene Weg: pro Versuch ein neuer erster ``X-Forwarded-For``.

    Vorher bekam jeder Fehlversuch damit einen eigenen Zähler, und die Grenze
    von zehn griff nie — übrig blieb nur die Rechenzeit von Argon2 gegen eine
    Million Kombinationen.

    Der Header trägt hier eine zweite, feste Adresse am Ende: das ist die, die
    der Reverse-Proxy anhängt, und sie entscheidet. Die ausführliche Fassung
    dieses Falls samt der Messwerte zu Uvicorn steht in ``test_absender.py``.
    """
    letzte = None
    for i in range(12):
        letzte = client.post(
            "/login",
            data={"pin": "000000"},
            headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.5"},
        )
    assert letzte is not None
    assert letzte.status_code == 429, (
        f"Nach zwölf Versuchen mit wechselndem Header antwortet die App mit "
        f"{letzte.status_code} — die Drossel greift nicht."
    )


def test_der_zaehler_waechst_nicht_unbegrenzt() -> None:
    """Jeder erfundene Absender legte einen Eintrag an, der nie wieder besucht
    wurde. Der Container hat 1 GB — hunderttausende Einträge sind ein spürbarer
    Teil davon, und am Ende holt der OOM-Killer den Prozess."""
    for i in range(auth_pin._FAIL_KEYS_MAX + 200):
        auth_pin._record_failure(f"erfunden-{i}")
    assert len(auth_pin._fail_times) <= auth_pin._FAIL_KEYS_MAX, (
        f"{len(auth_pin._fail_times)} Einträge — der Deckel greift nicht"
    )


def test_die_drossel_sperrt_weiterhin_denselben_absender(client: TestClient) -> None:
    """Gegenprobe: die eigentliche Aufgabe bleibt erfüllt."""
    for _ in range(auth_pin._FAIL_MAX):
        client.post("/login", data={"pin": "000000"})
    assert client.post("/login", data={"pin": "000000"}).status_code == 429


# ---------------------------------------------------------------------------
# XML: DTD-Sperre
# ---------------------------------------------------------------------------
def _camt_mit_dtd(vorlauf: int) -> bytes:
    """CAMT-Datei, deren DOCTYPE hinter ``vorlauf`` Byte Kommentar steht."""
    kommentar = b"<!-- " + b"x" * vorlauf + b" -->"
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n' + kommentar + b"\n"
        b'<!DOCTYPE Document [<!ENTITY lol "lollollollollollollol">]>\n'
        b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">\n'
        b"  <BkToCstmrStmt><GrpHdr><MsgId>&lol;</MsgId></GrpHdr></BkToCstmrStmt>\n"
        b"</Document>\n"
    )


def test_dtd_wird_auch_hinter_neun_kilobyte_kommentar_abgelehnt() -> None:
    """Die alte Sperre durchsuchte nur ``xml_bytes[:8192]``.

    Ein Kommentar davor schob die Deklaration aus dem Fenster — und weil die
    Datei mit ``<`` beginnt, ging sie zuverlässig in den XML-Zweig.
    """
    with pytest.raises(ValueError, match="DTD"):
        parse_camt053_all(_camt_mit_dtd(9000))


def test_dtd_ganz_vorne_wird_weiterhin_abgelehnt() -> None:
    with pytest.raises(ValueError, match="DTD"):
        parse_camt053_all(_camt_mit_dtd(0))


def test_saubere_datei_geht_weiterhin_durch() -> None:
    """Die Sperre darf keine Fehlalarme haben — auch dann nicht, wenn die
    Zeichenfolge im Buchungstext steht (der alte Vorschlag „ganze Datei
    durchsuchen" hätte genau das getan)."""
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">\n'
        b"  <BkToCstmrStmt><GrpHdr><MsgId>PROBE</MsgId></GrpHdr>\n"
        b"    <Stmt><Id>S1</Id>\n"
        b"      <Acct><Id><IBAN>CH8100000000000000099</IBAN></Id></Acct>\n"
        b"      <Ntry><Amt Ccy=\"CHF\">10.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts>\n"
        b"        <BookgDt><Dt>2026-07-01</Dt></BookgDt>\n"
        b"        <NtryDtls><TxDtls><RmtInf><Ustrd>Text mit &lt;!doctype im Wort</Ustrd>\n"
        b"        </RmtInf></TxDtls></NtryDtls></Ntry>\n"
        b"    </Stmt></BkToCstmrStmt></Document>\n"
    )
    auszuege = parse_camt053_all(xml)
    assert len(auszuege) == 1
    assert auszuege[0].entries[0].description.startswith("Text mit")


# ---------------------------------------------------------------------------
# Upload-Grenze
# ---------------------------------------------------------------------------
def test_zu_grosse_datei_wird_abgelehnt(logged_in_client: TestClient) -> None:
    """Verhalten unverändert — nur liegt die Datei nicht mehr ganz im Speicher."""
    from moneten.routers.import_bank import _MAX_UPLOAD_BYTES

    zu_gross = b"<" + b"x" * _MAX_UPLOAD_BYTES
    antwort = logged_in_client.post(
        "/import", files=[("files", ("riesig.xml", zu_gross, "application/xml"))]
    )
    assert antwort.status_code in (200, 400)
    assert "zu gross" in antwort.text


def test_kein_ungebremstes_lesen_mehr_im_import(  ) -> None:
    """Naht-Wächter: ``read()`` ohne Grenze darf im Upload-Weg nicht zurückkommen.

    Die Wirkung selbst lässt sich in der Suite nicht messen (der Speicherbedarf
    hängt am Testserver), die Naht dagegen schon — und genau sie ist zweimal
    übersehen worden.
    """
    from pathlib import Path

    quelle = (Path(__file__).resolve().parents[1]
              / "src" / "moneten" / "routers" / "import_bank.py").read_text(encoding="utf-8")
    treffer = [z.strip() for z in quelle.splitlines() if ".file.read()" in z]
    assert not treffer, f"Ungebremstes Lesen: {treffer}"


# ---------------------------------------------------------------------------
# PDF-Rendern
# ---------------------------------------------------------------------------
def _pdf(seiten: int, breite: float = 595, hoehe: float = 842) -> bytes:
    """Ein PDF mit ``seiten`` leeren Seiten der angegebenen Grösse (in Punkt)."""
    import fitz

    doc = fitz.open()
    for _ in range(seiten):
        doc.new_page(width=breite, height=hoehe)
    daten = doc.tobytes()
    doc.close()
    return daten


def test_uebergrosse_seite_wird_kleiner_gerendert() -> None:
    """Der gemessene Fall: 3000 × 3000 Punkt, 520 Byte Datei, 1091 MB Spitze.

    Abgelehnt wird die Seite NICHT — ein als A0 eingescannter Beleg soll lesbar
    bleiben. Nur die Auflösung sinkt so weit, dass die Fläche unter das Budget
    passt.
    """
    import fitz

    doc = fitz.open(stream=_pdf(1, 3000, 3000), filetype="pdf")
    try:
        pix = receipt_ocr._pixmap(doc[0])
    finally:
        doc.close()
    flaeche = pix.width * pix.height
    assert flaeche <= receipt_ocr.MAX_RENDER_PIXEL, f"{flaeche} Pixel"
    # Und nicht ins Unkenntliche: mindestens 72 dpi bleiben.
    assert pix.width >= 3000 * 72 / 72 * 0.9


def test_normale_seite_wird_weiterhin_in_voller_aufloesung_gerendert() -> None:
    """A4 bei 300 dpi sind 2480 × 3508 — daran darf sich nichts ändern."""
    import fitz

    doc = fitz.open(stream=_pdf(1), filetype="pdf")
    try:
        pix = receipt_ocr._pixmap(doc[0])
    finally:
        doc.close()
    # 595 x 842 pt bei 300 dpi, aufgerundet: 2480 x 3509.
    assert (pix.width, pix.height) == (2480, 3509), (pix.width, pix.height)


def test_nur_die_ersten_seiten_gehen_durch_die_erkennung(monkeypatch) -> None:
    """Zeit statt Speicher: gemessen 118 ms je Seite allein fürs Rendern, dazu
    bis zu vier Lagen Erkennung. 10'000 Seiten passen in 3,8 MB."""
    gelesen: list[int] = []

    def _stub(bild) -> str:  # noqa: ANN001
        gelesen.append(1)
        return "x"

    monkeypatch.setattr(receipt_ocr, "_ocr_pil_image", _stub)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        pfad = Path(d) / "viele.pdf"
        pfad.write_bytes(_pdf(receipt_ocr.MAX_OCR_SEITEN + 15))
        receipt_ocr._ocr_pdf(str(pfad))

    assert len(gelesen) == receipt_ocr.MAX_OCR_SEITEN, (
        f"{len(gelesen)} Seiten gerendert statt {receipt_ocr.MAX_OCR_SEITEN}"
    )


# ---------------------------------------------------------------------------
# Gleichzeitige Erkennungen
# ---------------------------------------------------------------------------
def test_hoechstens_zwei_erkennungen_laufen_gleichzeitig(monkeypatch) -> None:
    """Zwei gleichzeitig abgeschickte Grossformat-PDFs reissen zusammen das
    Speicherlimit, auch wenn keines allein es tut. Auf dem Handy genügt dafür
    zweimaliges Antippen."""
    from moneten.services import jobs

    gleichzeitig = 0
    hoechststand = 0
    sperre = threading.Lock()
    weiter = threading.Event()

    def _langsam(data: bytes, suffix: str = ""):  # noqa: ANN202, ARG001
        nonlocal gleichzeitig, hoechststand
        with sperre:
            gleichzeitig += 1
            hoechststand = max(hoechststand, gleichzeitig)
        weiter.wait(timeout=5)
        with sperre:
            gleichzeitig -= 1
        return receipt_ocr.OcrResult("", "none", None, None)

    monkeypatch.setattr("moneten.services.receipt_ocr.extract_text_from_bytes", _langsam)

    for _ in range(4):
        jobs.start_scan_job(b"x", ".jpg", bild_speichern=None)
    zeit = time.monotonic()
    while hoechststand == 0 and time.monotonic() - zeit < 5:
        time.sleep(0.02)
    time.sleep(0.3)
    weiter.set()

    assert hoechststand <= 2, f"{hoechststand} Erkennungen liefen gleichzeitig"


# ---------------------------------------------------------------------------
# Schlüsseldatei
# ---------------------------------------------------------------------------
def test_der_sitzungsschluessel_wird_eng_angelegt(tmp_path, monkeypatch) -> None:
    """0600 statt 0644 — die Datei liegt im gemounteten Datenordner.

    Geprüft wird der Modus beim Anlegen und nicht hinterher: unter Windows sagt
    ``stat`` darüber nichts, der Aufruf dagegen ist überall derselbe.
    """
    import os

    from moneten.config import _DEFAULT_SECRET, Settings

    gesehen: list[int] = []
    echt = os.open

    def _merken(pfad, flags, mode=0o777, *a, **kw):  # noqa: ANN001, ANN202
        if str(pfad).endswith("secret_key"):
            gesehen.append(mode)
        return echt(pfad, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", _merken)
    Settings(
        database_url=f"sqlite:///{(tmp_path / 'moneten.db').as_posix()}",
        attachments_dir=str(tmp_path / "anhaenge"),
        # Der Vorgabewert ist die Marke für „nicht gesetzt" — nur dann legt
        # die App überhaupt eine Schlüsseldatei an.
        secret_key=_DEFAULT_SECRET,
        initial_pin="424242",
    )
    assert gesehen, "Die Schlüsseldatei wurde gar nicht angelegt"
    assert gesehen[0] == 0o600, f"Modus {oct(gesehen[0])}"
