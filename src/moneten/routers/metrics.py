"""Verläufe: Werte aus Belegen — Prämie, Strom, Lohn, Vorsorge, Steuern.

Diese Reihen sind **bewusst keine Buchungen**. Sie stammen aus Belegen
(Prämienabrechnung, Stromrechnung, Vorsorgeausweis, Police) und stehen zum Teil
längst als Kontobelastung in den Transaktionen — die Krankenkassenprämie etwa
wird jeden Monat abgebucht. Ein Import als Buchung zählte sie doppelt und die
Monatsbilanz wäre falsch. Der Unterschied muss auch auf der Seite lesbar sein;
den Satz dazu trägt jede Reihe in ``MetricSeries.note``.

Geschrieben wird auf zwei Wegen, beide über :func:`services.metrics.setze_punkt`:

* **Von Hand** — eine Periode, ein Wert. Ersetzt einen vorhandenen Punkt.
* **Aus ``verlaeufe.json``** — der Datei, die ``scripts/verlaeufe_aus_scans.py``
  lokal aus den Scans schreibt. Auch unsichere (OCR-)Werte werden geschrieben,
  aber als unbestätigt markiert.

RENDERN: eine einzige Vorlage, ``metrics.html``, kein Partial. Jede Route gibt
die vollständige Seite zurück; die Formulare holen sich mit ``hx-select`` den
Ausschnitt ``#verlaeufe-root`` heraus. Ein Partial wäre hier eine zweite Datei
für genau einen Ausschnitt, den nur diese Seite kennt — und Import und
Bestätigung ändern ohnehin auch den Bereich ausserhalb davon.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import (
    BudgetInterval,
    Category,
    ManualSubscription,
    MetricCadence,
    MetricKind,
    MetricPoint,
    MetricSeries,
    MetricUnit,
    User,
)
from moneten.db.session import get_db
from moneten.money import parse_amount
from moneten.services.metrics import (
    alle_verlaeufe,
    archiviere_reihe,
    formatiere,
    loesche_punkt,
    periode_aus_takt,
    periode_text,
    reihe_nach_slug,
    reihen,
    setze_punkt,
    verlauf,
)
from moneten.services.soll_ist import TOLERANZ, alle_abgleiche
from moneten.services.subscriptions import _merchant_key
from moneten.services.verlauf_positionen import bilder, hat_positionen
from moneten.templating import templates

router = APIRouter(tags=["metrics"])

# ``verlaeufe.json`` ist eine Textdatei mit ein paar hundert Zeilen. Die Grenze
# schützt nicht vor Angreifern (die App steht im Tailscale-Netz hinter dem PIN),
# sondern vor dem Vertipper am Handy: eine versehentlich gewählte Fotodatei soll
# nicht erst vollständig im Speicher landen und dann abgelehnt werden.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# Das Format, das das Skript schreibt. Eine künftige Version 2 darf NICHT
# stillschweigend als 1 gelesen werden — sonst landen Werte falsch gedeutet in
# der DB, und das fällt erst auf, wenn die Kurve schon Unsinn zeigt.
_FORMAT_VERSION = 1

# Mehr Befunde als das kann keine Belegsammlung dieser Grösse hergeben; eine
# Datei darüber ist keine Verlaufsdatei mehr.
_MAX_BEFUNDE = 2000

# So viele Einzelprobleme werden benannt, der Rest nur gezählt. Eine Seite mit
# dreihundert roten Zeilen liest niemand — die ersten paar sagen ohnehin, was
# in der Datei schiefliegt.
_MAX_MELDUNGEN = 10

# Schlüssel des Unsicher-Markers in ``MetricPoint.extras``. Das Template liest
# ihn (``p.extras.get('unsicher')``) und zeichnet solche Punkte gestrichelt. Der
# WERT trägt keine Aussage — die Anwesenheit des Schlüssels ist die Aussage;
# darum eine feste Marke und nicht etwa der Lesehinweis, der als Notiz am Punkt
# steht und beim Bestätigen erhalten bleiben soll.
_UNSICHER = "unsicher"
_MARKE = "1"

# Grenzen für ``extras``. Sie schützen nicht vor Angreifern, sondern vor einer
# Datei, die nicht ist, was sie zu sein vorgibt: ein Punkt mit tausend
# Nebenwerten wäre keine gelesene Rechnung mehr, sondern ein Fehler im
# Extraktionsskript — und er landete ungebremst als JSON-Blob in der Zeile.
# Die grösste gemessene Anbieter-Rechnung trägt elf Positionen; sechzig lässt
# reichlich Luft und zieht die Grenze trotzdem lange vor dem Unsinn.
_MAX_EXTRAS = 60
_MAX_EXTRA_SCHLUESSEL = 120

# Positionen eines aufgeschlüsselten Belegs (Konvention siehe ``MetricPoint``).
# Der Wert MUSS eine Zahl sein: die Verlaufsseite stapelt sie zum Balken, und
# ein Text darin führte dort zu einer Ausnahme mitten im Rendern statt zu einer
# Meldung beim Import, wo sie hingehört.
_POS_PRAEFIX = "pos:"


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------


def _seite(
    request: Request,
    user: User,
    db: Session,
    *,
    error: str | None = None,
    form_slug: str | None = None,
    form_values: dict | None = None,
    import_report: dict | None = None,
    status_code: int = 200,
) -> Response:
    """Die vollständige Verlaufsseite — auch als Antwort auf ein HTMX-Formular.

    ``form_values`` sind die getippten Rohwerte, ``form_slug`` die Reihe, deren
    Formular den Fehler warf. Beide kommen bei jedem Validierungsfehler zurück,
    damit das Formular nicht leer neu aufbaut und offen bleibt — der Fall, den
    ``tests/test_form_retention.py`` für die anderen Formulare festhält.

    Fehlerantworten tragen einen gerenderten Rumpf und keine nackte
    HTTPException: ``static/js/app.js`` swappt 4xx nur ein, wenn HTML mitkommt.
    Ohne das verschluckt HTMX die Meldung und die Oberfläche wirkt tot.
    """
    verlaeufe = alle_verlaeufe(db)
    # Archivierte gehoeren auf dieselbe Seite: ein Weg hinaus ohne sichtbaren Weg
    # zurueck ist ein Loeschen mit Umweg.
    archivierte = [r for r in reihen(db, mit_archivierten=True) if r.archived]
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {
            "user": user,
            "active_tab": "metrics",
            "verlaeufe": verlaeufe,
            "archivierte": archivierte,
            # Abgleich Beleg gegen Buchung, je Reihe. Auffällige zuerst — bei elf
            # Reihen soll man nicht suchen müssen, wo etwas nicht stimmt.
            "abgleiche": {a.reihe.slug: a for a in alle_abgleiche(db, verlaeufe)},
            # Balkenbild je Reihe, die nach Positionen aufgeschlüsselt ist.
            # Fehlt der Eintrag, zeichnet das Template die Linie wie bisher —
            # Reihen ohne Positionen dürfen sich nicht ändern, nur weil eine
            # andere jetzt Balken kann.
            "positions_bilder": bilder(verlaeufe, heute_lokal()),
            # Die FUNKTION, nicht ihr Ergebnis: die Einheit hängt an der Reihe,
            # nicht an der Zahl — das Template ruft sie je Wert selbst auf.
            "formatiere": formatiere,
            # Ebenfalls die Funktion, und zwar DIESELBE, die die Route prüft:
            # der Knopf „Fixposten anlegen" darf nur dort stehen, wo die Route
            # ihn auch annimmt. Zwei getrennte Regeln liefen auseinander, und
            # der Knopf führte in einen 400er.
            "fixposten_takt": fixposten_takt,
            # Ab welcher Abweichung eine Periode als „passt nicht" gilt. Steht im
            # Tooltip der Abgleich-Zeile, damit dort keine zweite, von Hand
            # gepflegte Zahl neben der wirksamen Schwelle steht.
            "toleranz": TOLERANZ,
            # Als date-Objekt, nicht als ISO-String: das Formular leitet daraus
            # den Periodenbeginn ab (`heute.replace(day=1)`). Mit einem String
            # schlug genau dort `str.replace(day=1)` fehl und riss die ganze
            # Seite mit.
            "heute": heute_lokal(),
            "error": error,
            "form_slug": form_slug,
            "form_values": form_values or {},
            "import_report": import_report,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Seite
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def metrics_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Verlaufsseite: alle nicht archivierten Reihen samt Punkten."""
    return _seite(request, user, db)


# ---------------------------------------------------------------------------
# Handerfassung
# ---------------------------------------------------------------------------


def _periode_parsen(roh: str, takt: MetricCadence) -> date | None:
    """Formulareingabe zu einem Perioden-Beginn — oder ``None``.

    Nimmt ``2026``, ``2026-07`` und ``2026-07-01`` entgegen: auf dem Handy ist
    ``<input type="month">`` die angenehmere Eingabe, es liefert aber nur
    ``JJJJ-MM``, und für Jahresreihen reicht die Jahreszahl.

    Der Tag wird normalisiert, weil ``period_start`` der Schlüssel der Periode
    ist (Unique je Reihe): derselbe Monat mit zwei verschiedenen Tagen ergäbe
    sonst zwei Punkte für dieselbe Rechnung.
    """
    s = roh.strip()
    if not s:
        return None
    try:
        if len(s) == 4 and s.isdigit():
            d = date(int(s), 1, 1)
        elif len(s) == 7:
            d = date.fromisoformat(f"{s}-01")
        else:
            d = date.fromisoformat(s)
    except ValueError:
        return None

    if takt == MetricCadence.JAEHRLICH:
        # Bei einer Jahresreihe trägt nur das Jahr Information; alle Belege
        # (Police, Verfügung, Veranlagung) beginnen am 1. Januar. Ohne das
        # Angleichen wäre „2026-03" ein zweiter Punkt für dasselbe Jahr.
        return date(d.year, 1, 1)
    if takt == MetricCadence.UNREGELMAESSIG:
        # Stichtag statt Spanne: hier IST der Tag die Aussage (Vorsorgeausweis
        # per 30.06.). Ihn auf den Monatsersten zu ziehen, verfälschte ihn.
        return d
    # Monatlich/quartalsweise: der Monat bleibt, wie er eingegeben wurde. Eine
    # Quartalsrechnung beginnt nicht zwingend im Januar/April/Juli/Oktober; sie
    # aufs Kalenderquartal zu schieben, legte den Wert in eine Periode, die der
    # Beleg gar nicht abdeckt.
    return d.replace(day=1)


@router.post("/{slug}/punkt", response_class=HTMLResponse)
def punkt_setzen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    slug: str,
    start: Annotated[str, Form()] = "",
    wert: Annotated[str, Form()] = "",
    neben: Annotated[str, Form()] = "",
    notiz: Annotated[str, Form()] = "",
) -> Response:
    """Trägt einen Wert von Hand ein (oder korrigiert den der Periode).

    Ein von Hand gesetzter Punkt **ersetzt** den alten vollständig: Quelle und
    Nebenwerte des Belegs fallen weg (``quelle=None``). Das ist Absicht — einen
    Betrag zu korrigieren und daneben den aus dem alten Betrag gerechneten
    Rappenpreis stehen zu lassen, ergäbe eine Zeile, die sich selbst
    widerspricht. Die fehlende Quelle ist zugleich die Zusicherung der Seite:
    ein Punkt ohne Quelle ist von Hand erfasst und wird von keinem Import
    überschrieben.
    """
    roh = {"start": start, "wert": wert, "neben": neben, "notiz": notiz}

    reihe = reihe_nach_slug(db, slug)
    if reihe is None:
        return _seite(
            request, user, db, status_code=404,
            error=f"Reihe „{slug}“ gibt es nicht.",
        )

    beginn = _periode_parsen(start, reihe.cadence)
    if beginn is None:
        return _seite(
            request, user, db, status_code=400, form_slug=slug, form_values=roh,
            error="Periodenbeginn fehlt oder ist kein Datum.",
        )
    if not wert.strip():
        return _seite(
            request, user, db, status_code=400, form_slug=slug, form_values=roh,
            error="Wert fehlt.",
        )
    try:
        betrag = parse_amount(wert)
    except InvalidOperation:
        return _seite(
            request, user, db, status_code=400, form_slug=slug, form_values=roh,
            error="Wert ist keine Zahl.",
        )
    if betrag < 0:
        # Die Richtung steckt in der Art der Reihe (Ausgabe/Einnahme/Vermögen),
        # nicht im Vorzeichen. Eine negative Prämie wäre eine stumme Falschangabe:
        # sie würde die Prozentrechnung im Verlauf umdrehen.
        return _seite(
            request, user, db, status_code=400, form_slug=slug, form_values=roh,
            error="Ein Verlaufswert ist ein Betrag ohne Vorzeichen.",
        )

    extras: dict[str, str] = {}
    if reihe.secondary_key and neben.strip():
        try:
            extras[reihe.secondary_key] = str(parse_amount(neben))
        except InvalidOperation:
            label = reihe.secondary_label or "Nebenwert"
            return _seite(
                request, user, db, status_code=400, form_slug=slug, form_values=roh,
                error=f"{label}: keine Zahl.",
            )

    setze_punkt(
        db, reihe,
        start=beginn,
        ende=periode_aus_takt(reihe.cadence, beginn),
        wert=betrag,
        extras=extras or None,
        quelle=None,  # von Hand erfasst — es gibt keinen Beleg, auf den zu zeigen wäre
        notiz=notiz.strip() or None,
        ueberschreiben=True,
    )
    db.commit()
    return _seite(request, user, db)


# POST und nicht DELETE: die Zeile trägt ein echtes <form method="post"> als
# Rückfallweg, falls HTMX nicht lädt — und ein HTML-Formular kann nur GET oder
# POST senden. Mit DELETE lief der Knopf ohne JavaScript ins Leere.
@router.post("/punkt/{punkt_id:int}/delete", response_class=HTMLResponse)
def punkt_loeschen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    punkt_id: int,
) -> Response:
    """Entfernt einen Messwert."""
    if not loesche_punkt(db, punkt_id):
        # Zweimal getippt ist am Handy der Normalfall. Die Seite kommt trotzdem
        # gerendert zurück, damit HTMX etwas zum Einsetzen hat.
        return _seite(
            request, user, db, status_code=404,
            error="Diesen Messwert gibt es nicht mehr.",
        )
    db.commit()
    return _seite(request, user, db)


# POST wie beim Punkt-Löschen: die Karte trägt ein echtes Formular als
# Rückfallweg, und ein HTML-Formular kann nur GET oder POST.
@router.post("/{slug}/archiv", response_class=HTMLResponse)
def reihe_archivieren(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    slug: str,
    zurueck: Annotated[str, Form()] = "",
) -> Response:
    """Blendet eine Reihe aus — oder holt sie zurück (``zurueck=1``).

    Eine App, die man weitergibt, bringt zwölf Reihen mit, von denen sieben
    schweizspezifisch sind. Wer anderswo lebt, muss sie loswerden können, ohne
    in der Datenbank herumzuschreiben — und ohne dabei Werte zu verlieren.
    """
    if archiviere_reihe(db, slug, archiviert=not zurueck) is None:
        return _seite(request, user, db, status_code=404,
                      error="Diese Reihe gibt es nicht.")
    db.commit()
    return _seite(request, user, db)


# ---------------------------------------------------------------------------
# Fixposten aus einer Reihe
# ---------------------------------------------------------------------------
#
# Der Abgleich unter jedem Diagramm meldet „kein Fixposten" oder „Fixposten CHF
# 700.00, Beleg CHF 777.00". Bisher war das eine Sackgasse: die Zahl stand da,
# und man musste sie von Hand in die Abo-Seite tippen. Genau daran ist es
# hängen geblieben — gemeldet als „aktuell nur wohnung drin, dachte habe mehr
# erfasst". Die beiden Routen hier machen aus dem Befund eine Handlung.

# Takt der Reihe → Intervall des Fixpostens. Bewusst NUR diese zwei Paare.
#
# ``MetricCadence`` kennt vier Takte, ``BudgetInterval`` zwei. Für QUARTALSWEISE
# und UNREGELMAESSIG bliebe nur eine Umrechnung, und jede davon ist falsch:
#
# * quartalsweise als monatlich (÷3) legt eine Zahl in den Fixposten, die auf
#   keiner Stromrechnung steht — und rundet dabei (700/3 · 12 ≠ 700 · 4).
# * quartalsweise als jährlich (×4) wäre rechnerisch exakt, aber
#   ``soll_ist._abo_befund`` rechnet Beträge NUR zwischen monatlich und
#   jährlich um. Bei einer Quartalsreihe vergleicht es den hinterlegten Betrag
#   ungerechnet gegen den Belegwert: der frisch angelegte Jahresposten (4 ×
#   Quartalsrechnung) stünde ab der nächsten Zeile als „veraltet" da, und der
#   Knopf daneben böte an, ihn auf eine einzelne Quartalsrechnung zu senken.
#
# Ein Knopf, dessen Ergebnis dieselbe Seite im nächsten Atemzug für falsch
# erklärt, ist schlechter als kein Knopf. Darum bleibt der Platz hier leer —
# aber nicht stumm: das Template schreibt hin, warum, und verlinkt die
# Abo-Seite. Damit Strom dennoch einen Knopf bekommt, müsste zuerst
# ``soll_ist._abo_befund`` den Quartalstakt umrechnen können; das ist eine
# Änderung an einer Datei, die dieser Auftrag nicht umfasst.
_TAKT_ZU_INTERVALL: dict[MetricCadence, BudgetInterval] = {
    MetricCadence.MONATLICH: BudgetInterval.MONATLICH,
    MetricCadence.JAEHRLICH: BudgetInterval.JAEHRLICH,
}


def fixposten_takt(reihe: MetricSeries) -> BudgetInterval | None:
    """Intervall, mit dem sich diese Reihe als Fixposten führen lässt — sonst ``None``.

    ``None`` heisst: kein Knopf. Der Grund kann der Takt sein (siehe oben) oder
    die Art der Reihe — ein Fixposten ist ein wiederkehrender ABGANG. Der
    Jahreslohn oder das Altersguthaben als Fixposten wäre eine Ausgabe, die es
    nicht gibt; ``soll_ist._abo_befund`` stellt für solche Reihen aus demselben
    Grund gar keine Abo-Frage.
    """
    if reihe.kind != MetricKind.AUSGABE:
        return None
    return _TAKT_ZU_INTERVALL.get(reihe.cadence)


def posten_art(kategorie: Category | None, reihe: MetricSeries, aufgeschluesselt: bool) -> str:
    """„abo" oder „fix" — in welchem Topf der Posten auf ``/abos`` landet.

    Die erste Antwort ist DIESELBE, die die Abo-Seite bei erkannten Zahlungen
    gibt (``subscriptions._detected_kind`` und ``adopt_detected``): das
    ``is_subscription``-Flag der Kategorie. Zwei verschiedene Regeln für
    dieselbe Frage hiessen, dass derselbe Händler je nach Weg mal unter „Abos"
    und mal unter „Fixkosten" steht.

    Die zweite Antwort kommt aus einem gemeldeten Befund: „Handy-Abo" trägt im
    Seed ``is_subscription=False``, die Handyrechnung landete damit unter den
    Fixkosten — „ist ein abo, die abokosten sollen unter abos erscheinen". Eine
    MONATLICHE Reihe, deren Punkte nach Positionen aufgeschlüsselt sind, stammt
    aus einer Rechnung mit Abonnements-, Options- und Rabattzeilen; das ist
    genau, was diese App „Abo" nennt — eine monatliche Leistung, die man
    kündigen kann. Für Jahresreihen gilt es NICHT: eine Jahrespolice mit
    Positionen bleibt ein Fixposten.
    """
    if kategorie is not None and kategorie.is_subscription:
        return "abo"
    if aufgeschluesselt and reihe.cadence == MetricCadence.MONATLICH:
        return "abo"
    return "fix"


def haendler_schluessel(reihe: MetricSeries) -> str | None:
    """Schlüssel, über den der Posten an den echten Buchungen hängt.

    Aus dem NAMEN der Reihe, normalisiert mit genau der Funktion, die die
    Abo-Erkennung auf jeden Buchungstext anwendet. Das ist kein Raten: dieselbe
    Ableitung nehmen ``services/committed.py`` und ``services/upcoming.py``
    längst vor, wenn ein Posten kein ``match_keyword`` trägt. Nur zieht sie dort
    die zwei Wirkungen nicht nach sich, um die es hier geht:

    * die Abo-Seite zählt „N verbundene Buchungen" — der Posten ist damit
      belegt und nicht bloss behauptet;
    * die Auto-Erkennung überspringt den Händler (``extra_skip``). Ohne das
      stünde die Handyrechnung zweimal da, einmal erkannt und einmal von Hand,
      und beide zählten im Monatsbetrag mit.

    ``None``, wenn vom Namen nichts übrig bleibt (zu kurz, nur Füllwörter): ein
    leerer Schlüssel überspränge keinen Händler, sondern gar nichts, und stünde
    als Rätsel im Formular der Abo-Seite. Dort lässt er sich ohnehin ändern —
    diese Ableitung ist ein Vorschlag mit Wirkung, keine Festlegung.
    """
    return _merchant_key(reihe.name) or None


def _fixposten_der_kategorie(db: Session, kategorie_id: int) -> ManualSubscription | None:
    """Der laufende Posten dieser Kategorie — dieselbe Frage wie in ``soll_ist``.

    Bewusst dieselbe Abfrage OHNE ``is_active``-Filter: der Abgleich sucht so.
    Filterte diese Stelle enger, legte der Knopf einen zweiten Posten neben
    einen stillgelegten — und der Abgleich fände weiter den stillgelegten.
    """
    return db.scalar(
        select(ManualSubscription).where(ManualSubscription.category_id == kategorie_id)
    )


def _juengster_wert(db: Session, reihe: MetricSeries) -> Decimal | None:
    """Der Wert des aktuellsten Punktes — oder ``None`` bei leerer Reihe.

    Über ``verlauf(...).aktuell`` und nicht über eine eigene Abfrage: genau so
    bestimmt ``soll_ist.abgleich`` den Wert, gegen den es „veraltet" prüft.
    Zwei eigene Definitionen von „aktuell" (etwa jüngstes ``created_at`` statt
    jüngster Periode) hiessen: der Knopf schreibt einen Betrag, den der
    Abgleich unmittelbar danach wieder anmahnt.
    """
    aktuell = verlauf(db, reihe).aktuell
    return aktuell.wert if aktuell else None


def _betrag_im_intervall(
    belegwert: Decimal, takt: MetricCadence, intervall: BudgetInterval
) -> Decimal:
    """Belegwert in der Rechnungseinheit des vorhandenen Fixpostens.

    Gegenstück zur Umrechnung in ``soll_ist._abo_befund``: die bringt den
    hinterlegten Betrag auf den Takt der Reihe, hier geht es zurück. Wer den
    Monatswert ungerechnet in ein Jahresabo schriebe, machte aus 777 im Monat
    777 im Jahr — der Abgleich meldete danach dieselbe Abweichung weiter, nur
    zwölfmal grösser, und der Knopf sähe aus, als täte er nichts.

    Die Rundung im Zwölftel-Fall ist unvermeidlich (Rappen sind die kleinste
    Einheit) und bleibt mit höchstens 6 Rappen aufs Jahr weit unter
    ``soll_ist.ABO_TOLERANZ`` — der Posten gilt danach als aktuell.
    """
    if intervall == BudgetInterval.JAEHRLICH and takt == MetricCadence.MONATLICH:
        return belegwert * 12
    if intervall == BudgetInterval.MONATLICH and takt == MetricCadence.JAEHRLICH:
        return (belegwert / 12).quantize(Decimal("0.01"))
    return belegwert


@router.post("/{slug}/fixposten", response_class=HTMLResponse)
def fixposten_anlegen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    slug: str,
) -> Response:
    """Legt aus der Reihe einen wiederkehrenden Posten an.

    Name und Kategorie kommen aus der Reihe, der Betrag aus dem aktuellsten
    Punkt, das Intervall aus dem Takt. Kein Formular dazwischen: alle vier
    Angaben stehen bereits auf der Karte, und ein vorausgefülltes Formular
    wäre nur eine Gelegenheit, sie zu verstellen.

    Die Art (``kind``) und der Händler-Schlüssel kommen aus :func:`posten_art`
    und :func:`haendler_schluessel` — beides mit Begründung dort. Ohne den
    Schlüssel stand die Handyrechnung anschliessend ZWEIMAL auf ``/abos``: als
    erkannte Zahlung und als neuer Posten.

    Kein Konto (``account_id``): welches Konto die Zahlung trägt, weiss die
    Reihe wirklich nicht, und geraten wäre es eine Behauptung. Auf der
    Abo-Seite lässt sich der Posten danach ergänzen.
    """
    reihe = reihe_nach_slug(db, slug)
    if reihe is None:
        return _seite(
            request, user, db, status_code=404,
            error=f"Reihe „{slug}“ gibt es nicht.",
        )
    if reihe.category_id is None:
        return _seite(
            request, user, db, status_code=400,
            error=f"„{reihe.name}“ ist keiner Kategorie zugeordnet — der Abgleich fände "
                  "den Fixposten danach nicht wieder.",
        )
    intervall = fixposten_takt(reihe)
    if intervall is None:
        return _seite(
            request, user, db, status_code=400,
            error=f"„{reihe.name}“ lässt sich nicht als Fixposten führen: ein Fixposten "
                  "ist ein monatlicher oder jährlicher Abgang.",
        )
    betrag = _juengster_wert(db, reihe)
    if betrag is None:
        return _seite(
            request, user, db, status_code=400,
            error=f"„{reihe.name}“ hat noch keinen Wert — es gäbe keinen Betrag.",
        )
    # Null ist ein gueltiger Messwert, aber kein Fixposten. „Direkte
    # Bundessteuer 2023" steht echt auf 0.00 — ein Posten darueber plant nichts
    # und BELEGT die Kategorie: der naechste Versuch mit einem richtigen Betrag
    # liefe in den 409 „gibt es bereits". Darum hier abweisen und sagen, warum.
    if betrag == 0:
        return _seite(
            request, user, db, status_code=400,
            error=f"„{reihe.name}“ steht zuletzt auf null — daraus wird kein "
                  "Fixposten. Trag einen Wert nach oder leg den Posten von Hand an.",
        )
    if (vorhanden := _fixposten_der_kategorie(db, reihe.category_id)) is not None:
        # 409 und nicht 400: die Anfrage war in Ordnung, die Lage hat sich
        # geändert. Am Handy ist der zweite Tipp auf denselben Knopf der
        # Normalfall — die Meldung nennt darum den Posten, der schon da ist.
        return _seite(
            request, user, db, status_code=409,
            error=f"Für diese Kategorie gibt es bereits den Fixposten „{vorhanden.name}“.",
        )

    db.add(ManualSubscription(
        name=reihe.name,
        amount=betrag,
        interval=intervall,
        kind=posten_art(
            db.get(Category, reihe.category_id), reihe, hat_positionen(verlauf(db, reihe))
        ),
        match_keyword=haendler_schluessel(reihe),
        category_id=reihe.category_id,
    ))
    db.commit()
    return _seite(request, user, db)


@router.post("/{slug}/fixposten/betrag", response_class=HTMLResponse)
def fixposten_betrag_uebernehmen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    slug: str,
) -> Response:
    """Bringt den hinterlegten Betrag auf den Wert des jüngsten Belegs.

    Nur der Betrag. Name, Intervall, Kategorie und Konto bleiben, wie man
    sie gesetzt hat — der Befund lautet „der Betrag ist veraltet" und nicht
    „der Posten ist falsch".

    Steht der Betrag schon richtig, ist das kein Fehler, sondern nichts zu tun:
    die Seite kommt unverändert zurück. Ein 4xx auf den zweiten Tipp desselben
    Knopfes wäre eine Fehlermeldung für einen Erfolg.
    """
    reihe = reihe_nach_slug(db, slug)
    if reihe is None:
        return _seite(
            request, user, db, status_code=404,
            error=f"Reihe „{slug}“ gibt es nicht.",
        )
    if reihe.kind != MetricKind.AUSGABE or reihe.category_id is None:
        return _seite(
            request, user, db, status_code=400,
            error=f"Zu „{reihe.name}“ gibt es keinen Fixposten-Abgleich.",
        )
    abo = _fixposten_der_kategorie(db, reihe.category_id)
    if abo is None:
        return _seite(
            request, user, db, status_code=404,
            error=f"Zu „{reihe.name}“ ist kein Fixposten erfasst.",
        )
    belegwert = _juengster_wert(db, reihe)
    if belegwert is None:
        return _seite(
            request, user, db, status_code=400,
            error=f"„{reihe.name}“ hat noch keinen Wert — es gäbe nichts zu übernehmen.",
        )

    abo.amount = _betrag_im_intervall(belegwert, reihe.cadence, abo.interval)
    db.commit()
    return _seite(request, user, db)


# ---------------------------------------------------------------------------
# Import aus verlaeufe.json
# ---------------------------------------------------------------------------


def _iso_datum(wert: object) -> date | None:
    """``"2026-07-01"`` zu ``date`` — alles andere zu ``None``."""
    if not isinstance(wert, str):
        return None
    try:
        return date.fromisoformat(wert)
    except ValueError:
        return None


def _kopf_pruefen(daten: object) -> str | None:
    """Prüft Hülle und Version der Datei. Meldung, wenn etwas fehlt."""
    if not isinstance(daten, dict):
        return "Erwartet wird ein JSON-Objekt mit den Schlüsseln „version“ und „befunde“."
    if "version" not in daten:
        return "Der Datei fehlt die Angabe „version“ — ist das wirklich eine verlaeufe.json?"
    if daten.get("version") != _FORMAT_VERSION:
        return (
            f"Format-Version {daten.get('version')!r} kennt diese App nicht — "
            f"erwartet wird Version {_FORMAT_VERSION}."
        )
    befunde = daten.get("befunde")
    if not isinstance(befunde, list):
        return "„befunde“ fehlt oder ist keine Liste."
    if len(befunde) > _MAX_BEFUNDE:
        return f"{len(befunde)} Einträge sind zu viele (max. {_MAX_BEFUNDE})."
    return None


def _extras_lesen(roh: dict) -> tuple[dict[str, str] | None, str | None]:
    """Nebenwerte prüfen und zu ``dict[str, str]`` machen.

    Geprüft wird, was später nicht mehr zu retten ist: Anzahl und Schlüssellänge
    (sonst schreibt ein verirrtes Skript beliebig viel JSON in eine Zeile) und
    bei Positionen der Betrag. Eine Position mit dem Wert „ca. 20" käme sonst
    durch den Import und fiele erst beim Zeichnen des Balkens auf — dann aber
    als Fehlerseite, nicht als Meldung an der Datei, aus der sie stammt.
    """
    if len(roh) > _MAX_EXTRAS:
        return None, f"{len(roh)} Nebenwerte sind zu viele (max. {_MAX_EXTRAS})."
    extras: dict[str, str] = {}
    for k, w in roh.items():
        schluessel = str(k)
        if len(schluessel) > _MAX_EXTRA_SCHLUESSEL:
            return None, (
                f"der Nebenwert „{schluessel[:40]}…“ hat einen zu langen Namen "
                f"(max. {_MAX_EXTRA_SCHLUESSEL} Zeichen)."
            )
        wert = str(w)
        if schluessel.startswith(_POS_PRAEFIX):
            try:
                # Über str(): eine Zahl aus dem JSON darf nicht den Umweg über
                # float nehmen, dort verlieren Beträge Rappen.
                zahl = Decimal(wert)
            except (ArithmeticError, ValueError):
                return None, f"die Position „{schluessel}“ hat keinen Betrag („{wert}“)."
            if not zahl.is_finite():
                return None, f"die Position „{schluessel}“ hat keinen endlichen Betrag."
        extras[schluessel] = wert
    return extras, None


def _befund_lesen(
    roh: object, reihen_nach_slug: dict[str, MetricSeries]
) -> tuple[dict | None, str | None]:
    """Einen Eintrag prüfen und in Python-Werte übersetzen.

    Gibt entweder den geprüften Befund oder eine Meldung zurück, die sagt, WAS
    fehlt: „ungültiger Eintrag“ hilft beim Suchen in einer Datei mit
    dreihundert Zeilen niemandem.
    """
    if not isinstance(roh, dict):
        return None, "kein Objekt (erwartet werden Felder wie slug, start, wert)."
    slug = str(roh.get("slug") or "").strip()
    if not slug:
        return None, "ohne „slug“ — dazu lässt sich keine Reihe finden."
    reihe = reihen_nach_slug.get(slug)
    if reihe is None:
        return None, f"unbekannte Reihe „{slug}“."

    start = _iso_datum(roh.get("start"))
    if start is None:
        return None, f"„{slug}“: „start“ fehlt oder ist kein Datum (erwartet JJJJ-MM-TT)."
    # Fehlt das Ende, ergibt es sich aus dem Takt der Reihe — dafür gibt es
    # periode_aus_takt. Ein fehlendes Ende ist kein Grund, den Wert wegzuwerfen.
    ende = _iso_datum(roh.get("ende")) or periode_aus_takt(reihe.cadence, start)
    if ende < start:
        return None, f"„{slug}“ {start.isoformat()}: „ende“ liegt vor „start“."

    try:
        # Über str(): kommt der Wert als Zahl statt als Text an, darf er nicht
        # den Umweg über float nehmen — dort verlieren Beträge Rappen.
        betrag = Decimal(str(roh.get("wert")))
    except (ArithmeticError, ValueError):
        return None, f"„{slug}“ {start.isoformat()}: „wert“ ist keine Zahl."
    if not betrag.is_finite():
        return None, f"„{slug}“ {start.isoformat()}: „wert“ ist keine endliche Zahl."
    if betrag < 0:
        return None, f"„{slug}“ {start.isoformat()}: „wert“ ist negativ."

    roh_extras = roh.get("extras") or {}
    if not isinstance(roh_extras, dict):
        return None, f"„{slug}“ {start.isoformat()}: „extras“ ist kein Objekt."
    extras, extra_meldung = _extras_lesen(roh_extras)
    if extras is None:
        return None, f"„{slug}“ {start.isoformat()}: {extra_meldung}"

    hinweis = str(roh.get("hinweis") or "").strip()
    return {
        "reihe": reihe,
        "slug": slug,
        "start": start,
        "ende": ende,
        "wert": betrag,
        "extras": extras,
        "unsicher": bool(roh.get("unsicher")),
        "hinweis": hinweis,
        "quelle": str(roh.get("quelle") or "").strip() or None,
    }, None


def _reihen_karte(db: Session) -> dict[str, MetricSeries]:
    """slug → Reihe, auch archivierte.

    Archivierte bewusst mit: der slug ist bekannt, die Reihe existiert. Einen
    gelesenen Wert zu verwerfen, weil die Reihe gerade ausgeblendet ist, wäre
    der schlechtere von zwei Fehlern — beim Entarchivieren stünde die Lücke da.
    """
    return {r.slug: r for r in reihen(db, mit_archivierten=True)}


def _punkt_der_periode(db: Session, reihe: MetricSeries, start: date) -> MetricPoint | None:
    """Der bereits erfasste Punkt dieser Reihe für diese Periode — oder ``None``."""
    return db.scalar(
        select(MetricPoint).where(
            MetricPoint.series_id == reihe.id,
            MetricPoint.period_start == start,
        )
    )


def _wert_text(wert: Decimal, einheit: MetricUnit) -> str:
    """Zahl mit Einheit, wie das Makro ``wert_text`` sie setzt.

    ``formatiere`` liefert bei CHF bewusst nur die Zahl (sie wiederholte sich
    sonst in jeder Listenzeile); in der Bestätigungsliste steht der Betrag
    allein, dort gehört die Währung davor.
    """
    if einheit == MetricUnit.CHF:
        return f"CHF {formatiere(wert, einheit)}"
    return formatiere(wert, einheit)


def _unveraendert(
    punkt: MetricPoint, befund: dict, extras: dict[str, str], notiz: str | None
) -> bool:
    """Steht der Befund schon genau so in der DB?

    Ohne diesen Vergleich meldete ein zweiter Lauf derselben Datei jeden Wert
    als „aktualisiert“ — eine Zahl, die dann nichts mehr über die Datei aussagt.

    ``notiz`` kommt von aussen und wird NICHT hier aus dem Befund abgeleitet.
    Genau daran ging der Vergleich vorbei: bei einem unsicheren Befund wird der
    Prüfhinweis bewusst nicht gespeichert (``notiz = None``), verglichen wurde
    aber gegen ``befund["hinweis"]`` — also gegen den Hinweistext. Beide waren
    nie gleich, und jeder Import schrieb dieselben unsicheren Werte erneut.
    Gemessen an der echten Datei: der zweite Lauf meldete „6 aktualisiert",
    obwohl sich nichts geändert hatte.
    """
    return (
        punkt.value == befund["wert"]
        and punkt.period_end == befund["ende"]
        and punkt.source == befund["quelle"]
        and punkt.note == notiz
        and dict(punkt.extras or {}) == extras
    )


def importiere_befunde(db: Session, daten: dict) -> dict:
    """Schreibt alle Befunde einer gelesenen ``verlaeufe.json``.

    Getrennt von der Route, weil derselbe Import zweimal gebraucht wird: aus
    dem Formular und beim Deploy, wo er im Container laeuft und niemanden zum
    Hochladen braucht (``scripts/verlaeufe_importieren.py``). Die Huelle
    darum herum ist verschieden -- Datei entgegennehmen, Fehler anzeigen --,
    die Regeln duerfen es nicht sein.

    Erwartet wird die schon geparste und mit :func:`_kopf_pruefen` gepruefte
    Huelle. Rueckgabe ist der Bericht, den die Seite anzeigt.
    """
    reihen_nach_slug = _reihen_karte(db)
    neu = aktualisiert = uebersprungen = 0
    fehler: list[str] = []
    unsicher: list[dict] = []

    for nr, eintrag in enumerate(daten["befunde"], 1):
        befund, meldung = _befund_lesen(eintrag, reihen_nach_slug)
        if befund is None:
            fehler.append(f"Eintrag {nr}: {meldung}")
            continue

        reihe = befund["reihe"]
        vorhanden = _punkt_der_periode(db, reihe, befund["start"])
        extras = dict(befund["extras"])

        # Der Prüfhinweis („bitte gegen die Verfügung prüfen") wird NICHT als
        # Notiz gespeichert: er fordert zu einer Handlung auf, die mit dem
        # Bestätigen erledigt ist, und stünde danach für immer an der Zeile.
        # Für den Bericht reicht er als Text in dieser Schleife. Bei sicheren
        # Befunden ist der Hinweis dagegen eine dauerhafte Einordnung
        # („Hochgerechnet aus dem Vorsorgeausweis") und wird gespeichert.
        notiz = None if befund["unsicher"] else (befund["hinweis"] or None)

        if vorhanden is None:
            if befund["unsicher"]:
                extras[_UNSICHER] = _MARKE
            punkt, _ = setze_punkt(
                db, reihe,
                start=befund["start"],
                ende=befund["ende"],
                wert=befund["wert"],
                extras=extras or None,
                quelle=befund["quelle"],
                notiz=notiz,
                ueberschreiben=False,
            )
            db.flush()  # die Bestätigungsliste braucht die vergebene id
            neu += 1
        else:
            punkt = vorhanden
            # Der Marker gehört VOR den Vergleich, nicht danach. Sonst wurde ein
            # noch unbestätigter Punkt mit Extras ohne Marker gegen einen
            # gespeicherten MIT Marker verglichen — sie waren nie gleich, und
            # jeder Import schrieb dieselben Werte erneut. Gemessen: ein zweiter
            # Lauf derselben Datei meldete „6 aktualisiert", obwohl sich nichts
            # geändert hatte. Die Zahl im Bericht war damit keine Auskunft mehr.
            if (vorhanden.extras or {}).get(_UNSICHER):
                extras[_UNSICHER] = _MARKE
            schreibt = vorhanden.source is not None and not _unveraendert(
                vorhanden, befund, extras, notiz
            )
            # Eine Bestätigung gilt nur für den Wert, der bestätigt wurde. Ändert
            # ein späterer Lauf ihn, muss die Marke zurück — sonst stünde der
            # neue Betrag als geprüft da, obwohl niemand ihn je gesehen hat.
            # Bleibt der Wert gleich, bleibt auch die Bestätigung.
            if befund["unsicher"] and schreibt:
                extras[_UNSICHER] = _MARKE
            if not schreibt:
                uebersprungen += 1
            else:
                setze_punkt(
                    db, reihe,
                    start=befund["start"],
                    ende=befund["ende"],
                    wert=befund["wert"],
                    extras=extras or None,
                    quelle=befund["quelle"],
                    notiz=notiz,
                    ueberschreiben=True,
                )
                aktualisiert += 1

        if (punkt.extras or {}).get(_UNSICHER):
            unsicher.append({
                "punkt_id": punkt.id,
                "reihe": reihe.name,
                "periode": periode_text(reihe.cadence, punkt.period_start, punkt.period_end),
                "quelle": punkt.source,
                # Aus dem Befund, nicht aus der gespeicherten Notiz: der
                # Prüfhinweis wird bewusst nicht mehr am Punkt abgelegt.
                "grund": befund["hinweis"] or None,
                "wert": _wert_text(punkt.value, reihe.unit),
            })

    db.commit()

    if (rest := len(fehler) - _MAX_MELDUNGEN) > 0:
        fehler = fehler[:_MAX_MELDUNGEN] + [f"{rest} weitere Einträge nicht gelesen."]
    return {
        "neu": neu,
        "aktualisiert": aktualisiert,
        "uebersprungen": uebersprungen,
        "fehler": fehler,
        "unsicher": unsicher,
    }


@router.post("/import", response_class=HTMLResponse)
def import_datei(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    datei: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Nimmt die ``verlaeufe.json`` entgegen und schreibt ALLE Befunde.

    Auch die unsicheren: sie bekommen ``extras['unsicher']`` und stehen damit
    gestrichelt im Diagramm und mit dem Vermerk „unbestätigt“ in der Werteliste.
    Ein Wert, der sichtbar ist und sich als unbestätigt zu erkennen gibt, lässt
    sich gegen den Beleg prüfen; einer, der bis zur Bestätigung gar nicht
    erscheint, ist beim nächsten Seitenaufbau weg.

    Unangetastet bleibt, was von Hand erfasst wurde — das sind die Punkte ohne
    Quelle, und genau das sagt die Seite über dem Formular zu. Punkte aus einem
    früheren Lauf tragen eine Quelle und werden auf den Stand der Datei
    gebracht; wo sich nichts geändert hat, wird auch nichts geschrieben.

    Der Unsicher-Marker wird nur an NEUE Punkte gesetzt. Sonst nähme ein zweiter
    Lauf derselben Datei jede Bestätigung wieder zurück.
    """
    if datei is None or not datei.filename:
        return _seite(request, user, db, error="Keine Datei ausgewählt.", status_code=400)

    # Ein Byte über die Grenze hinaus lesen: das genügt, um „zu gross" zu
    # erkennen, ohne die ganze Datei erst einzulesen.
    roh_bytes = datei.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(roh_bytes) > _MAX_UPLOAD_BYTES:
        return _seite(
            request, user, db, status_code=400,
            error=f"Die Datei ist zu gross (max. {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )
    if not roh_bytes.strip():
        return _seite(request, user, db, error="Die Datei ist leer.", status_code=400)

    try:
        daten = json.loads(roh_bytes.decode("utf-8"))
    except UnicodeDecodeError:
        return _seite(
            request, user, db, status_code=400,
            error="Die Datei ist kein UTF-8-Text — erwartet wird die verlaeufe.json aus "
                  "scripts/verlaeufe_aus_scans.py.",
        )
    except json.JSONDecodeError as defekt:
        return _seite(
            request, user, db, status_code=400,
            error=f"Die Datei ist kein gültiges JSON (Zeile {defekt.lineno}, "
                  f"Spalte {defekt.colno}).",
        )

    if (meldung := _kopf_pruefen(daten)) is not None:
        return _seite(request, user, db, error=meldung, status_code=400)

    bericht = importiere_befunde(db, daten)

    # Bewusst 200, auch wenn einzelne Einträge nicht lesbar waren: die übrigen
    # sind geschrieben. Ein 4xx würde behaupten, der Import sei nicht gelaufen.
    return _seite(
        request, user, db,
        import_report={"dateiname": datei.filename, **bericht},
    )


@router.post("/bestaetigen", response_class=HTMLResponse)
def import_bestaetigen(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    punkt_id: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Nimmt den Unsicher-Marker von den angehakten Punkten.

    Geschrieben wird kein Wert: der steht seit dem Import in der Reihe. Die
    Bestätigung sagt nur, dass er stimmt — sie ändert die Darstellung
    (gestrichelt → durchgezogen), nicht die Zahl.

    ``punkt_id`` kommt als Text und wird hier selbst geprüft. Über
    ``list[int]`` würde FastAPI bei einem krummen Wert mit einer nackten
    422-JSON antworten — und die zeigt die Oberfläche nicht an.
    """
    ids = punkt_id or []
    bestaetigt = fehlend = 0

    for roh in ids:
        punkt = db.get(MetricPoint, int(roh)) if roh.strip().isdigit() else None
        if punkt is None:
            fehlend += 1
            continue
        extras = dict(punkt.extras or {})
        if extras.pop(_UNSICHER, None) is None:
            continue  # war schon bestätigt — kein Fehler, nur nichts zu tun
        # Neues dict statt Änderung am alten: die JSON-Spalte hat keine
        # Mutations-Überwachung, eine Änderung im selben Objekt sähe SQLAlchemy
        # nicht und der Marker bliebe in der DB stehen.
        punkt.extras = extras or None
        bestaetigt += 1
    db.commit()

    if not fehlend:
        return _seite(request, user, db)
    return _seite(
        request, user, db,
        error=f"Nicht gefunden: {fehlend} von {len(ids)} Werten.",
        # Nur wenn gar nichts ankam, ist die Anfrage als Ganzes ins Leere
        # gelaufen; sonst hat sie getan, was möglich war.
        status_code=404 if bestaetigt == 0 else 200,
    )
