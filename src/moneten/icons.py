"""Zentrale Icon-Bibliothek (offline, Tabler-Stil).

Eine Liste aller verfügbaren Icons mit deutschem Label + Such-Stichwörtern.
Jeder ``name`` muss als ``<symbol id="i-{name}">`` in
``templates/partials/icon_sprite.html`` existieren. Diese Liste ist die
einzige Wahrheit: ``templating._ICON_NAMES`` leitet sich daraus ab, und der
Icon-Picker der Kategorie-Verwaltung rendert genau diese Einträge (mit
``keywords`` fürs Suchfeld).

Bewusst kuratiert (~75 Stück, alle sauber gezeichnet) statt einer riesigen,
teils kaputten Sammlung — offline-tauglich, alle currentColor.
"""

from __future__ import annotations

# (name, label, keywords) — keywords klein geschrieben, leerzeichengetrennt.
ICONS: list[dict] = [
    # — Wohnen / Haushalt —
    {"name": "home", "label": "Haus", "keywords": "haus wohnen zuhause miete wohnung daheim"},
    {"name": "home-2", "label": "Haus (Detail)", "keywords": "haus wohnen wohnung gebäude zuhause"},
    {"name": "key", "label": "Schlüssel", "keywords": "miete schlüssel wohnung zugang schloss"},
    {"name": "sofa", "label": "Sofa", "keywords": "möbel sofa couch einrichtung wohnen"},
    {"name": "bed", "label": "Bett", "keywords": "hotel bett schlafen übernachtung möbel"},
    {"name": "bolt", "label": "Blitz", "keywords": "strom elektrizität energie blitz heizung"},
    {"name": "bulb", "label": "Glühbirne", "keywords": "strom licht lampe energie idee"},
    {"name": "flame", "label": "Flamme", "keywords": "heizung gas feuer flamme rauchen"},
    {"name": "droplet", "label": "Tropfen", "keywords": "wasser tropfen flüssigkeit nebenkosten"},
    {"name": "wifi", "label": "WLAN", "keywords": "internet wlan wifi netz online"},
    {"name": "device-tv", "label": "TV", "keywords": "tv fernsehen streaming media"},
    {"name": "plant", "label": "Pflanze", "keywords": "pflanze garten blumen natur grün"},
    # — Lebensmittel / Essen —
    {"name": "shopping-cart", "label": "Einkaufswagen", "keywords": "einkauf einkaufen lebensmittel supermarkt wagen"},
    {"name": "basket", "label": "Korb", "keywords": "einkauf korb lebensmittel markt"},
    {"name": "tools-kitchen-2", "label": "Besteck", "keywords": "essen restaurant küche gabel auswärts"},
    {"name": "coffee", "label": "Kaffee", "keywords": "kaffee getränk café trinken"},
    {"name": "glass-full", "label": "Glas", "keywords": "alkohol getränk wein bar drink bier"},
    {"name": "cookie", "label": "Keks", "keywords": "snack süssigkeit keks naschen"},
    # — Mobilität —
    {"name": "car", "label": "Auto", "keywords": "auto fahrzeug wagen transport parken"},
    {"name": "fuel", "label": "Tanken", "keywords": "tanken benzin diesel tankstelle treibstoff"},
    {"name": "bus", "label": "Bus", "keywords": "öv bus transport verkehr fahrt"},
    {"name": "train", "label": "Zug", "keywords": "zug öv bahn transport pendeln ga"},
    {"name": "bike", "label": "Velo", "keywords": "velo fahrrad bike sport transport"},
    {"name": "beach", "label": "Strand", "keywords": "ferien reise urlaub strand sonne"},
    # — Gesundheit / Körper —
    {"name": "stethoscope", "label": "Arzt", "keywords": "gesundheit arzt medizin praxis"},
    {"name": "pill", "label": "Pille", "keywords": "medikament apotheke gesundheit pille"},
    {"name": "tooth", "label": "Zahn", "keywords": "zahnarzt zähne gesundheit dentist"},
    {"name": "eye", "label": "Auge", "keywords": "optiker brille auge sehen"},
    {"name": "scissors", "label": "Schere", "keywords": "coiffeur friseur haare schere"},
    {"name": "sparkles", "label": "Funkeln", "keywords": "kosmetik schönheit drogerie pflege putzen"},
    {"name": "heart", "label": "Herz", "keywords": "gesundheit liebe spende herz wohltätig"},
    {"name": "heart-b", "label": "Herz mit Buchstabe", "keywords": "sparziel treffen herz ziel"},
    {"name": "dumbbell", "label": "Hantel", "keywords": "fitness sport gym kraft hantel training"},
    {"name": "running", "label": "Laufen", "keywords": "sport laufen joggen fitness bewegung"},
    # — Freizeit / Hobby —
    {"name": "device-gamepad-2", "label": "Gamepad", "keywords": "gaming spiele games konsole"},
    {"name": "music", "label": "Musik", "keywords": "musik streaming konzert hobby spotify"},
    {"name": "guitar-pick", "label": "Plektrum", "keywords": "musik hobby gitarre instrument"},
    {"name": "camera", "label": "Kamera", "keywords": "foto kamera fotografie hobby bilder"},
    {"name": "ticket", "label": "Ticket", "keywords": "ticket eintritt billet kino veranstaltung"},
    {"name": "book", "label": "Buch", "keywords": "bildung buch lesen schule kurs weiterbildung"},
    {"name": "gift", "label": "Geschenk", "keywords": "geschenk präsent gutschein spende"},
    {"name": "paw", "label": "Pfote", "keywords": "haustier tier hund katze pfote tierarzt"},
    # — Kleidung —
    {"name": "shirt", "label": "Shirt", "keywords": "kleider kleidung mode shirt textil schuhe"},
    # — Technik / Abos —
    {"name": "device-mobile", "label": "Handy", "keywords": "handy smartphone telefon abo mobile"},
    {"name": "device-laptop", "label": "Laptop", "keywords": "laptop computer technik elektronik"},
    {"name": "code", "label": "Code", "keywords": "software code entwicklung programm app"},
    {"name": "cloud", "label": "Wolke", "keywords": "cloud software speicher online abo"},
    {"name": "message-circle-2", "label": "Nachricht", "keywords": "nachricht chat kommunikation"},
    {"name": "calendar", "label": "Kalender", "keywords": "termin kalender datum abo planung"},
    {"name": "repeat", "label": "Wiederkehrend", "keywords": "abo wiederkehrend dauerauftrag wiederholen"},
    {"name": "mail", "label": "Brief", "keywords": "post brief mail porto sendung"},
    # — Arbeit / Einkommen —
    {"name": "briefcase", "label": "Aktentasche", "keywords": "arbeit job beruf büro lohn geschäft"},
    {"name": "id-badge", "label": "Ausweis", "keywords": "ausweis mitgliedschaft beitrag verein"},
    {"name": "arrow-down-right", "label": "Eingang", "keywords": "einnahme eingang gutschrift pfeil"},
    # — Geld / Finanzen —
    {"name": "wallet", "label": "Portemonnaie", "keywords": "geld portemonnaie bargeld wallet"},
    {"name": "cash", "label": "Bargeld", "keywords": "bargeld geld noten cash"},
    {"name": "credit-card", "label": "Kreditkarte", "keywords": "karte kreditkarte zahlung bezahlen visa"},
    {"name": "building-bank", "label": "Bank", "keywords": "bank konto geld institut sparkasse"},
    {"name": "pig-money", "label": "Sparschwein", "keywords": "sparen sparschwein geld rücklage"},
    {"name": "trending-up", "label": "Trend", "keywords": "anlage investition aktien wachstum rendite börse"},
    {"name": "chart-line", "label": "Verlauf", "keywords": "verlauf entwicklung kurve diagramm zeitreihe messwerte"},
    {"name": "target", "label": "Ziel", "keywords": "ziel sparziel budget zielwert"},
    {"name": "arrows-exchange", "label": "Tausch", "keywords": "transfer umbuchung tausch wechsel"},
    {"name": "receipt", "label": "Quittung", "keywords": "quittung beleg rechnung bon"},
    # — Versicherung / Schutz —
    {"name": "shield", "label": "Schild", "keywords": "versicherung schutz schild sicherheit"},
    {"name": "shield-check", "label": "Schild OK", "keywords": "versicherung schutz haftpflicht geprüft"},
    {"name": "shield-lock", "label": "Schild Schloss", "keywords": "versicherung schutz sicherheit datenschutz"},
    {"name": "umbrella", "label": "Schirm", "keywords": "versicherung schirm regen schutz vorsorge"},
    {"name": "alert-triangle", "label": "Warnung", "keywords": "warnung achtung gebühr busse steuer"},
    # — Allgemein / System —
    {"name": "tag", "label": "Etikett", "keywords": "etikett kategorie allgemein tag sonstiges"},
    {"name": "plus", "label": "Plus", "keywords": "plus neu hinzufügen"},
    {"name": "settings", "label": "Einstellungen", "keywords": "einstellungen zahnrad konfiguration"},
    {"name": "download", "label": "Download", "keywords": "download export herunterladen"},
    {"name": "upload", "label": "Upload", "keywords": "upload import hochladen einlesen"},
    {"name": "pencil", "label": "Stift", "keywords": "bearbeiten stift schreiben"},
    {"name": "archive", "label": "Archiv", "keywords": "archiv ablage box"},
    {"name": "trash", "label": "Mülleimer", "keywords": "löschen müll abfall entsorgen"},
    {"name": "rotate", "label": "Drehen", "keywords": "wiederherstellen drehen aktualisieren"},
    {"name": "search", "label": "Lupe", "keywords": "suchen suche lupe finden"},
    {"name": "dots", "label": "Menü-Punkte", "keywords": "menü aktionen mehr optionen punkte"},
    {"name": "check", "label": "Haken", "keywords": "haken erledigt ok bestätigen abhaken"},
]

ICON_NAMES: frozenset[str] = frozenset(i["name"] for i in ICONS)

# Reine UI-Chrome-Icons: via ``icon()`` nutzbar (⋯/Lupe/Haken in Buttons & Menüs),
# aber als Kategorie-Icon sinnlos → aus dem Icon-Picker der Kategorie-Verwaltung
# ausblenden. (ICON_NAMES bleibt vollständig, damit ``icon()`` sie weiter auflöst.)
_CHROME_ONLY: frozenset[str] = frozenset({"dots", "search", "check"})
PICKER_ICONS: list[dict] = [i for i in ICONS if i["name"] not in _CHROME_ONLY]
