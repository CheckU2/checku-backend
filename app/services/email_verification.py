from datetime import datetime, timedelta, timezone
import hmac
import logging
import secrets

from app.config import get_settings
from app.services.email import send_email

CODE_EXPIRY_MINUTES = 10
RESEND_COOLDOWN_SECONDS = 60
MAX_CODE_ATTEMPTS = 5


def verification_email_html(
    code: str,
    *,
    title: str = "Confirma tu correo",
    instruction: str = "Ingresa este código de verificación en CheckU para completar tu registro.",
) -> str:
    return f"""<!doctype html>
<html lang="es">
<body style="margin:0;background:#f0f6f4;color:#142c26;font-family:Arial,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f0f6f4;">
    <tr><td align="center" style="padding:32px 12px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
             style="max-width:480px;background:#ffffff;border:1px solid #d7e5df;border-radius:16px;">
        <tr><td style="padding:28px 24px;">
          <p style="margin:0 0 18px;color:#13654f;font-size:18px;font-weight:bold;">CheckU</p>
          <h1 style="margin:0 0 12px;font-size:24px;line-height:1.25;">{title}</h1>
          <p style="margin:0 0 20px;font-size:16px;line-height:1.5;">
            {instruction}
          </p>
          <p style="margin:0 0 20px;padding:16px 8px;background:#e4f4ee;border-radius:10px;
                    text-align:center;color:#104d3e;white-space:nowrap;
                    font:bold 32px/1.2 Consolas,monospace;letter-spacing:0.18em;">{code}</p>
          <p style="margin:0 0 12px;font-size:15px;line-height:1.5;">
            Vence en <strong>{CODE_EXPIRY_MINUTES} minutos</strong> y solo se puede usar una vez.
          </p>
          <p style="margin:0;color:#425e55;font-size:14px;line-height:1.5;">
            Si no solicitaste este código, puedes ignorar este correo.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def code_hash(email: str, code: str, *, purpose: str = "email-verification") -> str:
    key = get_settings().jwt_secret_key
    if not key:
        raise RuntimeError("JWT_SECRET_KEY es necesaria para verificar correos")
    message = f"checku-{purpose}:{email}:{code}"
    return hmac.digest(key.encode(), message.encode(), "sha256").hex()


def send_verification_code(db, email: str) -> bool:
    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = code_hash(email, code)
    result = db.users.update_one(
        {
            "email": email,
            "is_active": True,
            "email_verified_at": {"$type": "null"},
            "email_verification.code_hash": {"$ne": digest},
            "$or": [
                {"email_verification.sent_at": {"$exists": False}},
                {"email_verification.sent_at": {
                    "$lte": now - timedelta(seconds=RESEND_COOLDOWN_SECONDS),
                }},
            ],
        },
        {"$set": {"email_verification": {
            "code_hash": digest,
            "sent_at": now,
            "expires_at": now + timedelta(minutes=CODE_EXPIRY_MINUTES),
            "attempts": 0,
        }}},
    )
    if result.modified_count != 1:
        return False

    try:
        send_email(
            email,
            "Código de verificación de CheckU",
            f"Tu código es {code}. Vence en {CODE_EXPIRY_MINUTES} minutos.\n\n"
            "Si no solicitaste este código, ignora este mensaje.",
            html=verification_email_html(code),
        )
    except (OSError, RuntimeError):
        logging.getLogger("checku.auth").warning("Verification email delivery failed")
        return False
    return True


def verify_email_code(db, email: str, code: str) -> bool:
    now = datetime.now(timezone.utc)
    digest = code_hash(email, code)
    challenge = {
        "email": email,
        "is_active": True,
        "email_verified_at": {"$type": "null"},
        "email_verification.expires_at": {"$gt": now},
        "email_verification.attempts": {"$lt": MAX_CODE_ATTEMPTS},
    }
    verified = db.users.update_one(
        {**challenge, "email_verification.code_hash": digest},
        {
            "$set": {"email_verified_at": now, "updated_at": now},
            "$unset": {"email_verification": ""},
        },
    )
    if verified.modified_count == 1:
        return True

    db.users.update_one(
        {**challenge, "email_verification.code_hash": {"$ne": digest}},
        {"$inc": {"email_verification.attempts": 1}},
    )
    return False
