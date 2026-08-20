"""Wer zählt als Absender — die Frage, an der die Login-Drossel hängt.

**Warum eine eigene Datei.** Der bisherige Wächter dazu stand in
``test_e3_haerten.py`` und war grün, obwohl der Weg offen war. Der Grund ist
lehrreich: im Testlauf steht kein Uvicorn zwischen Client und App.
``request.client.host`` ist dort immer ``testclient``, egal welchen
``X-Forwarded-For`` man mitschickt — der Test prüfte also eine Schicht, die es
im Betrieb so nicht gibt.

Nachgemessen am eingebauten Uvicorn 0.48.0, Header ``198.51.100.9, 203.0.113.5``:

===============================  ==============================
``--forwarded-allow-ips``        Adresse, die in der App ankommt
===============================  ==============================
``*``  (die Vorgabe)             ``198.51.100.9``  — der gefälschte
``198.51.100.0/24``                ``203.0.113.5`` — der echte
===============================  ==============================

Bei ``*`` nimmt Uvicorn den ERSTEN Eintrag, und den setzt der Klopfende selbst —
auch hinter dem Reverse-Proxy. Deshalb wertet die App den Header Seither
selbst aus, statt sich auf die Adresse im ``scope`` zu verlassen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from moneten.auth import drossel
from moneten.routers import auth_pin

# Die Drossel liegt Seither in auth/drossel.py — sie gilt jetzt
# fuer JEDE Tuer, die eine PIN prueft, nicht nur fuer den Login. Die alten
# Namen in auth_pin zeigen weiterhin auf dieselben Objekte; die Einstellung
# wird aber dort gelesen, wo sie benutzt wird.


@pytest.fixture(autouse=True)
def _drossel_leeren():
    auth_pin._clear_failures()
    yield
    auth_pin._clear_failures()


def _falsche_pin(client: TestClient, headers: dict | list | None = None):
    return client.post("/login", data={"pin": "000000"}, headers=headers)


def _gezaehlter_schluessel() -> str:
    """Unter welchem Namen die Drossel den Fehlversuch verbucht hat."""
    assert len(auth_pin._fail_times) == 1, dict(auth_pin._fail_times)
    return next(iter(auth_pin._fail_times))


# ---------------------------------------------------------------------------
# Welcher Eintrag zählt
# ---------------------------------------------------------------------------
def test_der_vom_proxy_angehaengte_wert_zaehlt(client: TestClient) -> None:
    """Der Proxy hängt seine Sicht HINTEN an — also ist der letzte Eintrag der
    einzige, den der Klopfende nicht selbst bestimmt."""
    _falsche_pin(client, {"X-Forwarded-For": "198.51.100.9, 203.0.113.5"})
    assert _gezaehlter_schluessel() == "203.0.113.5"


def test_beliebig_viele_erfundene_eintraege_helfen_nicht(client: TestClient) -> None:
    """Die Liste lässt sich nach links beliebig auffüllen — rechts steht trotzdem
    der Proxy."""
    _falsche_pin(client, {"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.5"})
    assert _gezaehlter_schluessel() == "203.0.113.5"


def test_ein_gefaelschter_erster_eintrag_sprengt_die_drossel_nicht(client: TestClient) -> None:
    """Der gemessene Angriff: pro Versuch ein neuer erster Eintrag.

    Genau so lief er vorher ins Leere — jeder Versuch bekam einen eigenen
    Zähler. Mit dem angehängten Wert des Proxys landen alle auf demselben.
    """
    letzte = None
    for i in range(auth_pin._FAIL_MAX + 2):
        letzte = _falsche_pin(client, {"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.5"})
    assert letzte is not None and letzte.status_code == 429, (
        f"Nach {auth_pin._FAIL_MAX + 2} Versuchen antwortet die App mit "
        f"{letzte.status_code if letzte else '?'} — die Drossel greift nicht."
    )


def test_zwei_echte_absender_sperren_sich_nicht_gegenseitig(client: TestClient) -> None:
    """Die frühere Entscheidung bleibt gültig: gezählt wird JE Absender.

    Eine gemeinsame Zählung war selbst eine Lücke — zehn falsche PINs eines
    Fremden sperrten den Betreiber aus.
    """
    for _ in range(auth_pin._FAIL_MAX + 2):
        _falsche_pin(client, {"X-Forwarded-For": "203.0.113.5"})
    anderer = _falsche_pin(client, {"X-Forwarded-For": "203.0.113.8"})
    assert anderer.status_code != 429, "Ein fremder Absender sperrt den zweiten mit"


def test_mehrere_kopfzeilen_werden_zusammengezogen(client: TestClient) -> None:
    """Nicht jeder Proxy hängt an die bestehende Zeile an; manche setzen eine
    zweite. Ohne das Zusammenziehen zählte dann die erste Zeile — die des
    Klopfenden."""
    _falsche_pin(client, [("X-Forwarded-For", "198.51.100.9"), ("X-Forwarded-For", "203.0.113.5")])
    assert _gezaehlter_schluessel() == "203.0.113.5"


def test_ohne_header_zaehlt_die_verbindung(client: TestClient) -> None:
    _falsche_pin(client)
    assert _gezaehlter_schluessel() == "testclient"


# ---------------------------------------------------------------------------
# Die Einstellung
# ---------------------------------------------------------------------------
def test_zwei_proxys_zaehlen_einen_schritt_weiter_links(client: TestClient, monkeypatch) -> None:
    """Bei zwei Proxys hängen ZWEI Werte an: der äusserste Proxy trägt den
    Absender ein, der innere die Adresse des äusseren."""
    monkeypatch.setattr(drossel.settings, "proxy_hops", 2)
    _falsche_pin(client, {"X-Forwarded-For": "198.51.100.9, 203.0.113.5, 203.0.113.9"})
    assert _gezaehlter_schluessel() == "203.0.113.5"


def test_ohne_proxy_wird_der_header_gar_nicht_angesehen(client: TestClient, monkeypatch) -> None:
    """``MONETEN_PROXY_HOPS=0`` ist die Aussage „vor mir steht niemand".

    Dann ist jeder Wert im Header eine Behauptung des Absenders, und die App
    sieht ihn nicht an.
    """
    monkeypatch.setattr(drossel.settings, "proxy_hops", 0)
    _falsche_pin(client, {"X-Forwarded-For": "198.51.100.9, 203.0.113.5"})
    assert _gezaehlter_schluessel() == "testclient"


def test_kuerzere_liste_als_angegeben_weicht_nach_links_aus(client: TestClient, monkeypatch) -> None:
    """Steht weniger drin als erwartet, wird nicht über den Rand hinaus gegriffen.

    Nach links auszuweichen ist die sichere Richtung: dort steht im Zweifel der
    Proxy. Ohne diese Grenze wäre ``eintraege[-3]`` bei zwei Einträgen der
    ERSTE — also wieder der Wert des Klopfenden.
    """
    monkeypatch.setattr(drossel.settings, "proxy_hops", 3)
    _falsche_pin(client, {"X-Forwarded-For": "198.51.100.9, 203.0.113.5"})
    assert _gezaehlter_schluessel() == "198.51.100.9"


# ---------------------------------------------------------------------------
# Warum wir uns nicht auf Uvicorn verlassen
# ---------------------------------------------------------------------------
def test_uvicorn_nimmt_bei_stern_den_ersten_eintrag() -> None:
    """Der Messwert, auf dem der ganze Umbau steht — festgenagelt.

    Ändert eine künftige Uvicorn-Fassung diese Auswahl, soll das hier auffallen
    und nicht im Betrieb. Die App hängt nicht davon ab; sie wertet selbst aus.
    """
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    kopf = "198.51.100.9, 203.0.113.5"
    assert _TrustedHosts("*").get_trusted_client_address(kopf)[0] == "198.51.100.9"
    assert _TrustedHosts("198.51.100.0/24").get_trusted_client_address(kopf)[0] == "203.0.113.5"
