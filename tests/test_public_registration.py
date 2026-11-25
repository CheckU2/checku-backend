from types import SimpleNamespace
from unittest.mock import Mock

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import auth
from app.services import email_verification, public_registration


def setup_code(monkeypatch):
    monkeypatch.setattr(email_verification, "get_settings", lambda: SimpleNamespace(jwt_secret_key="test-key"))
    monkeypatch.setattr(public_registration.secrets, "randbelow", lambda _: 12345)
    monkeypatch.setattr(public_registration, "hash_password", lambda _: "password-hash")
    delivery = Mock()
    monkeypatch.setattr(public_registration, "send_email", delivery)
    return delivery


def test_registration_does_not_write_accounts_or_assignments_until_confirmation(monkeypatch):
    delivery = setup_code(monkeypatch)
    db = Mock()
    db.users.find_one.return_value = None
    db.pending_registrations.find_one.return_value = None
    settings = SimpleNamespace(public_registration_enabled=True, jwt_secret_key="x" * 32)
    monkeypatch.setattr(auth, "get_database", lambda: db)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "rate_limit", lambda *args: None)
    app = FastAPI()
    app.include_router(auth.router)
    client = TestClient(app)
    email = "test@example.com"

    response = client.post("/auth/register", json={"full_name": "Test User", "email": email, "password": "Clave!9809"})
    assert response.status_code == 202
    data = response.json()["data"]
    assert data["email"] == email and data["verification_email_sent"]
    assert "id" not in data and "password_hash" not in data
    db.users.insert_one.assert_not_called()
    db.institution_memberships.update_one.assert_not_called()
    db.role_assignments.update_one.assert_not_called()
    db.role_requests.update_one.assert_not_called()

    query, update = db.pending_registrations.update_one.call_args.args
    assert query["_id"] == email
    stored = update["$set"]
    challenge = stored["challenge"]
    assert stored["payload"]["password_hash"] == "password-hash"
    assert (stored["expires_at"] - challenge["sent_at"]).total_seconds() == 86400
    assert "012345" in delivery.call_args.args[2]

    login = client.post("/auth/login", json={"email": email, "password": "Clave!9809"})
    assert login.status_code == 401

    db.pending_registrations.find_one_and_update.return_value = {
        "_id": email, **stored, **update["$setOnInsert"], "verified_at": challenge["sent_at"],
    }
    response = client.post("/auth/verify-email", json={"email": email, "code": "012345"})
    assert response.status_code == 200
    check, _ = db.pending_registrations.find_one_and_update.call_args.args
    assert check["challenge.code_hash"] == challenge["code_hash"]
    assert "$gt" in check["challenge.expires_at"] and "$gt" in check["expires_at"]
    assert check["challenge.attempts"] == {"$lt": 5}
    account = db.users.insert_one.call_args.args[0]
    assert account["_id"] == update["$setOnInsert"]["user_id"]
    assert account["email_verified_at"] is not None
    assert not account["is_active"]
    db.users.update_one.assert_called_once()
    db.pending_registrations.delete_one.assert_called_once()


def test_invalid_code_cannot_provision_an_account(monkeypatch):
    setup_code(monkeypatch)
    db = Mock()
    db.pending_registrations.find_one_and_update.return_value = None
    db.pending_registrations.find_one.return_value = None
    validate_scope = Mock()
    assert not public_registration.confirm_registration(db, "test@example.com", "999999", validate_scope)
    validate_scope.assert_not_called()
    assert db.pending_registrations.update_one.call_args.args[1] == {"$inc": {"challenge.attempts": 1}}
    db.users.insert_one.assert_not_called()


def test_verified_teacher_activation_retains_pending_approval():
    db = Mock()
    uid = ObjectId()
    user = {"_id": uid, "email": "teacher@example.com", "account_type": "teacher", "institution_id": "institution"}
    public_registration.complete_registration(db, user)
    public_registration.complete_registration(db, user)
    for call in db.role_requests.update_one.call_args_list:
        query, update = call.args
        assert query == {"_id": uid}
        assert update["$setOnInsert"]["status"] == "pending"
        assert call.kwargs["upsert"]
    db.role_assignments.update_one.assert_not_called()
