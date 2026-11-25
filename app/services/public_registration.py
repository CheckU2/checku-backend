from datetime import datetime, timedelta, timezone
import logging
import secrets

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.security import hash_password
from app.services.email import send_email
from app.services.email_verification import (
    CODE_EXPIRY_MINUTES,
    MAX_CODE_ATTEMPTS,
    RESEND_COOLDOWN_SECONDS,
    code_hash,
    verification_email_html,
)

PENDING_REGISTRATION_TTL = timedelta(hours=24)


def _challenge(email, now):
    code = f"{secrets.randbelow(1_000_000):06d}"
    return code, {
        "code_hash": code_hash(email, code, purpose="public-registration"),
        "expires_at": now + timedelta(minutes=CODE_EXPIRY_MINUTES),
        "sent_at": now,
        "attempts": 0,
    }


def _send_code(email, code):
    try:
        send_email(
            email,
            "Código de registro de CheckU",
            f"Tu código para completar el registro es {code}. Vence en {CODE_EXPIRY_MINUTES} minutos.\n\n"
            "Si no solicitaste el registro, ignora este correo.",
            html=verification_email_html(code, instruction="Ingresa este código en CheckU para crear tu cuenta."),
        )
    except (OSError, RuntimeError):
        logging.getLogger("checku.auth").warning("Registration verification email failed")
        return False
    return True


def _can_issue_code(email, now, digest):
    return {
        "_id": email,
        "verified_at": {"$exists": False},
        "challenge.sent_at": {"$lte": now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)},
        "challenge.code_hash": {"$ne": digest},
    }


def start_registration(db, email, *, full_name, password, account_type, institution_id, career_id, timezone_name):
    now = datetime.now(timezone.utc)
    code, challenge = _challenge(email, now)
    if db.pending_registrations.find_one({
        "_id": email,
        "$or": [
            {"verified_at": {"$exists": True}},
            {"challenge.sent_at": {"$gt": now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)}},
        ],
    }, {"_id": 1}):
        return None
    payload = {
        "full_name": full_name.strip(),
        "password_hash": hash_password(password),
        "account_type": account_type,
        "institution_id": institution_id,
        "career_id": career_id,
        "timezone": timezone_name,
    }
    try:
        db.pending_registrations.update_one(
            _can_issue_code(email, now, challenge["code_hash"]),
            {
                "$set": {"payload": payload, "expires_at": now + PENDING_REGISTRATION_TTL, "challenge": challenge},
                "$setOnInsert": {"user_id": ObjectId()},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return None
    return _send_code(email, code), challenge


def resend_registration_code(db, email):
    now = datetime.now(timezone.utc)
    code, challenge = _challenge(email, now)
    result = db.pending_registrations.update_one(
        {**_can_issue_code(email, now, challenge["code_hash"]),
         "expires_at": {"$gt": now + timedelta(minutes=CODE_EXPIRY_MINUTES)}},
        {"$set": {"challenge": challenge}},
    )
    return result.modified_count == 1 and _send_code(email, code)


def find_pending_registration(db, email):
    return db.pending_registrations.find_one({"_id": email, "expires_at": {"$gt": datetime.now(timezone.utc)}})


def confirm_registration(db, email, code, validate_scope):
    now = datetime.now(timezone.utc)
    digest = code_hash(email, code, purpose="public-registration")
    identity = {"_id": email, "expires_at": {"$gt": now}}
    unverified = {
        **identity,
        "verified_at": {"$exists": False},
        "challenge.expires_at": {"$gt": now},
        "challenge.attempts": {"$lt": MAX_CODE_ATTEMPTS},
    }
    pending = db.pending_registrations.find_one_and_update(
        {**unverified, "challenge.code_hash": digest},
        {"$set": {"verified_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if pending is None:
        pending = db.pending_registrations.find_one(
            {**identity, "verified_at": {"$exists": True}, "challenge.code_hash": digest},
        )
    if pending is None:
        db.pending_registrations.update_one(
            {**unverified, "challenge.code_hash": {"$ne": digest}},
            {"$inc": {"challenge.attempts": 1}},
        )
        return False

    details = pending["payload"]
    validate_scope(details)
    uid = pending["user_id"]
    user = {
        "_id": uid, "email": email, "role": "student", "is_active": False, "registration_pending": True,
        "account_type": details["account_type"], "email_verified_at": pending["verified_at"],
        "password_hash": details["password_hash"], "full_name": details["full_name"],
        "created_at": pending["verified_at"], "updated_at": now,
        **{key: details[key] for key in ("institution_id", "career_id", "timezone") if details.get(key) is not None},
    }
    try:
        db.users.insert_one(user)
    except DuplicateKeyError:
        existing = db.users.find_one({"_id": uid, "email": email})
        if not existing:
            raise
        if not existing.get("registration_pending"):
            db.pending_registrations.delete_one({"_id": email, "user_id": uid})
            return True

    complete_registration(db, user)
    return True


def complete_registration(db, user):
    now = datetime.now(timezone.utc)
    uid = user["_id"]
    email = user["email"]
    institution_id = user.get("institution_id")
    career_id = user.get("career_id")
    if institution_id:
        is_student = user["account_type"] == "student"
        membership = {"institution_id": institution_id, "user_id": str(uid)}
        db.institution_memberships.update_one(
            membership,
            {"$setOnInsert": {"status": "active" if is_student else "pending", "joined_at": now}},
            upsert=True,
        )
        if is_student:
            db.role_assignments.update_one(
                {**membership, "role": "student", "scope_type": "career", "scope_id": career_id},
                {"$setOnInsert": {"status": "active", "granted_at": now}},
                upsert=True,
            )
        else:
            db.role_requests.update_one(
                {"_id": uid},
                {"$setOnInsert": {
                    **membership, "requested_role": "teacher",
                    "scope_type": "career" if career_id else "institution", "scope_id": career_id,
                    "status": "pending", "created_at": now, "reviewed_by": None, "reviewed_at": None, "reason": None,
                }},
                upsert=True,
            )
    db.users.update_one(
        {"_id": uid, "registration_pending": True},
        {"$set": {"is_active": True}, "$unset": {"registration_pending": ""}},
    )
    db.pending_registrations.delete_one({"_id": email, "user_id": uid})
