"""Geht der Kontoauszug in sich auf?

Der Import vergleicht schon den Kontostand der App mit dem Schlusssaldo der Bank.
Das beantwortet aber eine andere Frage: weicht er ab, kann das an fehlenden
Altdaten liegen, an übersprungenen Duplikaten oder an einem Konto, das man
manuell korrigiert hat. Man weiss nur, dass etwas nicht passt — nicht was.

Diese Prüfung schaut nur in die Datei selbst. Ein CAMT.053-Auszug liefert
Anfangssaldo (OPBD) und Schlusssaldo (CLBD) mit; dazwischen stehen die
Buchungen. Also muss gelten:

    Anfangssaldo + Summe der Buchungen = Schlusssaldo

Das ist keine Heuristik und kein Schätzwert — die Bank liefert die richtige
Antwort mit, man muss sie nur nachrechnen. Geht die Rechnung nicht auf, hat der
Parser Buchungen verloren oder doppelt gelesen, oder die Datei ist unvollständig.
Das ist ein Fehler *vor* dem Import und muss anders aussehen als „dein Konto
steht anders da als erwartet".

Ohne OPBD oder CLBD gibt es kein Urteil — manche Institute liefern nur einen der
beiden. Dann steht dort ``None``, nicht „geprüft und in Ordnung".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from moneten.services.camt053_parser import Camt053Statement

# Rundungsspielraum. Ein Rappen deckt Darstellungsdifferenzen ab; alles darüber
# ist eine fehlende Buchung, kein Rundungsfehler.
TOLERANZ = Decimal("0.01")


@dataclass
class AuszugsPruefung:
    """Ergebnis der Selbstprüfung eines Auszugs."""

    pruefbar: bool           # OPBD und CLBD beide vorhanden?
    stimmt: bool | None      # None, wenn nicht prüfbar
    anfang: Decimal | None
    schluss: Decimal | None
    summe: Decimal           # Summe der Buchungen in der Datei
    erwartet: Decimal | None  # anfang + summe
    differenz: Decimal | None
    anzahl: int

    @property
    def hinweis(self) -> str:
        """Ein Satz, der sagt, was zu tun ist."""
        if not self.pruefbar:
            return (
                "Die Datei enthält keinen Anfangs- oder Schlusssaldo — der Auszug "
                "lässt sich nicht gegen sich selbst prüfen."
            )
        if self.stimmt:
            return (
                f"Anfangssaldo plus {self.anzahl} Buchung"
                f"{'en' if self.anzahl != 1 else ''} ergeben genau den Schlusssaldo."
            )
        return (
            f"Anfangssaldo plus die {self.anzahl} gelesenen Buchungen ergeben "
            f"{self.erwartet}, die Bank nennt aber {self.schluss} — Differenz "
            f"{self.differenz}. In der Datei fehlen Buchungen, oder sie wurden "
            "falsch gelesen. Der Import ist damit unvollständig."
        )


def pruefe_auszug(st: Camt053Statement) -> AuszugsPruefung:
    """Rechnet nach, ob Anfangssaldo + Buchungen den Schlusssaldo ergeben."""
    summe = sum((e.amount for e in st.entries), Decimal("0"))
    anfang, schluss = st.opening_balance, st.closing_balance

    if anfang is None or schluss is None:
        return AuszugsPruefung(
            pruefbar=False, stimmt=None, anfang=anfang, schluss=schluss,
            summe=summe, erwartet=None, differenz=None, anzahl=len(st.entries),
        )

    erwartet = anfang + summe
    differenz = schluss - erwartet
    return AuszugsPruefung(
        pruefbar=True, stimmt=abs(differenz) <= TOLERANZ,
        anfang=anfang, schluss=schluss, summe=summe,
        erwartet=erwartet, differenz=differenz, anzahl=len(st.entries),
    )
