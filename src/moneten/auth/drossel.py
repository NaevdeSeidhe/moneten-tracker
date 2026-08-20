"""Die Bremse gegen durchprobierte PINs — an EINER Stelle, für alle Türen.

**Warum sie hier steht und nicht im Login-Router.** Dort galt sie für genau eine
Route, und jede weitere Stelle, die eine PIN prüft, hätte ihre eigene gebraucht.
Eine Regel, die nur an einer von mehreren Türen gilt, ist keine Regel.

**Was gezählt wird.** Fehlversuche je Absender, nicht global. Eine gemeinsame
Zählung war selbst eine Lücke: wer den Port erreicht, sperrt mit zehn falschen
PINs den Betreiber aus und hält ihn mit zwei Versuchen pro Minute dauerhaft
draussen. Gemessen — danach antwortete auch die richtige PIN mit 429.
"""

from __future__ import annotations

import time

from fastapi import Request

from moneten.config import settings

#: Fünf Minuten Fenster, zehn Versuche darin.
FENSTER_SEKUNDEN = 300
MAX_VERSUCHE = 10

#: Mehr Schlüssel als das: der älteste fliegt raus. Ohne Deckel wuchs die
#: Zuordnung unbegrenzt — bei frei wählbarem Schlüssel war das ein Weg, den
#: Container gegen sein Speicherlimit zu fahren.
MAX_SCHLUESSEL = 512

_fehlversuche: dict[str, list[float]] = {}


def absender(request: Request) -> str:
    """Wer klopft — und zwar der Wert, den der Klopfende NICHT selbst wählt.

    **Der Weg, der offen war.** Die Drossel zählt je Absender. Steht dort ein
    Wert, den der Absender bestimmt, zählt sie nichts: eine neue Zufallszahl pro
    Versuch, und jeder Fehlversuch bekommt seinen eigenen Zähler. Übrig bleibt
    Argon2 gegen eine Million PINs.

    **Warum ``request.client.host`` nicht genügt — nachgemessen.** Mit
    ``--forwarded-allow-ips="*"`` ersetzt Uvicorn 0.48.0 die Adresse durch den
    **ersten** Eintrag von ``X-Forwarded-For``, und den setzt der Absender::

        Header:  X-Forwarded-For: 198.51.100.9, 203.0.113.5
        *                  -> 198.51.100.9   (gefälscht)
        198.51.100.0/24    -> 203.0.113.5    (echt)

    Das gilt AUCH hinter dem Reverse-Proxy. Die Suite hat das nie gesehen: im
    Testlauf steht kein Uvicorn dazwischen, ``request.client.host`` ist dort
    schlicht ``testclient``. Ein grüner Test war hier kein Beweis.

    **Was jetzt zählt.** Ein Reverse-Proxy hängt seine Sicht HINTEN an die Liste
    an. Der letzte Eintrag stammt also vom Proxy und nicht vom Absender — und
    genau der wird genommen. Stehen mehrere Proxys hintereinander, sagt
    ``MONETEN_PROXY_HOPS``, wie viele es sind.

    **Die Grenze, offen ausgesprochen:** kommt jemand OHNE Proxy an die App,
    ist sein selbst gesetzter Wert der einzige in der Liste, und er gewinnt.
    Dagegen hilft keine Auswertung, sondern nur, dass dieser Weg zu ist
    (``docker-compose.yml`` bindet auf ``127.0.0.1``). Mit
    ``MONETEN_PROXY_HOPS=0`` wird der Header gar nicht erst angesehen.
    """
    hops = settings.proxy_hops
    if hops > 0:
        # Mehrfach gesetzte Kopfzeilen zusammenziehen: manche Proxys hängen
        # nicht an die bestehende Zeile an, sondern setzen eine zweite.
        eintraege = [
            teil.strip()
            for zeile in request.headers.getlist("x-forwarded-for")
            for teil in zeile.split(",")
            if teil.strip()
        ]
        if eintraege:
            # Weniger Einträge als Proxys: dann ist der linkeste alles, was da
            # ist. Nach links auszuweichen ist die sichere Richtung — dort steht
            # im Zweifel der Proxy und nicht der Absender.
            return eintraege[-hops] if len(eintraege) >= hops else eintraege[0]
    return request.client.host if request.client else "unbekannt"


def zu_viele_versuche(wer: str) -> bool:
    grenze = time.monotonic() - FENSTER_SEKUNDEN
    versuche = [t for t in _fehlversuche.get(wer, []) if t > grenze]
    if versuche:
        _fehlversuche[wer] = versuche
    else:
        _fehlversuche.pop(wer, None)
    return len(versuche) >= MAX_VERSUCHE


def fehlversuch_merken(wer: str) -> None:
    _fehlversuche.setdefault(wer, []).append(time.monotonic())
    if len(_fehlversuche) > MAX_SCHLUESSEL:
        # Aufgeräumt wird sonst nur der gerade abgefragte Schlüssel; alle
        # anderen blieben für immer liegen. Erst die abgelaufenen weg, und wenn
        # das nicht reicht, die ältesten.
        grenze = time.monotonic() - FENSTER_SEKUNDEN
        for schluessel in [k for k, v in _fehlversuche.items() if not any(t > grenze for t in v)]:
            _fehlversuche.pop(schluessel, None)
        while len(_fehlversuche) > MAX_SCHLUESSEL:
            aeltester = min(_fehlversuche, key=lambda k: max(_fehlversuche[k]))
            _fehlversuche.pop(aeltester, None)


def zuruecksetzen(wer: str | None = None) -> None:
    if wer is None:
        _fehlversuche.clear()
    else:
        _fehlversuche.pop(wer, None)
