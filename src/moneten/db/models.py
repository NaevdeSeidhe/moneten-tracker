"""ORM-Models gemäss Abschnitt 5 der Spezifikation.

Konventionen:
* Beträge werden als ``Numeric(14, 2)`` gespeichert — niemals als ``float``,
  weil Float-Rundungsfehler im Finanzkontext absolut tabu sind.
* Zeitstempel sind UTC. Die Konvertierung in lokale Zeit übernimmt die UI.
* ``management_type`` ist eine kurze Code-Liste (D/R/KL/B/S) gemäss
  Schweizer Budgetberatungs-Logik:
  D=Dauerauftrag, R=Rückstellung, KL=Kost und Logis, B=Bargeld, S=Sparen.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from moneten.dates import heute_lokal
from moneten.db.session import Base
from moneten.themes import DEFAULT_THEME, Theme

# ---------------------------------------------------------------------------
# Enum-Typen — Strings im Klartext, damit die DB lesbar bleibt.
# ---------------------------------------------------------------------------


class ManagementType(enum.StrEnum):
    """Verwaltungs-Klassifizierung aus der CH-Budgetberatung."""

    DAUERAUFTRAG = "D"
    RUECKSTELLUNG = "R"
    KOST_LOGIS = "KL"
    BARGELD = "B"
    SPAREN = "S"
    EINKOMMEN = "E"
    TRANSFER = "T"


class AccountType(enum.StrEnum):
    """Konto-Typen aus dem Mockup."""

    BANK = "bank"
    CASH = "cash"
    SAVINGS = "savings"
    INVESTMENT = "investment"
    CRYPTO = "crypto"
    STOCKS = "stocks"


class ThemePref(enum.StrEnum):
    """Nur noch für Altbestand/Migration.

    Das Theme ist seit 0.53 **einachsig** und wird als freier String gespeichert
    (siehe :mod:`moneten.themes`), damit neue Farbwelten ohne Migration dazukommen
    können. Dieses Enum bleibt bestehen, weil ältere Migrationen darauf verweisen.
    """

    DARK = "dark"
    LIGHT = "light"


class ImportSource(enum.StrEnum):
    """Herkunft eines Import-Batches."""

    CAMT053 = "camt053"
    CSV = "csv"
    MANUAL = "manual"
    MIGROS_APP = "migros_app"


class ImportStatus(enum.StrEnum):
    """Lebenszyklus eines Import-Batches."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class GoalPriority(enum.StrEnum):
    """Priorität eines Sparziels."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BudgetInterval(enum.StrEnum):
    """Intervall eines Standard-Solls: monatlich oder jährlich (Rückstellung)."""

    MONATLICH = "monatlich"
    JAEHRLICH = "jaehrlich"


class MetricUnit(enum.StrEnum):
    """Masseinheit einer Verlaufsreihe — bestimmt Formatierung und Achsenbeschriftung."""

    CHF = "chf"
    KWH = "kwh"
    PROZENT = "prozent"


class MetricCadence(enum.StrEnum):
    """Erwarteter Takt einer Reihe.

    Nur eine Erwartung, keine Zwangsjacke: Lücken und abweichende Perioden sind
    erlaubt. Der Takt steuert, wie die Verlaufsseite auf Monate umrechnet und ab
    wann sie eine fehlende Periode als Lücke meldet.
    """

    MONATLICH = "monatlich"
    QUARTALSWEISE = "quartalsweise"
    JAEHRLICH = "jaehrlich"
    UNREGELMAESSIG = "unregelmaessig"


class MetricKind(enum.StrEnum):
    """Wofür die Reihe steht — steuert Vorzeichen-Deutung und Gruppierung.

    ``VERMOEGEN`` ist bewusst getrennt: ein Altersguthaben ist ein Bestand, kein
    Fluss. Würde es als ``AUSGABE`` laufen, tauchte es in Summen auf, in die es
    nicht gehört.
    """

    AUSGABE = "ausgabe"
    EINNAHME = "einnahme"
    VERMOEGEN = "vermoegen"


def _now() -> datetime:
    """Aktueller Zeitstempel in UTC — einheitlich für ``created_at`` / ``updated_at``."""
    return datetime.now(UTC)


def _str_enum(enum_cls: type[enum.Enum], length: int) -> Enum:
    """Erzeugt einen String-Enum-Spaltentyp, der die ``.value``-Codes speichert.

    Wichtig: SQLAlchemy speichert bei ``Enum`` standardmässig den *Namen* eines
    Members (z.B. ``DAUERAUFTRAG``), nicht dessen Wert (``D``). Wir wollen aber
    die kompakten Codes in der DB — damit sie lesbar bleibt und zu den
    Spaltenlängen aus der Migration passt. ``values_callable`` erzwingt das.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


# ---------------------------------------------------------------------------
# 1) Users
# ---------------------------------------------------------------------------


class User(Base):
    """Single-User-Tabelle. Es existiert immer genau ein Datensatz.

    Mehrere Datensätze sind technisch nicht verboten, aber die App rechnet
    durchgehend mit ``users.id = 1``.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), default="Odysseus")
    pin_hash: Mapped[str] = mapped_column(String(255))
    webauthn_credentials_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Theme EINACHSIG als freier String ("dark" | "light" | "nord" | …).
    # Bewusst kein Enum: eine neue Farbwelt soll nur einen Block in skins.css und
    # einen Eintrag in moneten/themes.py brauchen — keine DB-Migration.
    preferred_theme: Mapped[str] = mapped_column(
        String(20), default=DEFAULT_THEME, server_default=DEFAULT_THEME
    )
    # Foto-Belege: Original nach OCR verwerfen (Default, datensparsam) oder ein
    # reduziertes Bild als Safety auf dem NAS behalten.
    receipt_photo_keep: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Gewünschter Bargeld-Anteil an den Alltagsausgaben in Prozent. 0 = kein Ziel
    # gesetzt; dann zeigt die Auswertung nur den Stand ohne Ziellinie.
    cash_goal_pct: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Wann die PIN zuletzt selbst gesetzt wurde. ``None`` heisst: es gilt noch die
    # Start-PIN aus der Konfiguration — und die steht in einer Datei, die jeder
    # mitliest, der die App aufsetzt. Solange das Feld leer ist, lässt die App
    # nichts ausser der Wechsel-Seite zu.
    pin_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def theme(self) -> Theme:
        """Das aktive Theme als Objekt (Label, Helligkeit, Hintergrundfarbe)."""
        from moneten.themes import get as _get_theme

        return _get_theme(self.preferred_theme)


# ---------------------------------------------------------------------------
# 2) Accounts
# ---------------------------------------------------------------------------


class Account(Base):
    """Bank-, Bargeld-, Spar- und Investment-Konten."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[AccountType] = mapped_column(_str_enum(AccountType, length=16))
    currency: Mapped[str] = mapped_column(String(3), default="CHF")
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    current_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    color: Mapped[str | None] = mapped_column(String(9), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transactions: Mapped[list[Transaction]] = relationship(back_populates="account")


# ---------------------------------------------------------------------------
# 3) Categories
# ---------------------------------------------------------------------------


class Category(Base):
    """Hierarchische Kategorien (Top-Level und Sub-Kategorien).

    ``parent_id`` ist NULL für Top-Level (z.B. WOHNEN). Die Anzeige im UI
    setzt darauf, dass jede Buchung auf der Blatt-Ebene (Sub-Kategorie)
    landet, Reports aber bis zum Wurzelknoten aggregieren können.
    """

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(80))
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    color: Mapped[str | None] = mapped_column(String(9), nullable=True)
    management_type: Mapped[ManagementType] = mapped_column(
        _str_enum(ManagementType, length=4)
    )
    is_subscription: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    parent: Mapped[Category | None] = relationship(remote_side="Category.id", backref="children")


# ---------------------------------------------------------------------------
# 4) Transactions
# ---------------------------------------------------------------------------


class Transaction(Base):
    """Eine einzelne Buchung.

    ``amount`` ist vorzeichenbehaftet: positiv = Einnahme, negativ = Ausgabe.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Indizes: fast jede Abfrage (Dashboard, Buchungen, Budget, Vergleich, Prognose)
    # filtert über date / account_id / category_id. Ohne Index = Full-Table-Scan über
    # alle Buchungen — auf dem NAS (schwache CPU + verschlüsselte DB) spürbar langsam.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), index=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )

    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    description: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    management_type: Mapped[ManagementType | None] = mapped_column(
        _str_enum(ManagementType, length=4), nullable=True
    )

    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True
    )

    # Verknüpft die beiden Seiten eines Transfers (Umbuchung zwischen eigenen
    # Konten). Beide Buchungen teilen dieselbe transfer_group_id und tragen
    # management_type=TRANSFER. NULL bei normalen Buchungen.
    transfer_group_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # Dedup-Hash: Datum + Betrag + erste 50 Zeichen Beschreibung.
    # Wird beim Bank-Import gesetzt, manuelle Buchungen lassen ihn leer.
    dedup_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Referenz der Bank (``AcctSvcrRef`` im CAMT.053). Der **verlässliche**
    # Schlüssel gegen Dubletten: er unterscheidet zwei gleich aussehende
    # Buchungen am selben Tag, was ein Inhalts-Hash grundsätzlich nicht kann.
    # NULL bei CSV-Importen (die Datei trägt sie nicht), bei manuellen Buchungen
    # und bei allem, was vor Migration 0030 importiert wurde — dort entscheidet
    # weiterhin ``dedup_hash``.
    bank_reference: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # True, wenn diese Buchung in mehrere Kategorie-Anteile aufgeteilt ist
    # (siehe TransactionSplit). Dann zählt für Kategorie-Auswertungen NICHT
    # die ``category_id`` der Buchung, sondern die Summe ihrer Splits. Der
    # vorzeichenbehaftete ``amount`` (und damit Saldo/Monatszahlen) bleibt
    # unverändert — die Aufteilung verschiebt kein Geld, nur die Zuordnung.
    is_split: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    account: Mapped[Account] = relationship(back_populates="transactions")
    category: Mapped[Category | None] = relationship()
    splits: Mapped[list[TransactionSplit]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )


class TransactionSplit(Base):
    """Ein Kategorie-Anteil einer aufgeteilten Buchung (Auto-Split).

    Beispiel: Ein Migros-Einkauf über CHF 78.40 wird in Lebensmittel 56.80,
    Haushalt 15.20 und Alkohol 6.40 aufgeteilt → drei Splits zur selben
    Buchung. Die Summe der Splits **muss** dem ``amount`` der Buchung
    entsprechen (gleiches Vorzeichen wie die Buchung — bei Ausgaben negativ).

    Kategorie-Auswertungen (Budget-Ist, Vergleich, Geldfluss) rechnen die
    Anteile ihren jeweiligen Kategorien zu; reine Vorzeichen-Summen (Saldo,
    Monats-Einnahmen/-Ausgaben) bleiben unberührt, weil sie den Eltern-Betrag
    nutzen — der unverändert bleibt.
    """

    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Vorzeichenbehaftet wie die Eltern-Buchung (Ausgabe → negativ).
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    transaction: Mapped[Transaction] = relationship(back_populates="splits")
    category: Mapped[Category | None] = relationship()


# ---------------------------------------------------------------------------
# 5b) Lohnzusammensetzung: was hinter einer Lohn-Gutschrift steckt
# ---------------------------------------------------------------------------


class LohnHerkunft(enum.StrEnum):
    """Woher ein einzelner Betrag stammt — die wichtigste Angabe des Moduls.

    Ein Lohnblatt gibt es nur bei einer **Lohnänderung**, sonst nie. Zwischen
    zwei Blättern gilt der letzte belegte Stand unverändert weiter. Ein Monat
    mit Bonus, Nachzahlung oder Pensumsänderung stimmt dann nicht — und niemand
    sieht es der Zahl an. Darum trägt jeder Posten mit, worauf er beruht.

    Drei Stufen, von der besten Kenntnis zur schlechtesten:

    * ``ERFASST`` — der Nutzer hat diese Zahl für DIESEN Monat eingetragen; sie
      steht so auf einem Blatt.
    * ``FORTGESCHRIEBEN`` — sie stammt unverändert aus einem früheren, selbst
      erfassten Monat. Der Wert ist exakt abgelesen, nur eben nicht in diesem
      Monat: er ist richtig, solange sich nichts geändert hat.
    * ``GERECHNET`` — sie ist aus einem Jahreswert oder einem Beitragssatz
      abgeleitet und war nie auf einem Blatt zu sehen.

    Die mittlere Stufe ist der HÄUFIGSTE Fall und lag vorher bei ``GERECHNET``.
    Das untertreibt: ein aus dem Jahreslohn geteilter Betrag ist geschätzt, ein
    fortgeschriebener ist abgelesen und höchstens veraltet. Beides gleich zu
    kennzeichnen macht die Kennzeichnung wertlos — es steht dann überall.

    Die Trennlinie zwischen den beiden unteren Stufen ist scharf: **führt der
    Wert auf ein Blatt zurück oder nicht.** Deshalb bleibt die Kopie eines
    gerechneten Postens gerechnet (:func:`moneten.services.lohn.vorschlag`) —
    Abschreiben macht aus einer Schätzung keine Ablesung.
    """

    ERFASST = "erfasst"
    FORTGESCHRIEBEN = "fortgeschrieben"
    GERECHNET = "gerechnet"


class LohnPostenArt(enum.StrEnum):
    """Richtung eines Postens. ``betrag`` ist immer positiv, die Art gibt das
    Vorzeichen: Bruttolohn und Zulagen zählen dazu, Abzüge davon weg."""

    BRUTTO = "brutto"
    ABZUG = "abzug"


class Lohnabrechnung(Base):
    """Die Zusammensetzung EINER Lohn-Gutschrift (1:1 zur Buchung).

    Bewusst an der Buchung und nicht als eigene Reihe (:class:`MetricSeries`):
    die Frage lautet „woraus besteht **diese** Gutschrift", und die Antwort
    gehört an die Zeile, an der sie gestellt wird.

    Der Nettolohn wird **nicht** gespeichert. Er ergibt sich aus den Posten und
    wird in der Anzeige dem tatsächlich gebuchten Betrag gegenübergestellt. Ein
    gespeicherter Nettolohn liesse sich an die Buchung angleichen — dann sähe
    eine aus Jahreswerten geschätzte Aufstellung exakt aus, obwohl sie es nicht
    ist. Die Differenz ist der ehrlichste Teil der ganzen Darstellung.

    ``grundlage`` benennt die Quelle der hergeleiteten Werte („Jahreslohn 2025
    ÷ 12"). Kein Fliesstext: die Zeile steht nur da, wenn sie etwas benennt.
    """

    __tablename__ = "lohn_abrechnungen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Genau eine Aufstellung je Buchung — mehrere wären zwei Antworten auf
    # dieselbe Frage, und die Anzeige müsste raten, welche gilt.
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), unique=True, index=True
    )
    grundlage: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    posten: Mapped[list[Lohnposten]] = relationship(
        back_populates="abrechnung",
        cascade="all, delete-orphan",
        order_by="Lohnposten.sort_order",
    )


class Lohnposten(Base):
    """Eine Zeile der Aufstellung: Bruttolohn, Zulage oder Abzug.

    Freier ``label`` statt fester Spalten je Beitragsart: welche Abzüge
    vorkommen, hängt am Arbeitgeber (NBUV- und KTG-Satz, Pensionskassenplan,
    allenfalls Quellensteuer). Feste Spalten wären zur Hälfte leer und trotzdem
    unvollständig.
    """

    __tablename__ = "lohn_posten"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    abrechnung_id: Mapped[int] = mapped_column(
        ForeignKey("lohn_abrechnungen.id", ondelete="CASCADE"), index=True
    )
    art: Mapped[LohnPostenArt] = mapped_column(_str_enum(LohnPostenArt, length=8))
    label: Mapped[str] = mapped_column(String(60))
    # Immer positiv — das Vorzeichen steckt in ``art``. Ein vorzeichenbehafteter
    # Betrag plus eine Art wären zwei Quellen für dieselbe Aussage.
    betrag: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    # 16 und nicht 10: „fortgeschrieben" ist der längste Code der Stufe.
    herkunft: Mapped[LohnHerkunft] = mapped_column(_str_enum(LohnHerkunft, length=16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    abrechnung: Mapped[Lohnabrechnung] = relationship(back_populates="posten")


# ---------------------------------------------------------------------------
# 6) Budgets
# ---------------------------------------------------------------------------


class Budget(Base):
    """Pro Monat und Kategorie ein Soll-Betrag.

    ``is_auto_calculated=True`` wenn der Wert per Median der letzten 6 Monate
    automatisch befüllt wurde. Manuelle Override setzt das Flag auf False.
    """

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category_id", "month", name="uq_budgets_cat_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    # Erster Tag des Monats als kanonische Repräsentation (YYYY-MM-01).
    month: Mapped[date] = mapped_column(Date)
    planned_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    is_auto_calculated: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class StandardBudget(Base):
    """Standard-Soll je Kategorie — einmal ausfüllen, gilt fortlaufend.

    ``interval`` bestimmt die Bedeutung von ``amount``:
    * ``MONATLICH`` — ``amount`` ist der monatliche Soll-Betrag.
    * ``JAEHRLICH`` — ``amount`` ist der **Jahresbetrag**; ins Monatsbudget
      fliesst 1/12, zusätzlich erscheint die Position als Rückstellung.

    Ein monatsspezifischer :class:`Budget`-Eintrag überschreibt diesen Standard
    für den jeweiligen Monat.
    """

    __tablename__ = "standard_budgets"
    __table_args__ = (UniqueConstraint("category_id", name="uq_standard_budget_cat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    interval: Mapped[BudgetInterval] = mapped_column(
        _str_enum(BudgetInterval, length=10), default=BudgetInterval.MONATLICH
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CategoryRule(Base):
    """Regel zur automatischen Kategorisierung von Buchungen.

    Enthält der Buchungstext ``keyword`` (Teilstring, case-insensitiv), wird die
    Buchung der ``category_id`` zugeordnet. ``sort_order`` bestimmt die Reihenfolge
    (erste passende Regel gewinnt). Manuell gesetzte Kategorien werden NIE
    überschrieben.
    """

    __tablename__ = "category_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(120))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    category: Mapped[Category] = relationship()


# ---------------------------------------------------------------------------
# 7) Savings Goals
# ---------------------------------------------------------------------------


class SavingsGoal(Base):
    """Sparziele wie ``Notgroschen`` oder ``Reise``."""

    __tablename__ = "savings_goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    target_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[GoalPriority] = mapped_column(
        _str_enum(GoalPriority, length=8), default=GoalPriority.MEDIUM
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_achieved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------
# 8) Import Batches
# ---------------------------------------------------------------------------


class ImportBatch(Base):
    """Ein einzelner Import-Vorgang (z.B. eine CAMT.053-Datei)."""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[ImportSource] = mapped_column(_str_enum(ImportSource, length=16))
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    period_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    total_transactions: Mapped[int] = mapped_column(Integer, default=0)
    auto_categorized_count: Mapped[int] = mapped_column(Integer, default=0)

    expected_closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    actual_closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    balance_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[ImportStatus] = mapped_column(
        _str_enum(ImportStatus, length=12), default=ImportStatus.PENDING
    )


# ---------------------------------------------------------------------------
# 11) Attachments
# ---------------------------------------------------------------------------


class Attachment(Base):
    """Datei-Anhang zu einer Buchung (Quittung, Rechnung, Bon)."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE")
    )
    # Pfad zur referenzierten Datei im Quittungs-Ordner. NULL, wenn nur der
    # Dateiname vermerkt wurde (kein konfigurierter Ordner / kein Match).
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON mit strukturierten Beleg-Daten (digitale Quittung):
    #   {method, merchant, date, amount, items:[{name, price, category_id}]}
    # Für Foto-Belege ist KEIN Bild gespeichert (file_path = NULL) — nur diese Daten.
    parsed_items_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PendingReceipt(Base):
    """Digitalisierte Quittung, die (noch) KEINER Buchung zugeordnet ist.

    Entsteht v.a. beim mobilen Foto-Upload: das Foto wird im Speicher per OCR
    analysiert und **verworfen** — hier bleiben nur die extrahierten,
    strukturierten Daten. Sobald eine passende Bankbuchung auftaucht (Betrag +
    Datum), wird die Quittung automatisch als :class:`Attachment` an die Buchung
    gehängt und dieser Datensatz gelöscht (siehe ``services.receipt_digital``).
    """

    __tablename__ = "pending_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant: Mapped[str | None] = mapped_column(String(160), nullable=True)
    receipt_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="photo")  # photo | folder
    # Nur gesetzt, wenn der Nutzer „reduziert behalten" gewählt hat.
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReceiptItemRule(Base):
    """Gelernte Positions→Kategorie-Zuordnung (z.B. „tomate" → Lebensmittel).

    Wird beim Bestätigen/Korrigieren einer digitalen Quittung gepflegt. Der
    optionale Händler-Schlüssel macht die Regel händlerspezifisch (z.B. nur bei
    Migros). So „lernt" die Kategorisierung den tatsächlichen Einkauf — rein
    lokal, keine KI, gleiche Philosophie wie die :class:`CategoryRule`.
    """

    __tablename__ = "receipt_item_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(120))  # normalisiertes Stichwort
    merchant_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("keyword", "merchant_key", name="uq_receipt_item_rule"),)


# ---------------------------------------------------------------------------
# Abos (manuell gepflegt) + ausgeblendete erkannte Händler
# ---------------------------------------------------------------------------


class ManualSubscription(Base):
    """Manuell erfasstes Abo (ergänzt die automatische Erkennung).

    Für Abos, die die Auto-Erkennung nicht/falsch sieht — der Nutzer kann sie
    hier anlegen, anpassen und löschen.
    """

    __tablename__ = "manual_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    interval: Mapped[BudgetInterval] = mapped_column(
        _str_enum(BudgetInterval, length=10), default=BudgetInterval.MONATLICH
    )
    # Art: "abo" (Handy/Software/Games/Streaming) oder "fix" (wiederkehrende
    # Zahlung wie Miete/Strom/GA/Steuern). Steuert nur die Trennung der Anzeige.
    kind: Mapped[str] = mapped_column(String(10), default="abo")
    # Optionaler Händler-Schlüssel, der dieses manuelle Abo mit echten Bankbuchungen
    # verbindet (alle Buchungen mit diesem Schlüssel gehören dazu). Dient der
    # Anzeige „N verbundene Buchungen" und verhindert, dass derselbe Händler
    # zusätzlich automatisch als Abo erkannt + doppelt gezählt wird.
    match_keyword: Mapped[str | None] = mapped_column(String(120), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    category: Mapped[Category | None] = relationship()


class DismissedMerchant(Base):
    """Händler-Schlüssel, die NICHT als Abo gelten sollen (falsch erkannt).

    Die Auto-Erkennung blendet diese Schlüssel künftig aus.
    """

    __tablename__ = "dismissed_merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_key: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ArchivedReceipt(Base):
    """Quittung, die OHNE Bankeintrag abgelegt ist (z.B. Belege vor dem Start der
    E-Banking-Daten). Sie verschwindet aus dem Zuordnungs-Assistenten, die Datei
    bleibt im Ordner. ``reason``: ``vor-banktabelle`` (automatisch) | ``manuell``.
    """

    __tablename__ = "archived_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), unique=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------
# Treffen-Fonds: gemeinsamer Spar-Topf zweier Personen für gegenseitige Besuche
# ---------------------------------------------------------------------------
#
# Die beiden Personen heissen im Schema **A** und **B**, nicht mit Namen. Das
# war einmal anders — die Spalten trugen die Vornamen, und das Feld ``person``
# ihre Kurzform. Ein Schema ist aber kein Notizzettel: es steht im Quelltext, in
# jeder Migration und in jedem Backup. Wer es liest, erfährt eine Beziehung,
# zwei Länder und Monatsbeträge.
#
# Die ANZEIGENAMEN stehen jetzt in den Einstellungen und damit in den Daten.
# Dasselbe Prinzip wie beim Anbieterprofil: was zu einer Person gehört, gehört
# nicht in den Code.


def _fonds_start() -> date:
    """Vorgabe für den Fonds-Start: 1. Januar des laufenden Jahres.

    **Kein fest eingetragenes Datum.** Ein solches veraltet lautlos in die
    falsche Richtung: es bleibt stehen, während die Gegenwart weiterläuft. Liegt
    es voraus, bietet die Monatsliste keinen einzigen vergangenen Monat an —
    also genau die, die man nachtragen will. Liegt es lange zurück, wächst die
    Liste um Monate, in denen es den Fonds nicht gab.

    Der Jahresanfang bindet die Vorgabe an den Zeitpunkt des ersten Zugriffs und
    hält damit das laufende Jahr nachtragbar. Verschieben lässt er sich weiter,
    aber niemand MUSS es tun, nur um einen vergessenen Monat einzutragen.
    """
    return heute_lokal().replace(month=1, day=1)


class MeetFundSettings(Base):
    """Einstellungen des Treffen-Fonds (genau EINE Zeile, Single-User).

    Planungs-Modul mit EINER Verbindung zur Buchhaltung: ``holiday_account_id``
    (siehe unten). **A** spart in der Währung der App (CHF), **B** in EUR;
    gerechnet wird alles in CHF über den **manuell** gepflegten Kurs (kein
    Live-Abruf: die App bleibt offline). Alle Kosten-Faktoren sind im UI
    anpassbar.
    """

    __tablename__ = "meet_fund_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Anzeigenamen der beiden Personen. Sie stehen HIER und nicht im Code:
    # so trägt das Schema keine Namen, und wer die App benutzt, sieht trotzdem
    # seine eigenen.
    name_a: Mapped[str] = mapped_column(String(40), default="Odysseus")
    name_b: Mapped[str] = mapped_column(String(40), default="Penelope")
    monthly_a_chf: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("300"))
    monthly_b_eur: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("100"))
    eur_chf_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0.95"))
    # Kosten-Faktoren je Besuch (alle in CHF; Flug = Hin+Rück pro Besuch).
    flight_a_chf: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("300"))
    flight_b_chf: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("300"))
    airbnb_night_chf: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("100"))
    food_day_chf: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("30"))
    default_nights: Mapped[int] = mapped_column(Integer, default=3)  # Fr–Mo
    start_month: Mapped[date] = mapped_column(Date, default=_fonds_start)
    start_balance_chf: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    # Das Konto, auf dem die Rückstellung wirklich liegt (Ferienkonto im
    # E-Banking). NULL = keins gewählt; dann bleibt der ganze Abgleich aus der
    # Oberfläche. Ohne diese Spalte war „bestätigt" nur eine Behauptung: der
    # Klick sagte, das Geld sei zurückgelegt, und nichts prüfte es nach.
    holiday_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MeetContribution(Base):
    """Bestätigte Monats-Rücklage einer Person (``a`` | ``b``).

    ``amount_native`` ist der Betrag in der Spar-Währung der Person (A in CHF,
    B in EUR) — eingefroren zum Bestätigungszeitpunkt. Die CHF-Summe wird immer
    mit dem AKTUELLEN Kurs gerechnet: die Euro von B liegen real in Euro da, ihr
    CHF-Wert bewegt sich mit dem Kurs.
    """

    __tablename__ = "meet_contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[date] = mapped_column(Date)  # 1. des Monats
    person: Mapped[str] = mapped_column(String(10))  # a | b
    amount_native: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("month", "person", name="uq_meet_contribution"),)


class MeetVisit(Base):
    """Geplantes oder vergangenes Treffen.

    ``location`` sagt, WO man sich trifft, und daraus folgt, wer reist:
    ``bei_b`` (A reist: Flug + Unterkunft×Nächte + Verpflegung)
    | ``bei_a`` (B reist: Flug + Verpflegung, keine Übernachtungskosten).
    Die Werte hiessen einmal nach den beiden Ländern — auch das war eine
    Auskunft, die niemanden etwas angeht.
    Verpflegungstage = Nächte + 1. Vergangene Treffen (Datum ≤ heute) sind
    ausgegebenes Geld und mindern den Topf; künftige senken nur die Prognose.
    ``cost_override_chf`` ersetzt die Formel, wenn gesetzt (z.B. echter Endpreis).
    """

    __tablename__ = "meet_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    location: Mapped[str] = mapped_column(String(10))  # bei_b | bei_a
    nights: Mapped[int] = mapped_column(Integer, default=3)
    cost_override_chf: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ArtikelAlias(Base):
    """Eine gelesene Schreibweise und die, die bestätigt wurde.

    Die Erkennung liest denselben Artikel auf jedem Beleg etwas anders
    („MUSTEBOL" statt „Musterol"). Ohne diese Tabelle korrigiert man das
    jedes Mal von Neuem, und der Preisverlauf führt eine Ware unter drei Namen —
    drei Verläufe mit je einem Punkt statt einem mit dreien.

    ``alias_key`` ist der normalisierte Schlüssel der gelesenen Schreibweise
    (dieselbe Normalisierung wie im Preisverlauf), ``kanonisch`` der Name, der
    angezeigt und gespeichert wird.

    **Einträge entstehen nur durch Bestätigung** — beim Korrigieren im
    Beleg-Editor oder beim Vereinheitlichen des Bestands. Geraten wird nichts:
    ähnliche Namen werden vorgeschlagen, nie im Stillen zusammengelegt.
    """

    __tablename__ = "artikel_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    kanonisch: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ScanProtokoll(Base):
    """Was eine Beleg-Erkennung gesehen hat — für die Fehlersuche.

    **Warum es das gibt.** Wenn die Erkennung danebenliegt, ist der Rohtext die
    einzige Antwort auf „warum steht da das?". Bisher war er nur im offenen
    Dialog zu haben: Fenster zu, Text weg. Man musste den Beleg abfotografieren
    und das Bild schicken, damit sich der Fehler nachstellen liess — für jeden
    einzelnen Fall, und jedes Mal von Hand.

    Hier bleibt er stehen. Der letzte Scan ist damit auch morgen noch
    nachvollziehbar, ohne Papier, ohne Foto.

    **Nur der Text, nicht das Bild.** Das Foto zu behalten ist eine eigene
    Entscheidung (``User.receipt_photo_keep``, standardmässig aus). Der Text
    reicht für die Fehlersuche und ist ein Bruchteil so gross.

    Alt einträge fallen weg (:data:`services.scan_protokoll.MAX_EINTRAEGE`) —
    ein Protokoll, das ewig wächst, ist ein Datenlager, kein Werkzeug.
    """

    __tablename__ = "scan_protokoll"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    # Was die Erkennung daraus gemacht hat — zum Vergleich mit dem Rohtext.
    haendler: Mapped[str | None] = mapped_column(String(160), nullable=True)
    betrag: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    beleg_datum: Mapped[date | None] = mapped_column(Date, nullable=True)
    # "ocr" | "text-layer" | "none" — sagt, ob überhaupt gelesen wurde.
    methode: Mapped[str] = mapped_column(String(16), default="")
    positionen: Mapped[int] = mapped_column(Integer, default=0)
    ocr_text: Mapped[str] = mapped_column(Text, default="")


class MetricSeries(Base):
    """Eine Verlaufsreihe — Prämie, Stromrechnung, Lohn, Steuern.

    **Bewusst getrennt von den Buchungen.** Diese Werte stammen aus Belegen
    (Police, Rechnung, Vorsorgeausweis), nicht aus dem Konto. Die
    Krankenkassenprämie steht bereits als monatliche Belastung in den
    Transaktionen; würde der Beleg zusätzlich als Buchung importiert, zählte
    jeder Monat doppelt und die Monatsbilanz wäre falsch. Reihen sind darum eine
    eigene Schicht, die man gegen die Buchungen *vergleichen* kann — siehe
    ``services.metrics.soll_ist``.

    ``secondary_key`` benennt den Wert aus ``MetricPoint.extras``, den die
    Verlaufsseite auf einer zweiten Achse zeichnet. Bei Strom ist das der
    Verbrauch in kWh: erst dadurch lässt sich eine höhere Rechnung als „mehr
    verbraucht" oder „teurer geworden" lesen.
    """

    __tablename__ = "metric_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stabiler Schlüssel für Importe und Seeds — der Anzeigename darf sich ändern.
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    unit: Mapped[MetricUnit] = mapped_column(_str_enum(MetricUnit, 10))
    cadence: Mapped[MetricCadence] = mapped_column(_str_enum(MetricCadence, 16))
    kind: Mapped[MetricKind] = mapped_column(_str_enum(MetricKind, 12))
    secondary_key: Mapped[str | None] = mapped_column(String(30), nullable=True)
    secondary_unit: Mapped[MetricUnit | None] = mapped_column(
        _str_enum(MetricUnit, 10), nullable=True
    )
    secondary_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Kategorie, in der die Zahlungen zu dieser Reihe gebucht sein sollten.
    # Ermöglicht den Soll/Ist-Abgleich: der Beleg sagt, was verlangt wurde,
    # die Buchungen sagen, was wirklich abging.
    #
    # Als Spalte und nicht als Stichwortsuche über den Kategorienamen: der
    # Steuerauszug macht es über Stichwörter, und genau dort blieben Positionen
    # stillschweigend leer, wenn kein Name passte. Eine Verknüpfung, die der
    # Nutzer sehen und korrigieren kann, scheitert wenigstens sichtbar.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    points: Mapped[list[MetricPoint]] = relationship(
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="MetricPoint.period_start",
    )


class MetricPoint(Base):
    """Ein Messwert einer Reihe für eine Periode.

    Die Periode ist ein Intervall, kein Stichtag: eine Stromrechnung deckt ein
    Quartal ab, eine Prämienabrechnung einen Monat. ``period_end`` wird
    gebraucht, um Quartals- und Jahreswerte für den Vergleich sauber auf Monate
    zu verteilen — ohne das sähe eine Jahresrechnung wie ein einzelner
    Ausreisser aus.

    ``extras`` hält benannte Nebenwerte (kWh, Beitrag Arbeitgeber, versicherter
    Lohn). Als JSON, weil die Zahl der Nebenwerte je Reihe verschieden ist und
    feste Spalten für jede Reihe die Tabelle aufblähen würden.

    **Konventionen in ``extras``.** Das Feld ist flach — ``dict[str, str]``, ein
    Wert nie ein Objekt oder eine Liste. Mehr Struktur entsteht allein über den
    Schlüsselnamen, und dafür gelten drei Regeln:

    * ``unsicher`` — Marke ohne Aussagekraft im Wert. Der Punkt stammt aus einer
      OCR-Quelle und ist noch nicht bestätigt; die Verlaufsseite zeichnet ihn
      gestrichelt. Gesetzt und entfernt wird er in ``routers/metrics.py``.
    * ``pos:<Name>`` — **eine Position eines aufgeschlüsselten Belegs.** Der Wert
      ist ein Dezimal-String in der Einheit der Reihe; ein negatives Vorzeichen
      bedeutet Rabatt oder Gutschrift. Der Name hinter dem Doppelpunkt ist der
      Positionsname des Belegs und darf selbst Doppelpunkte enthalten — getrennt
      wird nur am ersten. Es gilt: **Summe aller ``pos:``-Werte plus ``rundung``
      ergibt ``value``.** Wer Positionen schreibt, ohne diese Gleichung zu
      erfüllen, macht den gestapelten Balken zur Lüge; darum prüft der Parser
      sie gegen den Beleg, bevor ein Punkt überhaupt entsteht.
    * jeder andere Schlüssel ist ein benannter Nebenwert der Reihe (``kwh``,
      ``franchise``, ``rundung``, ``rabatt``). Genau einer davon kann über
      ``MetricSeries.secondary_key`` als zweite Kurve gezeigt werden.

    ``source`` ist reine Herkunftsangabe — der Belegname, damit nachvollziehbar
    bleibt, woher ein Wert kam. Manuell erfasste Punkte lassen sie leer.
    """

    __tablename__ = "metric_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(
        ForeignKey("metric_series.id", ondelete="CASCADE"), index=True
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    extras: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    series: Mapped[MetricSeries] = relationship(back_populates="points")

    # Eine Periode je Reihe genau einmal. Der Einmal-Import darf mehrfach laufen,
    # ohne Werte zu verdoppeln, und eine Nacherfassung überschreibt statt zu häufen.
    __table_args__ = (
        UniqueConstraint("series_id", "period_start", name="uq_metric_point_periode"),
    )


# ---------------------------------------------------------------------------
# Gemeinsame Query-Bausteine
# ---------------------------------------------------------------------------


def not_transfer() -> ColumnElement[bool]:
    """SQL-Filter: schliesst Umbuchungen (``management_type=TRANSFER``) aus.

    Wird in Dashboard, Budget und Kategorisierung gebraucht. Wichtig ist die
    ``is_(None)``-Hälfte: In SQL ist ``NULL != 'T'`` nämlich *nicht* True,
    sondern NULL — ohne diese Hälfte würden Buchungen ohne ``management_type``
    fälschlich herausfallen. Darum hier zentral, statt überall neu zu tippen.
    """
    return or_(
        Transaction.management_type.is_(None),
        Transaction.management_type != ManagementType.TRANSFER,
    )


# Fluchtzeichen für LIKE. Backslash ist die übliche Wahl und in Buchungstexten
# praktisch nie zu finden — er muss trotzdem selbst maskiert werden, sonst
# entkäme die Maskierung über einen eingegebenen Backslash.
LIKE_FLUCHT = "\\"


def like_escape(text: str) -> str:
    """Maskiert die LIKE-Platzhalter ``%`` und ``_`` in einem Suchtext.

    Ohne das ist jedes ``%`` im Suchfeld ein „passt auf alles": die Suche ``%``
    trifft den GANZEN Bestand, während die Oberfläche daneben behauptet, ein
    Filter sei aktiv. Bei der Massen-Zuweisung ist genau das der teuerste Fall —
    die Vorschau soll ja davor schützen.

    Verworfen: die Zeichen einfach wegwerfen. Dann fände „50%" oder „Konto_2"
    nichts mehr, obwohl es solche Buchungstexte gibt; gesucht wird nach dem
    Zeichen, nicht nach einem Muster.
    """
    return (
        text.replace(LIKE_FLUCHT, LIKE_FLUCHT * 2)
        .replace("%", LIKE_FLUCHT + "%")
        .replace("_", LIKE_FLUCHT + "_")
    )


def enthaelt(spalte: ColumnElement, text: str) -> ColumnElement[bool]:
    """SQL-Filter „Spalte enthält den Suchtext" — mit maskierten Platzhaltern.

    Der einzige Ort im Projekt, an dem ein ``%…%``-Muster aus Nutzereingaben
    gebaut wird. Zentral, weil Vorschau und Ausführung der Massen-Zuweisung
    buchstäblich dieselbe Bedingung erzeugen müssen: liefe die Maskierung nur an
    einer der beiden Stellen, zeigte die Vorschau eine andere Menge an, als der
    Knopf anfasst — schlimmer als gar keine Vorschau.
    """
    return spalte.ilike(f"%{like_escape(text)}%", escape=LIKE_FLUCHT)


class SeedMarke(Base):
    """Merkt, welche nachgelieferten Vorgaben schon einmal angelegt wurden.

    **Warum das nicht an der Kategorie hängen darf.** ``ensure_extra_categories``
    prüfte, ob eine Kategorie mit diesem Namen existiert — und legte sie sonst
    an, bei JEDEM Start. Wer eine der acht löschte, hatte sie nach dem nächsten
    Neustart wieder; wer sie umbenannte, hatte sie ZUSÄTZLICH, und ab da liefen
    Buchungen auf zwei Töpfe für dieselbe Sache. Das sind falsche Zahlen in jeder
    Auswertung, ohne dass irgendwo etwas fehlschlägt.

    Ein Schlüssel AN der Kategorie hätte das nicht gelöst: mit der Zeile ginge
    auch die Merkung. Deshalb eine eigene Tabelle — sie sagt nicht „diese
    Kategorie existiert", sondern „diese Vorgabe wurde dem Nutzer schon einmal
    angeboten". Was er danach damit macht, ist seine Sache.
    """

    __tablename__ = "seed_marks"

    schluessel: Mapped[str] = mapped_column(String(60), primary_key=True)
    angelegt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------
# Sammlung aller Models — wird in Alembic-env.py importiert.
# ---------------------------------------------------------------------------

__all__ = [
    "Account",
    "AccountType",
    "ArchivedReceipt",
    "Attachment",
    "Base",
    "Budget",
    "BudgetInterval",
    "Category",
    "CategoryRule",
    "DismissedMerchant",
    "GoalPriority",
    "ManualSubscription",
    "ImportBatch",
    "ImportSource",
    "ImportStatus",
    "Lohnabrechnung",
    "LohnHerkunft",
    "Lohnposten",
    "LohnPostenArt",
    "ManagementType",
    "SavingsGoal",
    "SeedMarke",
    "StandardBudget",
    "ThemePref",
    "Transaction",
    "TransactionSplit",
    "User",
    "enthaelt",
    "like_escape",
    "not_transfer",
]
