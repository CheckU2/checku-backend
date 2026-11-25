from types import SimpleNamespace
from unittest.mock import Mock

from app.services import email_verification, password_change


def test_password_change_stays_pending_until_code_is_confirmed(monkeypatch):
    monkeypatch.setattr(email_verification, "get_settings", lambda: SimpleNamespace(jwt_secret_key="test-key"))
    monkeypatch.setattr(password_change.secrets, "randbelow", lambda _: 12345)
    monkeypatch.setattr(password_change, "hash_password", lambda _: "new-hash")
    send_email = Mock()
    monkeypatch.setattr(password_change, "send_email", send_email)
    db = Mock()
    db.users.update_one.return_value.modified_count = 1
    user = {"_id": "account-id", "email": "persona@example.com", "password_hash": "current-hash"}

    assert password_change.request_password_change(db, user, "new-password") == "sent"
    query, update = db.users.update_one.call_args.args
    pending = update["$set"]["password_change"]
    assert set(update["$set"]) == {"password_change"}
    assert query["password_hash"] == "current-hash"
    assert pending["new_password_hash"] == "new-hash"
    assert pending["code_hash"] != email_verification.code_hash(user["email"], "012345")
    assert send_email.call_args.args[0] == user["email"]
    assert "012345" in send_email.call_args.args[2]
    db.refresh_sessions.update_many.assert_not_called()

    db.users.find_one_and_update.return_value = None
    assert not password_change.confirm_password_change(db, user, "999999")
    assert db.users.update_one.call_args.args[1] == {"$inc": {"password_change.attempts": 1}}
    db.refresh_sessions.update_many.assert_not_called()

    db.users.find_one_and_update.return_value = {"_id": user["_id"]}
    assert password_change.confirm_password_change(db, user, "012345")
    query, pipeline = db.users.find_one_and_update.call_args.args
    assert query["password_change.code_hash"] == pending["code_hash"]
    assert query["password_change.attempts"] == {"$lt": 5}
    assert pipeline[0]["$set"]["password_hash"] == "$password_change.new_password_hash"
    assert pipeline[1] == {"$unset": ["password_change", "email_change"]}
    db.refresh_sessions.update_many.assert_called_once()
