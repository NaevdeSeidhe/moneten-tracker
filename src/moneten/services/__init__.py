"""Service-Layer (Business-Logik, weitgehend datenbank-/UI-unabhängig).

Hier liegen u.a.:

* ``camt053_parser`` / ``csv_parser`` — Bank-Auszüge einlesen
* ``receipt_ocr`` / ``receipt_match`` / ``receipt_split`` — Belege auslesen,
  zuordnen und in Kategorie-Anteile aufteilen (Auto-Split)
* ``categorization`` — Regel-Engine + Lern-Vorschläge
* ``splits`` — Kategorie-Auswertungen, die Aufteilungen berücksichtigen
* ``median_budget`` — Budget-Vorschläge aus dem Median der letzten Monate
* ``forecasting`` — 12-Monats-Prognose + Stresstest
* ``account_charts`` / ``charts`` / ``sankey`` — Diagramm-Geometrie (offline-SVG)
* ``comparison`` / ``subscriptions`` / ``balances`` / ``attachments``
"""
