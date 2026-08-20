"""Die Warteschleife des Deploys muss unter Windows PowerShell 5.1 laufen.

**Warum es das gibt.** Zweimal hintereinander meldete der Deploy „Die App
antwortet nach 60s noch nicht", während sie längst antwortete. Beide Male lag es
nicht am NAS, sondern an der Prüfung selbst — und beide Male lautlos, weil ein
leeres ``catch {}`` den echten Fehler schluckte:

1. ``-SkipCertificateCheck`` gibt es erst ab PowerShell 6. In 5.1 scheitert
   schon die Parameterbindung, bevor überhaupt eine Anfrage rausgeht.
2. ``ServerCertificateValidationCallback = { $true }`` — ein Skriptblock als
   Rückruf lässt sich aus dem IO-Thread nicht ausführen. Der TLS-Handschlag
   bricht ab, und zwar für JEDE Adresse, auch die mit gültigem Zertifikat.

Das Skript läuft nicht in der Testsuite, es ist PowerShell. Prüfbar ist aber,
dass diese beiden Konstrukte nicht zurückkommen — und dass der Fehler nicht
wieder verschluckt wird.

**In einem Klon dieses Repositorys werden diese Tests übersprungen.**
``deploy.ps1`` gehört zum Arbeitsordner des Autors und wird bewusst nicht
mitgeliefert (es beschreibt genau einen NAS). Das ist kein Defekt und kein
fehlendes Stück Installation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKRIPT = Path(__file__).resolve().parents[2] / "deploy.ps1"


def _ohne_kommentare(ps: str) -> str:
    """PowerShell-Kommentare raus — ``#`` bis Zeilenende und ``<# … #>``.

    Ohne das prueft der Test seine eigene Begruendung mit: die verbotenen
    Konstrukte stehen im Kommentar daneben, weil dort erklaert wird, WARUM es
    sie nicht mehr gibt.
    """
    ohne_block = re.sub(r"<#.*?#>", "", ps, flags=re.S)
    return "\n".join(re.sub(r"#.*$", "", zeile) for zeile in ohne_block.splitlines())


@pytest.fixture(scope="module")
def deploy() -> str:
    if not SKRIPT.is_file():
        pytest.skip(
            "deploy.ps1 gehört zum Arbeitsordner des Autors und wird nicht "
            "mitgeliefert — in einem Klon ist das der Normalfall."
        )
    return _ohne_kommentare(SKRIPT.read_text(encoding="utf-8"))


def test_kein_parameter_aus_powershell_6(deploy: str) -> None:
    """``-SkipCertificateCheck`` kennt Windows PowerShell 5.1 nicht."""
    assert "-SkipCertificateCheck" not in deploy, (
        "Der Parameter existiert erst ab PowerShell 6. Unter 5.1 scheitert die "
        "Bindung, und die Wartepruefung meldet eine laufende App als tot."
    )


def test_kein_skriptblock_als_zertifikats_rueckruf(deploy: str) -> None:
    """Er bricht den Handschlag ab — auch dort, wo das Zertifikat gültig ist."""
    treffer = re.search(r"ServerCertificateValidationCallback\s*=\s*\{", deploy)
    assert not treffer, (
        "Ein Skriptblock als Zertifikats-Rueckruf laesst sich aus dem IO-Thread "
        "nicht ausfuehren; die Verbindung stirbt mit 'Unerwarteter Fehler beim "
        "Senden' - fuer JEDE Adresse, nicht nur die selbstsignierte."
    )


def test_der_fehler_wird_nicht_verschluckt(deploy: str) -> None:
    """Ohne festgehaltene Meldung sieht ein Skriptfehler wie ein kaputter Deploy aus.

    Genau das war zweimal der Fall: 60 Sekunden Warten, dann eine Warnung über
    den NAS — während der eigentliche Fehler zwei Zeilen weiter oben im
    ``catch`` verschwand.
    """
    block = deploy[deploy.index("$bereit = $false"):]
    block = block[:block.index('CT "  App: $AppUrl" Text')]
    assert "$letzterFehler = $_.Exception.Message" in block, (
        "Die Warteschleife merkt sich den Fehler nicht mehr."
    )
    assert "Letzte Meldung: $letzterFehler" in block, (
        "Der gemerkte Fehler wird nie ausgegeben — dann kann man ihn auch weglassen."
    )
    # Nur die Schleife selbst. Das leere `catch` am Setzen des TLS-Protokolls
    # daneben ist in Ordnung: schlaegt es fehl, wird trotzdem gefragt.
    schleife = block[block.index("foreach ($i in 1..30)"):]
    schleife = schleife[:schleife.index("\nif ($bereit)")]
    assert not re.search(r"catch\s*\{\s*\}", schleife), (
        "Ein leeres catch in der Warteschleife: genau der Mechanismus, der die "
        "beiden bisherigen Fehler unsichtbar gemacht hat."
    )


def test_geprueft_wird_die_adresse_die_danach_aufgeht(deploy: str) -> None:
    """Der Healthcheck fragt den Weg, den auch der Browser nimmt.

    Sonst meldet das Skript „bereit", waehrend die Adresse im Browser nicht
    erreichbar ist — oder umgekehrt.
    """
    block = deploy[deploy.index("$bereit = $false"):]
    block = block[:block.index('CT "  App: $AppUrl" Text')]
    assert '"$AppUrl/health"' in block
    assert "$AppUrlLan/health" not in block, (
        "Der LAN-Weg hat ein selbstsigniertes Zertifikat; ihn zu pruefen "
        "verlangt genau den Rueckruf, der den Handschlag zerlegt."
    )


def test_gebaut_wird_vor_dem_tauschen(deploy: str) -> None:
    """Ein fehlgeschlagener Bau darf die laufende App nicht mitnehmen.

    Vorher stand ``docker rm -f`` VOR dem Bauen: der alte Container war weg,
    bevor der neue existierte. Scheiterte der Bau — an einem Paket, einer
    Prüfsumme oder am Speicher der NAS —, war die App weg und der alte Stand auf
    dem NAS bereits überschrieben. Der Bau ist der langsame und der einzige
    Schritt, der wirklich fehlschlagen kann; er gehört vor den Tausch.
    """
    bau = deploy.find("$DC build")
    weg = deploy.find("rm -f bilanz")
    hoch = deploy.find("$DC up -d")
    assert bau != -1, "Es gibt keinen eigenen Bau-Schritt mehr"
    assert weg != -1, "Der Schritt, der den alten Container entfernt, fehlt"
    assert bau < weg, "Gebaut wird nach dem Entfernen — ein Fehlschlag nimmt die App mit"
    assert weg < hoch, "Der neue Container startet, bevor der alte weg ist (Port-Konflikt)"
    assert "up -d --build" not in deploy, (
        "``up -d --build`` baut wieder im selben Schritt — dann ist die Reihenfolge wirkungslos"
    )
