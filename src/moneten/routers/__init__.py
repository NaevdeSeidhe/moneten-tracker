"""FastAPI-Router der App (je Seite/Bereich ein Modul).

Enthält u.a. ``auth_pin`` (Login/Logout), ``dashboard`` (Übersicht),
``transactions``, ``budget``, ``accounts``, ``savings_goals``, ``subscriptions``,
``import_bank``, ``rules``, ``forecast``, ``compare``, ``quick`` und
``settings_view`` (Theme + PIN). Alle datentragenden Routen erzwingen Login.
"""
