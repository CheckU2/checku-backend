from datetime import datetime, timedelta, timezone
import logging
import secrets

from pymongo import ReturnDocument

from app.security import hash_password
from app.services.email import send_email
from app.services.email_verification import (
    CODE_EXPIRY_MINUTES,
    MAX_CODE_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    code_hash,
    verification_email_html,
)


def request_password_change(db, user, new_password: str) -> str:
    now = datetime.now(timezone.utc)
    email = user["email"]
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = code_hash(email, code, purpose="password-change")
    account = {
        "_id": user["_id"],
        "email": email,
        "is_active": True,
        "password_hash": user["password_hash"],
    }
    result = db.users.update_one(
        {
            **account,
            "password_change.code_hash": {"$ne": digest},
            "$or": [
                {"password_change.sent_at": {"$exists": False}},
                {"password_change.sent_at": {"$lte": now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)}},
            ],
        },
        {"$set": {"password_change": {
            "code_hash": digest,
            "new_password_hash": hash_password(new_password),
            "sent_at": now,
            "expires_at": now + timedelta(minutes=CODE_EXPIRY_MINUTES),
            "attempts": 0,
        }}},
    )
    if result.modified_count != 1:
        return "unavailable"

    try:
        send_email(
            email,
            "Confirma tu cambio de contraseña en CheckU",
            f"Tu código para cambiar la contraseña es {code}. Vence en {CODE_EXPIRY_MINUTES} minutos.\n\n"
            "Si no solicitaste el cambio, ignora este correo.",
            html=verification_email_html(
                code,
                title="Confirma tu cambio de contraseña",
                instruction="Ingresa este código en CheckU para autorizar el cambio de contraseña.",
            ),
        )
    except (OSError, RuntimeError):
        db.users.update_one(
            {**account, "password_change.code_hash": digest},
            {"$unset": {"password_change": ""}},
        )
        logging.getLogger("checku.auth").warning("Password change email delivery failed")
        return "delivery_failed"
    return "sent"


def confirm_password_change(db, user, code: str) -> bool:
    now = datetime.now(timezone.utc)
    digest = code_hash(user["email"], code, purpose="password-change")
    challenge = {
        "_id": user["_id"],
        "is_active": True,
        "email": user["email"],
        "password_hash": user["password_hash"],
        "password_change.expires_at": {"$gt": now},
        "password_change.attempts": {"$lt": MAX_CODE_ATTEMPTS},
    }
    updated = db.users.find_one_and_update(
        {**challenge, "password_change.code_hash": digest},
        [
            {"$set": {"password_hash": "$password_change.new_password_hash", "updated_at": now}},
            {"$unset": ["password_change", "email_change"]},
        ],
        projection={"_id": 1},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        db.users.update_one(
            {**challenge, "password_change.code_hash": {"$ne": digest}},
            {"$inc": {"password_change.attempts": 1}},
        )
        return False
    db.refresh_sessions.update_many(
        {"user_id": user["_id"], "revoked_at": None},
        {"$set": {"revoked_at": now}},
    )
    return True
