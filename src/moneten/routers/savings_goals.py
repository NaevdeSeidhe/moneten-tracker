"""Sparziele: Zielbetrag, optionales Zielkonto, Fortschritt.

Ist ein Ziel mit einem **Konto** verknüpft, dient dessen aktueller Saldo als
Fortschritt (gespart / Ziel). Ohne Konto wird nur der Zielbetrag angezeigt.

UI-Muster wie sonst: ein Container ``#savings-root``, der bei jeder Aktion per
HTMX neu gerendert wird.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.auth.pin import require_login
from moneten.dates import add_months, heute_lokal
from moneten.db.models import Account, GoalPriority, MeetContribution, MeetVisit, SavingsGoal, User
from moneten.db.session import get_db
from moneten.money import parse_amount
from moneten.services import meet_fund
from moneten.services.account_charts import account_balance_series
from moneten.services.savings_alloc import _sort_key, allocate_savings
from moneten.templating import MONATE, templates

router = APIRouter(tags=["savings_goals"])

_PRIO_LABEL = {GoalPriority.HIGH: "Hoch", GoalPriority.MEDIUM: "Mittel", GoalPriority.LOW: "Niedrig"}

def _forecast(db: Session, acc: Account, saved: Decimal, target: Decimal, today: date) -> dict | None:
    """Schätzt das Erreichungsdatum aus der monatlichen Sparrate.

    Sparrate = durchschnittliche Saldo-Veränderung des verknüpften Kontos über
    die letzten 6 Monate. Gibt ``None`` zurück, wenn keine sinnvolle Prognose
    möglich ist (Ziel schon erreicht oder Saldo wächst nicht).
    """
    if target <= 0 or saved >= target:
        return None
    series = account_balance_series(db, acc, today, n=6)  # Monatsend-Salden, alt→neu
    if len(series) < 2:
        return None
    rate = (series[-1] - series[0]) / (len(series) - 1)  # Ø-Veränderung pro Monat
    if rate <= 0:
        return {"rate": rate, "date": None}  # spart aktuell nicht (zeigt „kein Fortschritt")
    months = math.ceil(float((target - saved) / rate))
    if months > 600:  # > 50 Jahre → praktisch „nie", keine sinnvolle Datumsangabe
        return {"rate": rate, "date": None}
    fc = add_months(today.replace(day=1), months)
    return {"rate": rate, "date": fc, "months": months}


def _accounts(db: Session) -> list[Account]:
    return list(
        db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.sort_order, Account.id))
    )


def _build_rows(db: Session) -> list[dict]:
    """Sparziele mit berechnetem Fortschritt (aus dem verknüpften Konto)."""
    acc_by_id = {a.id: a for a in db.scalars(select(Account))}
    # Sortierung in Python statt per ORDER BY: `priority` ist ein String, die
    # DB würde „high, low, medium" liefern (s. _PRIO_ORDER).
    goals = sorted(db.scalars(select(SavingsGoal)), key=_sort_key)
    zuteilung = allocate_savings(
        goals, {a.id: a.current_balance or Decimal("0") for a in acc_by_id.values()}
    )
    today = heute_lokal()
    rows: list[dict] = []
    for g in goals:
        acc = acc_by_id.get(g.account_id) if g.account_id else None
        saved = zuteilung.get(g.id, Decimal("0")) if acc is not None else None
        target = g.target_amount or Decimal("0")
        pct = int(min(float(saved) / float(target) * 100, 100)) if (saved is not None and target > 0) else None
        remaining = (target - saved) if saved is not None else None

        # Prognose nur bei verknüpftem Konto + noch offenem, nicht erreichtem Ziel.
        forecast = None
        if acc is not None and not g.is_achieved and saved is not None:
            fc = _forecast(db, acc, saved, target, today)
            if fc is not None:
                fc_date = fc.get("date")
                forecast = {
                    "rate": fc["rate"],
                    "label": f"{MONATE[fc_date.month - 1]} {fc_date.year}" if fc_date else None,
                    # „auf Kurs": kein Zieldatum gesetzt ODER Prognose liegt davor.
                    "on_track": fc_date is not None and (g.target_date is None or fc_date <= g.target_date),
                    "stalled": fc_date is None,  # spart aktuell nicht / Ziel unerreichbar
                }
        rows.append({
            "g": g,
            "account": acc,
            "saved": saved,
            "remaining": remaining if (remaining is None or remaining > 0) else Decimal("0"),
            "pct": pct,
            "prio_label": _PRIO_LABEL.get(g.priority, "—"),
            "forecast": forecast,
        })
    return rows


def _meet_context(db: Session, offen: str | None = None) -> dict:
    """Kontext des Treffen-Fonds (Sektion oben auf der Sparziele-Seite).

    ``offen`` benennt den Aufklapper, der nach dem Rendern offen bleiben soll
    (``monate`` | ``faktoren``). Jede Aktion tauscht ``#savings-root`` komplett
    aus; ohne diese Angabe klappte die Monatsliste nach JEDEM nachgetragenen
    Betrag wieder zu — bei einem halben Jahr Rückstand einmal pro Monat.
    """
    today = heute_lokal()
    settings = meet_fund.get_settings(db)
    balance = meet_fund.fund_balance(db, settings, today)
    visits = list(db.scalars(select(MeetVisit).order_by(MeetVisit.date)))
    months = meet_fund.month_rows(db, settings, today)
    verbrauch = meet_fund.verbrauch(db, settings, today)
    verbrauch_je_visit = {z["visit"].id: z for z in verbrauch}
    return {
        "meet": {
            "offen": offen,
            "settings": settings,
            "balance": balance,
            "jar": meet_fund.jar_stat(db, settings, balance["total"], today),
            "months": months,
            # Getrennt gezählt, damit die zugeklappte Liste sagen kann, WAS sie
            # enthält — „Weitere Monate (4)" beantwortet die Frage nicht, ob
            # vergangene Monate überhaupt nachtragbar sind.
            "months_past": sum(1 for r in months if not r["current"] and not r["future"]),
            "months_future": sum(1 for r in months if r["future"]),
            # Obergrenze fürs Startmonat-Feld: der Browser soll gar nicht erst
            # anbieten, was die Route ablehnen muss.
            "start_max": meet_fund.start_grenze(db, today).strftime("%Y-%m"),
            "projection": meet_fund.projection(db, settings, today),
            "visits": [
                {
                    "v": v,
                    "cost": meet_fund.visit_cost_chf(settings, v.location, v.nights, v.cost_override_chf),
                    "past": v.date <= today,
                    # Der Verbrauch steht an SEINEM Treffen und nicht in einer
                    # zweiten Liste darunter: dieselbe Reise zweimal zu zeigen
                    # hiesse, den Bezug im Kopf herstellen zu lassen.
                    "verbrauch": verbrauch_je_visit.get(v.id),
                }
                for v in visits
            ],
            "monthly_total": meet_fund.monthly_total_chf(settings),
            # Beides ``None``/leer, solange kein Ferienkonto gewählt ist — die
            # Abschnitte fehlen dann ganz, statt leer dazustehen.
            "rueckstellung": meet_fund.rueckstellung(db, settings, today),
            "konten": _accounts(db),
        }
    }


def _base_context(db: Session, *, error: str | None = None, meet_offen: str | None = None) -> dict:
    rows = _build_rows(db)
    total_target = sum((r["g"].target_amount or Decimal("0") for r in rows if not r["g"].is_achieved), Decimal("0"))
    total_saved = sum((r["saved"] or Decimal("0") for r in rows if not r["g"].is_achieved), Decimal("0"))
    return {
        "rows": rows,
        "accounts": _accounts(db),
        "error": error,
        "today": heute_lokal().isoformat(),
        "total_target": total_target,
        "total_saved": total_saved,
        **_meet_context(db, meet_offen),
    }


def _render_root(request: Request, db: Session, *, error: str | None = None, status_code: int = 200,
                 meet_offen: str | None = None) -> Response:
    return templates.TemplateResponse(
        request, "partials/savings_root.html",
        _base_context(db, error=error, meet_offen=meet_offen), status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def savings_page(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Sparziele-Seite mit Fortschrittsbalken."""
    if request.headers.get("HX-Request") == "true":
        return _render_root(request, db)
    ctx = _base_context(db)
    ctx |= {"user": user, "active_tab": "savings_goals"}
    return templates.TemplateResponse(request, "savings_goals.html", ctx)


@router.post("", response_class=HTMLResponse)
def create_goal(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    target_amount: Annotated[str, Form()] = "",
    target_date: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    priority: Annotated[str, Form()] = "medium",
) -> Response:
    """Legt ein neues Sparziel an."""
    if not name.strip():
        return _render_root(request, db, error="Bitte einen Namen für das Sparziel angeben.", status_code=400)
    try:
        target = parse_amount(target_amount)
    except InvalidOperation:
        return _render_root(request, db, error="Zielbetrag ist keine gültige Zahl.", status_code=400)
    if target <= 0:
        return _render_root(request, db, error="Bitte einen Zielbetrag grösser als 0 angeben.", status_code=400)

    tdate = None
    if target_date:
        try:
            tdate = date.fromisoformat(target_date)
        except (ValueError, TypeError):
            return _render_root(request, db, error="Bitte ein gültiges Zieldatum wählen.", status_code=400)

    try:
        prio = GoalPriority(priority)
    except ValueError:
        prio = GoalPriority.MEDIUM

    db.add(SavingsGoal(
        name=name.strip(),
        target_amount=target,
        target_date=tdate,
        account_id=int(account_id) if account_id else None,
        priority=prio,
        icon="target",
    ))
    db.commit()
    return _render_root(request, db)


@router.post("/{goal_id:int}/toggle", response_class=HTMLResponse)
def toggle_goal(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    goal_id: int,
) -> Response:
    """Markiert ein Sparziel als erreicht / wieder offen."""
    g = db.get(SavingsGoal, goal_id)
    if g is not None:
        g.is_achieved = not g.is_achieved
        db.commit()
    return _render_root(request, db)


@router.post("/{goal_id:int}/delete", response_class=HTMLResponse)
def delete_goal(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    goal_id: int,
) -> Response:
    """Löscht ein Sparziel."""
    g = db.get(SavingsGoal, goal_id)
    if g is not None:
        db.delete(g)
        db.commit()
    return _render_root(request, db)


# ---------------------------------------------------------------------------
# Treffen-Fonds
# ---------------------------------------------------------------------------


def _meet_month(settings, raw: str) -> date:
    """Prüft einen geposteten Monat gegen die angebotene Spanne.

    Wirft ``ValueError``. Ein Beitrag ausserhalb der Spanne zählte im Topf mit,
    stünde aber in keiner Monatsliste — der Stand liesse sich dann nicht mehr
    herleiten.
    """
    m = date.fromisoformat(raw).replace(day=1)
    erster, letzter = meet_fund.month_span(settings, heute_lokal())
    if not erster <= m <= letzter:
        raise ValueError("Monat liegt ausserhalb der Fonds-Spanne")
    return m


def _meet_offen(month: date) -> str | None:
    """Nach einer Buchung im laufenden Monat bleibt alles zu, sonst die Liste offen.

    Der laufende Monat steht ohnehin sichtbar oben; ein aufgeklappter Kasten
    darunter waere nur Bewegung ohne Grund.
    """
    return None if month == heute_lokal().replace(day=1) else "monate"


def _meet_beitrag(db: Session, month: date, person: str) -> MeetContribution | None:
    return db.scalar(select(MeetContribution).where(
        MeetContribution.month == month, MeetContribution.person == person
    ))


@router.post("/meet/confirm", response_class=HTMLResponse)
def meet_confirm(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    month: Annotated[str, Form()],
    person: Annotated[str, Form()],
) -> Response:
    """Schaltet die Monats-Bestätigung einer Person um (zurückgelegt ja/nein).

    Der schnelle Weg für den Regelfall: es kam genau die geplante Rate zusammen.
    Beim Bestätigen wird der AKTUELLE Monatsbetrag eingefroren — spätere
    Raten-Änderungen schreiben die Historie nicht um. Ein abweichender Betrag
    läuft über ``meet_amount``."""
    if person not in meet_fund.PERSONS:
        return _render_root(request, db, error="Unbekannte Person.", status_code=400)
    settings = meet_fund.get_settings(db)
    try:
        m = _meet_month(settings, month)
    except (ValueError, TypeError):
        return _render_root(request, db, error="Ungültiger Monat.", status_code=400)
    existing = _meet_beitrag(db, m, person)
    if existing is not None:
        db.delete(existing)
    else:
        db.add(MeetContribution(month=m, person=person,
                                amount_native=meet_fund.planned_amount(settings, person)))
    db.commit()
    return _render_root(request, db, meet_offen=_meet_offen(m))


@router.post("/meet/amount", response_class=HTMLResponse)
def meet_amount(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    month: Annotated[str, Form()],
    person: Annotated[str, Form()],
    amount: Annotated[str, Form()] = "",
) -> Response:
    """Trägt einen ABWEICHENDEN Monatsbetrag ein — auch für vergangene Monate.

    Der Haken bestätigt nur die geplante Rate. Wer in einem Monat weniger (oder
    mehr) zurückgelegt hat, käme damit nie auf den echten Stand des Topfs.
    Betrag 0 oder leer löscht den Eintrag: dann wurde nichts zurückgelegt.
    """
    if person not in meet_fund.PERSONS:
        return _render_root(request, db, error="Unbekannte Person.", status_code=400)
    settings = meet_fund.get_settings(db)
    try:
        m = _meet_month(settings, month)
    except (ValueError, TypeError):
        return _render_root(request, db, error="Ungültiger Monat.", status_code=400)
    try:
        value = parse_amount(amount)
    except InvalidOperation:
        return _render_root(request, db, error="Betrag ist keine gültige Zahl.", status_code=400)
    if not 0 <= value <= 100000:
        return _render_root(request, db, error="Betrag: bitte 0–100'000 angeben.", status_code=400)

    existing = _meet_beitrag(db, m, person)
    if value == 0:
        if existing is not None:
            db.delete(existing)
    elif existing is not None:
        existing.amount_native = value
    else:
        db.add(MeetContribution(month=m, person=person, amount_native=value))
    db.commit()
    return _render_root(request, db, meet_offen=_meet_offen(m))


@router.post("/meet/settings", response_class=HTMLResponse)
def meet_settings(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    name_a: Annotated[str, Form()] = "",
    name_b: Annotated[str, Form()] = "",
    monthly_a_chf: Annotated[str, Form()] = "",
    monthly_b_eur: Annotated[str, Form()] = "",
    eur_chf_rate: Annotated[str, Form()] = "",
    flight_a_chf: Annotated[str, Form()] = "",
    flight_b_chf: Annotated[str, Form()] = "",
    airbnb_night_chf: Annotated[str, Form()] = "",
    food_day_chf: Annotated[str, Form()] = "",
    default_nights: Annotated[str, Form()] = "",
    start_balance_chf: Annotated[str, Form()] = "",
    start_month: Annotated[str, Form()] = "",
    holiday_account_id: Annotated[str, Form()] = "",
    offen: Annotated[str, Form()] = "faktoren",
) -> Response:
    """Speichert die Faktoren des Treffen-Fonds (alle Felder optional — leer = unverändert).

    ``start_month`` bestimmt, wie weit die Monatsliste zurückreicht. Ohne dieses
    Feld liess sich ein vergessener Monat vor dem eingestellten Start gar nicht
    nachtragen — die Liste begann schlicht später.

    ``offen`` sagt, welcher Aufklapper danach offen bleibt. Der Startmonat steht
    an zwei Stellen; wer ihn am Fuss der Monatsliste ändert, will die Liste
    sehen — mit den Monaten, die gerade dazugekommen sind, nicht die Faktoren.
    """
    settings = meet_fund.get_settings(db)
    zurueck = offen if offen in ("monate", "faktoren") else "faktoren"
    # Die beiden Namen sind Text, keine Beträge — sie dürfen NICHT durch die
    # Betragsprüfung darunter laufen. Leer heisst „unverändert", wie bei allen
    # anderen Feldern auch; gekürzt wird auf die Spaltenbreite, damit ein zu
    # langer Name nicht erst in der Datenbank auffällt.
    for attr, roh in (("name_a", name_a), ("name_b", name_b)):
        if roh.strip():
            setattr(settings, attr, roh.strip()[:40])

    fields = {
        "monthly_a_chf": monthly_a_chf, "monthly_b_eur": monthly_b_eur,
        "eur_chf_rate": eur_chf_rate, "flight_a_chf": flight_a_chf,
        "flight_b_chf": flight_b_chf, "airbnb_night_chf": airbnb_night_chf,
        "food_day_chf": food_day_chf, "start_balance_chf": start_balance_chf,
    }
    try:
        for attr, raw in fields.items():
            if raw.strip():
                value = parse_amount(raw)
                if value < 0 or (attr != "start_balance_chf" and attr != "eur_chf_rate" and value > 100000):
                    raise InvalidOperation
                setattr(settings, attr, value)
        if default_nights.strip():
            n = int(default_nights) if default_nights.strip().isdigit() else -1
            if not 1 <= n <= 60:
                raise InvalidOperation
            settings.default_nights = n
    except InvalidOperation:
        return _render_root(request, db, error="Treffen-Fonds: ein Wert ist keine gültige Zahl.",
                            status_code=400, meet_offen=zurueck)
    if start_month.strip():
        # <input type="month"> liefert „2026-07"; Firefox fällt auf ein Textfeld
        # zurück und schickt dann genau dasselbe Format.
        try:
            jahr, monat = (int(t) for t in start_month.strip().split("-")[:2])
            neu = date(jahr, monat, 1)
        except (ValueError, TypeError):
            return _render_root(request, db, error="Fonds-Start: bitte einen Monat wählen.",
                                status_code=400, meet_offen=zurueck)
        # Nach hinten ist der Start frei, nach vorn nicht: er darf weder die
        # Liste in die Zukunft schieben noch einen erfassten Beitrag aus ihr
        # herausdrängen. Die Meldung nennt die Grenze, sonst bleibt nur Raten.
        grenze = meet_fund.start_grenze(db, heute_lokal())
        if neu > grenze:
            grund = ("später begänne die Monatsliste in der Zukunft"
                     if grenze == heute_lokal().replace(day=1)
                     else "für diesen Monat ist ein Betrag eingetragen, der sonst in keiner Liste stünde")
            return _render_root(
                request, db, status_code=400, meet_offen=zurueck,
                error=f"Fonds-Start: höchstens {meet_fund.monats_label(grenze)} — {grund}.",
            )
        settings.start_month = neu
    # Ferienkonto. Leer heisst „keins" und ist eine gültige Wahl — nicht
    # „unverändert": der Nutzer muss die Verknüpfung auch wieder lösen können,
    # und ein Auswahlfeld schickt immer einen Wert.
    gewaehlt = holiday_account_id.strip()
    if gewaehlt:
        if not gewaehlt.isdigit() or db.get(Account, int(gewaehlt)) is None:
            return _render_root(request, db, error="Ferienkonto: dieses Konto gibt es nicht.",
                                status_code=400, meet_offen=zurueck)
        settings.holiday_account_id = int(gewaehlt)
    else:
        settings.holiday_account_id = None
    db.commit()
    return _render_root(request, db, meet_offen=zurueck)


@router.post("/meet/visit", response_class=HTMLResponse)
def meet_add_visit(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    visit_date: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "bei_b",
    nights: Annotated[str, Form()] = "",
) -> Response:
    """Trägt ein (geplantes oder vergangenes) Treffen ein."""
    if location not in meet_fund.LOCATIONS:
        return _render_root(request, db, error="Unbekannter Treffpunkt.", status_code=400)
    try:
        d = date.fromisoformat(visit_date)
    except (ValueError, TypeError):
        return _render_root(request, db, error="Bitte ein gültiges Datum fürs Treffen wählen.",
                            status_code=400)
    settings = meet_fund.get_settings(db)
    n = int(nights) if nights.strip().isdigit() else settings.default_nights
    if not 1 <= n <= 60:
        return _render_root(request, db, error="Nächte: bitte 1–60 angeben.", status_code=400)
    db.add(MeetVisit(date=d, location=location, nights=n))
    db.commit()
    return _render_root(request, db)


@router.post("/meet/visit/{visit_id:int}/delete", response_class=HTMLResponse)
def meet_delete_visit(
    request: Request,
    user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
    visit_id: int,
) -> Response:
    """Entfernt ein Treffen (Plan geändert)."""
    v = db.get(MeetVisit, visit_id)
    if v is not None:
        db.delete(v)
        db.commit()
    return _render_root(request, db)
