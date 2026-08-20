# Änderungen

Grobe Übersicht, eine Zeile je Sache. Die ausführliche Entwicklungs-Historie
gehört nicht hierher.

## 0.82.0

- Beleg-Fotos werden verschlüsselt abgelegt (AES-256-GCM, Schlüssel aus `MONETEN_DB_KEY` abgeleitet)
- Die App prüft beim Start, dass die Datenbank-Verschlüsselung wirklich greift
- Sicherung prüft ihren eigenen Schnappschuss, bevor sie ihn behält
- Sitzungscookie mit `__Host-`, HSTS, `Cache-Control: no-store`
- Zugriffsprotokoll ohne Suchbegriffe
- Container mit `no-new-privileges` und ohne überflüssige Fähigkeiten
- Veröffentlichte Abbilder tragen eine Herkunftsbescheinigung (`gh attestation verify`)
- Höchstens vier gleichzeitige Beleg-Erkennungen
- Robustere Behandlung unsinniger Adress-Parameter

## 0.81.x

- Passkey anlegen und entfernen verlangt die PIN; Nutzerverifikation vorausgesetzt
- Gemeinsame Sperre nach zu vielen Fehlversuchen an allen PIN-Türen
- Bank-Import: Spaltenerkennung und CAMT-Erkennung korrigiert
- Sicherung verschlüsselt auch die Belege, wenn ein GPG-Empfänger gesetzt ist

## 0.80.0 und früher

- Erstveröffentlichung: Budget, Konten, Bank-Import, Beleg-Erkennung, Auswertungen
