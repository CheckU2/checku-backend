from datetime import datetime, timedelta, timezone
import secrets

from app.services.email import send_email
from app.services.email_verification import (
    CODE_EXPIRY_MINUTES,
    MAX_CODE_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    code_hash,
    verification_email_html,
)


def _account_filter(user):
    return {
        "_id": user["_id"],
        "is_active": True,
        "email": user["email"],
        "password_hash": user["password_hash"],
    }


def _digest(user, email, code):
    return code_hash(f"{user['_id']}:{email}", code, purpose="email-change")


def request_email_change(db, user, email: str) -> str:
    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = _digest(user, email, code)
    account = _account_filter(user)
    pending = {
        "email": email, "code_hash": digest, "sent_at": now,
        "expires_at": now + timedelta(minutes=CODE_EXPIRY_MINUTES), "attempts": 0,
    }
    existing = db.users.find_one_and_update(
        {**account, "$or": [
            {"email_change.sent_at": {"$exists": False}},
            {"email_change.sent_at": {"$lte": now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)}},
        ]},
        {"$set": {"email_change": pending}},
        projection={"email_change": 1},
    )
    if existing is None:
        return "unavailable"
    try:
        send_email(
            email, "Confirma tu correo en CheckU",
            f"Tu código es {code}. Vence en {CODE_EXPIRY_MINUTES} minutos.",
            html=verification_email_html(code, instruction="Ingresa este código en CheckU para confirmar tu correo."),
        )
    except (OSError, RuntimeError):
        old = existing.get("email_change")
        restore = {"$set": {"email_change": old}} if old else {"$unset": {"email_change": ""}}
        db.users.update_one({**account, "email_change.sent_at": now}, restore)
        return "delivery_failed"
    return "sent"


def confirm_email_change(db, user, email: str, code: str) -> bool:
    now = datetime.now(timezone.utc)
    digest = _digest(user, email, code)
    challenge = {
        **_account_filter(user),
        "email_change.email": email,
        "email_change.expires_at": {"$gt": now},
        "email_change.attempts": {"$lt": MAX_CODE_ATTEMPTS},
    }
    changes = {"email": email, "email_verified_at": now, "updated_at": now}
    if email != user["email"]:
        changes["email_changed_at"] = now
    verified = db.users.update_one(
        {**challenge, "email_change.code_hash": digest},
        {"$set": changes, "$unset": {"email_change": "", "email_verification": "", "password_change": ""}},
    )
    if verified.modified_count == 1:
        return True
    db.users.update_one(
        {**challenge, "email_change.code_hash": {"$ne": digest}},
        {"$inc": {"email_change.attempts": 1}},
    )
    return False
