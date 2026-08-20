"""Ein Artikel, eine Schreibweise — und die App merkt sie sich.

**Das Problem.** Dieselbe Ware kommt auf jedem Beleg leicht anders an: die
Erkennung liest „FEFENTEROL" statt „Perenterol", „Muitifruchtsaft" statt
„Multifruchtsaft". Wer das im Editor richtigstellt, hat es beim nächsten Beleg
wieder zu tun — und der Preisverlauf führt denselben Artikel unter drei Namen,
also drei Verläufe mit je einem Punkt.

**Die Lösung in zwei Hälften, beide hier:**

1. :func:`lerne` hält fest, dass eine gelesene Schreibweise für eine bestätigte
   steht. Gelernt wird an der Stelle, an der ohnehin korrigiert wird — im
   Beleg-Editor. Kein zusätzlicher Schritt, keine Pflege einer Liste.
2. :func:`anwenden` setzt das beim nächsten Scan um, **bevor** die Position im
   Editor erscheint.

**Was hier NICHT passiert: raten.** Angewendet wird ausschliesslich, was jemand
selbst einmal bestätigt hat. Ähnliche Namen werden vorgeschlagen
(:func:`buendel`), aber nie im Stillen zusammengelegt — zwei Artikel dürfen sich
ähnlich heissen, und ein automatisch verschmolzener Preisverlauf wäre falsch,
ohne dass es jemandem auffällt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import ArtikelAlias, Attachment
from moneten.services.price_history import artikel_schluessel

# Ab dieser Ähnlichkeit gelten zwei Schreibweisen als Vorschlag fürs selbe Ding.
# 0.82 ist gemessen und nicht geraten. Ein Lesefehler im selben Namen
# („vollkornbrot gross" ↔ „vo11kornbrot gross", l→1) liegt bei 0.89, zwei
# wirklich verschiedene Waren („tomaten cherry" ↔ „tomaten passiert") bei 0.67.
# Dazwischen ist Platz.
#
# (Hier standen Frueher zwei echte Artikelnamen aus dem Bestand,
# darunter ein Medikament. Beim Nachmessen fiel ausserdem auf, dass die zweite
# dort genannte Zahl nicht stimmte — 0.57, nicht 0.67. Beide Beispiele sind
# jetzt erfunden UND nachgerechnet.)
AEHNLICH_AB = 0.82

# Unterhalb dieser Länge wird nicht mehr über Ähnlichkeit verglichen: bei drei
# Zeichen ist jeder zweite Name zu 0.8 ähnlich, und aus „Ei" würde „Eis".
MIN_LAENGE = 5


def schluessel(name: str) -> str:
    """Vergleichs-Schlüssel eines Positionsnamens.

    Dieselbe Normalisierung wie im Preisverlauf — zwei Schlüssel für dasselbe
    wären zwei Wahrheiten, und die Bereinigung soll genau das zusammenlegen, was
    dort auch zusammenfällt.
    """
    return artikel_schluessel(name)


def alias_karte(db: Session) -> dict[str, str]:
    """Alle bestätigten Zuordnungen als ``{gelesener Schlüssel: richtiger Name}``.

    Einmal geladen statt je Position gefragt: eine Quittung hat zwanzig Zeilen,
    und die Karte ist ein paar Dutzend Einträge gross.
    """
    return {a.alias_key: a.kanonisch for a in db.scalars(select(ArtikelAlias))}


def anwenden(name: str, karte: dict[str, str]) -> str:
    """Die bestätigte Schreibweise — oder der Name unverändert."""
    return karte.get(schluessel(name), name)


def lerne(db: Session, gelesen: str, bestaetigt: str) -> bool:
    """Merkt sich, dass ``gelesen`` für ``bestaetigt`` steht. True, wenn neu.

    **Nicht gelernt wird**, wenn beide auf denselben Schlüssel fallen (dann ist
    es keine andere Schreibweise, sondern dieselbe) oder wenn der bestätigte
    Name leer ist. Und nicht, wenn die Namen einander unähnlich sind: wer eine
    Position komplett umbenennt — „Ware 3" wird zu „Milch" —, meint diesen einen
    Beleg, nicht eine Regel für alle künftigen.
    """
    gelesen, bestaetigt = (gelesen or "").strip(), (bestaetigt or "").strip()
    if not gelesen or not bestaetigt:
        return False
    k_gelesen, k_bestaetigt = schluessel(gelesen), schluessel(bestaetigt)
    if not k_gelesen or k_gelesen == k_bestaetigt:
        return False
    if not aehnlich(gelesen, bestaetigt):
        return False
    vorhanden = db.scalar(select(ArtikelAlias).where(ArtikelAlias.alias_key == k_gelesen))
    if vorhanden is not None:
        vorhanden.kanonisch = bestaetigt
        return False
    db.add(ArtikelAlias(alias_key=k_gelesen, kanonisch=bestaetigt))
    db.flush()
    return True


def vergleichstext(name: str) -> str:
    """Kleinbuchstaben, nur Buchstaben und Ziffern — **Reihenfolge erhalten**.

    Ausdrücklich NICHT :func:`schluessel`: der sortiert die Wörter alphabetisch,
    damit „Bio Butter" und „Butter Bio" zusammenfallen. Für den
    Ähnlichkeitsvergleich ist das verheerend — ein falscher ANFANGSBUCHSTABE
    verschiebt die Sortierung und macht aus zwei fast gleichen Namen zwei ganz
    verschiedene Zeichenketten. Gemessen: „Fusterol Kaps" und „Musterol
    Kaps" ergeben sortiert „fusterol kaps" und „kaps musterol" — Ähnlichkeit
    0.53 statt 0.87.
    """
    return " ".join(re.sub(r"[^0-9a-zà-ÿ]+", " ", (name or "").lower()).split())


def aehnlich(a: str, b: str) -> bool:
    """Meinen zwei Namen vermutlich dasselbe?

    Verglichen wird der Text in seiner Reihenfolge (:func:`vergleichstext`),
    nicht der sortierte Schlüssel.
    """
    ta, tb = vergleichstext(a), vergleichstext(b)
    if len(ta) < MIN_LAENGE or len(tb) < MIN_LAENGE:
        return ta == tb
    return SequenceMatcher(None, ta, tb).ratio() >= AEHNLICH_AB


# ---------------------------------------------------------------------------
# Bereinigung des Bestands
# ---------------------------------------------------------------------------


@dataclass
class Buendel:
    """Mehrere Schreibweisen, die vermutlich dieselbe Ware meinen."""

    # Die häufigste Schreibweise — der naheliegende Vorschlag.
    vorschlag: str
    # [(Schreibweise, wie oft sie vorkommt)], häufigste zuerst.
    varianten: list[tuple[str, int]] = field(default_factory=list)

    @property
    def gesamt(self) -> int:
        return sum(n for _s, n in self.varianten)

    @property
    def eindeutig(self) -> bool:
        """Gibt es eine Schreibweise, die haeufiger ist als alle anderen?

        Nur dann darf die Oberflaeche eine vorauswaehlen. Bei Gleichstand ist die
        Reihenfolge alphabetisch — und damit gewinnt der Zufall: gemessen stand
        „Muitifruchtsaft" vor „Multifruchtsaft", beide einmal erfasst. Ein
        unbedachter Klick haette den ganzen Bestand auf den Lesefehler gelegt.
        """
        return len(self.varianten) > 1 and self.varianten[0][1] > self.varianten[1][1]


def _alle_positionen(db: Session) -> list[tuple[Attachment, dict]]:
    """Jeder Anhang mit seinen gespeicherten Daten — einmal geladen."""
    out = []
    for att in db.scalars(select(Attachment).where(Attachment.parsed_items_json.isnot(None))):
        try:
            daten = json.loads(att.parsed_items_json or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(daten, dict) and daten.get("items"):
            out.append((att, daten))
    return out


def buendel(db: Session) -> list[Buendel]:
    """Schreibweisen, die vermutlich dasselbe meinen — häufigste Gruppe zuerst.

    Zwei Stufen, weil sie verschiedene Fehler fangen:

    * **Gleicher Schlüssel** — „Bio Butter" und „Butter Bio". Das ist sicher
      dasselbe; der Schlüssel sortiert die Wörter.
    * **Ähnlicher Schlüssel** — „Perenterol" und „Fefenterol". Das ist ein
      Lesefehler und nur *wahrscheinlich* dasselbe. Darum ein Vorschlag, keine
      automatische Zusammenlegung.

    Zurück kommen nur Gruppen mit mehr als einer Schreibweise: eine einzelne ist
    nichts zu entscheiden.
    """
    zaehler: dict[str, dict[str, int]] = {}
    for _att, daten in _alle_positionen(db):
        for eintrag in daten.get("items") or []:
            name = (eintrag.get("name") or "").strip()
            if not name:
                continue
            zaehler.setdefault(schluessel(name), {}).setdefault(name, 0)
            zaehler[schluessel(name)][name] += 1

    # Ähnliche Schlüssel zusammenziehen. Der erste Schlüssel einer Gruppe zieht
    # die weiteren an sich; die Reihenfolge ist die Häufigkeit, damit der
    # gebräuchlichste Schlüssel den Ton angibt.
    # Verglichen wird je ein VERTRETER der Gruppe — die häufigste Schreibweise
    # unter diesem Schlüssel. Schlüssel gegeneinander zu halten ginge nicht:
    # sie sind alphabetisch sortiert (siehe :func:`vergleichstext`).
    def vertreter(k: str) -> str:
        return max(zaehler[k].items(), key=lambda p: p[1])[0]

    reihenfolge = sorted(zaehler, key=lambda k: -sum(zaehler[k].values()))
    gruppen: list[list[str]] = []
    for k in reihenfolge:
        for g in gruppen:
            if aehnlich(vertreter(g[0]), vertreter(k)):
                g.append(k)
                break
        else:
            gruppen.append([k])

    out: list[Buendel] = []
    for g in gruppen:
        namen: dict[str, int] = {}
        for k in g:
            for name, n in zaehler[k].items():
                namen[name] = namen.get(name, 0) + n
        if len(namen) < 2:
            continue
        varianten = sorted(namen.items(), key=lambda p: (-p[1], p[0]))
        out.append(Buendel(vorschlag=varianten[0][0], varianten=varianten))
    return sorted(out, key=lambda b: -b.gesamt)


def vereinheitliche(db: Session, kanonisch: str, varianten: list[str]) -> int:
    """Schreibt alle genannten Schreibweisen auf ``kanonisch`` um. Zahl der Änderungen.

    Zwei Wirkungen, und beide sind gewollt:

    * Der **Bestand** wird umgeschrieben — sonst führte der Preisverlauf den
      Artikel weiter unter drei Namen, egal was künftig passiert.
    * Für jede Variante entsteht ein **Alias**, damit derselbe Lesefehler beim
      nächsten Beleg gar nicht erst im Editor landet.
    """
    kanonisch = (kanonisch or "").strip()
    if not kanonisch:
        return 0
    ziel_key = schluessel(kanonisch)
    zu_ersetzen = {schluessel(v) for v in varianten if v.strip()} - {""}
    if not zu_ersetzen:
        return 0

    geaendert = 0
    for att, daten in _alle_positionen(db):
        beruehrt = False
        for eintrag in daten.get("items") or []:
            name = (eintrag.get("name") or "").strip()
            if name and name != kanonisch and schluessel(name) in zu_ersetzen:
                eintrag["name"] = kanonisch
                beruehrt = True
                geaendert += 1
        if beruehrt:
            att.parsed_items_json = json.dumps(daten, ensure_ascii=False)

    for key in zu_ersetzen - {ziel_key}:
        vorhanden = db.scalar(select(ArtikelAlias).where(ArtikelAlias.alias_key == key))
        if vorhanden is None:
            db.add(ArtikelAlias(alias_key=key, kanonisch=kanonisch))
        else:
            vorhanden.kanonisch = kanonisch
    db.commit()
    return geaendert
