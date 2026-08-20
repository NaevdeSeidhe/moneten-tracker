"""Globale Test-Fixtures.

* ``client`` — TestClient mit isolierter In-Memory-SQLite-DB.
* Die DB wird pro Test-Modul neu aufgebaut (Tabellen via ``Base.metadata.create_all``)
  und mit Seed-Daten befüllt. So sind Tests deterministisch.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Vor dem App-Import die Konfiguration auf eine temporäre DB umstellen.
_TMP = tempfile.TemporaryDirectory()
_DB_PATH = Path(_TMP.name) / "test_bilanz.db"

os.environ["MONETEN_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["MONETEN_ATTACHMENTS_DIR"] = str(Path(_TMP.name) / "attachments")
os.environ["MONETEN_SECRET_KEY"] = "test-secret-key-do-not-use-in-prod"
os.environ["MONETEN_INITIAL_PIN"] = "424242"
os.environ["MONETEN_DEV_MODE"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from moneten.db.models import Base  # noqa: E402
from moneten.db.seeds import seed_all  # noqa: E402
from moneten.db.session import SessionLocal, engine  # noqa: E402
from moneten.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _setup_schema() -> Iterator[None]:
    """Erzeugt das Schema und Seeds einmalig pro Testlauf."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_all(db)
    yield
    engine.dispose()
    _TMP.cleanup()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """HTTP-Test-Client. Cookies werden zwischen Aufrufen beibehalten."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _drossel_vor_jedem_test_leeren():
    """Die Bremse gegen durchprobierte PINs zählt IM PROZESS — also über Tests hinweg.

    Ohne diesen Griff färbte ein Test, der die Sperre absichtlich auslöst, auf
    jeden späteren ab: die Anmeldung in ``logged_in_client`` lief in dieselbe
    Sperre, und plötzlich scheiterten Tests, die mit PINs nichts zu tun haben —
    mit „Falsche PIN" als Begründung, was die Suche in die Irre schickt.

    Der Zähler gehört zum Laufzeit-Zustand, nicht zum Prüfgegenstand; er wird
    darum vor jedem Test geleert.
    """
    from moneten.auth import drossel

    drossel.zuruecksetzen()
    yield
    drossel.zuruecksetzen()


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """Wie ``client``, aber direkt nach erfolgreichem Login.

    Die PIN gilt hier als bereits gewechselt. Ohne das schickte die Sperre aus
    ``auth.pin.require_login`` JEDEN dieser Tests auf die Wechsel-Seite — sie
    prüften dann alle dasselbe. Der Zwang selbst hat eigene Tests in
    ``test_pin_erstwechsel.py``.
    """
    from datetime import UTC, datetime

    from moneten.db.models import User

    with SessionLocal() as db:
        user = db.get(User, 1)
        if user is not None and user.pin_changed_at is None:
            user.pin_changed_at = datetime.now(UTC)
            db.commit()

    response = client.post("/login", data={"pin": "424242"}, follow_redirects=False)
    assert response.status_code in (303, 204), response.text
    return client
