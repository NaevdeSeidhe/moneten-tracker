"""Abo-Erkennung: wiederkehrende Zahlungen aus den realen Buchungen ableiten.

Ein „Abo" ist hier eine Zahlung, die in einem **regelmässigen Rhythmus** von
mindestens einem Monat an denselben Empfänger geht (Netflix, Handy, Miete,
Krankenkasse). Was dichter kommt als alle 20 Tage, ist kein Abo, sondern
Alltag — der Wocheneinkauf fällt allein daran schon heraus.

Der Betrag muss **nicht über die ganze Historie gleich** sein. Ein Abo, das von
20 auf 100 wechselt, ist kein Zufall, sondern eine Preiserhöhung: die Reihe
zerfällt dann in zwei saubere **Stufen**, und genau das ist das Erkennungsmerkmal
gegenüber einem Wocheneinkauf, dessen Beträge bei jeder Zahlung neu würfeln.
Gerechnet wird mit dem **aktuellen** Betrag, nicht mit dem Mittel über alles;
der frühere Betrag wird als Änderung mitgeliefert.

Alles wird aus den vorhandenen Buchungen berechnet — keine externe Quelle,
läuft autonom auf dem NAS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from moneten.dates import add_months, heute_lokal
from moneten.db.models import Category, DismissedMerchant, Transaction, enthaelt, not_transfer
from moneten.services.categorization import _NOISE_WORDS

# Dichter als alle 20 Tage ist kein Abo-Rhythmus. Die Schranke trennt Bills von
# Alltagseinkäufen zuverlässiger als jede Betragsprüfung: zwei Migros-Besuche in
# derselben Woche scheitern hier, bevor die Beträge überhaupt angeschaut werden.
_MIN_ABSTAND_TAGE = 20

# (Obergrenze des Median-Abstands in Tagen, Anzeigename, Monate je Periode).
# Über 400 Tagen ist es kein Rhythmus mehr, sondern eine Wiederholung nach
# Jahren — daraus lässt sich kein Monatsbetrag ableiten.
_RHYTHMEN: tuple[tuple[int, str, int], ...] = (
    (45, "monatlich", 1),
    (75, "zweimonatlich", 2),
    (135, "vierteljährlich", 3),
    (225, "halbjährlich", 6),
    (400, "jährlich", 12),
)

# Zwei Zahlungen gehören zur selben Betragsstufe, wenn sie höchstens so weit
# auseinanderliegen. 12 % decken Kursdifferenzen und Rundungen ab, ohne einen
# Wechsel von 20 auf 100 zu verschlucken.
_STUFEN_TOLERANZ = Decimal("0.12")

# Anteil der Zahlungen, der auf einer mehrfach belegten Stufe liegen muss, damit
# die Reihe als „gleichbleibender Betrag mit Stufen" gilt.
_STUFEN_ANTEIL = Decimal("0.6")

# Ab wann eine Zahlung als ausgeblieben gilt: knapp der doppelte Rhythmus plus
# Puffer für verschobene Buchungstage. Der Faktor ist an den Rhythmus gekoppelt —
# eine Vierteljahresrechnung darf nicht nach zwei Monaten als beendet gelten.
_STALE_FAKTOR = 1.8
_STALE_PUFFER_TAGE = 20


def _merchant_key(description: str | None) -> str:
    """Normalisiert einen Buchungstext auf einen Händler-Schlüssel.

    Entfernt Ziffern (Daten/Referenzen), Sonderzeichen und Füllwörter; nimmt die
    ersten bedeutungstragenden Wörter. So landen „TWINT Spotify 12.04" und
    „Spotify AB" im selben Topf.

    Die Normalisierung ist **bewusst unverändert**: dieselben Schlüssel stehen
    als ``DismissedMerchant.merchant_key`` und ``ManualSubscription.match_keyword``
    in der Datenbank. Wo verschieden lange Buchungstexte denselben Händler in
    zwei Schlüssel zerlegen, räumt :func:`_kanonische_schluessel` auf; wo ein
    gespeicherter Schlüssel auf einen Gruppenschlüssel treffen muss, entscheidet
    :func:`key_passt` — beides ohne die gespeicherten Werte anzufassen.
    """
    # Maskierte Kartennummern („123456xxxxxx7890") hinterlassen sonst ein
    # bedeutungsloses „xxxxxx" als Schluesselwort — nachgemessen ergab
    # „Einkauf Buchhandlung … Visa Debit-Nr. 123456xxxxxx7890" den Schluessel
    # „buchhandlung xxxxxx" statt „buchhandlung".
    s = re.sub(r"x{4,}", " ", (description or "").lower())
    s = re.sub(r"[0-9]", " ", s)
    s = re.sub(r"[^a-zà-ÿ ]", " ", s)
    toks = [t for t in s.split() if len(t) >= 3 and t not in _NOISE_WORDS]
    return " ".join(toks[:3])


def key_passt(gespeichert: str, gruppe: str) -> bool:
    """Trifft ein **gespeicherter** Händler-Schlüssel diese Buchungsgruppe?

    Nicht auf Gleichheit prüfen. Gespeicherte Schlüssel („ist kein Abo",
    Verknüpfung eines manuellen Abos) stammen aus einem einzelnen früheren
    Buchungstext und können kürzer oder länger sein als der heutige
    Gruppenschlüssel — „muster mobile" gegen „muster mobile abo". Eine
    Wort-Präfix-Übereinstimmung in eine der beiden Richtungen genügt; damit
    bleiben alle bestehenden Verknüpfungen gültig, auch wenn eine Gruppe
    zusammengezogen wurde.
    """
    a, b = (gespeichert or "").split(), (gruppe or "").split()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _trifft_eine(gespeicherte: set[str], gruppe: str) -> bool:
    """Ob irgendein gespeicherter Schlüssel diese Gruppe trifft."""
    return any(key_passt(g, gruppe) for g in gespeicherte)


def _schreibweise(wort: str) -> str:
    """Ein Wort für die Anzeige: Abkürzungen bleiben gross, Geschrei wird zahm.

    „MUSTERDIENST" soll als „Musterdienst" erscheinen, „SBB" und „ZVV" aber nicht
    als „Sbb"/„Zvv" — deshalb bleiben kurze Vollversalien unangetastet.
    """
    if wort.isupper():
        return wort if len(wort) <= 4 else wort.capitalize()
    return wort[0].upper() + wort[1:]


def display_name(description: str | None, *, max_len: int = 42) -> str:
    """Lesbarer Anzeigename aus einem rohen Buchungstext.

    Der Rohtext („Online Einkauf Musterdienst by Muster03.07.2026, 16:04, Visa
    Debit-Nr. 123456xxxxxx7890") ist als Name unbrauchbar: er stand bisher
    vollständig im Namensfeld des Formulars und in jeder erkannten Zeile.
    Genommen werden die ersten zwei bedeutungstragenden Wörter — dieselben, die
    auch den Händler-Schlüssel bilden — aber in der Schreibweise der Buchung.
    """
    roh = (description or "").strip()
    gesucht = _merchant_key(roh).split()[:2]
    if not gesucht:
        return roh[:max_len]
    teile: list[str] = []
    for wort in re.findall(r"[A-Za-zÀ-ÿ]+", roh):
        if gesucht and wort.lower() == gesucht[0]:
            gesucht.pop(0)
            teile.append(_schreibweise(wort))
            if not gesucht:
                break
    return " ".join(teile)[:max_len] or roh[:max_len]


def _median(werte: list[Decimal]) -> Decimal:
    """Median in Decimal — Beträge dürfen nie über float laufen."""
    s = sorted(werte)
    m = len(s) // 2
    if len(s) % 2:
        return s[m]
    return ((s[m - 1] + s[m]) / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _variationskoeffizient(werte: list[Decimal]) -> Decimal:
    """Streuung im Verhältnis zum Mittel (0 = alle Beträge gleich)."""
    n = len(werte)
    if n < 2:
        return Decimal("0")
    mittel = sum(werte, Decimal("0")) / n
    if mittel <= 0:
        return Decimal("999")
    varianz = sum(((w - mittel) ** 2 for w in werte), Decimal("0")) / n
    return varianz.sqrt() / mittel


def _rhythmus(abstand: float) -> tuple[str, int] | None:
    """Anzeigename und Monate je Periode zu einem Median-Abstand in Tagen."""
    if abstand < _MIN_ABSTAND_TAGE:
        return None
    for grenze, name, monate in _RHYTHMEN:
        if abstand <= grenze:
            return name, monate
    return None


def _takt(daten: list[date]) -> tuple[float, bool]:
    """Median-Abstand in Tagen und ob die Abstände gleichmässig sind.

    „Gleichmässig" heisst: mindestens 60 % der Abstände liegen im Band
    0.6–1.6 × Median. Ein einzelner Ausreisser (Zahlung am Feiertag verschoben,
    ein Monat übersprungen) darf eine Reihe nicht kippen.
    """
    if len(daten) < 2:
        return 0.0, False
    abstaende = [(b - a).days for a, b in zip(daten, daten[1:], strict=False)]
    mitte = float(_median([Decimal(a) for a in abstaende]))
    if mitte <= 0:
        return 0.0, False
    treffer = sum(1 for a in abstaende if 0.6 * mitte <= a <= 1.6 * mitte)
    return mitte, (treffer / len(abstaende)) >= 0.6


@dataclass
class _Stufe:
    """Ein Abschnitt der Zahlungsreihe mit (nahezu) gleichem Betrag."""

    seit: date
    betraege: list[Decimal]

    @property
    def betrag(self) -> Decimal:
        return _median(self.betraege)


def _stufen(zahlungen: list[tuple[date, Decimal]]) -> list[_Stufe]:
    """Zerlegt die nach Datum sortierte Reihe in Betrags-Stufen.

    Eine Stufe endet, wo ein Betrag mehr als :data:`_STUFEN_TOLERANZ` vom Median
    der laufenden Stufe abweicht. Eine Preiserhöhung erzeugt so genau zwei
    Stufen — ein Wocheneinkauf so viele Stufen wie Zahlungen. Dieser Unterschied
    ersetzt die alte Prüfung „Betrag über das ganze Fenster stabil", an der jede
    Preiserhöhung scheiterte.
    """
    stufen: list[_Stufe] = []
    for tag, betrag in zahlungen:
        if stufen:
            bezug = stufen[-1].betrag
            if bezug > 0 and abs(betrag - bezug) <= bezug * _STUFEN_TOLERANZ:
                stufen[-1].betraege.append(betrag)
                continue
        stufen.append(_Stufe(seit=tag, betraege=[betrag]))
    return stufen


def _aktueller_preis(stufen: list[_Stufe]) -> tuple[Decimal, date, Decimal | None]:
    """Aktueller Betrag, seit wann er gilt, und der Preis davor.

    Rückwärts durch die Stufen: gleich hohe Stufen gehören zum selben Preis (die
    Reihe war nur einmal unterbrochen), eine **einzelne** abweichende Zahlung ist
    ein Ausreisser (Sonderrechnung, Nachzahlung) und kein Preiswechsel. Als
    „vorher" gilt erst die letzte Stufe, die mehrfach belegt war — sonst meldete
    die Seite bei jeder Sonderrechnung eine Preisänderung, die nie stattfand.
    """
    aktuell = stufen[-1]
    betrag, seit = aktuell.betrag, aktuell.seit
    for s in reversed(stufen[:-1]):
        if betrag > 0 and abs(s.betrag - betrag) <= betrag * _STUFEN_TOLERANZ:
            seit = s.seit
            continue
        if len(s.betraege) == 1:
            continue
        return betrag, seit, s.betrag
    return betrag, seit, None


def _stufig(stufen: list[_Stufe], anzahl: int) -> bool:
    """Liegt der grösste Teil der Zahlungen auf mehrfach belegten Stufen?"""
    if anzahl <= 0:
        return False
    wiederholt = sum(len(s.betraege) for s in stufen if len(s.betraege) >= 2)
    return (Decimal(wiederholt) / anzahl) >= _STUFEN_ANTEIL


def _kanonische_schluessel(keys: set[str]) -> dict[str, str]:
    """Ordnet jedem Gruppen-Schlüssel den Schlüssel zu, unter dem er zählt.

    Buchungstexte desselben Händlers sind nicht immer gleich lang: „Muster Mobile
    SA" und „Muster Mobile SA Abo" ergeben zwei Schlüssel und damit zwei Gruppen,
    von denen jede zu wenig Zahlungen hat — beide fallen durch. Ist ein
    Schlüssel Wort-Präfix von **genau einem** längeren, wird er dorthin gezogen.
    Gibt es mehrere Kandidaten („muster mobile" gegen „muster mobile abo" *und*
    „muster mobile home"), bleibt er stehen: dann wären zwei verschiedene Produkte
    zusammengeworfen worden.

    Ketten kann es dabei nicht geben — hätte das Ziel selbst eine längere
    Fortsetzung, gäbe es für den Ausgangsschlüssel zwei Kandidaten und er bliebe
    stehen. Ein einziger Umzugsschritt genügt deshalb.
    """
    zerlegt = {k: tuple(k.split()) for k in keys}
    ziel: dict[str, str] = {}
    for k, worte in zerlegt.items():
        laenger = [o for o, w in zerlegt.items() if o != k and len(w) > len(worte) and w[: len(worte)] == worte]
        ziel[k] = laenger[0] if len(laenger) == 1 else k
    return ziel


@dataclass
class _Gruppe:
    """Alle Buchungen eines Händlers innerhalb des Fensters."""

    key: str
    zahlungen: list[tuple[date, Decimal]] = field(default_factory=list)
    last_desc: str = ""
    last_date: date | None = None
    category_counts: dict[int, int] = field(default_factory=dict)

    def add(self, tx_date: date, amount: Decimal, desc: str, category_id: int | None) -> None:
        self.zahlungen.append((tx_date, amount.copy_abs()))
        if self.last_date is None or tx_date >= self.last_date:
            self.last_date = tx_date
            self.last_desc = desc or self.last_desc
        if category_id is not None:
            self.category_counts[category_id] = self.category_counts.get(category_id, 0) + 1


@dataclass
class Subscription:
    """Eine erkannte wiederkehrende Zahlung."""

    name: str                 # aufgeräumter Anzeigename („Zahldienst Musterdienst")
    monthly: Decimal          # Monatsäquivalent des AKTUELLEN Betrags
    yearly: Decimal           # monthly * 12
    months_seen: int          # in wie vielen Monaten gesehen
    last_date: date | None
    category: Category | None
    key: str = ""             # Händler-Schlüssel (für „ist kein Abo" / Übernehmen)
    months_since: int = 0     # Monate seit der letzten Zahlung
    stale: bool = False       # länger keine Zahlung → vermutlich gekündigt
    amount: Decimal = Decimal("0")   # Betrag je Zahlung (aktuelle Stufe)
    rhythmus: str = "monatlich"      # monatlich / vierteljährlich / jährlich …
    zahlungen: int = 0               # Anzahl Buchungen im Fenster
    vorher: Decimal | None = None    # Betrag vor dem letzten Stufenwechsel
    seit: date | None = None         # erste Zahlung auf dem aktuellen Betrag
    beleg: str = ""                  # letzter Buchungstext (Herkunftsnachweis)


def _auswerten(
    g: _Gruppe, *, today: date, min_events: int, max_cv: float
) -> Subscription | None:
    """Prüft eine Händler-Gruppe und baut daraus ein :class:`Subscription`.

    Gibt ``None`` zurück, wenn die Gruppe keine wiederkehrende Zahlung ist.
    """
    reihe = sorted(g.zahlungen)
    if len(reihe) < min_events:
        return None
    daten = [d for d, _ in reihe]
    betraege = [b for _, b in reihe]
    if sum(betraege, Decimal("0")) <= 0:
        return None

    abstand, gleichmaessig = _takt(daten)
    rhythmus = _rhythmus(abstand)
    if rhythmus is None or not gleichmaessig:
        return None
    name_rhythmus, monate_je_periode = rhythmus

    stufen = _stufen(reihe)
    # Zwei Wege zum Ja: die Reihe zerfällt in wenige Betragsstufen (Preiswechsel
    # inbegriffen) ODER sie streut insgesamt kaum (leicht schwankende Rechnung,
    # etwa Strom). Nur wer beides verfehlt, ist keine wiederkehrende Zahlung.
    if not (_stufig(stufen, len(reihe)) or _variationskoeffizient(betraege) <= Decimal(str(max_cv))):
        return None

    betrag, seit, vorher = _aktueller_preis(stufen)
    monthly = (betrag / monate_je_periode).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tage_seit = (today - daten[-1]).days
    months_since = (today.year - daten[-1].year) * 12 + (today.month - daten[-1].month)
    return Subscription(
        name=display_name(g.last_desc) or g.key,
        monthly=monthly,
        yearly=(monthly * 12).quantize(Decimal("0.01")),
        months_seen=len({(d.year, d.month) for d in daten}),
        last_date=daten[-1],
        category=None,
        key=g.key,
        months_since=max(months_since, 0),
        stale=tage_seit > (abstand * _STALE_FAKTOR + _STALE_PUFFER_TAGE),
        amount=betrag,
        rhythmus=name_rhythmus,
        zahlungen=len(reihe),
        vorher=vorher,
        seit=seit,
        beleg=(g.last_desc or "")[:160],
    )


def detect_subscriptions(
    db: Session,
    *,
    today: date | None = None,
    window_months: int = 24,
    min_events: int = 3,
    max_cv: float = 0.25,
    extra_skip: set[str] | None = None,
) -> list[Subscription]:
    """Erkennt wiederkehrende Zahlungen (Abos) aus den Buchungen.

    Kriterien: mindestens ``min_events`` Zahlungen an denselben Händler im
    Fenster, ein **gleichmässiger Rhythmus** von mindestens 20 Tagen, und ein
    Betrag, der entweder in wenigen Stufen verläuft (Preiserhöhung erlaubt) oder
    insgesamt kaum streut (Variationskoeffizient ≤ ``max_cv``). Gibt die Abos
    absteigend nach Monatsbetrag zurück.
    """
    today = today or heute_lokal()
    window_start = add_months(today.replace(day=1), -window_months)

    rows = db.execute(
        select(
            Transaction.description, Transaction.amount,
            Transaction.date, Transaction.category_id,
        ).where(
            Transaction.amount < 0,
            Transaction.date >= window_start,
            not_transfer(),
        )
    ).all()

    roh: dict[str, _Gruppe] = {}
    for desc, amount, tx_date, category_id in rows:
        key = _merchant_key(desc)
        if not key:
            continue
        roh.setdefault(key, _Gruppe(key)).add(tx_date, Decimal(str(amount)), desc or "", category_id)

    # Verschieden lange Buchungstexte desselben Händlers zusammenziehen, BEVOR
    # gezählt wird — sonst scheitern beide Teilgruppen an der Mindestanzahl.
    ziel = _kanonische_schluessel(set(roh))
    groups: dict[str, _Gruppe] = {}
    for key, g in roh.items():
        k = ziel[key]
        z = groups.setdefault(k, _Gruppe(k))
        for tag, betrag in g.zahlungen:
            z.zahlungen.append((tag, betrag))
        for cat_id, n in g.category_counts.items():
            z.category_counts[cat_id] = z.category_counts.get(cat_id, 0) + n
        if g.last_date is not None and (z.last_date is None or g.last_date >= z.last_date):
            z.last_date, z.last_desc = g.last_date, g.last_desc

    dismissed = set(db.scalars(select(DismissedMerchant.merchant_key)))  # falsch erkannte ausblenden
    dismissed |= (extra_skip or set())  # bereits manuell verbundene Händler nicht doppelt erkennen
    cats = {c.id: c for c in db.scalars(select(Category))}
    result: list[Subscription] = []
    for g in groups.values():
        if _trifft_eine(dismissed, g.key):
            continue
        sub = _auswerten(g, today=today, min_events=min_events, max_cv=max_cv)
        if sub is None:
            continue
        top_cat_id = max(g.category_counts, key=g.category_counts.get) if g.category_counts else None
        sub.category = cats.get(top_cat_id) if top_cat_id else None
        result.append(sub)

    result.sort(key=lambda s: s.monthly, reverse=True)
    return result


def match_transactions(db: Session, query: str, *, window_months: int = 24) -> dict | None:
    """Findet Bankbuchungen zu einem Such-Stichwort (für „Abo aus Buchungen
    verbinden").

    Liefert neben dem kanonischen Händler-Schlüssel bewusst die **einzelnen
    Treffer** (Datum + Betrag) und den **aktuellen** Betrag statt eines Mittels
    über die Vergangenheit: bei einer Preiserhöhung beschreibt der Durchschnitt
    einen Preis, den es nie gab.
    """
    q = (query or "").strip().lower()
    if len(q) < 2:
        return None
    today = heute_lokal()
    window_start = add_months(today.replace(day=1), -window_months)
    rows = db.execute(
        select(Transaction.description, Transaction.amount, Transaction.date).where(
            Transaction.amount < 0,
            Transaction.date >= window_start,
            not_transfer(),
            enthaelt(Transaction.description, q),
        ).order_by(Transaction.date)
    ).all()
    if not rows:
        return None

    reihe = sorted((d, Decimal(str(a)).copy_abs()) for _, a, d in rows)
    key_counts: dict[str, int] = {}
    last_date, last_desc = reihe[-1][0], ""
    for desc, _amount, tx_date in rows:
        k = _merchant_key(desc)
        if k:
            key_counts[k] = key_counts.get(k, 0) + 1
        if tx_date == last_date and desc:
            last_desc = desc

    betrag, seit, vorher = _aktueller_preis(_stufen(reihe))
    abstand, _gleichmaessig = _takt([d for d, _ in reihe])
    takt = _rhythmus(abstand)
    name_rhythmus, monate_je_periode = takt if takt else ("unregelmässig", 1)
    monthly = (betrag / monate_je_periode).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "keyword": max(key_counts, key=key_counts.get) if key_counts else q,
        "name": display_name(last_desc) or query.strip()[:42],
        # Konkrete Treffer, neueste zuerst — ohne sie sagt die Vorschau nichts
        # darüber, WAS gefunden wurde.
        "hits": [{"date": d, "amount": b} for d, b in reversed(reihe)],
        "amount": betrag,                   # aktueller Betrag je Zahlung
        "interval": "jaehrlich" if monate_je_periode == 12 else "monatlich",
        "monthly": monthly,                 # Monatsäquivalent des aktuellen Betrags
        "vorher": vorher,
        "seit": seit,
        "rhythmus": name_rhythmus,
        "count": len(reihe),
        "months_seen": len({(d.year, d.month) for d, _ in reihe}),
        "first_date": reihe[0][0],
        "last_date": last_date,
    }


def connected_counts(db: Session, keywords: set[str], *, window_months: int = 24) -> dict[str, int]:
    """Anzahl Buchungen je Händler-Schlüssel (für „N verbundene Buchungen")."""
    if not keywords:
        return {}
    today = heute_lokal()
    window_start = add_months(today.replace(day=1), -window_months)
    # In SQL nach Beschreibung gruppieren: statt jede einzelne Buchungszeile zu
    # hydrieren, kommen nur die (wenigen) distinkten Texte samt Zähler zurück —
    # die Händler-Normalisierung läuft dann über Dutzende statt Tausende Zeilen.
    rows = db.execute(
        select(Transaction.description, func.count()).where(
            Transaction.amount < 0, Transaction.date >= window_start, not_transfer()
        ).group_by(Transaction.description)
    ).all()
    out: dict[str, int] = {}
    for desc, n in rows:
        k = _merchant_key(desc)
        if not k:
            continue
        for kw in keywords:
            if key_passt(kw, k):
                out[kw] = out.get(kw, 0) + n
    return out


def subscriptions_summary(subs: list[Subscription]) -> dict:
    """Gesamtsummen über alle erkannten Abos (Monat/Jahr + Anzahl)."""
    monthly = sum((s.monthly for s in subs), Decimal("0"))
    return {
        "count": len(subs),
        "monthly": monthly,
        "yearly": (monthly * 12).quantize(Decimal("0.01")),
    }
