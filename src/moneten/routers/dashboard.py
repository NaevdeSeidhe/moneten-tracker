"""Dashboard / Übersicht."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import Integer, case, func, select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import add_months, heute_lokal, local_now
from moneten.db.models import Account, AccountType, Category, Transaction, User, not_transfer
from moneten.db.session import get_db
from moneten.palette import color_at
from moneten.services.charts import sparkline
from moneten.services.payment_mix import kassensturz_faellig, payment_mix
from moneten.services.sankey import build_flow
from moneten.services.splits import effective_category_amounts
from moneten.services.treemap import build_treemap
from moneten.services.upcoming import was_kommt
from moneten.templating import MONATE, chf, templates

router = APIRouter(tags=["dashboard"])

# Donut-Geometrie (SVG): Radius und Umfang.
DONUT_R = 90
DONUT_C = 2 * math.pi * DONUT_R
# Liquide (Ausgabe-)Konten vs. Spar-/Anlagekonten.
_LIQUID_TYPES = {AccountType.BANK, AccountType.CASH}
_SAVINGS_TYPES = {AccountType.SAVINGS, AccountType.INVESTMENT, AccountType.CRYPTO, AccountType.STOCKS}


def _account_chart(accounts: list[Account], types: set, theme: str | None = None) -> dict:
    """Baut ein Donut-Chart für eine Konto-Gruppe.

    * Ring: nur aktive Konten mit Saldo > 0 (0-Konten ergeben kein Segment).
    * Legende: ALLE Konten der Gruppe — auch inaktive / mit Saldo 0 (z.B.
      Crypto, Aktien), damit sie sichtbar bleiben.
    * Abgerundete Segmente mit kleinen Lücken, flache Volltöne je Segment
      (Farben aus der zentralen Palette — wie Treemap/Sankey/Budget).
    """
    group = [a for a in accounts if a.type in types]
    colored = [(a, color_at(i, theme)) for i, a in enumerate(group)]

    ring = [(a, c) for a, c in colored if a.is_active and (a.current_balance or 0) > 0]
    # Zwei verschiedene Summen, bewusst getrennt:
    # `ring_total` normiert die Segmente — dafür zählen nur die positiven Salden,
    # ein negatives Segment liesse sich nicht zeichnen.
    # `total` ist die Kopfzahl der Karte. Sie darf dieser Zeichen-Einschränkung
    # NICHT folgen: sonst verschwindet ein überzogenes Konto aus dem Vermögen,
    # statt es zu mindern, und das Dashboard weist mehr aus als die Konten-Seite.
    ring_total = sum((a.current_balance for a, _ in ring), Decimal("0"))
    total = sum(
        (a.current_balance or Decimal("0") for a, _ in colored if a.is_active),
        Decimal("0"),
    )

    segments: list[dict] = []
    if ring_total > 0:
        n = len(ring)
        gap = DONUT_C * 0.04 if n > 1 else 0.0  # Lücke zwischen Segmenten (für runde Kappen)
        avail = DONUT_C - n * gap
        cursor = gap / 2
        for idx, (acc, color) in enumerate(ring):
            frac = float(acc.current_balance / ring_total)
            seg_len = frac * avail
            segments.append({
                "idx": idx,
                "name": acc.name,
                "balance": acc.current_balance,
                "pct": round(frac * 100, 1),
                "color": color,
                "dash": round(seg_len, 2),
                "gap": round(DONUT_C - seg_len, 2),
                "offset": round(-cursor, 2),
            })
            cursor += seg_len + gap

    legend = [{
        "name": a.name,
        "balance": a.current_balance or Decimal("0"),
        "color": c,
        "active": a.is_active and (a.current_balance or 0) > 0,
    } for a, c in colored]

    return {"segments": segments, "legend": legend, "total": total}


def _monatslabel(tag: date) -> str:
    """Monat und Jahr ausgeschrieben („Juni 2026")."""
    return f"{MONATE[tag.month - 1]} {tag.year}"


#: Wie viele Monate rückwärts der Geldfluss höchstens nach Daten sucht. Findet
#: er in einem Jahr nichts, ist der Bestand leer und nicht bloss der Import alt.
_FLOW_RUECKBLICK = 12


def _monate_mit_buchungen(db: Session, today: date) -> list[date]:
    """Monatsanfänge mit Buchungen, jüngster zuerst — höchstens bis heute.

    Warum nicht schlicht der laufende Monat: Bankdaten kommen per Import, nicht
    über eine Live-Schnittstelle. Zwischen zwei Importen — beim Nutzer rund ein
    Monat — hat der laufende Monat keine Buchungen, und der Geldfluss war
    dadurch praktisch nie zu sehen. Dieselbe Ausweichung macht der
    Steuerjahr-Auszug (:func:`moneten.routers.tax.tax_page`) auf Jahresebene.

    Buchungen mit Datum in der ZUKUNFT zählen nicht — auch nicht innerhalb des
    laufenden Monats. Ein Dauerauftrag, der auf den 16. eingetragen ist, zog die
    Karte am 6. sonst auf den laufenden Monat und zeigte dort 1780 Franken
    Ausgang für Geld, das noch niemand ausgegeben hat; der Ausweichmonat mit den
    echten Zahlen blieb verborgen, und zwar ohne Hinweis.

    Transfers bleiben aussen vor — genau wie im Diagramm selbst. Ein Monat, in
    dem nur zwischen eigenen Konten umgebucht wurde, ergäbe sonst einen
    Zeitraum, zu dem das Diagramm nichts zu zeigen hat.

    Eine LISTE statt nur des jüngsten Monats, weil „Monat hat Buchungen" und
    „Diagramm hat etwas zu zeigen" nicht dasselbe sind: ein Monat, in dem nur ein
    Kauf und dessen Gutschrift stehen, hebt sich auf. Der Aufrufer geht deshalb
    weiter, bis wirklich etwas gezeichnet werden kann.
    """
    frueheste = add_months(today.replace(day=1), -_FLOW_RUECKBLICK)
    monate: list[date] = []
    obergrenze = today
    # Je Runde eine indizierte MAX-Abfrage statt eines Datums-Ausdrucks in SQL:
    # ``strftime`` gäbe es nur unter SQLite, und im Regelfall ist nach der ersten
    # Runde Schluss.
    while len(monate) <= _FLOW_RUECKBLICK:
        juengste = db.scalar(
            select(func.max(Transaction.date)).where(
                Transaction.date <= obergrenze,
                Transaction.date >= frueheste,
                not_transfer(),
            )
        )
        if juengste is None:
            break
        monat = juengste.replace(day=1)
        monate.append(monat)
        obergrenze = monat - timedelta(days=1)
    return monate


def _flow_items(rows: list, cats: dict[int, Category]) -> tuple[list, list]:
    """Einnahmen/Ausgaben des gezeigten Monats je TOP-Kategorie (für den Sankey).

    ``rows`` = Ergebnis von :func:`effective_category_amounts` (einmal im
    Dashboard berechnet, gemeinsam mit :func:`_top_expenses` genutzt),
    ``cats`` = alle Kategorien als {id: Kategorie}. Unkategorisiertes landet in
    „Übrige Einnahmen/Ausgaben". Transfers und Split-Children ausgeschlossen.
    """
    def top_name(cat_id: int | None) -> str | None:
        c = cats.get(cat_id)
        if c is None:
            return None
        seen: set[int] = set()
        while c.parent_id is not None and c.parent_id in cats and c.id not in seen:
            seen.add(c.id)
            c = cats[c.parent_id]
        return c.name

    inc: dict[str, Decimal] = {}
    exp: dict[str, Decimal] = {}
    # Aufgeteilte Buchungen je Split der passenden Top-Kategorie zurechnen; je
    # Top-Kategorie NETTO bilden → Gutschriften werden gegengerechnet (eine
    # Ausgaben-Kategorie mit Rückerstattung zeigt nur den Netto-Aufwand).
    net: dict[str, Decimal] = {}
    # Unkategorisiertes wird NICHT gegeneinander verrechnet. Innerhalb einer
    # Kategorie ist die Verrechnung richtig, im Resttopf liegen aber Lohn und
    # Miete nebeneinander: aus Eingang und Ausgang wurde dort EINE Zeile über
    # die Differenz — eine Zahl, die im Bestand nirgends steht, und „Übrige
    # Einnahmen" und „Übrige Ausgaben" konnten nie zugleich auftreten. Frisch
    # importierte Buchungen sind alle unkategorisiert; genau dann trifft es.
    ohne_kat = {"ein": Decimal("0"), "aus": Decimal("0")}
    for cat_id, amount, _ in rows:
        name = top_name(cat_id)
        if name is None:
            ohne_kat["ein" if amount > 0 else "aus"] += amount
        else:
            net[name] = net.get(name, Decimal("0")) + amount
    for name, v in net.items():
        if v > 0:
            inc[name] = v
        elif v < 0:
            exp[name] = -v
    if ohne_kat["ein"] > 0:
        inc["Übrige Einnahmen"] = ohne_kat["ein"]
    if ohne_kat["aus"] < 0:
        exp["Übrige Ausgaben"] = -ohne_kat["aus"]

    income_items = sorted(inc.items(), key=lambda kv: kv[1], reverse=True)
    expense_items = sorted(exp.items(), key=lambda kv: kv[1], reverse=True)
    return income_items, expense_items


def _top_expenses(rows: list, cats: dict[int, Category], limit: int = 5) -> list[dict]:
    """Grösste Ausgaben-Kategorien des GEZEIGTEN Monats (echte Werte).

    Welcher das ist, entscheidet der Aufrufer — es ist derselbe wie beim
    Geldfluss und nicht zwingend der laufende.
    Ersetzt den früheren toten „Konsum"-Platzhalter. ``rows``/``cats`` wie bei
    :func:`_flow_items` (dieselbe, einmal berechnete Kategorie-Auswertung).
    Aufgeteilte Buchungen zählen je Split. Liefert Name, Icon, Betrag (positiv)
    und Anteil am Maximum (für den Balken)."""
    # Netto je Kategorie (Gutschriften gegengerechnet) → nur echte Ausgaben (netto < 0).
    net: dict[int, Decimal] = {}
    for cid, amt, _ in rows:
        if cid is None:
            continue
        net[cid] = net.get(cid, Decimal("0")) + amt
    by_cat = {cid: -v for cid, v in net.items() if v < 0}
    items = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    mx = max((v for _, v in items), default=Decimal("0"))
    out: list[dict] = []
    for cid, v in items:
        c = cats.get(cid)
        out.append({
            "name": c.name if c else "Ohne Kategorie",
            "icon": (c.icon if c and c.icon else "tag"),
            "amount": v,
            "pct": round(float(v / mx * 100), 0) if mx > 0 else 0,
        })
    return out


def _greeting(hour: int) -> str:
    """Liefert eine tageszeitabhängige Begrüssung für den Hero-Streifen."""
    if hour < 5:
        return "Gute Nacht"
    if hour < 11:
        return "Guten Morgen"
    if hour < 18:
        return "Guten Tag"
    return "Guten Abend"


def _month_totals(db: Session, today: date) -> tuple[Decimal, Decimal, Decimal]:
    """Summe Einnahmen, Ausgaben (positiv) und Saldo des laufenden Monats.

    Nur Top-Level-Buchungen (keine Split-Children). Transfers (Umbuchungen
    zwischen eigenen Konten) sind ausgeschlossen — sie sind weder Einnahme
    noch Ausgabe. Einnahmen = amount > 0, Ausgaben = amount < 0 (positiv zurück).

    **Monat-bis-heute — mit oberer Grenze.** Hier stand nur ``date >= month_start``,
    ohne Ende. Jede Buchung in der Zukunft zählte damit in den laufenden Monat:
    ein vorerfasster Dauerauftrag des Folgemonats, und schlimmer, ein
    Jahres-Tippfehler (2027 statt 2026) erschien als Einnahme dieses Monats.
    Nachgemessen an vier Buchungen: die Leitzahl meldete einen Überschuss, wo in
    Wirklichkeit ein Minus stand — die Buchung des Folgejahres zählte mit.
    (Die gemessenen Beträge standen hier einmal ausgeschrieben. Sie belegen
    nichts, was der Satz nicht auch so sagt, und Beträge aus einer echten Anlage
    gehören nicht ins Repo.)

    Die Grenze ist ``<= today`` und nicht das Monatsende. Damit bedeutet die
    Leitzahl genau dasselbe wie der letzte Punkt der Kurve darunter
    (:func:`_monthly_series` rechnet Tag 1 bis ``today.day``) und wie der
    Vergleichspfeil daneben. Vorher widersprachen sich die drei Angaben auf
    derselben Karte.
    """
    month_start = today.replace(day=1)
    income = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.date >= month_start,
            Transaction.date <= today,
            Transaction.amount > 0,
            not_transfer(),
        )
    )
    expense = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.date >= month_start,
            Transaction.date <= today,
            Transaction.amount < 0,
            not_transfer(),
        )
    )
    income = Decimal(str(income or 0))
    expense = Decimal(str(expense or 0))  # negativ oder 0
    return income, -expense, income + expense  # (Eingang, Ausgang positiv, Saldo)


def _monthly_series(db: Session, today: date, n: int = 6) -> list[dict]:
    """Eingang / Ausgang (positiv) / Saldo je Monat für die letzten ``n`` Monate —
    jeweils **Monat-bis-heute** (Tag 1 bis zum selben Tag wie heute).

    So ist der laufende (Teil-)Monat fair mit den Vormonaten vergleichbar: Vergleichs-
    pfeil und Sparkline „tauchen" am Ende nicht künstlich ab, nur weil der Lohn z. B.
    erst Ende Monat kommt. Älteste zuerst. Transfers und Split-Children sind
    ausgeschlossen — analog zu :func:`_month_totals`. Läuft als EINE aggregierte
    Query (group by Jahr-Monat) statt zwei Queries pro Monat.
    """
    oldest = add_months(today, -(n - 1))  # Monatsanfang des ältesten Monats
    nxt = add_months(today, 1)            # exklusives Ende: Anfang des Folgemonats
    month_key = func.strftime("%Y-%m", Transaction.date)
    rows = db.execute(
        select(
            month_key,
            func.coalesce(func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.amount < 0, Transaction.amount), else_=0)), 0),
        ).where(
            Transaction.date >= oldest,
            Transaction.date < nxt,
            # Monat-bis-heute: nur Tage 1..(today.day) zählen. Kürzere Vergleichs-
            # monate (heute der 31., Monat = Februar) sind damit automatisch
            # komplett drin — jeder ihrer Tage liegt ≤ today.day.
            func.strftime("%d", Transaction.date).cast(Integer) <= today.day,
            not_transfer(),
        ).group_by(month_key)
    ).all()
    by_month = {mk: (inc, exp) for mk, inc, exp in rows}

    series: list[dict] = []
    for back in range(n - 1, -1, -1):
        start = add_months(today, -back)  # Monatsanfang `back` Monate zurück
        inc_raw, exp_raw = by_month.get(f"{start.year:04d}-{start.month:02d}", (0, 0))
        inc = Decimal(str(inc_raw or 0))
        exp = Decimal(str(exp_raw or 0))
        series.append({"month": start, "income": inc, "expense": -exp, "saldo": inc + exp})
    return series


def _pct_change(curr: Decimal, prev: Decimal) -> float | None:
    """Prozentuale Veränderung von ``prev`` zu ``curr``; None wenn nicht sinnvoll."""
    if prev is None or prev == 0:
        return None
    return round(float((curr - prev) / abs(prev) * 100), 1)


# Sparkline-Geometrie liegt zentral in services/charts.py (Dashboard + Konten).
# Alias mit führendem Unterstrich, damit bestehende Tests den Namen finden.
_sparkline = sparkline


def _prev_value(werte: list) -> Decimal | None:
    """Vorletzter Wert einer Reihe (= Vormonat) — für den Vergleichspfeil. None
    wenn es noch keinen Vormonat gibt."""
    return werte[-2] if len(werte) >= 2 else None


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Übersichts-Seite mit Konten und Monatszahlen.

    Das Datum wird zentral via ``heute_label()`` (templating.py) im Header
    angezeigt — hier nur die tageszeitabhängige Begrüssung.
    """
    today = heute_lokal()
    accounts = list(db.scalars(select(Account).order_by(Account.sort_order)).all())
    theme = user.preferred_theme  # Diagramm-Palette ans aktive Theme koppeln
    income, expense, saldo = _month_totals(db, today)
    chart_liquid = _account_chart(accounts, _LIQUID_TYPES, theme)
    chart_savings = _account_chart(accounts, _SAVINGS_TYPES, theme)
    total_assets = chart_liquid["total"] + chart_savings["total"]

    # 6-Monats-Reihe für Sparklines + Vormonatsvergleich.
    series = _monthly_series(db, today, 6)
    inc_vals = [s["income"] for s in series]
    exp_vals = [s["expense"] for s in series]
    sal_vals = [s["saldo"] for s in series]
    has_history = any(s["income"] or s["expense"] for s in series[:-1])

    # Die Beschriftungen hiessen einmal „Eingang Monat" / „Ausgang Monat" /
    # „Saldo Monat". Bei 390px stehen sie in drei Spalten nebeneinander, und die
    # mittlere brach um — das Wort „Monat" stand dreimal in einer Zeile, obwohl
    # direkt darunter „laufender Monat" steht. Weglassen kostet nichts und
    # nimmt den Umbruch mit.
    saldo_color = "var(--accent-tertiary)" if saldo >= 0 else "var(--danger)"
    metrics = [
        {"label": "Eingang", "value": income,
         "value_color": "var(--accent-tertiary)", "spark_color": "var(--accent-tertiary)",
         "spark": _sparkline(inc_vals), "pct": _pct_change(income, _prev_value(inc_vals)), "good_up": True},
        {"label": "Ausgang", "value": expense,
         "value_color": "var(--text-primary)", "spark_color": "var(--dusty-rose)",
         "spark": _sparkline(exp_vals), "pct": _pct_change(expense, _prev_value(exp_vals)), "good_up": False},
        {"label": "Saldo", "value": saldo,
         "value_color": saldo_color, "spark_color": saldo_color,
         "spark": _sparkline(sal_vals), "pct": _pct_change(saldo, _prev_value(sal_vals)), "good_up": True},
    ]
    # Hover-Datenpunkte je Sparkline: (x, y) aus der Geometrie + Monatslabel +
    # formatierter Betrag — fürs interaktive Tooltip beim Drüberfahren.
    month_dates = [s["month"] for s in series]

    def _spark_points(spark: dict, vals: list) -> list[dict]:
        return [
            {"x": px, "y": py, "label": _monatslabel(m), "value": chf(v)}
            for (px, py), m, v in zip(spark["pts"], month_dates, vals, strict=False)
        ]

    for met, vals in zip(metrics, (inc_vals, exp_vals, sal_vals), strict=True):
        met["points"] = _spark_points(met["spark"], vals)

    # S4: Leer-/Nullmonat freundlich statt Alarm. Eine Kennzahl, die diesen Monat
    # (noch) 0 ist, hat keinen sinnvollen „−100%"-Vergleich → keinen Trend zeigen
    # (sonst schreit ein frischer Monat „▼ 100.0%" in Rot).
    for met in metrics:
        if met["value"] == 0:
            met["pct"] = None

    liquide = chart_liquid["total"]
    liquide_pct = round(float(liquide / total_assets * 100), 0) if total_assets > 0 else None

    # Kategorie-Auswertung des laufenden Monats EINMAL berechnen — sie ist der
    # erste Kandidat für Geldfluss UND Treemap; beide zeigen denselben Monat.
    cats = {c.id: c for c in db.scalars(select(Category))}
    monat_start = today.replace(day=1)
    cat_rows = effective_category_amounts(
        db, date_from=monat_start, date_to=add_months(monat_start, 1)
    )

    # Geldfluss: laufender Monat, sonst der jüngste, aus dem sich wirklich ein
    # Diagramm bauen lässt. Weitergesucht wird erst, wenn ``build_flow`` nichts
    # hergibt — sonst stünde ein fremder Monat im Kopf über einer leeren Karte.
    # Im Normalfall ist das eine Runde mit denselben Zeilen wie die Treemap.
    flow_monat: date | None = None
    flow = None
    # Die Zeilen des Monats, den die Seite am Ende zeigt. Vorbelegt mit dem
    # laufenden Monat für den Fall, dass kein Kandidat trägt — und dieser Fall
    # ist kein theoretischer: ``_flow_items`` bildet netto je TOP-Kategorie,
    # ``_top_expenses`` je Blatt. Ein Kauf und seine Gutschrift in zwei
    # UNTERkategorien derselben Gruppe heben sich für den Geldfluss auf (kein
    # Diagramm, kein Ausweichmonat), lassen der Treemap aber sehr wohl eine
    # Kachel. Kippt diese Zeile, bleibt genau dann die Karte leer.
    gezeigte_rows = cat_rows
    flow_kandidaten = _monate_mit_buchungen(db, today)
    for kandidat in flow_kandidaten:
        rows = cat_rows if kandidat == monat_start else effective_category_amounts(
            db, date_from=kandidat, date_to=add_months(kandidat, 1)
        )
        flow = build_flow(*_flow_items(rows, cats), theme=theme)
        if flow is not None:
            flow_monat = kandidat
            gezeigte_rows = rows
            break
    # Zwei verschiedene Gründe für eine leere Karte, zwei verschiedene Sätze:
    # „nichts erfasst" wäre gelogen, wenn Buchungen da sind und sich nur
    # gegenseitig aufheben (ein Kauf und seine Gutschrift).
    flow_grund = (
        "Noch keine Buchungen erfasst."
        if not flow_kandidaten
        else f"In den letzten {_FLOW_RUECKBLICK} Monaten heben sich alle "
             "Buchungen gegenseitig auf."
    )

    # „Grösste Ausgaben" als Treemap (Fläche = Betrag) statt nüchterner Textliste.
    # Sie zeigt DENSELBEN Monat wie der Geldfluss — dieselben Zeilen, kein
    # zweiter Suchlauf. Vorher rechnete sie immer den laufenden Monat und meldete
    # „noch keine Ausgaben erfasst", während die Geldfluss-Karte auf derselben
    # Seite den Juni nannte: zwei Zeiträume, keiner davon benannt. Zwei VERSCHIEDENE
    # Ausweichmonate wären noch schlimmer, darum kommt der Monat aus dem
    # Geldfluss und wird nicht neu gesucht.
    gezeigter_monat = flow_monat or monat_start
    # Sieben ist gesetzt und nicht abgeleitet. Nachgemessen entscheidet die
    # VERTEILUNG darüber, ob die kleinste Kachel ihre Beschriftung noch trägt,
    # nicht ihre Anzahl: bei sieben stark fallenden Beträgen fällt sie weg, bei
    # neun gleich grossen nicht. Eine „richtige" Zahl gibt die Geometrie also
    # nicht her — umso wichtiger, dass ein Test die gewählte festhält, sonst
    # verrutscht sie still.
    top_exp = _top_expenses(gezeigte_rows, cats, limit=7)
    treemap = build_treemap([(e["name"], e["amount"], e["icon"]) for e in top_exp], theme=theme)

    # Anzahl Buchungen ohne Kategorie — als Hinweis + Direktlink zu den Regeln,
    # damit der nächste Schritt (Kategorisieren) nicht untergeht.
    uncategorized = db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.category_id.is_(None),
            Transaction.is_split.is_(False),  # aufgeteilte Buchungen gelten als kategorisiert
            not_transfer(),
        )
    ) or 0
    # Nenner für den Fortschritt („X von Y erledigt"): alle kategorisierbaren Buchungen.
    cat_total = db.scalar(
        select(func.count(Transaction.id)).where(Transaction.is_split.is_(False), not_transfer())
    ) or 0

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "accounts": accounts,
            "active_tab": "dashboard",
            "uncategorized": uncategorized,
            "cat_total": cat_total,
            "cat_done": cat_total - uncategorized,
            "greeting": _greeting(local_now().hour),
            "metrics": metrics,
            "has_history": has_history,
            "liquide": liquide,
            "liquide_pct": liquide_pct,
            "chart_liquid": chart_liquid,
            "chart_savings": chart_savings,
            "total_assets": total_assets,
            # Bar gegen digital: Anteil der Alltagsausgaben, 12 Monate.
            "mix": payment_mix(db, today, ziel_pct=(user.cash_goal_pct or 0)),
            # Fristen + erkannte Jahresposten in einer chronologischen Liste:
            # beide beantworten „was kommt auf mich zu", zwei Karten waeren
            # dieselbe Frage zweimal.
            "kommt": was_kommt(db, today),
            "kassensturz": kassensturz_faellig(db, today),
            "flow": flow,
            # Der gezeigte Zeitraum MUSS am Diagramm stehen: eine Karte, die
            # stillschweigend den Juni zeigt, während die Seite sonst August
            # meint, wäre schlimmer als gar keine.
            "flow_zeitraum": _monatslabel(flow_monat) if flow_monat else None,
            "flow_veraltet": flow_monat is not None and flow_monat != monat_start,
            "flow_laufend": _monatslabel(monat_start),
            "flow_grund": flow_grund,
            "treemap": treemap,
            # Steht im Kopf der Treemap-Karte, auch im Normalfall: ein Zeitraum,
            # der nur bei Abweichung erscheint, lässt den Leser im Regelfall
            # raten, welcher Monat gemeint ist.
            # ABER nicht bedingungslos. Ohne eine einzige Buchung nennt der
            # Geldfluss darüber keinen Monat (es gibt keinen), und die Karte
            # darunter schrieb trotzdem den laufenden hin — ein Zeitraum ohne
            # jede Zahl, direkt unter einer Karte, die aus Prinzip keinen nennt.
            # Genau der Widerspruch, den die Kopplung der beiden Karten beheben
            # sollte. Genannt wird der Monat deshalb, solange ihn etwas trägt:
            # der Ausweichmonat des Geldflusses oder eine eigene Kachel.
            "ausgaben_zeitraum": (
                _monatslabel(gezeigter_monat) if (flow_monat or treemap) else None
            ),
            "donut_c": round(DONUT_C, 2),
            "donut_r": DONUT_R,
        },
    )
