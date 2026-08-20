"""Was in den nächsten Monaten auf dich zukommt.

Zwei Dinge, die dieselbe Frage beantworten und darum in derselben Liste stehen:

* **Fristen**, die man verpasst, weil sie nur einmal im Jahr auftauchen — Säule
  3a bis 31.12., Krankenkasse kündigen bis 30.11., Steuererklärung.
* **Jahresposten aus der eigenen Historie** — Autosteuer, Vignette, Serafe,
  Versicherungsprämien. Nicht die, die man als Rückstellung erfasst hat, sondern
  gerade die, die man vergessen hat zu erfassen. Die App kennt sie trotzdem: sie
  stehen seit Jahren in den Buchungen.

Woran eine echte Jahresverpflichtung erkennbar ist
--------------------------------------------------

„Derselbe Händler kam in zwei Jahren im selben Monat" reicht **nicht**. Bei rund
500 Buchungen im Jahr entstehen solche Paare zwangsläufig: wer jede Woche bei
Migros einkauft, hat garantiert in jedem August zwei Jahre hintereinander dort
gezahlt. Das ist Rauschen, kein Termin — und es hat die Karte zugemüllt.

Eine Rechnung, die wiederkommt, unterscheidet sich von einem Einkauf, der sich
wiederholt, in vier Punkten. Alle vier müssen zutreffen:

1. **Sie kommt einmal im Jahr, nicht das ganze Jahr.** Gemessen am **Abstand
   zwischen zwei aufeinanderfolgenden Belegen**: jeder muss zwischen zehneinhalb
   und dreizehneinhalb Monaten liegen. Das ist die Definition dessen, was hier
   vorhergesagt wird, und es erledigt die Migros-Fälle, ohne einen einzigen
   Betrag anzuschauen — wer jede Woche einkauft, hat Abstände von Tagen.

   Bewusst **nicht** über Kalenderjahre gezählt. „Höchstens eine Zahlung pro
   Kalenderjahr" klingt gleichwertig, wirft aber genau den saubersten
   Jahresrhythmus weg: eine Rechnung, die um den Jahreswechsel schwankt
   (28.12., 27.12., 03.01., 29.12.), liegt in einem Kalenderjahr zweimal und in
   einem anderen gar nicht. Der Abstand kennt dieses Problem nicht.
2. **Der Betrag bleibt stehen.** Von einem Beleg zum nächsten höchstens ±30 %.
   Bewusst von Schritt zu Schritt und nicht gegen den Median über vier Jahre:
   eine Krankenkassenprämie legt in vier Jahren ohne Weiteres 25 % zu, und gegen
   einen festen Median gemessen fiel sie umso sicherer durch, je länger man die
   App benutzt. Ein Einkauf würfelt jedes Mal neu und scheitert weiterhin.
3. **Kein Ladenkauf im Buchungstext.** „Einkauf … Visa Debit", „TWINT",
   Bargeldbezug: Geld, das an einer Kasse abfliesst, ist keine Jahresrechnung —
   das gilt kategorisch und nicht bloss statistisch. Taucht bei einem Händler
   auch nur eine solche Buchung auf, ist die Beziehung ein Einkaufen.
4. **Keine Alltagskategorie.** Gemessen an der ``management_type`` der Kategorie,
   also am eigenen Modell statt an einer Namensliste: Kost & Logis, Bargeld,
   Sparen, Einkommen und Transfer tragen keine Jahresverpflichtung. Ohne
   Kategorie bleibt eine Buchung drin — frisch importierte sind unkategorisiert,
   und genau die vergisst man.

Zwei Jahre Beleg genügen weiterhin. Mit den vier Bedingungen zusammen ist ein
Zufallstreffer unwahrscheinlich genug, und eine Versicherung, die es erst seit
zwei Jahren gibt, soll nicht durchfallen.

**Nebenposten zählen nicht als Beleg.** Eine Mahngebühr über 20 Franken beim
Gebühren-Konto ist keine zweite Jahresrechnung — sie stünde aber mitten in der
Reihe und riss sowohl den Abstand als auch den Betragsvergleich auseinander.
Wer deutlich unter der Hälfte der übrigen Belege liegt, gehört deshalb nicht zur
Rechnung. Eine zweite Zahlung in voller Höhe bleibt drin und beendet den
Jahresrhythmus zu Recht: wer zweimal im Jahr voll zahlt, zahlt halbjährlich.

**Vorhergesagt wird der letzte Beleg, nicht der Median.** Der Median stand im
Widerspruch zu dem Beleg, den die Zeile daneben nennt: er liegt zwischen den
Jahren, der genannte Beleg ist der jüngste. Bei einer jährlich steigenden
Prämie war die Vorhersage dadurch systematisch zu tief. Der letzte Beleg ist
die frischeste Information, die es gibt — und die einzige, die zum Datum
daneben passt.

Bewusste Grenzen:

* **Keine Netzwerkaufrufe.** Die Fristen und der 3a-Maximalbetrag stehen als
  Konstanten im Code. Beträge und Termine ändern sich jährlich, kantonale
  Steuerfristen ohnehin — deshalb steht das Bezugsjahr sichtbar dabei, und nach
  dessen Ablauf sagt die App das offen, statt weiter eine veraltete Zahl zu
  behaupten. Eine still veraltende Konstante wäre schlimmer als keine Angabe.
* **Jahresposten werden erkannt, nicht erfunden.** Eine einzelne grosse Zahlung
  ist kein Muster.
* **Keine Doppelmeldung.** Was schon als Rückstellung (jährliches Standard-Soll)
  oder als Abo erfasst ist, taucht hier nicht auf — dafür gibt es die
  Budget-Seite.
* **Kein Auftrag an den Nutzer.** Der Hinweis unter einem Posten nennt den
  letzten Beleg, aus dem die Vorhersage stammt — nicht die Aufforderung, eine
  Rückstellung anzulegen. Ein Satz, der unter jedem Eintrag dieselbe Handlung
  verlangt und nie befolgt wird, ist Fülltext.
"""

from __future__ import annotations

import calendar
import re
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import add_months, heute_lokal
from moneten.db.models import (
    BudgetInterval,
    Category,
    ManagementType,
    ManualSubscription,
    StandardBudget,
    Transaction,
)
from moneten.services.subscriptions import _merchant_key, _trifft_eine, display_name

# --------------------------------------------------------------------------
# Fristen — Stand und Bezugsjahr stehen bewusst sichtbar im Code.
# --------------------------------------------------------------------------

#: Jahr, für das die Werte unten nachgeschlagen wurden. Danach warnt die App.
FRISTEN_STAND = 2026

#: Maximalbetrag Säule 3a für Erwerbstätige mit Pensionskasse (CHF, ``FRISTEN_STAND``).
#: Vom Nutzer  gegen seine Quelle bestätigt — die Zahl ist also nicht
#: geraten. Sie gilt für 2026; beim Jahreswechsel zusammen mit ``FRISTEN_STAND``
#: nachführen, sonst warnt die App (siehe ``was_kommt``).
#:
#: Ohne Pensionskasse (Selbständige) gilt ein ganz anderer Betrag — 20 % des
#: Erwerbseinkommens bis zu einer deutlich höheren Grenze. Diese Konstante deckt
#: diesen Fall NICHT ab; sie stimmt nur, solange eine Pensionskasse besteht.
SAEULE_3A_MAX = Decimal("7258")

#: (Monat, Tag, Titel, Erklärung) — jährlich wiederkehrend.
FRISTEN: list[tuple[int, int, str, str]] = [
    (11, 30, "Krankenkasse kündigen",
     "Wechsel auf den 1. Januar: Kündigung muss bis 30.11. beim Versicherer sein."),
    (11, 30, "Franchise ändern",
     "Höhere oder tiefere Franchise fürs nächste Jahr — gleiche Frist wie der Kassenwechsel."),
    (12, 31, "Säule 3a einzahlen",
     "Nur Einzahlungen mit Valuta bis 31.12. zählen fürs laufende Steuerjahr."),
    (3, 31, "Steuererklärung einreichen",
     "Übliche Frist in vielen Kantonen; eine Verlängerung ist meist formlos möglich."),
]

#: Stichwörter, an denen 3a-Einzahlungen in den Kategorienamen erkannt werden.
_3A_STICHWORTE = ("säule 3a", "saeule 3a", "3a", "vorsorge")

#: Ein Jahresposten muss mindestens so viel ausmachen, um zu stören.
_MIN_JAHRESPOSTEN = Decimal("100")

#: Wie weit nach vorn geschaut wird.
_HORIZONT_MONATE = 3

#: Wie weit zurück nach Belegen gesucht wird.
_HISTORIE_MONATE = 48

#: Wie stark ein Beleg vom VORHERGEHENDEN abweichen darf (Kriterium 2).
#: 30 % decken auch einen kräftigen Prämiensprung ab. Grosszügig sein kostet
#: hier wenig: den Einkauf wirft schon Kriterium 1 raus, weil seine Abstände
#: Tage und nicht Monate betragen. Gegen einen festen Median gemessen fiel
#: dagegen jede über Jahre steigende Rechnung irgendwann durch.
_BETRAGS_TOLERANZ = Decimal("0.30")

#: Erlaubter Abstand zwischen zwei aufeinanderfolgenden Belegen (Kriterium 1),
#: in Tagen. 320–410 Tage sind rund zehneinhalb bis dreizehneinhalb Monate:
#: genug Spielraum für einen verschobenen Rechnungslauf, zu wenig für eine
#: halbjährliche Rate (die läge bei ~180) oder ein ausgelassenes Jahr (~730).
_MIN_ABSTAND_TAGE = 320
_MAX_ABSTAND_TAGE = 410

#: Ab welchem Anteil am Median ein Beleg zur Rechnung selbst zählt. Darunter ist
#: es ein Nebenposten — Mahngebühr, Teilrate, Kontoführungszuschlag —, und der
#: darf den Rhythmus der eigentlichen Rechnung nicht zerreissen.
_NEBENPOSTEN_ANTEIL = Decimal("0.5")

#: Wörter, die einen Ladenkauf ausweisen (Kriterium 3). Kassenzahlungen sind
#: kategorisch keine Jahresrechnungen — kein Versicherer bucht seine Prämie als
#: „Einkauf … Visa Debit" ab. Geprüft wird der ROHE Buchungstext, denn
#: ``_merchant_key`` wirft genau diese Wörter als Füllwörter weg.
_LADENKAUF_WOERTER = frozenset({
    "einkauf", "kauf", "kaufdienstl", "twint", "kartenzahlung", "kartenkauf",
    "bargeldbezug", "bancomat", "geldautomat", "atm", "pos",
})

#: Verwaltungsarten ohne Jahresverpflichtung (Kriterium 4). Bewusst über
#: ``management_type`` statt über Kategorienamen: die Zuordnung pflegt der
#: Nutzer ohnehin schon, und eine umbenannte Kategorie bricht nichts.
_ALLTAGSARTEN = frozenset({
    ManagementType.KOST_LOGIS,
    ManagementType.BARGELD,
    ManagementType.SPAREN,
    ManagementType.EINKOMMEN,
    ManagementType.TRANSFER,
})


@dataclass
class Posten:
    """Ein Eintrag der Liste — Frist oder erwartete Zahlung."""

    datum: date
    titel: str
    hinweis: str
    art: str                      # "frist" | "jahresposten"
    betrag: Decimal | None = None
    veraltet: bool = False        # Konstante älter als das laufende Jahr

    def tage_bis(self, heute: date) -> int:
        return (self.datum - heute).days


def _naechstes_vorkommen(heute: date, monat: int, tag: int) -> date:
    """Das nächste Auftreten dieses Kalendertags — dieses oder nächstes Jahr."""
    dieses = date(heute.year, monat, tag)
    return dieses if dieses >= heute else date(heute.year + 1, monat, tag)


def _bereits_3a_eingezahlt(db: Session, jahr: int) -> Decimal:
    """Summe der 3a-Einzahlungen des Jahres — über Kategorienamen erkannt.

    Wie beim Steuerjahr-Auszug über Stichwörter statt feste IDs: eine umbenannte
    Kategorie fällt so sichtbar heraus, statt still eine falsche Zahl zu liefern.
    """
    ids = [
        c.id for c in db.scalars(select(Category))
        if any(w in (c.name or "").lower() for w in _3A_STICHWORTE)
    ]
    if not ids:
        return Decimal("0")
    summe = sum(
        (t.amount for t in db.scalars(
            select(Transaction).where(
                Transaction.category_id.in_(ids),
                Transaction.date >= date(jahr, 1, 1),
                Transaction.date <= date(jahr, 12, 31),
            )
        )),
        Decimal("0"),
    )
    # Einzahlungen sind Abgänge vom Lohnkonto (negativ) oder Zugänge auf dem
    # 3a-Konto (positiv) — je nachdem, wie das Konto geführt wird. Der Betrag
    # zählt in beiden Fällen.
    return abs(summe)


def fristen(db: Session, heute: date) -> list[Posten]:
    """Die anstehenden Schweizer Fristen, mit den eigenen Zahlen darin."""
    veraltet = heute.year > FRISTEN_STAND
    out: list[Posten] = []
    for monat, tag, titel, erklaerung in FRISTEN:
        wann = _naechstes_vorkommen(heute, monat, tag)
        hinweis = erklaerung
        if titel == "Säule 3a einzahlen":
            schon = _bereits_3a_eingezahlt(db, wann.year - (1 if wann.month < 6 else 0))
            offen = SAEULE_3A_MAX - schon
            if offen <= 0:
                continue  # Maximum ausgeschöpft — keine Frist mehr
            hinweis = (
                f"Noch {offen:.0f} von {SAEULE_3A_MAX:.0f} einzahlbar "
                f"(Stand {FRISTEN_STAND}). {erklaerung}"
            )
        out.append(Posten(datum=wann, titel=titel, hinweis=hinweis,
                          art="frist", veraltet=veraltet))
    return out


def _erfasste_schluessel(db: Session) -> set[str]:
    """Händler-Schlüssel, die schon als Abo oder Rückstellung erfasst sind.

    Ganze Schlüssel, nicht deren Einzelwörter: verglichen wird später mit
    :func:`~moneten.services.subscriptions.key_passt`, das dafür die Wortfolge
    braucht. Als Wortmenge verglichen genügte ein einziges gemeinsames Wort, um
    einen fremden Händler zu verschlucken — das Abo „Musterus Versicherung"
    liess damit die Jahresprämie eines anderen Versicherers („muster versicherung
    praemie") verschwinden,
    und ein Jahresbudget auf „Versicherungen & Abgaben" gleich eine ganze
    Gruppe.
    """
    keys = set()
    for sub in db.scalars(select(ManualSubscription).where(
            ManualSubscription.is_active.is_(True))):
        keys.add(_merchant_key(sub.match_keyword or sub.name))
    for sb in db.scalars(select(StandardBudget)):
        if sb.interval == BudgetInterval.JAEHRLICH and sb.amount and sb.amount > 0:
            kat = db.get(Category, sb.category_id)
            if kat is not None:
                keys.add(_merchant_key(kat.name))
    return {k for k in keys if k}


def _ist_ladenkauf(text: str | None) -> bool:
    """Weist der Buchungstext eine Kassenzahlung aus? (Kriterium 3)

    Wortweise und nicht als Teilstring: „Kaufmann" ist kein „Kauf", und
    „Einkauf" steht selbst in der Liste.
    """
    worte = set(re.findall(r"[a-zà-ÿ]+", (text or "").lower()))
    return bool(worte & _LADENKAUF_WOERTER)


def _rechnungsbelege(zahlungen: list[Transaction]) -> list[Transaction]:
    """Die Belege der Rechnung selbst, nach Datum sortiert — ohne Nebenposten.

    Eine Mahngebühr oder Teilrate steht beim selben Händler und im selben
    Zeitraum, ist aber nicht die Jahresrechnung. Bliebe sie in der Reihe, riss
    sie den Abstand (Kriterium 1) und den Betragsvergleich (Kriterium 2)
    gleichzeitig auseinander — nachgemessen liess eine einzige Mahngebühr über
    20 Franken die Jahresrechnung komplett verschwinden.

    Die Grenze liegt am Median, nicht am Maximum: gegen den grössten Beleg
    gemessen würde eine einzelne Nachzahlung die ganze Reihe wegputzen.
    """
    if not zahlungen:
        return []
    mitte = statistics.median([t.amount.copy_abs() for t in zahlungen])
    schwelle = mitte * _NEBENPOSTEN_ANTEIL
    return sorted(
        (t for t in zahlungen if t.amount.copy_abs() >= schwelle),
        key=lambda t: t.date,
    )


def _gleicher_tag_im_jahr(vorlage: date, jahr: int) -> date:
    """Derselbe Kalendertag in einem anderen Jahr, auf das Monatsende geklemmt.

    Nur der 29.–31. eines kürzeren Monats braucht das Klemmen (29.02. in einem
    Nicht-Schaltjahr, 31.01. → Februar gibt es nicht). Vorher war der Tag pauschal
    auf 28 gekappt: eine Rechnung vom 29.12. wurde dadurch für den 28.12.
    vorhergesagt — einen Tag daneben, ohne Grund.
    """
    letzter_tag = calendar.monthrange(jahr, vorlage.month)[1]
    return date(jahr, vorlage.month, min(vorlage.day, letzter_tag))


def _jahresrhythmus(belege: list[Transaction]) -> bool:
    """Liegt zwischen je zwei Belegen rund ein Jahr? (Kriterium 1)

    Erwartet die Belege nach Datum sortiert. Auf Tagesabständen gemessen und
    nicht auf Kalenderjahren — die Begründung steht im Modul-Docstring.
    """
    return all(
        _MIN_ABSTAND_TAGE <= (b.date - a.date).days <= _MAX_ABSTAND_TAGE
        for a, b in zip(belege, belege[1:], strict=False)
    )


def _betrag_bleibt_stehen(belege: list[Transaction]) -> bool:
    """Bleibt der Betrag von Beleg zu Beleg im Rahmen? (Kriterium 2)

    Schrittweise und nicht gegen einen festen Median — sonst fällt jede über
    Jahre steigende Rechnung durch, je länger man die App benutzt.
    """
    return all(
        (b.amount.copy_abs() - a.amount.copy_abs()).copy_abs()
        <= a.amount.copy_abs() * _BETRAGS_TOLERANZ
        for a, b in zip(belege, belege[1:], strict=False)
    )


def _verwaltungsarten(db: Session) -> dict[int, ManagementType]:
    """``category_id`` → Verwaltungsart, einmal geladen statt je Buchung."""
    return {c.id: c.management_type for c in db.scalars(select(Category))}


def jahresposten(db: Session, heute: date) -> list[Posten]:
    """Jahresrechnungen aus der eigenen Historie, die dieses Jahr noch fehlen.

    Die automatische Ergänzung zu den Rückstellungen: dort trägt man Jahreskosten
    von Hand ein, hier findet die App die, an die man nie gedacht hat. Die vier
    Kriterien, die eine Rechnung von einem wiederholten Einkauf trennen, stehen
    oben im Modul-Docstring.
    """
    erfasst = _erfasste_schluessel(db)
    arten = _verwaltungsarten(db)
    grenze = add_months(heute.replace(day=1), -_HISTORIE_MONATE)

    # Gruppiert wird nach Händler ALLEIN, nicht nach Händler und Monat: erst die
    # vollständige Zahlungsgeschichte zeigt, ob da einmal im Jahr eine Rechnung
    # kommt oder das ganze Jahr über eingekauft wird. Deshalb landet hier auch
    # jede Kleinbuchung — sie ist der Beleg dafür, dass der Händler Alltag ist.
    gruppen: dict[str, list[Transaction]] = {}
    for tx in db.scalars(select(Transaction).where(
            Transaction.date >= grenze, Transaction.date < heute, Transaction.amount < 0)):
        key = _merchant_key(tx.description)
        if not key or _trifft_eine(erfasst, key):
            continue
        gruppen.setdefault(key, []).append(tx)

    out: list[Posten] = []
    fenster_ende = add_months(heute.replace(day=1), _HORIZONT_MONATE + 1)
    for zahlungen in gruppen.values():
        # Kriterium 3: ein einziger Kassenbeleg macht den Händler zum Laden.
        # Vor dem Aussortieren der Nebenposten geprüft: gerade der kleine
        # Kassenbeleg ist der Beweis, und er darf nicht vorher rausfallen.
        if any(_ist_ladenkauf(t.description) for t in zahlungen):
            continue
        # Kriterium 4: Alltagskategorien tragen keine Jahresverpflichtung.
        if any(arten.get(t.category_id or -1, t.management_type) in _ALLTAGSARTEN
               for t in zahlungen):
            continue

        belege = _rechnungsbelege(zahlungen)
        if len({t.date.year for t in belege}) < 2:
            continue  # einmalig ist kein Muster
        # Im laufenden Jahr schon gekommen? Dann ist nichts mehr fällig.
        if heute.year in {t.date.year for t in belege}:
            continue
        # Kriterium 1: rund ein Jahr zwischen je zwei Belegen.
        if not _jahresrhythmus(belege):
            continue
        # Kriterium 2: eine Rechnung wiederholt sich, ein Einkauf würfelt neu.
        if not _betrag_bleibt_stehen(belege):
            continue

        # Der jüngste Beleg gibt Termin, Schreibweise UND Betrag vor — er ist der
        # Stand, den der Nutzer nachschlagen kann, und bei einer jedes Jahr
        # steigenden Prämie die einzige Zahl, die nicht schon bei der Anzeige
        # veraltet ist.
        letzter = belege[-1]
        betrag = letzter.amount.copy_abs()
        if betrag < _MIN_JAHRESPOSTEN:
            continue
        # Auf ganze Franken: die Karte zeigt die Zeilen ohne Rappen (``chf_kurz``),
        # die Summe mit (``chf``). Ein Beleg auf .50 stünde sonst als „312" in der
        # Zeile, zählte aber mit 312.50 in die Summe — die Summenzeile ginge um
        # 50 Rappen neben der Addition der sichtbaren Zahlen auf.
        betrag = betrag.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        wann = _gleicher_tag_im_jahr(letzter.date, heute.year)
        if wann < heute:
            wann = _gleicher_tag_im_jahr(letzter.date, heute.year + 1)
        if not (heute <= wann < fenster_ende):
            continue

        out.append(Posten(
            datum=wann,
            # Händlername statt Rohtext: „E-Banking Auftrag an MUSTER AG" stand
            # in jeder Zeile mit demselben Vorspann, brauchte auf 375 px zwei
            # Zeilen und drückte den Betrag aus dem Blick. ``display_name`` ist
            # dieselbe Aufbereitung, die die Abo-Liste schon benutzt.
            titel=display_name(letzter.description) or "Jahreszahlung",
            # Nur Anzahl und Datum, KEIN zweiter Betrag: der Betrag rechts in
            # der Zeile IST der dieses Belegs. Stand hier eine eigene Zahl,
            # widersprach sie ihm — der Median liegt zwischen den Jahren, der
            # genannte Beleg ist der jüngste. Stünde dieselbe Zahl zweimal,
            # wäre die zweite Fülltext.
            hinweis=(
                f"In {len({t.date.year for t in belege})} Jahren gekommen, "
                f"zuletzt am {letzter.date.strftime('%d.%m.%Y')}."
            ),
            art="jahresposten",
            betrag=betrag,
        ))
    return out


def summe_jahresposten(posten: list[Posten]) -> Decimal:
    """Summe genau der Beträge, die als Jahreszahlung in der Liste stehen.

    Eigene Funktion, damit die Regel prüfbar ist: die Zeile heisst „Erwartete
    Jahreszahlungen zusammen" und darf deshalb keine Frist mitzählen, falls eine
    je einen Betrag bekommt (die 3a-Frist kennt schon heute einen offenen
    Betrag — sie trägt ihn nur noch nicht im Feld).
    """
    return sum(
        (p.betrag for p in posten if p.art == "jahresposten" and p.betrag),
        Decimal("0"),
    )


def was_kommt(db: Session, heute: date | None = None) -> dict:
    """Fristen und erwartete Jahreszahlungen der nächsten Monate, chronologisch."""
    heute = heute or heute_lokal()
    fenster_ende = add_months(heute.replace(day=1), _HORIZONT_MONATE + 1)

    posten = [p for p in fristen(db, heute) if p.datum < fenster_ende]
    posten += jahresposten(db, heute)
    posten.sort(key=lambda p: p.datum)

    return {
        "posten": posten,
        "summe": summe_jahresposten(posten),
        "fristen_stand": FRISTEN_STAND,
        "veraltet": heute.year > FRISTEN_STAND,
    }
