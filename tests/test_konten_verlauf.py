"""Laufen die einzelnen Konten richtig im Vermögens-Verlauf mit?

Drei Dinge können hier still schiefgehen, und alle drei ergeben ein Bild, das
plausibel aussieht und falsch ist:

* **Getrennte Skalen.** Zeichnet man jede Reihe auf ihr eigenes Minimum/Maximum,
  liegen zwei Linien übereinander, obwohl die eine 40'000 und die andere 400
  bedeutet.
* **Achse auf anderer Skala als die Kurve.** Genau der Fall, den die Umstellung
  hier hätte hinterlassen können: die Hilfslinie zeigte dann auf einen Betrag,
  an dem die Kurve gar nicht liegt.
* **Linien, die zusammen nicht die Gesamtlinie ergeben.** Ein archiviertes Konto
  mitzuzeichnen oder eines wegzulassen bricht die Aussage „so setzt es sich
  zusammen", ohne dass das Diagramm anders aussieht.

Alle Beträge sind erfunden.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from html import unescape as html_unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from moneten.db.models import Account, AccountType, Transaction
from moneten.db.session import SessionLocal
from moneten.routers.accounts import _nw_axis, _verlauf_layout
from moneten.services.account_charts import konto_verlaeufe, net_worth_series

HEUTE = date(2026, 8, 5)
MARKE = "ZKV-"  # Präfix aller hier angelegten Konten


def _konto(db, name: str, saldo: str, *, aktiv: bool = True) -> Account:
    betrag = Decimal(saldo)
    konto = Account(name=MARKE + name, type=AccountType.BANK, currency="CHF",
                    opening_balance=betrag, current_balance=betrag,
                    is_active=aktiv, sort_order=800)
    db.add(konto)
    db.flush()
    return konto


@pytest.fixture
def konten() -> Iterator[None]:
    """Legt die erfundenen Konten an und räumt sie hinterher weg.

    Die Test-DB lebt für den ganzen Lauf; bliebe hier etwas stehen, verschöbe es
    die Vermögenszahlen aller späteren Testdateien.
    """
    with SessionLocal() as db:
        _konto(db, "Gross", "40000")
        _konto(db, "Mittel", "5000")
        _konto(db, "Klein", "250")
        _konto(db, "Winzig", "80")
        _konto(db, "Mini", "30")
        _konto(db, "Leer", "0")
        _konto(db, "Archiviert", "9000", aktiv=False)
        db.commit()
    yield
    with SessionLocal() as db:
        for konto in db.scalars(select(Account).where(Account.name.startswith(MARKE))):
            for tx in db.scalars(select(Transaction).where(Transaction.account_id == konto.id)):
                db.delete(tx)
            db.flush()
            db.delete(konto)
        db.commit()


def test_konto_ohne_saldo_bekommt_keine_linie(konten: None) -> None:
    """Eine Linie auf der Nulllinie trägt keine Information und deckt die Achse zu."""
    with SessionLocal() as db:
        namen = [r["name"] for r in konto_verlaeufe(db, HEUTE, 12, hoechstens=99)]
    assert MARKE + "Gross" in namen
    assert MARKE + "Leer" not in namen


def test_archiviertes_konto_bekommt_keine_linie(konten: None) -> None:
    """Es steckt nicht in der Gesamtlinie — als Einzellinie behauptete es das aber."""
    with SessionLocal() as db:
        namen = [r["name"] for r in konto_verlaeufe(db, HEUTE, 12, hoechstens=99)]
    assert MARKE + "Archiviert" not in namen


def test_die_linien_ergeben_zusammen_die_gesamtlinie(konten: None) -> None:
    """Monat für Monat, nicht nur am rechten Rand — sonst wäre der Verlauf beliebig."""
    with SessionLocal() as db:
        gesamt = [p["value"] for p in net_worth_series(db, HEUTE, 12)]
        reihen = konto_verlaeufe(db, HEUTE, 12, hoechstens=99)
    for i, soll in enumerate(gesamt):
        ist = sum((r["werte"][i] for r in reihen), Decimal("0"))
        assert ist == soll, f"Monat {i}: Linien ergeben {ist}, Gesamtlinie zeigt {soll}"


def test_zusammenfassung_haelt_die_summe_und_die_zahl_der_farben(konten: None) -> None:
    """Auch mit Sammelreihe muss die Summe stimmen — sonst wäre sie ein Deckel."""
    with SessionLocal() as db:
        gesamt = [p["value"] for p in net_worth_series(db, HEUTE, 12)]
        reihen = konto_verlaeufe(db, HEUTE, 12, hoechstens=3)

    assert len(reihen) == 3, "Mehr Linien als erlaubt gezeichnet"
    assert reihen[-1]["rest"] is True
    assert reihen[-1]["name"].startswith("Übrige (")
    assert MARKE + "Klein" in reihen[-1]["titel"], (
        "Die Sammelreihe muss sagen, welche Konten in ihr stecken"
    )
    for i, soll in enumerate(gesamt):
        ist = sum((r["werte"][i] for r in reihen), Decimal("0"))
        assert ist == soll, f"Monat {i}: mit Sammelreihe {ist} statt {soll}"


def test_reihenfolge_nach_groesstem_betrag_im_zeitraum(konten: None) -> None:
    """Ein heute leeres Konto, das im Frühjahr 20'000 trug, prägt das Bild.

    Nach heutigem Saldo stünde es zuhinterst und fiele als erstes der
    Zusammenfassung zum Opfer — die auffälligste Linie wäre die, die verschwindet.
    """
    with SessionLocal() as db:
        konto = _konto(db, "Geleert", "20000")
        # Im laufenden Monat vollständig abgeräumt: alle früheren Monate zeigen
        # 20'000, der aktuelle 0.
        db.add(Transaction(account_id=konto.id, date=HEUTE,
                           amount=Decimal("-20000"), description="ZKV Umbuchung"))
        db.commit()

        reihen = konto_verlaeufe(db, HEUTE, 12, hoechstens=99)

    namen = [r["name"] for r in reihen]
    geleert = namen.index(MARKE + "Geleert")
    klein = namen.index(MARKE + "Klein")
    assert geleert < klein, (
        f"Nach Betrag im Zeitraum gehört das geleerte Konto vor das kleine "
        f"(Reihenfolge: {namen})"
    )


# ---------------------------------------------------------------------------
# Geometrie: eine Skala für alle Reihen — und die Achse auf derselben
# ---------------------------------------------------------------------------

WERTE = [Decimal(v) for v in (1000, 1100, 1200, 1150, 1000, 1050,
                              1100, 1200, 1150, 1000, 1100, 1200)]
NW = [{"month": date(2026, m, 1), "value": v}
      for m, v in zip(range(1, 13), WERTE, strict=True)]
KLEIN = {"name": "Kleines Konto", "titel": "Kleines Konto", "rest": False,
         "werte": [Decimal("250")] * 12}


def test_alle_reihen_auf_einer_skala() -> None:
    """Die Skala spannt über Gesamtlinie UND Konto-Linien.

    Ohne das läge das Konto mit 250 auf derselben Höhe wie die Gesamtlinie mit
    1000 — beide wären auf ihr eigenes Minimum skaliert.
    """
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=120, pad=8)

    assert layout["hi"] == 1200.0
    konto_ys = {y for _, y in _pts(layout["linien"][0]["d"])}
    assert len(konto_ys) == 1, "Konstante Reihe darf nicht wandern"
    assert max(y for _, y in layout["pts"]) < konto_ys.pop(), (
        "Die Gesamtlinie muss über der kleineren Konto-Linie liegen"
    )


def test_mehrere_reihen_beginnen_bei_null() -> None:
    """Sonst lügt der Abstand zwischen zwei Linien über das Verhältnis der Beträge.

    Keine Reihe berührt hier die Null (kleinster Wert 250) — die Achse muss
    trotzdem dort anfangen, sobald mehr als eine Reihe im Bild ist.
    """
    mehrere = _verlauf_layout(WERTE, [KLEIN], w=620, h=120, pad=8)
    assert mehrere["lo"] == 0.0

    # Gegenprobe: die einzelne Linie behält ihren engen Ausschnitt, sonst wäre
    # der bestehende Verlauf ohne Konten still flachgedrückt worden.
    allein = _verlauf_layout(WERTE, [], w=620, h=120, pad=8)
    assert allein["lo"] == 1000.0


def test_achse_und_kurve_rechnen_mit_derselben_skala() -> None:
    """Die Hilfslinie muss auf den Betrag zeigen, an dem die Kurve auch liegt."""
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=120, pad=8)
    achse = _nw_axis(layout["lo"], layout["hi"], NW, h=120, pad=8)

    y_1k = next(t["y"] for t in achse["y"] if t["label"] == "1k")
    kurve_bei_1000 = layout["pts"][WERTE.index(Decimal("1000"))][1]
    assert kurve_bei_1000 == pytest.approx(y_1k, abs=0.05)


def test_achse_aus_der_alten_skala_saesse_daneben() -> None:
    """Gegenprobe: ohne die gemeinsame Skala wäre der Test darüber nicht wahr.

    Rechnete die Achse weiter nur mit den Gesamtwerten (1000–1200), landete die
    1k-Hilfslinie am unteren Rand, während die Kurve dort in der oberen Hälfte
    verläuft.
    """
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=120, pad=8)
    alt = _nw_axis(1000.0, 1200.0, NW, h=120, pad=8)

    y_1k_alt = next(t["y"] for t in alt["y"] if t["label"] == "1k")
    kurve_bei_1000 = layout["pts"][WERTE.index(Decimal("1000"))][1]
    assert abs(kurve_bei_1000 - y_1k_alt) > 5, (
        "Alte und neue Skala liefern fast dieselbe Position — der Test darüber "
        "belegt dann nichts"
    )


def test_ohne_konto_linien_bleibt_das_bild_wie_bisher() -> None:
    """Gibt es nichts einzuzeichnen, skaliert die Gesamtlinie wieder auf sich selbst."""
    layout = _verlauf_layout(WERTE, [], w=620, h=120, pad=8)
    assert (layout["lo"], layout["hi"]) == (1000.0, 1200.0)
    assert layout["linien"] == []


def test_leere_reihe_ergibt_kein_diagramm() -> None:
    """Kein Datenpunkt, kein Pfad — und kein Absturz beim Rendern."""
    layout = _verlauf_layout([], [])
    assert layout["linie"] == ""
    assert layout["linien"] == []


def _pts(d: str) -> list[tuple[float, float]]:
    """Stützstellen eines Pfads: der Startpunkt und das Ende jedes C-Segments."""
    kopf = d.split("C")[0].removeprefix("M ").strip()
    punkte = [tuple(float(z) for z in kopf.split(","))]
    for seg in d.split("C")[1:]:
        punkte.append(tuple(float(z) for z in seg.strip().split()[-1].split(",")))
    return punkte  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Die Seite selbst
# ---------------------------------------------------------------------------


def test_konten_seite_zeichnet_die_einzellinien(
    logged_in_client: TestClient, konten: None
) -> None:
    """Ohne diesen Test wäre nur belegt, dass die Geometrie stimmt — nicht, dass
    sie im Markup ankommt."""
    html = logged_in_client.get("/accounts").text

    assert html.count('class="nw-acc-line"') >= 2, "Konto-Linien fehlen im SVG"
    assert MARKE + "Gross" in html
    assert "Gesamtvermögen" in html, "Die Legende benennt die dominante Linie nicht"
    # Die Gesamtlinie wird NACH den Konto-Linien gezeichnet und liegt damit obenauf.
    assert html.index('class="nw-acc-line"') < html.index('class="nw-line"')


# ---------------------------------------------------------------------------
# Flächenfüllung: nur solange sie noch etwas sagt
# ---------------------------------------------------------------------------


def test_es_gibt_keine_flaechenfuellung_mehr() -> None:
    """Sie stammte aus der Zeit mit EINER Kurve.

    Mit Konto-Linien darüber wiederholt sie nur, was die bei 0 beginnende Achse
    genauer sagt — und legt sich unter jede Linie, deren 3:1-Kontrast gegen den
    Kartengrund gemessen ist. Genau daraus entstanden die dunklen Keile, die der
    Nutzer als „unleserlich" beschrieb.
    """
    for reihen in ([], [KLEIN]):
        layout = _verlauf_layout(WERTE, reihen, w=620, h=160, pad=8)
        assert "flaeche" not in layout, "Die Fläche ist ersatzlos entfallen"


def test_der_endpunkt_kommt_auch_in_prozent() -> None:
    """Die Punktmarkierung sitzt als HTML über dem Chart.

    Als <circle> im viewBox wäre sie eine Ellipse: das SVG wird mit
    preserveAspectRatio=none auf die Kartenbreite gestreckt.
    """
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=160, pad=8)
    assert layout["last_px"] == pytest.approx(layout["last_x"] / 620 * 100, abs=0.01)
    assert layout["last_py"] == pytest.approx(layout["last_y"] / 160 * 100, abs=0.01)


def test_jede_konto_linie_bringt_ihre_stuetzstellen_mit() -> None:
    """Der Werte-Kasten setzt auf jede Linie einen Punkt — dafür braucht er die y."""
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=160, pad=8)
    assert len(layout["linien"][0]["pts"]) == len(WERTE)


# ---------------------------------------------------------------------------
# Achsen-Beschriftung: über dem Punkt, nicht irgendwo
# ---------------------------------------------------------------------------


def test_monatsnamen_stehen_ueber_ihrem_punkt() -> None:
    """Vorher lagen sie in einem space-between-Streifen: „Okt" stand bei x=12 px,
    sein Kurvenpunkt bei x=118 px (nachgemessen bei 1159 px Chartbreite)."""
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=160, pad=8)
    xs = [x for x, _ in layout["pts"]]
    achse = _nw_axis(layout["lo"], layout["hi"], NW, h=160, pad=8, xs=xs, w=620)

    assert achse["x"], "Ohne Monatsnamen ist der Verlauf nicht datierbar"
    # Beschriftet wird jeder zweite Monat, der letzte ist immer dabei.
    assert achse["x"][-1]["pct"] == pytest.approx(xs[-1] / 620 * 100, abs=0.01)
    assert achse["x"][0]["pct"] == pytest.approx(xs[1] / 620 * 100, abs=0.01)


def test_randbeschriftung_wird_nicht_aus_der_flaeche_geschoben() -> None:
    """Ein mittig verankertes Label am rechten Rand ragte zur Hälfte hinaus."""
    layout = _verlauf_layout(WERTE, [KLEIN], w=620, h=160, pad=8)
    achse = _nw_axis(layout["lo"], layout["hi"], NW, h=160, pad=8,
                     xs=[x for x, _ in layout["pts"]], w=620)
    assert achse["x"][-1]["shift"] == "-100%"
    assert all(t["shift"] == "-50%" for t in achse["x"][:-1])


# ---------------------------------------------------------------------------
# Was im Markup ankommen muss
# ---------------------------------------------------------------------------


def test_die_seite_liefert_alle_werte_fuer_die_fuehrungslinie(
    logged_in_client: TestClient, konten: None
) -> None:
    """Ohne data-monate kann das Skript weder Monat noch Betrag anzeigen."""
    html = logged_in_client.get("/accounts").text
    roh = re.search(r"data-monate='([^']*)'", html)
    assert roh, "Die Zeichenfläche trägt keine Monatsdaten"

    monate = json.loads(html_unescape(roh.group(1)))
    assert len(monate) == 12
    for m in monate:
        assert m["monat"], "Ein Monat ohne Beschriftung ist im Kasten nicht ablesbar"
        assert m["gesamt"]
        # Je Konto-Linie ein Betrag UND eine y-Position für den Punkt darauf.
        assert len(m["werte"]) == len(m["wy"])


def test_jeder_legendeneintrag_traegt_ein_farbmuster(
    logged_in_client: TestClient, konten: None
) -> None:
    """Der Nutzer sah „eine nackte Wortkette ohne Farbmuster"."""
    html = logged_in_client.get("/accounts").text
    legende = html.split('class="nw-legend"')[1].split("</div>")[0]
    eintraege = legende.count("nw-legend-item")
    muster = legende.count("legend-swatch")
    assert eintraege >= 2
    assert muster == eintraege, (
        f"{eintraege} Legendeneinträge, aber {muster} Farbmuster"
    )


def test_kein_hover_ohne_kasten_und_fuehrungslinie(
    logged_in_client: TestClient, konten: None
) -> None:
    """Die Teile, die app.js bewegt, müssen im Markup stehen."""
    html = logged_in_client.get("/accounts").text
    for teil in ('class="nw-guide"', 'class="nw-tip"', "nw-tip-monat",
                 "nw-hdot-total", 'class="nw-line-kontur"'):
        assert teil in html, f"{teil} fehlt — das Hover bliebe wirkungslos"
