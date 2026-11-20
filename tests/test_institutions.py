from types import SimpleNamespace

from bson import ObjectId

from app.routers import institutions as institution_routes
from app.services.institutions import public_institution, valid_timezone


def test_create_institution_does_not_store_timezone(monkeypatch):
    inserted = {}
    institution_id = ObjectId()

    def insert_one(document):
        inserted.update(document)
        return SimpleNamespace(inserted_id=institution_id)

    db = SimpleNamespace(institutions=SimpleNamespace(insert_one=insert_one))
    monkeypatch.setattr(institution_routes, "check_platform", lambda user: None)
    monkeypatch.setattr(institution_routes, "db_required", lambda: db)

    result = institution_routes.create_institution(
        {"name": "Institución de prueba", "slug": "institucion-prueba", "timezone": "Asia/Tokyo"},
        {"role": "super_admin"},
    )

    assert "timezone" not in inserted
    assert inserted["status"] == "active"
    assert result["data"] == {"id": str(institution_id), "status": "active"}


def test_public_institution_omits_legacy_timezone():
    result = public_institution(
        {
            "_id": ObjectId(),
            "name": "Institución de prueba",
            "slug": "institucion-prueba",
            "timezone": "America/Mexico_City",
            "period_type": "quarter",
        }
    )

    assert "timezone" not in result


def test_valid_timezone_accepts_device_iana_zone():
    assert valid_timezone("America/Mexico_City") == "America/Mexico_City"
