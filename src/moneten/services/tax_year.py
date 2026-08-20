"""Steuerjahr-Auszug: die Zahlen, die die Steuererklärung jedes Jahr verlangt.

Die App **rechnet keine Steuern** und kennt keine Steuerregeln. Sie liefert die
Summen, die man sonst mühsam aus Kontoauszügen zusammensucht — mehr nicht. Alles
andere wäre Steuerberatung, und die gehört nicht in eine Budget-App.

Zwei Teile:

* **Abzüge** — Summe je Steuerposition über das Kalenderjahr. Welche Kategorie zu
  welcher Position gehört, steckt in :data:`POSITIONEN` und wird über Namens-
  Stichwörter zugeordnet. Bewusst über Namen statt über feste IDs: die Kategorien
  sind vom Nutzer benennbar, und eine ID-Zuordnung wäre nach der ersten
  Umbenennung still falsch.
* **Vermögen per 31.12.** — Saldo je Konto am Jahresende. Die Vermögenssteuer
  fragt genau diesen Stichtag ab, und er ist im Nachhinein aus den Buchungen
  rekonstruierbar (dieselbe Mechanik wie beim Vermögensverlauf).

Dazu kommt :func:`steuer_uebersicht` — dieselben zwei Zahlen, aber über MEHRERE
Jahre. Sie beantwortet die Frage, die ein Einzeljahr nicht beantworten kann:
bewegt sich etwas, und wohin. Die Geometrie (Prozenthöhen, Kurvenpfad) entsteht
schon hier und nicht im Template — wie bei Treemap und Sankey, damit sie
nachrechenbar bleibt.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.dates import heute_lokal
from moneten.db.models import Account, Category, Transaction
from moneten.services.charts import curve_path
from moneten.services.splits import effective_category_amounts

# Steuerposition → Stichwörter im Kategorienamen (klein geschrieben, Teiltreffer).
# Die Reihenfolge bestimmt die Anzeige. Trifft ein Stichwort auf mehrere
# Kategorien, werden sie zusammengezählt.
POSITIONEN: list[tuple[str, tuple[str, ...], str]] = [
    ("Säule 3a", ("säule 3a", "saeule 3a", "3a"),
     "Einzahlungen in die gebundene Vorsorge — abzugsfähig bis zum Maximalbetrag."),
    ("Krankenkasse (KVG)", ("krankenkasse grund", "kvg"),
     "Grundversicherungs-Prämien."),
    ("Krankenkasse (VVG)", ("krankenkasse zusatz", "vvg"),
     "Zusatzversicherungs-Prämien."),
    # „medikament", „optiker" und „brille" ergaenzt: ohne sie erfasste die
    # Position nur den Zahnarzt, obwohl selbst getragene Medikamenten- und
    # Brillenkosten genauso dazugehoeren. Gegen den echten Kategorienbaum
    # nachgemessen — die Position war dadurch systematisch zu tief.
    ("Gesundheitskosten",
     ("arzt", "spital", "apotheke", "medikament", "optiker", "brille", "zahnarzt", "physio"),
     "Selbst getragene Kosten — abzugsfähig erst über dem Selbstbehalt."),
    ("Spenden", ("spende", "spenden"),
     "Zuwendungen an gemeinnützige Organisationen."),
    ("Berufsauslagen", ("berufsaus", "arbeitsweg", "weiterbildung", "pendler"),
     "Fahrkosten, auswärtige Verpflegung, Weiterbildung."),
]


# Wie viele Jahre die Übersicht nebeneinander stellt. Bei acht Spalten bleiben
# auf 375px rund 30px je Spalte — gerade genug für eine vierstellige Jahreszahl
# unter dem Balken. Ältere Jahre fallen aus dem Bild; damit das keine stille
# Kürzung ist, nennt der Kartenkopf die gezeigte Spanne, und über ?jahr=YYYY
# bleibt jedes Jahr erreichbar.
MAX_JAHRE_UEBERSICHT = 8

# Farbslot der Vermögenslinie in der Übersicht. Bewusst der LETZTE der Palette:
# die Abzugs-Positionen belegen die Slots 0..len(POSITIONEN)-1, und
# tests/test_chart_kontrast.py garantiert den Abstand nur INNERHALB der acht
# Palettentöne. Eine Sonderfarbe (--chart-save o.ä.) wäre in „nord" identisch
# mit --chart-0 gewesen — die Linie hätte dort ausgesehen wie „Säule 3a".
VERMOEGEN_SLOT = 7


def _kategorie_ids(cats: list[Category], stichworte: tuple[str, ...]) -> list[int]:
    return [
        c.id for c in cats
        if any(w in (c.name or "").lower() for w in stichworte)
    ]


def jahre_mit_buchungen(db: Session) -> list[int]:
    """Kalenderjahre, in denen es überhaupt Buchungen gibt — aufsteigend.

    Bewusst in Python statt über ``strftime('%Y', …)``: das wäre SQLite-Syntax,
    und die Jahresliste ist die einzige Stelle, an der die Auswertung sonst an
    einer bestimmten Datenbank hinge.
    """
    return sorted({d.year for d in db.scalars(select(Transaction.date)) if d})


def steuerjahr(db: Session, jahr: int) -> dict:
    """Abzugs-Summen und Jahresend-Vermögen für ein Kalenderjahr."""
    von = date(jahr, 1, 1)
    bis = date(jahr, 12, 31)
    cats = list(db.scalars(select(Category)))

    # Über die Split-Auflösung statt direkt über Transaction.category_id: bei einer
    # aufgeteilten Buchung ist die Kategorie am Kopf NULL, die Kategorien stecken in
    # den Splits. Gerade Gesundheits- und Berufsauslagen-Belege werden vom
    # Quittungs-Scan routinemässig aufgeteilt — die direkte Abfrage hätte sie still
    # übersehen und eine zu tiefe Zahl in die Steuererklärung geliefert.
    zeilen = effective_category_amounts(
        db, date_from=von, date_to=bis + timedelta(days=1)
    )

    positionen = []
    for titel, stichworte, hinweis in POSITIONEN:
        ids = _kategorie_ids(cats, stichworte)
        if not ids:
            positionen.append({"titel": titel, "hinweis": hinweis, "betrag": None,
                               "kategorien": [], "anzahl": 0})
            continue
        treffer = [(cid, betrag) for cid, betrag, _ in zeilen if cid in ids]
        netto = sum((betrag for _, betrag in treffer), Decimal("0"))
        positionen.append({
            "titel": titel,
            "hinweis": hinweis,
            # Ausgaben sind negativ gespeichert; für die Steuererklärung will man
            # den positiven Betrag sehen. Bewusst NICHT abs(): überwiegen die
            # Rückerstattungen, ist die Netto-Summe positiv — man hat in dieser
            # Position unterm Strich Geld bekommen, nicht ausgegeben. abs() hätte
            # daraus einen Abzug in gleicher Höhe gemacht.
            "betrag": -netto if netto < 0 else Decimal("0"),
            "kategorien": [c.name for c in cats if c.id in ids],
            "anzahl": len(treffer),
        })

    # Vermögen per 31.12.: aktueller Saldo minus alles, was nach dem Stichtag
    # gebucht wurde. Für vergangene Jahre exakt, fürs laufende Jahr ist es der
    # heutige Stand — darum wird das im Template als solches gekennzeichnet.
    konten = []
    gesamt = Decimal("0")
    for a in db.scalars(select(Account).order_by(Account.sort_order, Account.id)):
        danach = db.scalar(
            select(func.sum(Transaction.amount))
            .where(Transaction.account_id == a.id, Transaction.date > bis)
        ) or Decimal("0")
        saldo = (a.current_balance or Decimal("0")) - danach
        if saldo == 0 and not a.is_active:
            continue
        konten.append({"konto": a, "saldo": saldo})
        gesamt += saldo

    jahre = sorted(jahre_mit_buchungen(db), reverse=True)

    return {
        "jahr": jahr,
        "positionen": positionen,
        "konten": konten,
        "vermoegen": gesamt,
        "laufend": jahr >= heute_lokal().year,
        "verfuegbare_jahre": jahre or [heute_lokal().year],
    }


# ---------------------------------------------------------------------------
# Mehrjahres-Übersicht
# ---------------------------------------------------------------------------


def _abzuege_je_jahr(db: Session, jahre: list[int]) -> dict[int, list[Decimal]]:
    """Positions-Summen je Jahr, in der Reihenfolge von :data:`POSITIONEN`.

    EIN Durchgang über die ganze Spanne statt :func:`steuerjahr` je Jahr: die
    Split-Auflösung liest sonst dieselbe Tabelle acht Mal.

    Über :func:`~moneten.services.splits.effective_category_amounts` — aus
    demselben Grund wie im Einzeljahr: bei einer aufgeteilten Buchung ist die
    Kategorie am Kopf NULL, und gerade Gesundheitsbelege werden routinemässig
    aufgeteilt. Die Übersicht zeigt damit exakt die Summen, die man beim Klick
    auf ein Jahr wiederfindet; zwei verschiedene Zahlen für dieselbe Frage wären
    schlimmer als gar keine Übersicht.
    """
    cats = list(db.scalars(select(Category)))
    ids_je_position = [set(_kategorie_ids(cats, worte)) for _, worte, _ in POSITIONEN]

    zeilen = effective_category_amounts(
        db, date_from=date(jahre[0], 1, 1), date_to=date(jahre[-1] + 1, 1, 1)
    )
    netto: dict[int, list[Decimal]] = {j: [Decimal("0")] * len(POSITIONEN) for j in jahre}
    for cid, betrag, tag in zeilen:
        eimer = netto.get(tag.year)
        if eimer is None:  # Lückenjahr innerhalb der Spanne — hat keine Spalte
            continue
        for i, ids in enumerate(ids_je_position):
            if cid in ids:
                eimer[i] += betrag

    # Dieselbe Vorzeichen-Regel wie im Einzeljahr: Ausgaben stehen negativ in der
    # DB, der Abzug ist der positive Betrag — und wer unterm Strich Geld
    # zurückbekommen hat, zieht nichts ab (kein abs()).
    return {
        j: [(-n if n < 0 else Decimal("0")) for n in werte]
        for j, werte in netto.items()
    }


def _vermoegen_je_jahr(db: Session, jahre: list[int]) -> dict[int, Decimal]:
    """Gesamtvermögen per 31.12. je Jahr — rückwärts aus dem heutigen Saldo.

    Summiert über ALLE Konten, auch archivierte. Das weicht scheinbar von
    :func:`steuerjahr` ab, die Konten mit Saldo 0 auslässt, wenn sie inaktiv
    sind — aber genau die tragen null bei. Die Gesamtzahl ist damit dieselbe.
    """
    heute_gesamt = sum(
        (a.current_balance or Decimal("0") for a in db.scalars(select(Account))),
        Decimal("0"),
    )
    out: dict[int, Decimal] = {}
    for j in jahre:
        danach = db.scalar(
            select(func.sum(Transaction.amount)).where(Transaction.date > date(j, 12, 31))
        ) or Decimal("0")
        out[j] = heute_gesamt - danach
    return out


def steuer_uebersicht(db: Session, *, max_jahre: int = MAX_JAHRE_UEBERSICHT) -> dict | None:
    """Render-Modell der Mehrjahres-Übersicht — oder ``None``, wenn sie lügen würde.

    Gezeigt werden nur Jahre MIT Buchungen. Ein Jahr ohne Buchungen als
    Null-Balken zu zeichnen hiesse zu behaupten, in diesem Jahr sei nichts
    ausgegeben worden — tatsächlich weiss die App über dieses Jahr gar nichts.

    ``None`` in zwei Fällen:

    * weniger als zwei Jahre mit Buchungen — eine Entwicklung braucht zwei
      Punkte, und für ein einzelnes Jahr steht der Auszug ohnehin darunter;
    * weder Abzüge noch Vermögen sind von null verschieden — dann bestünde die
      Grafik aus einer Nulllinie und zwei leeren Achsen.

    Alle Prozentwerte sind Bildschirmgeometrie und darum float; jeder Betrag,
    der angezeigt wird, bleibt Decimal.
    """
    jahre = jahre_mit_buchungen(db)[-max_jahre:]
    if len(jahre) < 2:
        return None

    abzuege = _abzuege_je_jahr(db, jahre)
    vermoegen = _vermoegen_je_jahr(db, jahre)

    summen = {j: sum(abzuege[j], Decimal("0")) for j in jahre}
    max_abzug = max(summen.values())
    # Positionen, die in KEINEM Jahr vorkommen, brauchen keine Legendenzeile:
    # sechs Farbtupfer, von denen vier nirgends auftauchen, machen die Legende
    # länger als das Diagramm.
    benutzt = [i for i in range(len(POSITIONEN)) if any(abzuege[j][i] > 0 for j in jahre)]

    werte = [vermoegen[j] for j in jahre]
    zeigt_vermoegen = any(v != 0 for v in werte)
    if max_abzug <= 0 and not zeigt_vermoegen:
        return None

    # Die Vermögenskurve skaliert auf ihre EIGENE Spanne, nicht ab null: liegen
    # zwei Jahreswerte nah beieinander, wäre eine Nullachse eine waagrechte
    # Linie und zeigte die Bewegung dazwischen gar nicht. Die echten
    # Zahlen stehen an der Achse, die Aussage bleibt damit ehrlich.
    #
    # Bewusst OHNE künstliche Luft um die Extremwerte: dann liegen Höchst- und
    # Tiefstwert exakt am oberen bzw. unteren Rand des Zeichenfelds, und die
    # beiden Achsenbeschriftungen dürfen schlicht oben und unten stehen. Mit
    # Luft müsste jede Marke ihre eigene Höhe mitbekommen — ein zweiter Ort, an
    # dem dieselbe Skala berechnet wird, und damit ein zweiter Ort, an dem sie
    # falsch werden kann. Den Abstand zum Rand macht die CSS über Innenabstand.
    hoch, tief = float(max(werte)), float(min(werte))
    spanne = hoch - tief

    heute_jahr = heute_lokal().year
    anzahl = len(jahre)
    spalten: list[dict] = []
    for i, j in enumerate(jahre):
        summe = summen[j]
        spalten.append({
            "jahr": j,
            # Fürs laufende (oder ein künftiges) Jahr sind die Abzüge
            # naturgemäss unvollständig und das „Vermögen per 31.12." ist der
            # heutige Stand. Ohne Kennzeichnung läse sich der halbhohe Balken
            # als Einbruch.
            "laufend": j >= heute_jahr,
            "abzuege": summe,
            "segmente": [
                {
                    "titel": POSITIONEN[k][0],
                    "slot": k,
                    "betrag": abzuege[j][k],
                    "anteil": round(float(abzuege[j][k] / summe) * 100, 2),
                }
                for k in benutzt if abzuege[j][k] > 0
            ],
            "hoehe": round(float(summe / max_abzug) * 100, 2) if max_abzug > 0 else 0.0,
            "vermoegen": vermoegen[j],
            # Spaltenmitte in Prozent. Das Balkenraster läuft ohne Spalt
            # (`gap: 0`) — nur dann liegt die Mitte der i-ten Spalte exakt bei
            # (i+0.5)/n, und die Kurvenpunkte stehen wirklich über ihren Balken.
            "vx": round((i + 0.5) / anzahl * 100, 2),
            # Spanne 0 (alle Jahre gleich hoch) → waagrechte Linie in der Mitte.
            "vy": (
                round(100 - (float(vermoegen[j]) - tief) / spanne * 100, 2)
                if spanne > 0 else 50.0
            ),
        })

    return {
        "jahre": spalten,
        "positionen": [{"titel": POSITIONEN[k][0], "slot": k} for k in benutzt],
        "max_abzug": max_abzug,
        "v_max": max(werte),
        "v_min": min(werte),
        # Bei gleichem Vermögen in allen Jahren gehört EINE Marke an die Achse.
        # Zwei — oben und unten dieselbe Zahl — behaupteten eine Spanne, die es
        # nicht gibt (dieselbe Regel wie auf der Verlaufsseite).
        "v_flach": spanne <= 0,
        # klemmen=True ist hier Pflicht: die Jahre stehen zwar gleich weit
        # auseinander, aber eine ungeklemmte Catmull-Rom-Kurve schwingt bei einem
        # Ausreisser über den höchsten bzw. unter den tiefsten Messwert hinaus —
        # die Linie liefe dann durch Vermögensstände, die es nie gab.
        "v_pfad": curve_path([(s["vx"], s["vy"]) for s in spalten], klemmen=True),
        "linien_slot": VERMOEGEN_SLOT,
        "zeigt_abzuege": max_abzug > 0,
        "zeigt_vermoegen": zeigt_vermoegen,
        "laufendes": next((s["jahr"] for s in spalten if s["laufend"]), None),
        "von": jahre[0],
        "bis": jahre[-1],
    }
