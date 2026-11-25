from types import SimpleNamespace
from unittest.mock import Mock

from app.services import email_change, email_verification


def test_email_change_requires_a_code_bound_to_account_and_target(monkeypatch):
    monkeypatch.setattr(email_verification, "get_settings", lambda: SimpleNamespace(jwt_secret_key="test-key"))
    monkeypatch.setattr(email_change.secrets, "randbelow", lambda _: 12345)
    deliver = Mock()
    monkeypatch.setattr(email_change, "send_email", deliver)
    db = Mock()
    db.users.find_one_and_update.return_value = {"_id": "account"}
    db.users.update_one.return_value.modified_count = 0
    user = {"_id": "account", "email": "old@example.com", "password_hash": "current-hash"}
    target = "new@example.com"

    assert email_change.request_email_change(db, user, target) == "sent"
    query, update = db.users.find_one_and_update.call_args.args
    assert query["email"] == user["email"]
    assert query["password_hash"] == user["password_hash"]
    assert set(update["$set"]) == {"email_change"}
    pending = update["$set"]["email_change"]
    assert deliver.call_args.args[0] == target
    assert "012345" in deliver.call_args.args[2]

    assert not email_change.confirm_email_change(db, user, target, "000000")
    assert db.users.update_one.call_args.args[1] == {"$inc": {"email_change.attempts": 1}}

    db.users.update_one.return_value.modified_count = 1
    assert email_change.confirm_email_change(db, user, target, "012345")
    query, update = db.users.update_one.call_args.args
    assert query["_id"] == user["_id"]
    assert query["email_change.email"] == target
    assert query["email_change.code_hash"] == pending["code_hash"]
    assert query["email_change.attempts"] == {"$lt": 5}
    assert update["$set"]["email"] == target
    assert "email_verified_at" in update["$set"]
    assert "email_changed_at" in update["$set"]
    assert "email_change" in update["$unset"]
