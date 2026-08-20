"""Datenbank-Layer.

Enthält die SQLAlchemy-Models, den Session-Factory und die Seed-Logik.
Die Modul-Struktur entspricht der Aufteilung im Konzept (Abschnitt 5):

* ``models``  — alle ORM-Klassen (User, Account, Category, ...)
* ``session`` — Engine, SessionLocal, FastAPI-Dependency
* ``seeds``   — Initial-Befüllung (Standard-Konten, Kategorien)
"""
