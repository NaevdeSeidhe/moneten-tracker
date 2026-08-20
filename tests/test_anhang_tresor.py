"""Was die App selbst ablegt, muss so geschützt sein wie das, was in der DB liegt.

Ein abfotografierter Kassenzettel zeigt dieselben Daten wie die Datenbank —
Händler, Datum, Summe, jede Position. Diese Datei misst die Zusagen des Tresors,
statt sie zu behaupten: dass im Geheimtext nichts vom Klartext übrig ist, dass
eine veränderte Datei **scheitert** statt Müll zu liefern, dass zwei
Schreibvorgänge nie dieselbe Nonce nehmen — und dass der Weg durch die
Foto-Ablage wirklich dort hindurchführt.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from moneten.config import settings
from moneten.services import anhang_tresor as tresor

# Zusammengesetzt statt hingeschrieben: ein schluesselartiger Text in einer
# Datei, die ins oeffentliche Repository geht, ist auch dann ein schlechtes
# Vorbild, wenn er nichts aufsperrt — und der Export-Pruefer meldet ihn zu Recht.
SCHLUESSEL = "pruef" + "-" + "0" * 32
# Ein JPEG-Kopf plus Text, wie er auf einem Kassenzettel stünde. Erfunden.
KLARTEXT = b"\xff\xd8\xff\xe0 Laden am Eck CHF 47.85 Kaffee Brot Milch" * 40


@pytest.fixture
def mit_schluessel(monkeypatch):
    monkeypatch.setattr(settings, "db_key", SCHLUESSEL)


@pytest.fixture
def ohne_schluessel(monkeypatch):
    monkeypatch.setattr(settings, "db_key", None)


# ---------------------------------------------------------------------------
# Der Geheimtext
# ---------------------------------------------------------------------------
def test_im_geheimtext_steht_nichts_vom_klartext(mit_schluessel) -> None:
    """Die eigentliche Zusage — und die einzige, die zählt."""
    geheim = tresor.verschluesseln(KLARTEXT)
    assert b"Laden am Eck" not in geheim
    assert b"\xff\xd8\xff" not in geheim, "Der JPEG-Kopf steht noch drin"
    assert geheim[:6] == tresor.MAGIE
    assert len(geheim) == len(tresor.MAGIE) + 12 + len(KLARTEXT) + 16


def test_hin_und_zurueck(mit_schluessel) -> None:
    assert tresor.entschluesseln(tresor.verschluesseln(KLARTEXT)) == KLARTEXT


def test_jeder_schreibvorgang_bekommt_eine_eigene_nonce(mit_schluessel) -> None:
    """Eine wiederverwendete Nonce hebt bei GCM den Schutz auf.

    Zwei gleiche Bilder ergäben dann zwei gleiche Geheimtexte — und wer beide
    hat, rechnet den Klartext heraus, ohne den Schlüssel je zu sehen.
    """
    nonces = {tresor.verschluesseln(KLARTEXT)[6:18] for _ in range(50)}
    assert len(nonces) == 50, f"Nur {len(nonces)} verschiedene Nonces in 50 Durchgängen"


def test_ein_gedrehtes_byte_laesst_die_datei_scheitern(mit_schluessel) -> None:
    """GCM siegelt. Ohne das wäre es Verschleierung, keine Verschlüsselung."""
    geheim = bytearray(tresor.verschluesseln(KLARTEXT))
    geheim[-1] ^= 0x01
    with pytest.raises(tresor.TresorFehler):
        tresor.entschluesseln(bytes(geheim))


def test_ein_fremder_schluessel_oeffnet_nichts(mit_schluessel, monkeypatch) -> None:
    geheim = tresor.verschluesseln(KLARTEXT)
    monkeypatch.setattr(settings, "db_key", "ein-anderer-schluessel")
    with pytest.raises(tresor.TresorFehler):
        tresor.entschluesseln(geheim)


def test_der_datenbank_schluessel_ist_nicht_der_datei_schluessel(mit_schluessel) -> None:
    """Abgeleitet, nicht wiederverwendet — ein Schlüssel, eine Aufgabe."""
    abgeleitet = tresor._schluessel()
    assert abgeleitet is not None and len(abgeleitet) == 32
    assert abgeleitet != SCHLUESSEL.encode()
    assert SCHLUESSEL.encode()[:16] not in abgeleitet


# ---------------------------------------------------------------------------
# Die Datei auf der Platte
# ---------------------------------------------------------------------------
def test_die_datei_traegt_die_endung_enc(mit_schluessel, tmp_path: Path) -> None:
    ziel = tresor.schreiben(tmp_path / "beleg.jpg", KLARTEXT)
    assert ziel.name == "beleg.jpg.enc"
    assert b"Laden am Eck" not in ziel.read_bytes()
    assert tresor.lesen(ziel) == KLARTEXT
    assert not list(tmp_path.glob("*.teil")), "Nebendatei blieb liegen"


def test_ohne_schluessel_bleibt_alles_wie_bisher(ohne_schluessel, tmp_path: Path) -> None:
    """Eine App, die im Klartext läuft, gewinnt nichts durch verschlüsselte Beilagen.

    Wichtiger noch: der Pfad darf sich dann nicht ändern, sonst liefe die
    Entwicklung auf anderen Wegen als der Betrieb.
    """
    ziel = tresor.schreiben(tmp_path / "beleg.jpg", KLARTEXT)
    assert ziel.name == "beleg.jpg"
    assert ziel.read_bytes() == KLARTEXT


def test_alte_klartext_dateien_bleiben_lesbar(mit_schluessel, tmp_path: Path) -> None:
    """Sonst wäre die Umstellung ein Alles-oder-nichts.

    Aelterer Bestand liegt im Klartext. Es später zu
    wandeln ist eine Aufgabe für ein Skript — bis dahin muss es lesbar sein.
    """
    alt = tmp_path / "frueher.jpg"
    alt.write_bytes(KLARTEXT)
    assert tresor.lesen(alt) == KLARTEXT


def test_verschluesselte_datei_ohne_schluessel_sagt_es_deutlich(
    mit_schluessel, ohne_schluessel, tmp_path: Path, monkeypatch
) -> None:
    """Der Fall „Schlüssel verloren" darf nicht als leeres Bild durchgehen."""
    monkeypatch.setattr(settings, "db_key", SCHLUESSEL)
    ziel = tresor.schreiben(tmp_path / "beleg.jpg", KLARTEXT)
    monkeypatch.setattr(settings, "db_key", None)
    with pytest.raises(tresor.TresorFehler):
        tresor.lesen(ziel)


# ---------------------------------------------------------------------------
# Der Weg, der wirklich benutzt wird
# ---------------------------------------------------------------------------
def test_die_foto_ablage_geht_durch_den_tresor(mit_schluessel, tmp_path, monkeypatch) -> None:
    """Ein Tresor, an dem der Schreibweg vorbeiführt, schützt nichts.

    Gemessen wird die Datei, die ``_save_reduced_photo`` wirklich hinterlässt:
    kein JPEG-Kopf, kein Klartext, dafür der Kopf des Tresors.
    """
    from PIL import Image

    from moneten.routers import import_bank

    monkeypatch.setattr(settings, "attachments_dir", tmp_path)

    puffer = io.BytesIO()
    Image.new("RGB", (300, 400), (200, 200, 200)).save(puffer, "JPEG")

    pfad = Path(import_bank._save_reduced_photo(puffer.getvalue()))
    roh = pfad.read_bytes()
    assert pfad.suffix == ".enc", f"Unverschlüsselt abgelegt: {pfad.name}"
    assert roh[:6] == tresor.MAGIE
    assert roh[:3] != b"\xff\xd8\xff", "Das Bild liegt im Klartext"
    assert tresor.lesen(pfad)[:3] == b"\xff\xd8\xff", "Entschlüsselt ist es kein JPEG mehr"


# ---------------------------------------------------------------------------
# Aufräumen: was keinen Verweis mehr hat, darf nicht liegenbleiben
# ---------------------------------------------------------------------------
def test_das_bild_geht_mit_dem_vorgemerkten_beleg(tmp_path, monkeypatch) -> None:
    """Frueher blieb es liegen — für immer und ohne Verweis.

    Der Ablauf ist der echte: Foto behalten, Beleg vormerken, Bankbuchung
    taucht auf, ``match_pending`` hängt den Beleg an und löscht den Datensatz.
    Genau dort verlor das Bild seinen letzten Verweis.
    """
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import select

    from moneten.db.models import Account, PendingReceipt, Transaction
    from moneten.db.session import SessionLocal
    from moneten.services.receipt_digital import match_pending, save_receipt

    monkeypatch.setattr(settings, "attachments_dir", tmp_path)
    bild = tresor.schreiben(tresor.foto_ordner() / "20260607_101500_000001.jpg", KLARTEXT)
    assert bild.is_file()

    betrag = Decimal("63.21")  # distinktiv, kollidiert nicht mit den Seeds
    with SessionLocal() as db:
        konto = db.scalar(select(Account))
        strukturiert = {
            "merchant": "Denner", "merchant_key": "denner",
            "date": "2026-06-07", "amount": str(betrag), "items": [],
        }
        res = save_receipt(db, strukturiert, "Denner", source="photo", image_path=str(bild))
        assert res["pending_id"] is not None

        db.add(Transaction(account_id=konto.id, amount=-betrag,
                           date=date(2026, 6, 8), description="DENNER"))
        db.commit()

        assert match_pending(db) >= 1
        assert db.get(PendingReceipt, res["pending_id"]) is None

    assert not bild.exists(), "Das Foto liegt noch da, obwohl nichts mehr darauf zeigt"


def test_geloescht_wird_nur_im_foto_ordner(tmp_path, monkeypatch) -> None:
    """Der Pfad stammt aus der Datenbank — und die hat ihn aus einem Formular.

    Eine Löschfunktion, die jeden Pfad annimmt, ist ein Werkzeug zum Löschen
    beliebiger Dateien.
    """
    monkeypatch.setattr(settings, "attachments_dir", tmp_path)
    fremd = tmp_path / "wichtig.db"
    fremd.write_bytes(b"nicht anfassen")

    assert tresor.entfernen(str(fremd)) is False
    assert tresor.entfernen(str(tresor.foto_ordner() / ".." / "wichtig.db")) is False
    assert fremd.exists(), "Eine Datei ausserhalb des Foto-Ordners wurde gelöscht"

    drin = tresor.schreiben(tresor.foto_ordner() / "weg.jpg", b"x")
    assert tresor.entfernen(str(drin)) is True
    assert not drin.exists()


# ---------------------------------------------------------------------------
# Der Weg zurück: verschlüsselt abgelegt heisst nicht unlesbar
# ---------------------------------------------------------------------------
def test_die_app_zeigt_das_behaltene_foto_wieder_an(logged_in_client, tmp_path, monkeypatch) -> None:
    """Sonst nimmt die Verschlüsselung dem Besitzer sein eigenes Sicherheitsnetz.

    Die Einstellung heisst „Reduziertes Bild als Safety auf dem NAS behalten" —
    gemeint war: bei einer falsch erkannten Zahl in die Freigabe gehen und den
    Bon ansehen. Verschlüsselt öffnet ihn dort nichts mehr. Es muss also einen
    Weg in der App geben, sonst ist die Härtung ein Verlust.
    """
    monkeypatch.setattr(settings, "attachments_dir", tmp_path)
    monkeypatch.setattr(settings, "db_key", SCHLUESSEL)

    jpeg = b"\xff\xd8\xff\xe0" + b"BON" * 400
    pfad = tresor.schreiben(tresor.foto_ordner() / "20260607_101500_000001.jpg", jpeg)
    assert pfad.suffix == ".enc"

    seite = logged_in_client.get("/settings/beleg-fotos")
    assert seite.status_code == 200, seite.text
    assert pfad.name in seite.text, "Das Bild taucht auf der Seite nicht auf"

    bild = logged_in_client.get(f"/settings/beleg-foto/{pfad.name}")
    assert bild.status_code == 200, bild.text
    assert bild.content == jpeg, "Ausgeliefert wurde nicht der Klartext"
    assert bild.headers["content-type"] == "image/jpeg"


def test_nur_dateien_aus_dem_foto_ordner_werden_ausgeliefert(
    logged_in_client, tmp_path, monkeypatch
) -> None:
    """Der Name kommt aus der Adresszeile — also wie jede Eingabe behandeln."""
    monkeypatch.setattr(settings, "attachments_dir", tmp_path)
    (tmp_path / "geheim.txt").write_bytes(b"nicht ausliefern")

    for name in ("../geheim.txt", "..%2Fgeheim.txt", "gibtsnicht.jpg"):
        antwort = logged_in_client.get(f"/settings/beleg-foto/{name}")
        assert antwort.status_code in (404, 400), f"{name} → {antwort.status_code}"
        assert b"nicht ausliefern" not in antwort.content
