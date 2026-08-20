"""Eine Anbieter-Rechnung als Beleg an der Monatsbuchung.

**Ein Parser, zwei Verwendungen.** Derselbe Befund, aus dem
:func:`~moneten.services.belege_parser.rechnung_nach_profil` die Verlaufsreihe
speist, wird hier zum Beleg an der Buchung: dieses Modul erkennt die Rechnung,
findet die Zahlung und liefert die Anhang-Daten; angelegt wird der Anhang von
``receipt_match``, wie bei jeder anderen Quittung auch. Der Unterschied zur
allgemeinen Zuordnung ist nicht die Bequemlichkeit, sondern die Verlässlichkeit:

* Dort wird der Betrag aus OCR-Text **geschätzt** und über Datumsnähe und
  Händlerwörter an eine Buchung **angenähert**.
* Hier ist er **gelesen** — aus der Betragsspalte der Rechnung, und die
  Selbstprüfung des Parsers hat ihn gegen die Summe aller Positionen geprüft.
  Die Positionen sind zugleich genau die Form, die ``parsed_items_json``
  erwartet: Name und Betrag je Zeile, Rabatte mit ihrem negativen Vorzeichen.

**Erkannt wird am Inhalt, nie am Dateinamen.** Ein Name lässt sich ändern, ein
Rechnungskopf nicht. Für die Zuordnung heisst das zugleich: wo die Rechnung
nichts hergibt (Positionen gehen nicht auf, mehrere Buchungen kämen in Frage),
bleibt sie unzugeordnet und der Nutzer entscheidet. Eine falsch zugeordnete
Quittung ist schlimmer als eine offene.

**Der Verbindungsnachweis gehört an KEINE Buchung.** Er liegt bei denselben
Belegen, weist aber Nutzung aus und keine Zahlung; seine Beträge sind fast
durchgehend null, weil die Verbindung im Abo enthalten war. Er wird darum
ausdrücklich gesperrt — auch für die allgemeine Zuordnung, die sonst über einen
zufällig betragsgleichen Wert an eine Buchung geriete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import Attachment, Transaction, not_transfer
from moneten.services.anbieter_profil import Anbieterprofil
from moneten.services.belege_parser import (
    POS_PRAEFIX,
    POS_RUNDUNG,
    PROFILE,
    Befund,
    PruefsummeFehler,
    rechnung_nach_profil,
)
from moneten.services.pdf_spalten import pdf_spalten

# Der Händlername steht im Profil des Anbieters — er ist Daten, keine Konstante.
# Vorher stand hier ein fester Name; damit war jeder weitere Anbieter eine
# Code-Änderung, und der Quelltext verriet, bei wem der Nutzer Kunde ist.

# Herkunft der Zahlen im strukturierten Anhang. ``receipt_ocr`` kennt
# „text-layer" und „ocr" — beides heisst „irgendwo im Text gefunden". Hier steht
# jede Zahl in ihrer eigenen Tabellenspalte und ist gegen die Rechnungssumme
# geprüft; das ist eine dritte, bessere Herkunft und darf nicht als OCR
# ausgewiesen werden.
METHODE = "rechnung"

# Zahlungsfenster. Die Rechnung ist Anfang Monat datiert und rund vier Wochen
# später zahlbar; die Bankbuchung liegt dazwischen oder kurz danach.
#
# Beide Ränder sind Ausschlüsse, keine Bequemlichkeit: Anfang Monat wird die
# Rechnung des VORMONATS bezahlt, und bei einem gleichbleibenden Abonnement trägt
# diese Zahlung denselben Betrag. Ohne den frühen Rand griffe die Rechnung nach
# der falschen der beiden Buchungen. Der späte Rand hält umgekehrt die Zahlung
# der Folgerechnung draussen.
_FRUEHESTENS_TAGE = 14  # ab Monatsmitte der Rechnungsperiode
_SPAETESTENS_TAGE = 30  # bis rund einen Monat nach ihrem Ende

# Nutzungsbeleg statt Zahlungsbeleg. Bewusst über den ganzen Text gesucht;
# die Feinheit steckt in der Reihenfolge beim Aufrufer, siehe unten.
# Nutzungsnachweise: Belege, die jede einzelne Nutzung aufführen und dabei fast
# durchgehend 0.00 ausweisen, weil sie im Abonnement enthalten war. Sie belegen
# keine Zahlung. Die Wörter sind bewusst allgemein — sie stehen so oder ähnlich
# auf den Nutzungsbelegen aller Anbieter.
_NUTZUNGSNACHWEIS = re.compile(r"Verbindungsnachweis|Nutzungsnachweis|Einzelverbindung",
                               re.IGNORECASE)

def _haendler_im_text(text: str) -> Anbieterprofil | None:
    """Welcher Anbieter steckt in dieser Buchungsbezeichnung — wenn überhaupt.

    Gesucht wird mit dem ``stichwort`` des Profils, also mit derselben
    Schreibung, an der die Buchung schon ihre Kategorie bekommt. Eine zweite,
    eigene Wortliste liefe der ersten irgendwann davon.
    """
    klein = (text or "").lower()
    for profil in PROFILE.values():
        if profil.stichwort in klein:
            return profil
    return None


@dataclass(frozen=True)
class Posten:
    """Eine Zeile der Aufstellung an der Buchung: Bezeichnung und Betrag."""

    name: str
    betrag: Decimal


def ist_verbindungsnachweis(text: str) -> bool:
    """Ist dieser Belegtext ein Verbindungsnachweis (und damit kein Beleg)?

    Gesucht wird im GANZEN Dokument — und das ist Absicht, obwohl es zu viel
    fängt. Der Versuch, es auf den Titel einzugrenzen (erste Zeilen, kurze
    Zeile), war ein Rückschritt: nachgemessen fiel eine Titelzeile von 75
    Zeichen durch die Prüfung, und ein durchgelassener Nutzungsbeleg landet
    über die geschätzte Zuordnung an der echten Anbieter-Zahlung — danach ist
    sie belegt, und die richtige Rechnung findet kein freies Gegenstück mehr.

    Die Kehrseite (eine echte Rechnung, die das Wort nur erwähnt, wird
    gesperrt) wird nicht hier gelöst, sondern durch die REIHENFOLGE beim
    Aufrufer: erst den Rechnungs-Parser fragen, dann diese Sperre. Was als
    Rechnung aufgeht, ist eine Rechnung — das ist eine Messung, keine Wortsuche,
    und darum die stärkere Aussage.
    """
    return bool(_NUTZUNGSNACHWEIS.search(text or ""))


def _profil_im_text(text: str) -> Anbieterprofil | None:
    """Billiger Vorfilter am schon gelesenen Belegtext.

    Nur wenn ein Anbieter erkannt wird, öffnet der Aufrufer das PDF ein zweites
    Mal — spaltenweise, was der Parser braucht. Ohne diesen Filter zahlte jede
    Ladenquittung im Ordner die Koordinaten-Extraktion einer Rechnung mit, die
    sie nie ist.
    """
    return _haendler_im_text(text)


def rechnung_aus_text(spaltentext: str) -> Befund | None:
    """Befund einer Anbieter-Rechnung aus Spaltentext — ``None``, wenn es keine ist.

    Die Prüfsumme wird hier **verschluckt und zu ``None``**, obwohl der Parser
    sie ausdrücklich wirft. Für den Verlauf ist eine nicht aufgehende Rechnung
    ein Fehler, den jemand sehen muss; für den Beleg an der Buchung ist sie
    schlicht kein verlässlicher Beleg. Ein Anhang mit Positionen, die den
    gebuchten Betrag nicht ergeben, behauptete eine Genauigkeit, die er nicht hat.
    """
    profil = _profil_im_text(spaltentext)
    if profil is None:
        return None
    try:
        befunde = rechnung_nach_profil(spaltentext, profil)
    except PruefsummeFehler:
        return None
    return befunde[0] if befunde else None


def rechnung_aus_datei(pfad: str) -> Befund | None:
    """Wie :func:`rechnung_aus_text`, aber aus einer Datei im Quittungs-Ordner.

    Nur PDFs: die Rechnung kommt als Datei mit Textebene aus dem Kundenportal,
    ein abfotografierter Ausdruck hätte keine Spaltenkoordinaten. Eine kaputte
    oder verschlüsselte Datei liefert ``None`` statt einer Ausnahme — für den
    Abgleich ist sie dann einfach keine Rechnung.
    """
    if not pfad.lower().endswith(".pdf"):
        return None
    try:
        return rechnung_aus_text(pdf_spalten(pfad))
    except Exception:
        # Eine kaputte Datei darf den Abgleich über alle anderen nicht abbrechen.
        return None


def rechnung_zur_datei(pfad: str, belegtext: str) -> Befund | None:
    """Ist diese Beleg-Datei eine Anbieter-Rechnung? Dann ihr Befund.

    ``belegtext`` ist die schon gelesene Textebene und dient als Vorfilter
    (siehe :func:`_profil_im_text`) — der Aufrufer hat sie ohnehin in der Hand.
    """
    return rechnung_aus_datei(pfad) if _profil_im_text(belegtext) else None


def anhangs_daten(pfad: str, belegtext: str) -> dict | None:
    """Strukturierte Anhang-Daten, wenn die Datei eine gelesene Rechnung ist.

    ``None`` heisst „gewöhnlicher Beleg" — dann bleibt es beim geschätzten
    Betrag aus dem Text.
    """
    befund = rechnung_zur_datei(pfad, belegtext)
    return strukturiert(befund) if befund is not None else None


def _anbietername(slug: str) -> str:
    """Anzeigename des Anbieters zu einem slug — leer, wenn es ihn nicht gibt.

    Leer statt Ausnahme: ein Beleg, dessen Profil inzwischen entfernt wurde,
    soll weiter anzeigbar bleiben. Der Name ist eine Beschriftung, keine Zahl.
    """
    profil = PROFILE.get(slug)
    return profil.name if profil else ""


def strukturiert(befund: Befund) -> dict:
    """Der Befund in der Form, die ``Attachment.parsed_items_json`` erwartet.

    Das Datum ist der Monatsanfang und nicht der Rechnungstag: die Rechnung nennt
    im Kopf ihren **Monat**, und ein aus dem Monat erfundener Tag wäre eine
    Angabe, die nirgends steht. Für die Zuordnung zählt ohnehin das Datum der
    Buchung.

    Die Rundungsdifferenz steht als eigener Schlüssel neben den Positionen. Sie
    ist keine — sie entsteht erst beim Runden der Summe —, aber ohne sie gingen
    die Zeilen der Aufstellung nicht auf den gebuchten Betrag auf.
    """
    daten: dict = {
        "method": METHODE,
        "merchant": _anbietername(befund.slug),
        "date": befund.period_start.isoformat(),
        "amount": str(befund.value),
        "items": [
            {"name": name[len(POS_PRAEFIX):], "price": wert}
            for name, wert in befund.extras.items()
            if name.startswith(POS_PRAEFIX)
        ],
    }
    if POS_RUNDUNG in befund.extras:
        daten["rounding"] = befund.extras[POS_RUNDUNG]
    return daten


def anzeige_posten(daten: dict) -> list[Posten]:
    """Die Zeilen der Aufstellung an der Buchung — leer für jeden anderen Beleg.

    Der Filter auf die Herkunft ist der Punkt: die Positionen eines Kassenbons
    sind OCR-geraten und ergeben zusammen nicht den gebuchten Betrag. Als
    Aufstellung untereinander gestellt sähen sie genauso verbindlich aus wie
    diese hier.
    """
    if daten.get("method") != METHODE:
        return []
    posten: list[Posten] = []
    for eintrag in daten.get("items") or []:
        betrag = _betrag(eintrag.get("price"))
        name = (eintrag.get("name") or "").strip()
        if name and betrag is not None:
            posten.append(Posten(name, betrag))
    rundung = _betrag(daten.get("rounding"))
    if rundung:
        posten.append(Posten("Rundungsdifferenz", rundung))
    return posten


def _betrag(roh: object) -> Decimal | None:
    try:
        return Decimal(str(roh))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _bereits_belegte(db: Session) -> set[int]:
    """Buchungen, an denen bereits eine GELESENE Anbieter-Rechnung hängt.

    Der Unterschied zu „irgendein Anhang" trägt die Zuordnung unten. Hängt am
    Januar-Beleg schon die Januar-Rechnung, ist das die Kette, die sich Monat um
    Monat füllt — die Februar-Rechnung darf die belegte Buchung dann übergehen.
    Hängt dort dagegen ein Kassenbon, sagt das über den Monat nichts aus.
    """
    ids: set[int] = set()
    for tx_id, roh in db.execute(
        select(Attachment.transaction_id, Attachment.parsed_items_json)
    ):
        if tx_id is None or not roh:
            continue
        try:
            daten = json.loads(roh)
        except (ValueError, TypeError):
            continue
        if isinstance(daten, dict) and daten.get("method") == METHODE:
            ids.add(tx_id)
    return ids


def passende_buchung(db: Session, befund: Befund) -> Transaction | None:
    """Die eine Buchung, die diese Rechnung bezahlt hat — sonst ``None``.

    Vier Bedingungen, alle vier hart: der Betrag stimmt **exakt** (er ist
    gelesen, nicht geschätzt — eine Toleranz hätte hier nichts zu tolerieren),
    die Buchung nennt den Händler, sie liegt im Zahlungsfenster, und sie ist die
    **einzige**, auf die das alles zutrifft.

    **Die Reihenfolge ist der Punkt.** Vorher fielen belegte Buchungen schon vor
    der Eindeutigkeitsprüfung heraus — das Abziehen SCHUF damit Eindeutigkeit,
    wo keine war: nachgemessen landete eine Januar-Rechnung an einer fremden,
    zufällig betragsgleichen Ausgabe, nur weil die richtige Buchung schon einen
    Anhang trug. Gemessen wird die Mehrdeutigkeit jetzt über ALLE Buchungen des
    Fensters; erst danach darf ein Beleg übergangen werden, und auch das nur,
    wenn dort bereits eine Anbieter-Rechnung hängt (siehe
    :func:`_bereits_belegte`). Genau das ist die Kette, die sich Monat um Monat
    füllt — jede andere Belegung lässt das Fenster mehrdeutig bleiben.

    **Der Händlername ist neu und nicht kosmetisch.** Ohne ihn entschied allein
    der Betrag, und zwei aufeinanderfolgende Zahlungsfenster überschneiden sich
    um gut zwei Wochen. Fehlte die richtige Buchung, griff die Rechnung nach der
    Zahlung des Folgemonats. Der Name steht in den Buchungen — dieselbe
    Schreibung, an der sie schon ihre Kategorie bekommen
    (``categorization``).

    Nur Ausgaben: der Vergleich läuft gegen den Betrag OHNE Vorzeichen, und eine
    Rechnung, die etwas gutschreibt, findet damit von sich aus keine Buchung.
    """
    von = befund.period_start + timedelta(days=_FRUEHESTENS_TAGE)
    bis = befund.period_end + timedelta(days=_SPAETESTENS_TAGE)
    kandidaten = [
        t
        for t in db.scalars(
            select(Transaction).where(not_transfer(), Transaction.date.between(von, bis))
        )
        # Der Betragsvergleich läuft in Python über Decimal: die Spalte kommt in
        # SQLite als Fliesskommazahl zurück, und ein Vergleich in SQL vergliche
        # damit Näherungen.
        if t.amount < 0
        and t.amount.copy_abs() == befund.value
        and _haendler_im_text(t.description or "") is not None
    ]
    if not kandidaten:
        return None
    belegt = {t for t in db.scalars(select(Attachment.transaction_id)) if t is not None}
    frei = [t for t in kandidaten if t.id not in belegt]
    if len(frei) != 1:
        return None
    mit_rechnung = _bereits_belegte(db)
    if any(t.id not in mit_rechnung for t in kandidaten if t.id != frei[0].id):
        return None
    return frei[0]
