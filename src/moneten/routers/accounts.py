"""Konten-Verwaltung (Phase 1).

CRUD für Konten: anlegen, bearbeiten, Startsaldo setzen, archivieren/reaktivieren,
löschen. Bewusst HTMX-zentriert mit *einem* Container ``#accounts-root``, der
bei jeder Mutation komplett neu gerendert wird — das hält den Client-Code
trivial (kein Out-of-Band-Swapping) und ist bei wenigen Konten problemlos.

Saldo-Logik: Das Eingabefeld setzt ``opening_balance`` (Startsaldo/Anfangsbestand).
``current_balance`` wird via ``recalc_account_balance`` immer aus opening + Summe
der Buchungen berechnet — siehe services/balances.py.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import heute_lokal
from moneten.db.models import Account, AccountType, Transaction, User
from moneten.db.session import get_db
from moneten.money import parse_amount
from moneten.palette import color_at
from moneten.services.account_charts import (
    GROUP_DEFS,
    konto_farbe,
    konto_verlaeufe,
    last_activity,
    net_worth_series,
)
from moneten.services.balances import recalc_account_balance
from moneten.services.charts import curve_path
from moneten.services.payment_mix import KASSENSTURZ_PREFIX
from moneten.templating import MONATE, chf_kurz, templates

router = APIRouter(tags=["accounts"])

# Zeichenraum des Vermögens-Verlaufs. Das SVG wird mit preserveAspectRatio=none
# auf die Kartenbreite gestreckt, die viewBox-Zahlen sind also nur ein
# Seitenverhältnis: 620 zu 160 ergibt bei 375 px Gerätebreite rund 2.3:1 — flach
# genug für zwölf Monate nebeneinander, hoch genug, dass sich sieben Linien noch
# trennen. Die Höhe steht hier UND im Template (viewBox) UND in der CSS; ändert
# sie sich, müssen alle drei mit, sonst zeigen die Achsenlabels daneben.
NW_W = 620.0
NW_H = 160.0
NW_PAD = 8.0


# Deutsche Anzeige-Labels für die Konto-Typen (Dropdown + Liste).
ACCOUNT_TYPE_LABELS: dict[AccountType, str] = {
    AccountType.BANK: "Bankkonto",
    AccountType.CASH: "Bargeld / Kassette",
    AccountType.SAVINGS: "Sparkonto",
    AccountType.INVESTMENT: "Anlagekonto",
    AccountType.CRYPTO: "Krypto",
    AccountType.STOCKS: "Aktien",
}

ACCOUNT_TYPE_CHOICES: list[tuple[str, str]] = [
    (t.value, ACCOUNT_TYPE_LABELS[t]) for t in AccountType
]


def _accounts_ordered(db: Session) -> list[Account]:
    return list(db.scalars(select(Account).order_by(Account.sort_order, Account.id)))


def _fmt_chf_short(v: float) -> str:
    """Kompaktes Achsen-Label: 25000 → „25k", 7500 → „7.5k", 800 → „800"."""
    if abs(v) >= 1000:
        s = f"{v / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return f"{v:.0f}"


def _nw_axis(
    lo: float,
    hi: float,
    nw: list[dict],
    *,
    h: float = 160,
    pad: float = 8,
    xs: list[float] | None = None,
    w: float = 620,
) -> dict:
    """Grobe Orientierungs-Achsen für den Vermögens-Verlauf.

    Y: 2–4 „schöne" CHF-Ticks (Hairlines im SVG; die Labels kommen als HTML-Overlay,
    weil der Chart mit ``preserveAspectRatio=none`` gestreckt wird — SVG-Text würde
    dabei verzerren). X: jeder zweite Monat, so gelegt, dass der aktuelle dabei ist.

    ``lo``/``hi`` kommen von aussen, weil die Skala seit den mitlaufenden
    Konto-Linien über ALLE Reihen spannt. Rechnete die Achse weiter nur mit den
    Gesamtwerten, läge sie auf einer anderen Skala als die Kurve — die
    Hilfslinie zeigte dann auf einen Betrag, an dem die Kurve gar nicht liegt.

    ``xs`` sind die x-Positionen der Kurvenpunkte im viewBox-Raum. Ohne sie
    hingen die Monatsnamen bisher in einem ``space-between``-Streifen: „Okt"
    stand am linken Rand, sein Punkt aber 106 px weiter rechts (nachgemessen bei
    1159 px Chartbreite). Jedes Label steht jetzt über SEINEM Punkt; ``shift``
    hält die Randlabels innerhalb der Fläche, statt sie halb hinauszuschieben.
    """
    if not nw:
        return {"y": [], "x": []}
    span = hi - lo
    y_ticks: list[dict] = []
    if span > 0:
        raw = span / 3
        mag = 10 ** math.floor(math.log10(raw))
        step = next((m * mag for m in (1, 2, 2.5, 5, 10) if span / (m * mag) <= 4), raw)
        v = math.ceil(lo / step) * step
        while v <= hi + 1e-9:
            ypx = (h - pad) - (v - lo) / span * (h - 2 * pad)
            y_ticks.append({"y": round(ypx, 1), "pct": round(ypx / h * 100, 2),
                            "label": _fmt_chf_short(v)})
            v += step
    n = len(nw)
    x_labels: list[dict] = []
    for i, p in enumerate(nw):
        if (n - 1 - i) % 2:
            continue
        pct = (xs[i] / w * 100) if (xs and i < len(xs)) else (i / (n - 1) * 100 if n > 1 else 50)
        shift = "0" if pct < 6 else ("-100%" if pct > 94 else "-50%")
        x_labels.append({"label": MONATE[p["month"].month - 1][:3],
                         "pct": round(pct, 3), "shift": shift})
    return {"y": y_ticks, "x": x_labels}


def _verlauf_layout(
    gesamt: list[Decimal],
    reihen: list[dict],
    *,
    w: float = 620,
    h: float = 160,
    pad: float = 8,
) -> dict:
    """Gesamtlinie und Konto-Linien auf EINER gemeinsamen Skala.

    Ersetzt für diesen Chart ``charts.sparkline``: die skaliert jede Reihe auf
    ihr eigenes Minimum/Maximum. Zwei Linien lägen damit übereinander, obwohl
    die eine 40'000 und die andere 400 bedeutet — die Zusammensetzung, um die
    es hier geht, wäre nicht mehr ablesbar.

    Der Preis ist real und soll benannt sein: die Gesamtlinie wird flacher, weil
    sie sich den Platz jetzt mit viel kleineren Beträgen teilt. Die genaue
    Auskunft über den Verlauf steht darum als Leitzahl und 12-Monats-Differenz
    im Kopf der Karte.

    Geglättet mit ``klemmen=True``: ohne Klemme liefe die Kurve zwischen zwei
    Monatswerten durch Beträge, die es nie gab.

    Es gibt keine Flächenfüllung mehr. Sie stammte aus der Zeit mit einer
    einzigen Kurve und trug dort die einzige Zusatzaussage „Abstand zur Null".
    Seit Konten mitlaufen, beginnt die Skala ohnehin bei 0 (siehe unten) und die
    beschriftete Achse sagt dasselbe genauer. Die Fläche wiederholte also nur
    noch — legte sich dabei aber unter jede Konto-Linie, deren 3:1-Kontrast
    gegen den *Kartengrund* gemessen ist, und machte aus jeder Kreuzung einen
    dunklen Keil. Genau das meinte der Nutzer mit „unleserlich".

    Sie nur bei einer einzelnen Linie zu zeichnen, wäre keine Lösung, sondern
    toter Code: eine einzige Linie gibt es erst, wenn kein einziges aktives
    Konto einen Betrag trägt — dann steht die Kurve flach auf der Null, und eine
    Fläche darunter hat keine Höhe.
    """
    werte = [float(v) for v in gesamt]
    if not werte:
        return {"linie": "", "pts": [], "last_x": 0.0,
                "last_y": h / 2, "last_px": 0.0, "last_py": 50.0,
                "lo": 0.0, "hi": 0.0, "linien": []}

    alle = werte + [float(v) for r in reihen for v in r["werte"]]
    lo, hi = min(alle), max(alle)
    # Sobald mehrere Reihen im selben Bild liegen, muss die Skala bei 0 beginnen:
    # sonst sagt der Abstand zweier Linien nichts mehr über das Verhältnis ihrer
    # Beträge — ein Konto mit 6'000 sähe neben einem mit 4'000 dreimal so gross
    # aus, wenn die Achse bei 3'500 anfängt. Bei einer einzelnen Linie bleibt es
    # beim engen Ausschnitt: dort geht es um den Verlauf, nicht um Verhältnisse,
    # und die Beträge stehen an der Achse.
    if reihen:
        lo = min(lo, 0.0)
    span = hi - lo
    anz = len(werte)
    step = (w - 2 * pad) / (anz - 1) if anz > 1 else 0.0

    def _punkte(vals: list[float]) -> list[tuple[float, float]]:
        return [
            (round(pad + i * step, 2),
             round(h / 2 if span == 0 else (h - pad) - (v - lo) / span * (h - 2 * pad), 2))
            for i, v in enumerate(vals)
        ]

    pts = _punkte(werte)
    d_linie = curve_path(pts, klemmen=True)

    linien = []
    for r in reihen:
        r_pts = _punkte([float(v) for v in r["werte"]])
        linien.append({"name": r["name"], "titel": r["titel"], "rest": r["rest"],
                       "d": curve_path(r_pts, klemmen=True), "pts": r_pts})
    return {"linie": d_linie, "pts": pts,
            "last_x": pts[-1][0], "last_y": pts[-1][1],
            # Endpunkt zusätzlich in Prozent: der Punkt sitzt als HTML-Element
            # über dem Chart, nicht als <circle> darin. Ein Kreis im viewBox
            # eines mit preserveAspectRatio=none gestreckten SVG wird zur
            # Ellipse — bei 1159 px Chartbreite aus r=3.5 ein 13×8-Ei.
            "last_px": round(pts[-1][0] / w * 100, 3),
            "last_py": round(pts[-1][1] / h * 100, 3),
            "lo": lo, "hi": hi, "linien": linien}


def _view_context(db: Session) -> dict:
    """Baut die angereicherte Konten-Ansicht: Vermögens-Verlauf + Gruppen + Sparklines."""
    accounts = _accounts_ordered(db)
    today = heute_lokal()
    # Bezugsgrösse für Leitzahl UND Anteile: nur aktive Konten. Archivierte
    # werden zwar noch (gedimmt) angezeigt, gehören aber nicht ins Vermögen —
    # sonst summierten sich die Anteile auf über 100 % und die Gruppen-
    # Zwischensummen ergäben zusammen nicht die Leitzahl darüber.
    total = sum((a.current_balance or Decimal("0") for a in accounts if a.is_active), Decimal("0"))

    # Vermögens-Verlauf (12 Monate): die Gesamtlinie, dahinter die einzelnen
    # Konten als dünne Linien.
    nw = net_worth_series(db, today, 12)
    nw_vals = [p["value"] for p in nw]
    nw_reihen = konto_verlaeufe(db, today, 12)
    nw_chart = _verlauf_layout(nw_vals, nw_reihen, w=NW_W, h=NW_H, pad=NW_PAD)
    nw_now = nw_vals[-1] if nw_vals else Decimal("0")
    nw_change = (nw_now - nw_vals[0]) if nw_vals else Decimal("0")
    # Ein Datensatz je Monat für die Führungslinie: Beschriftung, alle Beträge
    # dieses Monats und die y-Positionen, an denen die Punkte auf den Linien
    # sitzen. Beträge kommen fertig formatiert aus Python — im Browser gäbe es
    # weder Decimal noch das Schweizer Apostroph umsonst. Ohne Währungskürzel:
    # bei bis zu sieben Zeilen stünde „CHF" siebenmal untereinander, und die
    # Leitzahl im Kartenkopf nennt es einmal.
    nw_monate = [
        {"x": px,
         "monat": f"{MONATE[p['month'].month - 1]} {p['month'].year}",
         "gesamt": chf_kurz(p["value"]),
         "gy": py,
         "werte": [chf_kurz(r["werte"][i]) for r in nw_reihen],
         "wy": [linie["pts"][i][1] for linie in nw_chart["linien"]]}
        for i, ((px, py), p) in enumerate(zip(nw_chart["pts"], nw, strict=False))
    ]

    # Konten nach Typ gruppieren, je Konto Anteil + letzte Aktivität.
    # Gruppenfarben aus der zentralen Palette — die hängt am THEME-NAMEN, damit
    # ein Themenwechsel auch die Diagramme mitnimmt. Single-User → Vorliebe
    # direkt aus der DB.
    theme = db.scalar(select(User.preferred_theme)) or "dark"

    # Farben der Konto-Linien: die Palette der Reihe nach, aber ab --chart-1 —
    # --chart-0 ist die Gesamtlinie (Begründung in account_charts.konto_farbe).
    # Nicht die Farbe der jeweiligen Gruppe (Liquide/Sparen/Anlage): drei
    # Sparkonten bekämen sonst dieselbe Farbe und wären in der Legende nicht
    # mehr auseinanderzuhalten.
    nw_lines = [
        dict(linie, farbe=konto_farbe(i, rest=linie["rest"]))
        for i, linie in enumerate(nw_chart["linien"])
    ]
    group_colors = {
        "Liquide": color_at(0, theme),
        "Sparen": color_at(2, theme),
        "Anlage": color_at(3, theme),
    }
    groups = []
    for label, types in GROUP_DEFS:
        accs = [a for a in accounts if a.type in types]
        if not accs:
            continue
        subtotal = sum((a.current_balance or Decimal("0") for a in accs if a.is_active), Decimal("0"))
        rows = []
        for a in accs:
            bal = a.current_balance or Decimal("0")
            # Archivierte Konten tragen keinen Anteil — sie stecken auch nicht
            # in `total`, ein Prozentwert wäre also gegen eine fremde Basis.
            share = (
                round(float(bal / total * 100), 1)
                if (a.is_active and total > 0 and bal > 0) else 0
            )
            rows.append({
                "acc": a,
                "share": share,
                "last": last_activity(db, a.id),
            })
        groups.append({"label": label, "color": group_colors.get(label, color_at(0, theme)),
                       "accounts": rows, "subtotal": subtotal})

    return {
        "accounts": accounts,
        "total_balance": total,
        "type_labels": ACCOUNT_TYPE_LABELS,
        "type_choices": ACCOUNT_TYPE_CHOICES,
        "acc_groups": groups,
        "nw_chart": nw_chart,
        "nw_lines": nw_lines,
        "nw_monate": nw_monate,
        "nw_axis": _nw_axis(nw_chart["lo"], nw_chart["hi"], nw, h=NW_H, pad=NW_PAD,
                            xs=[x for x, _ in nw_chart["pts"]], w=NW_W),
        "nw_now": nw_now,
        "nw_change": nw_change,
    }


def _render_root(
    request: Request,
    db: Session,
    *,
    form_mode: str = "none",
    edit_account: Account | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """Rendert den ``#accounts-root``-Container (Liste + optionales Formular)."""
    ctx = _view_context(db)
    ctx |= {"form_mode": form_mode, "edit_account": edit_account, "error": error}
    return templates.TemplateResponse(
        request,
        "partials/accounts_root.html",
        ctx,
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Anzeige
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    form: str = "none",
    id: int | None = None,
) -> Response:
    """Konten-Seite. ``form=new`` oder ``form=edit&id=..`` öffnet das Formular."""
    edit_account = None
    if form == "edit" and id is not None:
        edit_account = db.get(Account, id)
        if edit_account is None:
            form = "none"

    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db, form_mode=form, edit_account=edit_account)

    ctx = _view_context(db)
    ctx |= {"user": user, "active_tab": "accounts", "form_mode": form, "edit_account": edit_account}
    return templates.TemplateResponse(request, "accounts.html", ctx)


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------


@router.post("", response_class=HTMLResponse)
def create_account(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    type: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "CHF",
    balance: Annotated[str, Form()] = "0",
    iban: Annotated[str, Form()] = "",
) -> Response:
    """Legt ein neues Konto an. ``balance`` = Startsaldo (opening_balance)."""
    name = name.strip()
    if not name:
        return _render_root(request, db, form_mode="new", error="Bitte einen Namen angeben.", status_code=400)
    try:
        acc_type = AccountType(type)
    except ValueError:
        return _render_root(request, db, form_mode="new", error="Unbekannter Konto-Typ.", status_code=400)
    try:
        amount = parse_amount(balance)
    except InvalidOperation:
        return _render_root(request, db, form_mode="new", error="Saldo ist keine gültige Zahl.", status_code=400)

    max_order = db.scalar(select(func.max(Account.sort_order))) or 0
    account = Account(
        name=name,
        type=acc_type,
        currency=(currency or "CHF").strip().upper()[:3],
        opening_balance=amount,
        current_balance=amount,
        iban=(iban.strip() or None),
        is_active=True,
        sort_order=max_order + 10,
    )
    db.add(account)
    db.flush()
    recalc_account_balance(db, account.id)
    db.commit()
    return _render_root(request, db, form_mode="none")


# ---------------------------------------------------------------------------
# Bearbeiten
# ---------------------------------------------------------------------------


@router.post("/{account_id}", response_class=HTMLResponse)
def update_account(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    account_id: int,
    name: Annotated[str, Form()],
    type: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "CHF",
    balance: Annotated[str, Form()] = "0",
    iban: Annotated[str, Form()] = "",
) -> Response:
    """Aktualisiert ein bestehendes Konto. ``balance`` = Startsaldo (opening_balance)."""
    account = db.get(Account, account_id)
    if account is None:
        return _render_root(request, db, error="Konto nicht gefunden.", status_code=404)

    name = name.strip()
    if not name:
        return _render_root(request, db, form_mode="edit", edit_account=account,
                            error="Bitte einen Namen angeben.", status_code=400)
    try:
        account.type = AccountType(type)
    except ValueError:
        return _render_root(request, db, form_mode="edit", edit_account=account,
                            error="Unbekannter Konto-Typ.", status_code=400)
    try:
        amount = parse_amount(balance)
    except InvalidOperation:
        return _render_root(request, db, form_mode="edit", edit_account=account,
                            error="Saldo ist keine gültige Zahl.", status_code=400)

    account.name = name
    account.currency = (currency or "CHF").strip().upper()[:3]
    account.opening_balance = amount
    account.iban = iban.strip() or None
    db.add(account)
    db.flush()
    recalc_account_balance(db, account.id)
    db.commit()
    return _render_root(request, db, form_mode="none")


# ---------------------------------------------------------------------------
# Archivieren / Reaktivieren / Löschen
# ---------------------------------------------------------------------------


@router.post("/{account_id}/inventory", response_class=HTMLResponse)
def cash_inventory(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    account_id: int,
    counted: Annotated[str, Form()],
) -> Response:
    """Bargeld-Inventur (Kassensturz): gezählten Bestand mit dem getrackten Saldo
    abgleichen. Die Differenz wird als Korrektur-Buchung gebucht, damit der
    Konto-Saldo der Realität entspricht. Eine Differenz < 0.01 wird ignoriert.
    """
    account = db.get(Account, account_id)
    if account is None:
        return _render_root(request, db, error="Konto nicht gefunden.", status_code=404)
    try:
        gezaehlt = parse_amount(counted)
    except InvalidOperation:
        return _render_root(request, db, error="Gezählter Betrag ist keine gültige Zahl.", status_code=400)

    diff = gezaehlt - (account.current_balance or Decimal("0"))
    if diff.copy_abs() >= Decimal("0.01"):
        # Positiv = mehr Bargeld gefunden (Einnahme), negativ = weniger (Aufwand).
        # Bewusst ohne Kategorie — eine Zähldifferenz gehört in keinen Budget-Topf.
        db.add(Transaction(
            account_id=account.id,
            date=heute_lokal(),
            amount=diff,
            description=f"{KASSENSTURZ_PREFIX} (gezählt {gezaehlt} CHF)",
        ))
        db.flush()
        recalc_account_balance(db, account.id)
        db.commit()
    return _render_root(request, db, form_mode="none")


@router.post("/{account_id}/toggle", response_class=HTMLResponse)
def toggle_account(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    account_id: int,
) -> Response:
    """Schaltet ein Konto aktiv/inaktiv (Archivieren ohne Datenverlust)."""
    account = db.get(Account, account_id)
    if account is not None:
        account.is_active = not account.is_active
        db.add(account)
        db.commit()
    return _render_root(request, db, form_mode="none")


@router.post("/{account_id}/delete", response_class=HTMLResponse)
def delete_account(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    account_id: int,
) -> Response:
    """Löscht ein Konto endgültig — nur, wenn keine Buchungen dranhängen.

    Der RESTRICT-Foreign-Key auf ``transactions.account_id`` würde das Löschen
    sonst erst beim Commit mit einem IntegrityError (→ 500) verhindern; wir
    prüfen vorher und antworten mit einer verständlichen Meldung (Archivieren
    ist dann der Weg). Sparziele/Importe/Abos hängen per SET NULL — unkritisch.
    """
    account = db.get(Account, account_id)
    if account is not None:
        tx_n = db.scalar(
            select(func.count(Transaction.id)).where(Transaction.account_id == account_id)
        ) or 0
        if tx_n:
            return _render_root(
                request, db,
                error=f"„{account.name}“ hat {tx_n} Buchung(en) — bitte stattdessen archivieren.",
                status_code=400,
            )
        db.delete(account)
        db.commit()
    return _render_root(request, db, form_mode="none")
