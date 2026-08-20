"""Treffen-Fonds: gemeinsamer Spar-Topf zweier Personen für Besuche.

Reines Planungs-Modul — NICHT an Konten/Buchungen gekoppelt. **A** spart
monatlich in CHF, **B** in EUR; je Monat bestätigt ein Klick, dass das Geld
wirklich zurückgelegt wurde. Gerechnet wird alles in CHF über den **manuell**
gepflegten Kurs (kein Live-Abruf — die App bleibt offline).

Kosten je Besuch (Faktoren im UI anpassbar, alle CHF):

* **bei B** (A reist): Flug + Unterkunft × Nächte + Verpflegung × Tage
* **bei A** (B reist): Flug + Verpflegung × Tage (keine Übernachtung)

Verpflegungstage = Nächte + 1 (Fr–Mo = 3 Nächte = 4 Tage). Vergangene Treffen
mindern den Topf (Geld ist ausgegeben), künftige senken nur die Prognose.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.dates import add_months
from moneten.db.models import (
    Account,
    MeetContribution,
    MeetFundSettings,
    MeetVisit,
    Transaction,
)
from moneten.services.charts import curve_path, curve_segments, sparkline
from moneten.templating import MONATE

# Die beiden Personen heissen im Code A und B. Ihre ANZEIGENAMEN stehen in den
# Einstellungen und damit in den Daten — ein Schema soll keine Menschen benennen.
PERSONS = ("a", "b")

# Wo man sich trifft, und daraus folgt, wer reist. Die Werte hiessen einmal nach
# den beiden Ländern; auch das war eine Auskunft, die niemanden etwas angeht.
LOCATIONS = ("bei_b", "bei_a")

# Spar-Währung je Person — an EINER Stelle, damit Template und Router nicht
# jeweils ihre eigene Zuordnung mitschleppen. A spart in der Währung der App,
# B in Euro; umgerechnet wird über den manuell gepflegten Kurs.
PERSON_CURRENCY = {"a": "CHF", "b": "€"}


def person_label(settings: MeetFundSettings) -> dict[str, str]:
    """Anzeigename je Person, aus den Einstellungen.

    Als Funktion und nicht als Konstante: die Namen sind Daten, und eine
    Konstante im Modul wäre genau die Stelle, an der wieder ein echter Name
    landet.
    """
    return {"a": settings.name_a, "b": settings.name_b}

# Wie weit die Monatsliste in die Zukunft reicht. Router und Dienst müssen
# dieselbe Grenze kennen: die Route prüft damit, ob ein geposteter Monat
# überhaupt in der Oberfläche angeboten wurde.
VORLAUF = 3

# Zeichenfläche der Prognose-Kurve — muss zum viewBox im Template passen.
# Der Rand muss die groesste Marke aufnehmen, die auf einem Kurvenpunkt sitzt.
# Bei 10 wurde die Flagge des letzten Treffens am Handy nachweislich um 2.9 px
# angeschnitten (Radius 11.8 mal 1.3 Handy-Skalierung = 15.3 gegen 10 Rand) und
# bekam eine gerade Kante. 16 nimmt sie vollstaendig auf; das Zeichenfeld
# verliert dafuer 12 Einheiten Hoehe, was bei 140 nicht ins Gewicht faellt.
PROG_W, PROG_H, PROG_PAD = 620.0, 140.0, 16.0


def get_settings(db: Session) -> MeetFundSettings:
    """Liefert die (einzige) Einstellungs-Zeile — legt sie beim ersten Zugriff an."""
    s = db.scalar(select(MeetFundSettings))
    if s is None:
        s = MeetFundSettings()
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def eur_to_chf(settings: MeetFundSettings, eur: Decimal) -> Decimal:
    return (eur * settings.eur_chf_rate).quantize(Decimal("0.01"))


def monthly_total_chf(settings: MeetFundSettings) -> Decimal:
    """Geplante gemeinsame Monats-Rücklage in CHF (A + B umgerechnet)."""
    return (settings.monthly_a_chf + eur_to_chf(settings, settings.monthly_b_eur)).quantize(
        Decimal("0.01")
    )


def planned_amount(settings: MeetFundSettings, person: str) -> Decimal:
    """Geplante Monatsrate einer Person in IHRER Währung (A in CHF, B in EUR)."""
    return settings.monthly_a_chf if person == "a" else settings.monthly_b_eur


def chf_anteil(settings: MeetFundSettings, person: str, native: Decimal) -> Decimal:
    """Rechnet einen Personen-Betrag in CHF um (B über den manuellen Kurs)."""
    return native if person == "a" else eur_to_chf(settings, native)


def feld_betrag(value: Decimal) -> str:
    """Betrag für ein Eingabefeld: ``345`` statt ``345.00``.

    Ein Feld ist kein Bericht — die zwei Nullen muss der Nutzer beim Ändern
    jedes Mal überschreiben. Rappen bleiben stehen, wenn es welche gibt.
    """
    s = f"{value:.2f}"
    return s[:-3] if s.endswith(".00") else s


def visit_cost_chf(
    settings: MeetFundSettings, location: str, nights: int,
    override: Decimal | None = None,
) -> Decimal:
    """Kosten eines Besuchs nach Formel (oder ``override``). Tage = Nächte + 1."""
    if override is not None:
        return override.quantize(Decimal("0.01"))
    days = nights + 1
    food = settings.food_day_chf * days
    if location == "bei_b":
        # A reist: Flug, Unterkunft je Nacht, Verpflegung.
        cost = settings.flight_a_chf + settings.airbnb_night_chf * nights + food
    else:  # bei_a — B reist und übernachtet nicht auswärts
        cost = settings.flight_b_chf + food
    return cost.quantize(Decimal("0.01"))


def fund_balance(db: Session, settings: MeetFundSettings, today: date) -> dict:
    """Aktueller Stand des Topfs.

    ``total`` = Startbetrag + bestätigte Rücklagen (B über den Kurs) − Kosten
    aller VERGANGENEN Treffen. Der Anteil von B wird immer mit dem AKTUELLEN
    Kurs bewertet — diese Euro liegen real in Euro da.
    """
    a_betrag = Decimal("0")
    b_eur = Decimal("0")
    for c in db.scalars(select(MeetContribution)):
        if c.person == "a":
            a_betrag += c.amount_native
        else:
            b_eur += c.amount_native
    spent = Decimal("0")
    past_visits = 0
    for v in db.scalars(select(MeetVisit).where(MeetVisit.date <= today)):
        spent += visit_cost_chf(settings, v.location, v.nights, v.cost_override_chf)
        past_visits += 1
    b_chf = eur_to_chf(settings, b_eur)
    total = (settings.start_balance_chf + a_betrag + b_chf - spent).quantize(Decimal("0.01"))
    return {
        "total": total,
        "a_chf": a_betrag,
        "b_eur": b_eur,
        "b_chf": b_chf,
        "spent": spent,
        "past_visits": past_visits,
    }


def monats_label(m: date) -> str:
    """„August 2026" — an EINER Stelle, weil Liste und Fehlermeldung denselben
    Monat benennen müssen."""
    return f"{MONATE[m.month - 1]} {m.year}"


def month_span(settings: MeetFundSettings, today: date) -> tuple[date, date]:
    """Erster und letzter Monat, den die Oberfläche anbietet (jeweils 1. des Monats).

    Die Route für Bestätigung und Betrag prüft dagegen: ein Beitrag ausserhalb
    dieser Spanne zählte im Topf mit, wäre aber in keiner Liste sichtbar — ein
    Stand, den man nicht mehr nachvollziehen kann.
    """
    return settings.start_month.replace(day=1), add_months(today.replace(day=1), VORLAUF)


def start_grenze(db: Session, today: date) -> date:
    """Spätester Monat, den der Fonds-Start annehmen darf.

    Die Spanne schützt sich beim Eintragen selbst (``_meet_month``) — aber nur
    nach vorn: der Startmonat liess sich nachträglich hinter einen bereits
    erfassten Beitrag schieben. Der zählte danach weiter im Topf mit und stand
    in keiner Liste mehr; der Stand war nicht mehr herleitbar. Genau der
    Zustand, gegen den die Prüfung beim Eintragen antritt.

    Zwei Grenzen, dieselbe Regel:

    * der laufende Monat — später begänne die Liste in der Zukunft und wäre leer;
    * der früheste erfasste Beitrag — später fiele er aus jeder Liste.
    """
    frueheste = db.scalar(select(MeetContribution.month).order_by(MeetContribution.month).limit(1))
    laufend = today.replace(day=1)
    return min(laufend, frueheste) if frueheste is not None else laufend


def month_rows(db: Session, settings: MeetFundSettings, today: date) -> list[dict]:
    """Monate mit Betrag und Bestätigungs-Status je Person, neueste zuerst.

    Reicht ``VORLAUF`` Monate in die ZUKUNFT: wer schon überwiesen hat
    (Dauerauftrag, Vorauszahlung), soll das eintragen können, ohne auf den
    Monatswechsel zu warten. Rückwärts geht es bis zum Startmonat des Fonds —
    vergessene Monate lassen sich also nachtragen. Der Startmonat ist in den
    Faktoren einstellbar; ohne das wäre die Vergangenheit fest zugemauert.

    Je Person liefert die Zeile ``amount`` (der EINGETRAGENE Betrag) und
    ``planned`` (die geplante Rate). Beide getrennt, weil sie verschieden sein
    dürfen: in einem knappen Monat legt man weniger zurück, und ohne diese
    Unterscheidung müsste die Oberfläche so tun, als wäre der Plan die Wahrheit.
    ``chf_sum`` ist die Summe der TATSÄCHLICH eingetragenen Beiträge in CHF.
    """
    confirmed: dict[tuple[date, str], MeetContribution] = {
        (c.month, c.person): c for c in db.scalars(select(MeetContribution))
    }
    rows: list[dict] = []
    aktuell = today.replace(day=1)
    start, m = month_span(settings, today)
    while m >= start:
        personen: list[dict] = []
        summe = Decimal("0")
        for key in PERSONS:
            c = confirmed.get((m, key))
            geplant = planned_amount(settings, key)
            if c is not None:
                summe += chf_anteil(settings, key, c.amount_native)
            personen.append({
                "key": key,
                "label": person_label(settings)[key],
                "currency": PERSON_CURRENCY[key],
                "confirmed": c is not None,
                "amount": c.amount_native if c is not None else geplant,
                # Leeres Feld + Platzhalter = „geplant, aber noch nicht
                # zurückgelegt". Stünde die Plan-Zahl als Wert drin, sähe jeder
                # offene Monat wie ein erledigter aus.
                "field": feld_betrag(c.amount_native) if c is not None else "",
                "placeholder": feld_betrag(geplant),
                "abweichend": c is not None and c.amount_native != geplant,
            })
        rows.append({
            "month": m,
            "label": monats_label(m),
            "future": m > aktuell,
            "current": m == aktuell,
            "persons": personen,
            "chf_sum": summe.quantize(Decimal("0.01")),
        })
        m = add_months(m, -1)
    return rows


def _prognose_geometrie(werte: list[Decimal]) -> dict:
    """Sparkline-Geometrie der Prognose — aber mit GEKLEMMTER Glättung.

    ``sparkline`` glättet ungeklemmt. Die Prognose steigt monatlich linear und
    knickt an jedem Treffen scharf nach unten; an so einem Knick schiesst eine
    ungeklemmte Catmull-Rom-Kurve über den Knickwert hinaus und zeigt einen
    Stand, den der Topf nie hat. ``klemmen=True`` hält sie zwischen den echten
    Werten — dieselbe Rundung wie im Vermögens-Verlauf der Kontenseite.

    Die Fläche wird aus ``curve_segments`` neu zusammengesetzt statt aus dem
    fertigen ``d``-Attribut von ``curve_path``: dessen String wieder aufzutrennen,
    nur um das führende ``M`` durch die Grundlinie zu ersetzen, wäre Textarbeit
    an einem Ergebnis, das man genauso gut direkt bauen kann.
    """
    geo = sparkline(werte, w=PROG_W, h=PROG_H, pad=PROG_PAD)
    pts = geo["pts"]
    if len(pts) < 2:
        return geo
    boden = PROG_H - PROG_PAD
    segmente = " ".join(curve_segments(pts, klemmen=True))
    geo["line"] = curve_path(pts, klemmen=True)
    geo["area"] = f"M {pts[0][0]},{boden} L {pts[0][0]},{pts[0][1]} {segmente} L {pts[-1][0]},{boden} Z"
    return geo


def projection(db: Session, settings: MeetFundSettings, today: date, horizon: int = 18) -> dict:
    """Zukunfts-Prognose: Stand heute, dann je Monat + geplante Rücklagen,
    − Kosten künftiger Treffen im jeweiligen Monat. Liefert Werte, Labels,
    Sparkline-Geometrie und Marker für die Treffen (Index in der Reihe)."""
    monthly = monthly_total_chf(settings)
    bal = fund_balance(db, settings, today)
    future_visits = list(db.scalars(
        select(MeetVisit).where(MeetVisit.date > today).order_by(MeetVisit.date)
    ))

    cur_month = today.replace(day=1)
    values: list[Decimal] = [bal["total"]]
    labels: list[str] = ["heute"]
    markers: list[dict] = []
    value = bal["total"]
    for i in range(1, horizon + 1):
        m = add_months(cur_month, i)
        value += monthly
        for v in future_visits:
            if v.date.replace(day=1) == m:
                cost = visit_cost_chf(settings, v.location, v.nights, v.cost_override_chf)
                value -= cost
                markers.append({"idx": i, "visit": v, "cost": cost})
        values.append(value.quantize(Decimal("0.01")))
        labels.append(f"{MONATE[m.month - 1][:3]} {str(m.year)[2:]}")

    geo = _prognose_geometrie(values)
    # Marker-Koordinaten aus den Rohpunkten der Sparkline ableiten.
    for mk in markers:
        if mk["idx"] < len(geo["pts"]):
            mk["x"], mk["y"] = geo["pts"][mk["idx"]]
    # Key heisst bewusst "series", NICHT "values": in Jinja würde ``p.values``
    # die dict-Methode ``.values`` treffen statt des Eintrags.
    return {"series": values, "labels": labels, "geo": geo, "markers": markers,
            "monthly": monthly, "negative": any(v < 0 for v in values)}


def jar_stat(db: Session, settings: MeetFundSettings, total: Decimal, today: date) -> dict:
    """Das EINE Glas: Füllstand des gemeinsamen Topfs Richtung nächstem Treffen.

    Vorher standen hier zwei gleich grosse Gläser (eines je Reiseziel). Zwei
    Gefässe lesen sich als zwei Kassen — und damit als Frage, wer wie viel in
    welches getan hat. Der Topf ist aber einer; die Aufteilung nach Person ist
    nur Herkunft, kein Besitz.

    Bezugsgrösse ist das nächste GEPLANTE Treffen mit seinen echten Kosten. Ist
    keines eingetragen, zählt der teurere der beiden Standard-Besuche: ein volles
    Glas soll reichen, egal in welche Richtung als nächstes gereist wird. Der
    billigere als Massstab würde ein „voll" zeigen, das für die andere Richtung
    nicht stimmt.

    ``pct`` ist bei 100 gedeckelt, ``visits`` = für wie viele solcher Besuche der
    Topf insgesamt reicht, ``missing`` = was bis zum ersten fehlt. ``missing``
    steht in der Oberfläche anstelle eines blossen „noch nicht gedeckt": die
    Aussage ist dieselbe, nur weiss man danach, um wie viel es geht.
    """
    naechstes = db.scalar(
        select(MeetVisit).where(MeetVisit.date > today).order_by(MeetVisit.date)
    )
    if naechstes is not None:
        location = naechstes.location
        cost = visit_cost_chf(settings, location, naechstes.nights, naechstes.cost_override_chf)
    else:
        kosten = {loc: visit_cost_chf(settings, loc, settings.default_nights) for loc in LOCATIONS}
        location = max(LOCATIONS, key=lambda loc: kosten[loc])
        cost = kosten[location]
    pct = 0
    visits = 0
    if cost > 0 and total > 0:
        pct = min(100, int(total / cost * 100))
        visits = int(total / cost)
    missing = cost - total
    return {
        "location": location, "cost": cost, "pct": pct, "visits": visits,
        "visit": naechstes, "missing": missing if missing > 0 else Decimal("0"),
    }


# ---------------------------------------------------------------------------
# Rückstellung: der Topf gegen das echte Konto
# ---------------------------------------------------------------------------
#
# Bis hier war der Fonds reine Planung. Ein Klick je Monat bestätigte, dass Geld
# zurückgelegt wurde — wohin, stand nirgends. Genau da klafft die Lücke: bestätigt
# ist schnell, überwiesen wird vergessen, und der Topf zeigt einen Stand, den kein
# Konto deckt.
#
# Zwei Fragen, zwei Rechnungen, bewusst getrennt:
#   1. Liegt das Geld da?      bestätigte Rücklagen  gegen  Zuflüsse aufs Konto
#   2. Was hat es gekostet?    gerechnete Kosten     gegen  Abflüsse vom Konto
#
# Nur die Seite von A. Die Euro von B liegen bei B und nicht auf dem Konto,
# das hier abgeglichen wird — sie mitzurechnen erzeugte eine Lücke, die nie zugeht.

# Rundungspuffer. Eine Überweisung ist auf den Rappen genau; der Puffer fängt nur
# ab, dass eine von Hand getippte Rate wie 345.005 gespeichert sein könnte.
ABGLEICH_TOLERANZ = Decimal("0.05")

# Wie lange nach dem letzten Reisetag eine Abbuchung noch zur Reise zählt.
# Kartenzahlungen aus dem Ausland treffen verspätet ein.
NACHLAUF_TAGE = 14


def ferienkonto(db: Session, settings: MeetFundSettings) -> Account | None:
    """Das gewählte Ferienkonto — ``None``, wenn keins gewählt ist.

    Auch ``None``, wenn das Konto inzwischen gelöscht wurde: die Spalte hält nur
    eine Nummer, und ein Abgleich gegen ein Konto, das es nicht mehr gibt, würde
    Zuflüsse von null melden und damit eine Lücke erfinden.
    """
    if settings.holiday_account_id is None:
        return None
    return db.get(Account, settings.holiday_account_id)


def _kontobewegungen(db: Session, konto_id: int, von: date, bis: date) -> list[Transaction]:
    """Buchungen des Kontos im Zeitraum — **Umbuchungen ausdrücklich mit**.

    Überall sonst filtert die App Umbuchungen weg (``not_transfer``), weil sie
    zwischen eigenen Konten nichts verdienen und nichts ausgeben. Hier sind sie
    der ganze Punkt: die Rückstellung IST eine Umbuchung vom Lohnkonto aufs
    Ferienkonto. Mit dem üblichen Filter wäre der Zufluss unsichtbar und der
    Abgleich meldete beharrlich, es sei nichts überwiesen worden.
    """
    return list(db.scalars(
        select(Transaction).where(
            Transaction.account_id == konto_id,
            Transaction.date >= von,
            Transaction.date <= bis,
        ).order_by(Transaction.date)
    ))


def _erster_monat(db: Session, settings: MeetFundSettings) -> date:
    """Ab wann gerechnet wird: Fonds-Start oder der früheste bestätigte Monat.

    Das Minimum, nicht der Fonds-Start allein: läge ein bestätigter Monat davor,
    zählte er beim Soll mit, seine Überweisung beim Ist aber nicht — die Rechnung
    ergäbe eine Lücke, die nur aus dem Zuschnitt des Zeitraums stammt.
    """
    frueheste = db.scalar(select(MeetContribution.month).order_by(MeetContribution.month))
    return min(settings.start_month, frueheste) if frueheste else settings.start_month


def rueckstellung(db: Session, settings: MeetFundSettings, today: date) -> dict | None:
    """Bestätigte Rücklagen gegen die Zuflüsse aufs Ferienkonto.

    ``None``, wenn kein Ferienkonto gewählt ist — dann entfällt der ganze
    Abschnitt in der Oberfläche. Ein Kasten, der „kein Konto gewählt" meldet,
    wäre Fülltext.

    ``differenz`` positiv heisst: bestätigt, aber noch nicht überwiesen. Das ist
    der Normalfall im laufenden Monat und kein Fehler — die Ampel schlägt darum
    nicht an, sie benennt nur den offenen Betrag.
    """
    konto = ferienkonto(db, settings)
    if konto is None:
        return None
    von = _erster_monat(db, settings)
    soll = sum(
        (c.amount_native for c in db.scalars(
            select(MeetContribution).where(MeetContribution.person == "a")
        )),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    bewegungen = _kontobewegungen(db, konto.id, von, today)
    ist = sum((t.amount for t in bewegungen if t.amount > 0), Decimal("0")).quantize(Decimal("0.01"))
    differenz = (soll - ist).quantize(Decimal("0.01"))
    return {
        "konto": konto,
        "seit": von,
        "soll": soll,
        "ist": ist,
        "differenz": differenz,
        "offen": differenz if differenz > ABGLEICH_TOLERANZ else Decimal("0"),
        "zuviel": -differenz if differenz < -ABGLEICH_TOLERANZ else Decimal("0"),
        "geht_auf": differenz.copy_abs() <= ABGLEICH_TOLERANZ,
        "saldo": konto.current_balance,
    }


def _reise_vorbei(v: MeetVisit, today: date) -> bool:
    """Ist die Reise durch? Nicht der Abreisetag zählt, sondern der letzte Tag."""
    return v.date + timedelta(days=v.nights) <= today


def verbrauch(db: Session, settings: MeetFundSettings, today: date) -> list[dict]:
    """Je abgeschlossenem Treffen: gerechnete Kosten gegen die echten Abflüsse.

    **Die Zeitfenster teilen die Zeitachse lückenlos und überschneidungsfrei.**
    Ein Fenster reicht vom Ende des vorigen bis zum letzten Reisetag plus
    :data:`NACHLAUF_TAGE`. Anders ginge es nicht sauber: Flug und Unterkunft
    werden Wochen im Voraus bezahlt, ein fester Vorlauf von zwei Wochen verlöre
    sie, und ein grosszügiger griffe in die vorige Reise. Der Preis dieser
    Aufteilung: wird ein Flug gebucht, BEVOR die vorige Reise vorbei ist, zählt
    er zu jener. Das ist selten und sichtbar — der Betrag steht dann dort.

    Gezählt wird nur, was das Ferienkonto verlässt. Wurde eine Reise von einem
    anderen Konto bezahlt, steht hier null; das ist keine Fehlmeldung, sondern
    die Antwort auf die Frage, ob die Rückstellung gebraucht wurde.
    """
    konto = ferienkonto(db, settings)
    if konto is None:
        return []
    besuche = [
        v for v in db.scalars(select(MeetVisit).order_by(MeetVisit.date))
        if _reise_vorbei(v, today)
    ]
    if not besuche:
        return []
    zeilen = []
    fenster_start = _erster_monat(db, settings)
    for v in besuche:
        ende = v.date + timedelta(days=v.nights + NACHLAUF_TAGE)
        abgang = sum(
            (-t.amount for t in _kontobewegungen(db, konto.id, fenster_start, ende) if t.amount < 0),
            Decimal("0"),
        ).quantize(Decimal("0.01"))
        gerechnet = visit_cost_chf(settings, v.location, v.nights, v.cost_override_chf)
        zeilen.append({
            "visit": v,
            "von": fenster_start,
            "bis": ende,
            "gerechnet": gerechnet,
            "abgang": abgang,
            "differenz": (abgang - gerechnet).quantize(Decimal("0.01")),
            "nichts_bezahlt": abgang == 0,
        })
        fenster_start = ende + timedelta(days=1)
    return zeilen
