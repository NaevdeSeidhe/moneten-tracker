"""Der Rohtext muss IM Beleg-Fenster stehen — nicht irgendwo im Markup.

Er ist die einzige Antwort auf „warum steht da das?". Beim ersten Versuch stand
er unterhalb der Knöpfe und ausserhalb von ``.kz-sheet``, also ausserhalb der
sichtbaren Dialogfläche — schlicht nicht vorhanden. Man musste den
Fehler dreimal melden, bevor die Ursache gefunden war.

Geprüft wird die REIHENFOLGE im Markup, nicht bloss das Vorkommen: „ist da"
war schon beim ersten Versuch wahr.
"""

from __future__ import annotations

from moneten.templating import templates

ROHTEXT = "Apotheke Zentral\nBahnhofplatz 1\nWare 17.25\nTotal 17.25"


def _render(**kontext) -> str:
    vorlage = templates.env.get_template("partials/receipt_scan_editor.html")
    basis = {"receipt": {"items": []}, "categories": [], "ocr_text": "",
             "image_path": "", "error": None, "no_text": False}
    return vorlage.render({**basis, **kontext})


def test_rohtext_steht_zwischen_beleg_und_knoepfen():
    """Innerhalb der Dialogfläche: nach dem Beleg, vor den Knöpfen."""
    html = _render(ocr_text=ROHTEXT)
    beleg = html.index('id="kz-paper"')
    roh = html.index('class="kz-roh"')
    knoepfe = html.index('class="kz-actions"')
    assert beleg < roh < knoepfe, (
        "Der Rohtext sitzt nicht zwischen Beleg und Knöpfen — genau so war er "
        "beim ersten Versuch ausserhalb der sichtbaren Fläche gelandet."
    )


def test_rohtext_ist_aufklappbar_und_kopierbar():
    """Aufklappbar, weil er im Normalfall niemanden interessiert.

    Und als ``<pre>``, damit die Spalten des Belegs stehen bleiben — in einer
    Fliesstext-Zeile wäre er nicht mehr das, was die Erkennung gesehen hat.
    """
    html = _render(ocr_text=ROHTEXT)
    assert "<details" in html and "<summary>" in html
    assert "<pre>" in html
    assert "Bahnhofplatz 1" in html


def test_ohne_rohtext_kein_leerer_aufklapper():
    """Kein Aufklapper, der nichts enthält."""
    assert 'class="kz-roh"' not in _render(ocr_text="")


def test_das_versteckte_feld_bleibt():
    """Der Bestätigen-POST liest den Rohtext aus ``#kz-ocr`` — das darf nicht
    dem sichtbaren Aufklapper zum Opfer fallen."""
    html = _render(ocr_text=ROHTEXT)
    assert 'id="kz-ocr"' in html and "hidden" in html
