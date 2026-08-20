"""Authentifizierungs-Layer.

Zwei Verfahren laufen parallel:

* ``pin``      — Standard-Login per 6-stelliger PIN (Argon2-Hash, signiertes Cookie).
* ``webauthn`` — Passkey/Biometrie als zusätzliche Anmeldung (am Gerät zu testen).
"""
