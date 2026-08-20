# Fremde Bestandteile

Der Code in diesem Repository steht unter der MIT-Lizenz (siehe [LICENSE](LICENSE)).
Er läuft aber nicht allein. Diese Datei sagt, was mitläuft und unter welchen
Bedingungen — vollständig für alles, was **mitausgeliefert** wird, und für jede
direkte Laufzeit-Abhängigkeit aus `pyproject.toml`.

Die Angaben stammen aus den Paket-Metadaten der installierten Versionen, nicht aus
einer Abschrift von Hand.

---

## Das Wichtigste zuerst: PyMuPDF ist AGPL

**PyMuPDF (`fitz`) steht unter GNU AGPL 3.0 oder einer kommerziellen Artifex-Lizenz
— nicht unter MIT.**

Die App benutzt es zum Lesen von PDF-Belegen und Rechnungen
(`services/receipt_ocr.py`, `services/pdf_spalten.py`). Was das heisst:

* **Für den privaten Betrieb im eigenen Netz** — der Normalfall dieser App —
  ändert es nichts. Die AGPL greift beim *Weitergeben* und beim Anbieten über ein
  Netz an Dritte, nicht beim eigenen Gebrauch.
* **Wer die App öffentlich anbietet** (für andere Menschen erreichbar im Internet),
  löst mit der AGPL die Pflicht aus, den Quelltext der laufenden Fassung
  herauszugeben. Bei diesem Repository ist das ohnehin gegeben; bei eigenen
  Änderungen muss man sie ebenfalls offenlegen.
* **Wer PDF-Belege nicht braucht**, kann PyMuPDF weglassen. Dann fallen die
  PDF-Pfade aus; CSV- und CAMT.053-Import, Fotobelege und alles andere laufen
  weiter.
* Wer die AGPL nicht will, aber PDF braucht, kauft eine Artifex-Lizenz oder tauscht
  die Bibliothek gegen eine permissive (z.B. `pypdfium2`, BSD).

Der MIT-lizenzierte Code dieses Repositorys bleibt davon unberührt — die
Kombination, die auf einem Rechner läuft, unterliegt aber der AGPL.

---

## Mitausgelieferte Dateien

Diese liegen als Datei im Repository und gehen bei jedem Klon mit:

| Bestandteil | Version | Lizenz | Volltext daneben |
| --- | --- | --- | --- |
| **Poppins** (WOFF2, 4 Dateien) | v24 | SIL Open Font License 1.1 | [`src/moneten/static/fonts/OFL.txt`](src/moneten/static/fonts/OFL.txt) |
| **htmx** (`htmx.min.js`) | 2.0.3 | Zero-Clause BSD (0BSD) | [`src/moneten/static/js/htmx-LICENSE.txt`](src/moneten/static/js/htmx-LICENSE.txt) |

Beide Lizenztexte liegen **als Datei bei**, nicht als Verweis ins Netz: die OFL
verlangt ausdrücklich, dass Lizenz und Copyright-Vermerk die Schrift begleiten.

**Stand der Prüfung auf Aktualisierungen: 2026-08-17.** Diese beiden Dateien
liegen im Repository und sind damit für `pip-audit` unsichtbar — es gibt sonst
keine Stelle, die je wieder über eine htmx-Lücke reden würde. Ein Test in
`tests/test_fremdlizenzen.py` schlägt fehl, sobald dieses Datum ein halbes Jahr
alt ist. Dann: Veröffentlichungen von htmx durchsehen, bei Bedarf
`htmx.min.js` austauschen, die Fassung in der Tabelle oben nachziehen und dieses
Datum neu setzen.

Alle Icons und das Logo sind eigene Zeichnungen (SVG-Sprite in
`templates/partials/icon_sprite.html`, PNG-Symbole in `static/img/`).

---

## Laufzeit-Abhängigkeiten

Aus `pyproject.toml`, mit der Lizenz, die das jeweilige Paket selbst angibt:

| Paket | Version | Lizenz |
| --- | --- | --- |
| `fastapi` | 0.136.3 | MIT |
| `uvicorn` | 0.48.0 | BSD-3-Clause |
| `SQLAlchemy` | 2.0.50 | MIT |
| `alembic` | 1.18.4 | MIT |
| `Jinja2` | 3.1.6 | BSD-3-Clause |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `pydantic` | 2.13.4 | MIT |
| `pydantic-settings` | 2.15.0 | MIT |
| `itsdangerous` | 2.2.0 | BSD-3-Clause |
| `argon2-cffi` | 25.1.0 | MIT |
| `python-dotenv` | 1.2.3 | BSD-3-Clause |
| `webauthn` | 3.0.0 | BSD-3-Clause |
| **`PyMuPDF`** | 1.27.2.3 | **AGPL-3.0 oder Artifex Commercial** (siehe oben) |
| `rapidocr-onnxruntime` | 1.4.4 | Apache-2.0 |
| `pytesseract` | 0.3.13 | Apache-2.0 |
| `pillow` | 12.3.0 | MIT-CMU |
| `tzdata` | 2026.3 | Apache-2.0 |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `cryptography` | 50.0.0 | Apache-2.0 oder BSD-3-Clause |
| `pyasn1` | 0.6.4 | BSD-2-Clause |

Ausser PyMuPDF ist alles permissiv (MIT/BSD/Apache) und verträgt sich mit MIT.

**Nicht mitgezählt:** die Abhängigkeiten der Abhängigkeiten und die reinen
Test-Werkzeuge (`pytest`, `ruff`), die nie mitlaufen. Die vollständige Kette
listet:

```bash
pip install pip-licenses && pip-licenses --with-urls --format=markdown
```

---

## Optionale äussere Werkzeuge

Diese werden **nicht** mitgeliefert und nicht automatisch installiert; die App
läuft ohne sie und schaltet die betroffene Funktion einfach ab:

| Werkzeug | Wozu | Lizenz |
| --- | --- | --- |
| Tesseract OCR | Texterkennung auf fotografierten Belegen | Apache-2.0 |

Wer es installiert, tut das selbst und unter dessen eigener Lizenz.
