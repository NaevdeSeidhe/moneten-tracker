"""Seed-Daten gemäss Abschnitt 6 und 7 des Konzepts.

Diese Funktionen sind idempotent: sie prüfen, ob bereits Datensätze existieren,
und legen nur fehlende an. Damit ist es sicher, ``seed_all`` bei jedem Start
aufzurufen — ohne Duplikate.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import NamedTuple

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.orm import Session

from moneten.db.models import (
    Account,
    AccountType,
    Category,
    ManagementType,
    MetricCadence,
    MetricKind,
    MetricSeries,
    MetricUnit,
    SeedMarke,
    User,
)
from moneten.themes import DEFAULT_THEME

logger = logging.getLogger(__name__)
_hasher = PasswordHasher()


# ---------------------------------------------------------------------------
# Standard-User mit initialer PIN
# ---------------------------------------------------------------------------


def _start_pin() -> str:
    """Die konfigurierte Start-PIN — oder eine gewuerfelte, die einmal gemeldet wird.

    Hier und nur hier, weil hier der Benutzer entsteht. Frueher wuerfelte ein
    Validator der Konfiguration: der laeuft in JEDEM Prozess, der sie importiert,
    beim Start also in den Migrationen UND in der App. Im Protokoll standen dann
    zwei verschiedene Start-PINs untereinander, und die zuerst genannte war tot.
    """
    from moneten.config import settings, start_pin_erzeugen

    if settings.initial_pin:
        return settings.initial_pin
    settings.initial_pin = start_pin_erzeugen()
    return settings.initial_pin


def seed_user(db: Session) -> User:
    """Legt den Single-User an, falls noch nicht vorhanden."""
    user = db.scalar(select(User).where(User.id == 1))
    if user is not None:
        return user

    user = User(
        id=1,
        # Anzeigename der einen Person, die diese App benutzt. Nur der
        # Startwert; er laesst sich in den Einstellungen aendern.
        name="Odysseus",  # Platzhalter aus der Sage — siehe Einstellungen
        pin_hash=_hasher.hash(_start_pin()),
        preferred_theme=DEFAULT_THEME,
    )
    db.add(user)
    db.flush()
    logger.info("[seeds] Standard-User angelegt")
    return user


# ---------------------------------------------------------------------------
# Konten (Abschnitt 7)
# ---------------------------------------------------------------------------

# Tupel-Reihenfolge: (name, type, currency, is_active, sort_order, icon)
# Die tatsächlich geführten Konten.
DEFAULT_ACCOUNTS: list[tuple[str, AccountType, str, bool, int, str]] = [
    ("Privatkonto", AccountType.BANK, "CHF", True, 10, "building-bank"),
    ("Geldkassette", AccountType.CASH, "CHF", True, 20, "cash"),
    ("Sparkonto", AccountType.SAVINGS, "CHF", True, 30, "pig-money"),
    ('Sparkonto "Ferien"', AccountType.SAVINGS, "CHF", True, 40, "beach"),
    ("Säule 3a", AccountType.SAVINGS, "CHF", True, 50, "shield-lock"),
    # Strukturell vorgesehen — initial inaktiv markiert, Saldo bleibt 0.
    ("Crypto", AccountType.CRYPTO, "CHF", False, 90, "currency-bitcoin"),
    ("Aktien", AccountType.STOCKS, "CHF", False, 91, "trending-up"),
]


def seed_accounts(db: Session) -> None:
    """Legt die sechs Standard-Konten an, falls leer."""
    existing = db.scalar(select(Account).limit(1))
    if existing is not None:
        return

    for name, acc_type, currency, active, order, icon in DEFAULT_ACCOUNTS:
        db.add(
            Account(
                name=name,
                type=acc_type,
                currency=currency,
                opening_balance=Decimal("0"),
                current_balance=Decimal("0"),
                is_active=active,
                sort_order=order,
                icon=icon,
            )
        )
    db.flush()
    logger.info("[seeds] %d Standard-Konten angelegt", len(DEFAULT_ACCOUNTS))


# ---------------------------------------------------------------------------
# Kategorien (Abschnitt 6) — hierarchisch
# ---------------------------------------------------------------------------

# Struktur:  (Top-Level-Name, management_type, icon, [
#                (Sub-Name, management_type, icon, is_subscription),
#                ...
#            ])
DEFAULT_CATEGORIES: list[tuple[str, ManagementType, str, list[tuple[str, ManagementType, str, bool]]]] = [
    (
        "Einnahmen",
        ManagementType.EINKOMMEN,
        "arrow-down-right",
        [
            ("Nettolohn", ManagementType.EINKOMMEN, "briefcase", False),
            ("Bonus", ManagementType.EINKOMMEN, "gift", False),
            ("Sonstige Einnahmen", ManagementType.EINKOMMEN, "plus", False),
        ],
    ),
    (
        "Wohnen",
        ManagementType.DAUERAUFTRAG,
        "home",
        [
            ("Miete", ManagementType.DAUERAUFTRAG, "home", False),
            ("Strom / Heizung", ManagementType.DAUERAUFTRAG, "bolt", False),
            ("Internet / TV", ManagementType.DAUERAUFTRAG, "wifi", False),
            ("Serafe", ManagementType.RUECKSTELLUNG, "device-tv", False),
            ("Hausrat / Privathaftpflicht", ManagementType.RUECKSTELLUNG, "shield", False),
        ],
    ),
    (
        "Lebenshaltung",
        ManagementType.KOST_LOGIS,
        "shopping-cart",
        [
            ("Lebensmittel", ManagementType.KOST_LOGIS, "shopping-cart", False),
            ("Auswärts essen Arbeit", ManagementType.KOST_LOGIS, "tools-kitchen-2", False),
            ("Auswärts essen privat", ManagementType.BARGELD, "tools-kitchen-2", False),
            ("Haushalt-Nebenkosten", ManagementType.BARGELD, "basket", False),
        ],
    ),
    (
        "Konsum",
        ManagementType.BARGELD,
        "sparkles",
        [
            ("Kaffee", ManagementType.BARGELD, "coffee", False),
            ("Alkohol", ManagementType.BARGELD, "glass-full", False),
            ("Rauchen", ManagementType.BARGELD, "flame", False),
            ("Gaming", ManagementType.BARGELD, "device-gamepad-2", False),
        ],
    ),
    (
        "Versicherungen & Abgaben",
        ManagementType.DAUERAUFTRAG,
        "shield-check",
        [
            ("Krankenkasse Grund (KVG)", ManagementType.DAUERAUFTRAG, "stethoscope", False),
            ("Krankenkasse Zusatz (VVG)", ManagementType.DAUERAUFTRAG, "stethoscope", False),
            ("KK Franchise / Selbstbehalt", ManagementType.RUECKSTELLUNG, "stethoscope", False),
            ("Zahnzusatz", ManagementType.DAUERAUFTRAG, "tooth", False),
            ("AHV / IV", ManagementType.RUECKSTELLUNG, "id-badge", False),
            ("Kantons- und Gemeindesteuern", ManagementType.RUECKSTELLUNG, "receipt", False),
            ("Bundessteuern", ManagementType.RUECKSTELLUNG, "receipt", False),
        ],
    ),
    (
        "Mobilität",
        ManagementType.DAUERAUFTRAG,
        "bus",
        [
            ("ÖV-Abo (GA / Halbtax)", ManagementType.DAUERAUFTRAG, "ticket", False),
            ("Einzelfahrten", ManagementType.BARGELD, "ticket", False),
            ("Velo", ManagementType.RUECKSTELLUNG, "bike", False),
        ],
    ),
    (
        "Kommunikation",
        ManagementType.DAUERAUFTRAG,
        "device-mobile",
        [
            ("Handy-Abo", ManagementType.DAUERAUFTRAG, "device-mobile", False),
        ],
    ),
    (
        "Abos",
        ManagementType.DAUERAUFTRAG,
        "repeat",
        [
            ("KI-Dienste", ManagementType.DAUERAUFTRAG, "message-circle-2", True),
            ("Streaming", ManagementType.DAUERAUFTRAG, "device-tv", True),
            ("Software", ManagementType.DAUERAUFTRAG, "code", True),
            ("Gaming-Pässe", ManagementType.DAUERAUFTRAG, "device-gamepad-2", True),
        ],
    ),
    (
        "Gesundheit & Körper",
        ManagementType.RUECKSTELLUNG,
        "heart",
        [
            ("Medikamente", ManagementType.RUECKSTELLUNG, "pill", False),
            ("Zahnarzt / Dentalhygiene", ManagementType.RUECKSTELLUNG, "tooth", False),
            ("Optiker", ManagementType.RUECKSTELLUNG, "eye", False),
            ("Coiffeur", ManagementType.RUECKSTELLUNG, "scissors", False),
            ("Körperpflege", ManagementType.RUECKSTELLUNG, "droplet", False),
        ],
    ),
    (
        "Freizeit & Persönlich",
        ManagementType.BARGELD,
        "guitar-pick",
        [
            ("Hobby", ManagementType.BARGELD, "guitar-pick", False),
            ("Sport / Vereinsbeitrag", ManagementType.RUECKSTELLUNG, "running", False),
            ("Kleider / Schuhe", ManagementType.RUECKSTELLUNG, "shirt", False),
            ("Geschenke", ManagementType.RUECKSTELLUNG, "gift", False),
            ("Ferien", ManagementType.RUECKSTELLUNG, "beach", False),
            ("Taschengeld / Diskretionär", ManagementType.BARGELD, "wallet", False),
        ],
    ),
    (
        "Sparen & Vorsorge",
        ManagementType.SPAREN,
        "pig-money",
        [
            ("Sparkonto regulär", ManagementType.SPAREN, "pig-money", False),
            ("Ferienkonto regulär", ManagementType.SPAREN, "beach", False),
            ("Säule 3a", ManagementType.SPAREN, "shield-lock", False),
            ("Spezifische Sparziele", ManagementType.SPAREN, "target", False),
        ],
    ),
    (
        "Transfer",
        ManagementType.TRANSFER,
        "arrows-exchange",
        [
            ("Bargeldbezug", ManagementType.TRANSFER, "cash", False),
            ("Kontoübertrag", ManagementType.TRANSFER, "arrows-exchange", False),
        ],
    ),
    (
        "Unvorhergesehenes",
        ManagementType.RUECKSTELLUNG,
        "alert-triangle",
        [
            ("Reserve", ManagementType.RUECKSTELLUNG, "alert-triangle", False),
        ],
    ),
]


def seed_categories(db: Session) -> None:
    """Legt die hierarchische Kategorien-Struktur an, falls leer."""
    existing = db.scalar(select(Category).limit(1))
    if existing is not None:
        return

    sort_top = 10
    for top_name, top_mgmt, top_icon, subs in DEFAULT_CATEGORIES:
        top = Category(
            name=top_name,
            icon=top_icon,
            management_type=top_mgmt,
            sort_order=sort_top,
        )
        db.add(top)
        db.flush()  # ID materialisieren, damit Children referenzieren können.

        sort_sub = 10
        for sub_name, sub_mgmt, sub_icon, is_sub in subs:
            db.add(
                Category(
                    parent_id=top.id,
                    name=sub_name,
                    icon=sub_icon,
                    management_type=sub_mgmt,
                    is_subscription=is_sub,
                    sort_order=sort_sub,
                )
            )
            sort_sub += 10
        sort_top += 10

    db.flush()
    count = len(db.scalars(select(Category.id)).all())
    logger.info("[seeds] %d Kategorien-Datensätze angelegt", count)


# ---------------------------------------------------------------------------
# Sammel-Funktion
# ---------------------------------------------------------------------------


# Zusätzliche Kategorien, die nach dem ersten Seeding ergänzt wurden.
# (Schlüssel, Name, Top-Kategorie, management_type, icon)
#
# Der Schlüssel ist stabil und vom Namen unabhängig: umbenennen darf nicht dazu
# führen, dass dieselbe Kategorie ein zweites Mal entsteht. Gemerkt wird er in
# ``seed_marks`` — NICHT an der Kategorie, sonst nähme ein Löschen die Merkung
# gleich mit, und die Vorgabe käme beim nächsten Start zurück.
_EXTRA_CATEGORIES: list[tuple[str, str, str, ManagementType, str]] = [
    ("technik", "Technik", "Konsum", ManagementType.BARGELD, "device-laptop"),
    ("snacks", "Snacks", "Konsum", ManagementType.BARGELD, "cookie"),
    ("haushalt", "Haushalt", "Wohnen", ManagementType.BARGELD, "home-2"),
    # TWINT-Zahlungen an andere Privatpersonen (Rückzahlungen, Aufteilen von
    # Rechnungen) — zählt als Ausgabe (BARGELD), nicht als Konto-Transfer.
    ("rueckzahlungen", "Rückzahlungen", "Freizeit & Persönlich", ManagementType.BARGELD, "arrows-exchange"),
    # Der Steuerjahr-Auszug sucht seine Positionen ueber Stichwoerter im
    # Kategorienamen. Ohne diese beiden blieben „Spenden" und „Berufsauslagen"
    # dauerhaft leer — die Seite zeigte dort verlaesslich nichts, ohne es zu sagen.
    ("spenden", "Spenden", "Freizeit & Persönlich", ManagementType.BARGELD, "heart"),
    # „weiterbildung" ist das Stichwort der Position Berufsauslagen; der Name
    # deckt zusaetzlich ein abgeschlossenes Studium ab, damit alte Studien-
    # buchungen nicht ohne Heimat bleiben.
    ("weiterbildung", "Weiterbildung / Studium", "Freizeit & Persönlich", ManagementType.BARGELD, "book"),
    # Kontofuehrung, Jahresgebuehr Debit-/Kreditkarte, Fremdwaehrungszuschlag.
    ("bankgebuehren", "Bankgebühren", "Versicherungen & Abgaben", ManagementType.DAUERAUFTRAG, "building-bank"),
    # Fuer Buchungen, deren Zweck sich nicht mehr rekonstruieren laesst (Text
    # ohne Haendler, kein Beleg). Unkategorisiert lassen waere die Alternative —
    # dann bleiben sie aber fuer immer in der Inbox stehen und der Zaehler
    # „N offen" geht nie auf null, verliert also seine Aussage.
    #
    # BARGELD und nicht RUECKSTELLUNG: Das Geld ist wirklich weg und soll im
    # Budget als Ausgabe zaehlen — nur der Zweck ist unbekannt. Eine
    # Rueckstellung waere etwas Geplantes, und geplant war es gerade nicht.
    ("nicht_zuordenbar", "Nicht mehr zuordenbar", "Konsum", ManagementType.BARGELD, "alert-triangle"),
]


def ensure_extra_categories(db: Session) -> int:
    """Bietet nachträglich definierte Kategorien EINMAL an.

    **Was hier vorher falsch war.** Geprüft wurde, ob eine Kategorie dieses
    NAMENS existiert — bei jedem Start. Wer eine der acht löschte, hatte sie nach
    dem nächsten Neustart wieder. Wer sie umbenannte, hatte sie zusätzlich: zwei
    Einträge für dieselbe Sache, die alten Buchungen am umbenannten, der neue
    leer und in der Auswahl nicht zu unterscheiden. Ab da konnten Buchungen auf
    zwei Töpfe laufen, und keine Auswertung stimmte mehr.

    Jetzt entscheidet eine Merkung in ``seed_marks``: „diese Vorgabe wurde schon
    einmal angeboten". Was der Nutzer danach damit tut — behalten, umbenennen,
    löschen, archivieren — ist seine Sache und bleibt so.
    """
    gemerkt = set(db.scalars(select(SeedMarke.schluessel)))
    vorhanden = {c.name for c in db.scalars(select(Category))}
    tops = {c.name: c.id for c in db.scalars(select(Category).where(Category.parent_id.is_(None)))}
    added = 0
    for key, name, parent_name, mgmt, icon in _EXTRA_CATEGORIES:
        if key in gemerkt or parent_name not in tops:
            continue
        # Der Name zählt weiterhin — aber nur beim ERSTEN Mal: eine
        # Installation, die diese Kategorie schon von Hand angelegt hat, soll
        # kein Duplikat bekommen. Gemerkt wird der Schlüssel in beiden Fällen.
        if name not in vorhanden:
            db.add(Category(parent_id=tops[parent_name], name=name, icon=icon,
                            management_type=mgmt, sort_order=900))
            added += 1
        db.add(SeedMarke(schluessel=key))
    db.commit()
    return added


# Verlaufsreihen (idempotent, wie die Kategorien oben).
#
# Bewusst KEINE Buchungen: diese Werte stammen aus Belegen und stehen zum Teil
# bereits als Kontobelastung in den Transaktionen. Siehe Migration 0020.
#
# Als benanntes Tupel und nicht als Stellungsparameter: bei zehn Feldern ist
# ``(..., None, None, None, "Text")`` nicht mehr lesbar, und ein verrutschtes
# Feld fiele niemandem auf.


class _Reihe(NamedTuple):
    """Definition einer Verlaufsreihe für den Seed."""

    slug: str
    name: str
    unit: MetricUnit
    cadence: MetricCadence
    kind: MetricKind
    note: str
    # Kategorie, in der die Zahlungen zu dieser Reihe gebucht sein sollten.
    # ``None`` heisst: ein Abgleich mit den Buchungen ergibt hier keinen Sinn.
    kategorie: str | None = None
    nebenwert: str | None = None
    nebeneinheit: MetricUnit | None = None
    nebenlabel: str | None = None


_METRIC_SERIES: list[_Reihe] = [
    _Reihe(
        slug="kk_praemie",
        name="Krankenkassenprämie",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.MONATLICH,
        kind=MetricKind.AUSGABE,
        note="Monatsprämie laut Prämienabrechnung der Kasse.",
        kategorie="Krankenkasse Grund (KVG)",
    ),
    _Reihe(
        slug="kk_verbilligung",
        name="Prämienverbilligung",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.EINNAHME,
        note="Jahresanspruch laut Verfügung — mindert die Prämienlast.",
        # Die Verbilligung wird in der Regel direkt mit der Prämie verrechnet und
        # erscheint gar nicht als eigene Gutschrift. Ein Abgleich gegen eine
        # Kategorie würde darum verlässlich „fehlt" melden, obwohl alles stimmt.
        kategorie=None,
    ),
    _Reihe(
        slug="kk_police",
        name="KK-Police: Sollprämie",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.AUSGABE,
        note="Was die Police für das Jahr vorsieht — Gegenprobe zur bezahlten Prämie.",
        kategorie="Krankenkasse Grund (KVG)",
        nebenwert="franchise",
        nebeneinheit=MetricUnit.CHF,
        nebenlabel="Franchise",
    ),
    _Reihe(
        # Der Nebenwert ist hier der eigentliche Gewinn: erst mit kWh daneben
        # lässt sich eine höhere Rechnung als „mehr verbraucht" oder „teurer
        # geworden" lesen. Der Betrag allein sagt das nicht.
        slug="strom",
        name="Strom",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.QUARTALSWEISE,
        kind=MetricKind.AUSGABE,
        note="Quartalsrechnung mit Verbrauch aus der Zählerablesung.",
        kategorie="Strom / Heizung",
        nebenwert="kwh",
        nebeneinheit=MetricUnit.KWH,
        nebenlabel="Verbrauch",
    ),
    _Reihe(
        slug="lohn",
        name="Jahreslohn",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.EINNAHME,
        note="AHV-Jahreslohn laut Lohnausweis oder Vorsorgeausweis.",
        # KEIN Abgleich gegen „Nettolohn": der Ausweis nennt den Bruttolohn vor
        # AHV, ALV und Pensionskasse, gebucht wird die Auszahlung. Die Differenz
        # ist gewollt und beträgt gut ein Fünftel — ein Abgleich würde also jeden
        # Monat eine Abweichung melden, die keine ist.
        kategorie=None,
        nebenwert="pensum",
        nebeneinheit=MetricUnit.PROZENT,
        nebenlabel="Pensum",
    ),
    _Reihe(
        # VERMOEGEN, nicht AUSGABE: ein Altersguthaben ist ein Bestand. Als
        # Ausgabe geführt, tauchte es in Summen auf, in die es nicht gehört.
        slug="pk_guthaben",
        name="Pensionskasse: Altersguthaben",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.UNREGELMAESSIG,
        kind=MetricKind.VERMOEGEN,
        note="Stand laut Vorsorgeausweis. Der Beitrag wird vom Lohn abgezogen "
        "und erscheint nie als Buchung.",
        kategorie=None,
        nebenwert="beitrag_monat",
        nebeneinheit=MetricUnit.CHF,
        nebenlabel="Beitrag/Monat",
    ),
    _Reihe(
        slug="steuern_kanton",
        name="Kantons- und Gemeindesteuern",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.AUSGABE,
        note="Veranlagung des Wohnkantons.",
        kategorie="Kantons- und Gemeindesteuern",
    ),
    _Reihe(
        slug="steuern_bund",
        name="Direkte Bundessteuer",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.AUSGABE,
        note="Veranlagung der direkten Bundessteuer.",
        kategorie="Bundessteuern",
    ),
    _Reihe(
        slug="hausrat_haftpflicht",
        name="Hausrat / Privathaftpflicht",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.AUSGABE,
        note="Jahresprämie laut Police.",
        kategorie="Hausrat / Privathaftpflicht",
    ),
    _Reihe(
        # Der Nebenwert ist der Referenzzinssatz und nicht etwa die Nebenkosten:
        # sinkt der landesweite Satz unter den im Vertrag festgehaltenen, entsteht
        # ein Anspruch auf Mietzinssenkung. Ein Viertelprozent macht grob drei
        # Prozent des Nettomietzinses aus — ein Anspruch, der still verjährt,
        # wenn ihn niemand bemerkt.
        slug="miete",
        name="Miete inkl. Nebenkosten",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.UNREGELMAESSIG,
        kind=MetricKind.AUSGABE,
        note="Gesamtbelastung laut Mietvertrag. Ändert sich nur bei einer Anpassung.",
        kategorie="Miete",
        nebenwert="referenzzinssatz",
        nebeneinheit=MetricUnit.PROZENT,
        nebenlabel="Referenzzinssatz",
    ),
    _Reihe(
        slug="nebenverdienst",
        name="Nebenverdienste",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.EINNAHME,
        note="Nettolohn laut Lohnausweis, alle Arbeitgeber eines Jahres zusammen.",
        # KEIN Abgleich gegen eine Kategorie: Nebenverdienste landen je nach Fall
        # als „Nettolohn" oder „Sonstige Einnahmen" in den Buchungen. Eine feste
        # Zuordnung träfe die eine Hälfte und meldete für die andere dauerhaft
        # „nicht gebucht" — eine Warnung, die nichts bedeutet.
        kategorie=None,
    ),
    _Reihe(
        slug="gesundheit_selbst",
        name="Gesundheitskosten selbst getragen",
        unit=MetricUnit.CHF,
        cadence=MetricCadence.JAEHRLICH,
        kind=MetricKind.AUSGABE,
        note="Franchise und Selbstbehalt laut Leistungsabrechnungen — der Teil, "
        "den die Kasse nicht übernimmt.",
        kategorie="KK Franchise / Selbstbehalt",
        nebenwert="franchise",
        nebeneinheit=MetricUnit.CHF,
        nebenlabel="davon Franchise",
    ),
]


def _reihen_aus_anbieterprofilen() -> list[_Reihe]:
    """Je EIGENEM Anbieterprofil eine Verlaufsreihe.

    Hier stand einmal eine feste Reihe mit dem Namen eines Anbieters. Damit war
    jeder weitere Anbieter eine Code-Änderung — und der Quelltext verriet, bei
    wem der Nutzer Kunde ist.

    **Nur eigene Profile.** Ein mitgeliefertes Beispiel soll keine Demo-Reihe in
    eine frische Installation legen; woher ein Profil stammt, sagt
    :func:`~moneten.services.anbieter_profil.ist_eigenes`.

    Was hier fest bleibt, ist keine Anbietersache, sondern eine Eigenschaft
    dieser Belegart: der Parser liefert stets monatliche Ausgaben in Franken,
    und der Nebenwert sind stets die Rabatte — sie sind der Teil der Rechnung
    mit Verfalldatum. Läuft eine Promotion aus, steigt der Betrag, ohne dass
    sich am Abonnement etwas geändert hätte.

    Diese Reihen sind die einzigen, deren Punkte nach Positionen aufgeschlüsselt
    sind (``pos:``-Schlüssel in ``MetricPoint.extras``, Konvention am Modell).
    Der Rechnungsbetrag allein sagt nicht, warum er steigt: eine auslaufende
    Promotion sieht darin genauso aus wie ein teureres Abonnement.
    """
    # Lokal importiert, wie beim Starter-Regelsatz weiter unten: ``services``
    # kennt die Modelle, und ein Import auf Modulebene liefe im Kreis.
    from moneten.services.anbieter_profil import ist_eigenes
    from moneten.services.belege_parser import PROFILE

    return [
        _Reihe(
            slug=profil.slug,
            name=profil.name,
            unit=MetricUnit.CHF,
            cadence=MetricCadence.MONATLICH,
            kind=MetricKind.AUSGABE,
            note=profil.notiz,
            kategorie=profil.kategorie,
            nebenwert="rabatt",
            nebeneinheit=MetricUnit.CHF,
            nebenlabel="Rabatte",
        )
        for profil in PROFILE.values()
        if ist_eigenes(profil)
    ]


def ensure_metric_series(db: Session) -> int:
    """Legt fehlende Verlaufsreihen an (idempotent, läuft bei jedem Start).

    Nur anlegen, nie ändern: wer eine Reihe umbenannt, neu verknüpft oder
    archiviert hat, soll das beim nächsten Start nicht zurückgesetzt bekommen.
    Der ``slug`` ist der stabile Schlüssel, an dem der Import die Reihe wiederfindet.

    Die Kategorie wird über den Namen aufgelöst. Findet sich keine, bleibt die
    Verknüpfung leer und der Soll/Ist-Abgleich sagt das offen — besser als eine
    stillschweigend falsche Zuordnung.
    """
    vorhanden = {s.slug for s in db.scalars(select(MetricSeries))}
    nach_name = {
        c.name: c.id
        for c in db.scalars(select(Category).where(Category.parent_id.is_not(None)))
    }
    neu = 0
    # Die Anbieter-Reihen hängen hinten an: so bleibt die Sortierung der
    # bestehenden Reihen unverändert, auch wenn jemand ein Profil ergänzt.
    for i, r in enumerate([*_METRIC_SERIES, *_reihen_aus_anbieterprofilen()]):
        if r.slug in vorhanden:
            continue
        db.add(MetricSeries(
            slug=r.slug, name=r.name, unit=r.unit, cadence=r.cadence, kind=r.kind,
            secondary_key=r.nebenwert, secondary_unit=r.nebeneinheit,
            secondary_label=r.nebenlabel, note=r.note,
            category_id=nach_name.get(r.kategorie) if r.kategorie else None,
            sort_order=i * 10,
        ))
        neu += 1
    if neu:
        db.commit()
    return neu


def seed_all(db: Session) -> None:
    """Führt sämtliche Seeds in der richtigen Reihenfolge aus."""
    seed_user(db)
    seed_accounts(db)
    seed_categories(db)
    db.commit()
    ensure_extra_categories(db)  # nachträgliche Kategorien (Technik, Snacks, Haushalt)
    ensure_metric_series(db)  # Verlaufsreihen (Prämie, Strom, Lohn, Steuern …)
    # Generisches CH-Starter-Regelset für die Auto-Kategorisierung (idempotent).
    # Import hier lokal, um Zyklen zu vermeiden (services importiert models).
    # Generisches CH-Starter-Regelset für die Auto-Kategorisierung (idempotent).
    # Import hier lokal, um Zyklen zu vermeiden (services importiert models).
    from moneten.services.categorization import seed_starter_rules
    seed_starter_rules(db)
