from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import auth
from app.services import email_verification


def test_verification_email_preserves_leading_zero_and_stores_hash(monkeypatch):
    monkeypatch.setattr(email_verification, "get_settings", lambda: SimpleNamespace(jwt_secret_key="test-key"))
    monkeypatch.setattr(email_verification.secrets, "randbelow", lambda _: 12345)
    send_email = Mock()
    monkeypatch.setattr(email_verification, "send_email", send_email)
    db = Mock()
    db.users.update_one.return_value.modified_count = 1

    assert email_verification.send_verification_code(db, "persona@example.com")

    query, update = db.users.update_one.call_args.args
    challenge = update["$set"]["email_verification"]
    assert query["email_verified_at"] == {"$type": "null"}
    assert challenge["code_hash"] != "012345"
    assert challenge["attempts"] == 0
    assert (challenge["expires_at"] - challenge["sent_at"]).total_seconds() == 600
    assert "012345" in send_email.call_args.args[2]
    assert ">012345</p>" in send_email.call_args.kwargs["html"]


def test_confirmation_consumes_code_in_same_update(monkeypatch):
    monkeypatch.setattr(email_verification, "get_settings", lambda: SimpleNamespace(jwt_secret_key="test-key"))
    db = Mock()
    db.users.update_one.return_value.modified_count = 1

    assert email_verification.verify_email_code(db, "persona@example.com", "012345")

    query, update = db.users.update_one.call_args.args
    assert query["email_verification.attempts"] == {"$lt": 5}
    assert "$gt" in query["email_verification.expires_at"]
    assert update["$set"]["email_verified_at"] is not None
    assert update["$unset"] == {"email_verification": ""}
    db.users.update_one.assert_called_once()


def test_failed_confirmation_counts_attempt_against_active_code(monkeypatch):
    monkeypatch.setattr(email_verification, "get_settings", lambda: SimpleNamespace(jwt_secret_key="test-key"))
    db = Mock()
    db.users.update_one.return_value.modified_count = 0

    assert not email_verification.verify_email_code(db, "persona@example.com", "123456")

    query, update = db.users.update_one.call_args.args
    assert query["email_verification.attempts"] == {"$lt": 5}
    assert "$ne" in query["email_verification.code_hash"]
    assert update == {"$inc": {"email_verification.attempts": 1}}


def test_login_blocks_pending_accounts_and_allows_existing_accounts(monkeypatch):
    db = Mock()
    user = {"email": "persona@example.com", "is_active": True, "email_verified_at": None}
    db.users.find_one.return_value = user
    issue_tokens = Mock(return_value={"access_token": "session"})
    monkeypatch.setattr(auth, "get_database", lambda: db)
    monkeypatch.setattr(auth, "rate_limit", lambda *args: None)
    monkeypatch.setattr(auth, "verify_password", lambda *args: True)
    monkeypatch.setattr(auth, "user_contexts", lambda *args: [])
    monkeypatch.setattr(auth, "auth_user_response", lambda *args: {})
    monkeypatch.setattr(auth, "issue_tokens", issue_tokens)
    app = FastAPI()
    app.include_router(auth.router)
    client = TestClient(app)
    credentials = {"email": user["email"], "password": "password"}

    pending = client.post("/auth/login", json=credentials)
    assert pending.status_code == 403
    assert pending.json()["detail"]["code"] == "EMAIL_NOT_VERIFIED"
    issue_tokens.assert_not_called()

    del user["email_verified_at"]
    assert client.post("/auth/login", json=credentials).status_code == 200
    issue_tokens.assert_called_once()
