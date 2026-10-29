from datetime import datetime, timedelta, timezone
import hashlib, secrets
import jwt
from pwdlib import PasswordHash
from app.config import get_settings
_password_hash = PasswordHash.recommended()
def hash_password(password: str) -> str: return _password_hash.hash(password)
def verify_password(password: str, password_hash: str) -> bool:
    try: return _password_hash.verify(password, password_hash)
    except Exception: return False
def token_hash(token: str) -> str: return hashlib.sha256(token.encode()).hexdigest()
def opaque_token() -> str: return secrets.token_urlsafe(40)
def create_access_token(subject: str, role: str, jti: str | None = None) -> str:
    s=get_settings()
    if len(s.jwt_secret_key)<32: raise RuntimeError("JWT_SECRET_KEY debe configurarse con al menos 32 caracteres")
    now=datetime.now(timezone.utc)
    return jwt.encode({"sub":subject,"role":role,"iat":now,"exp":now+timedelta(minutes=s.access_token_expire_minutes),"type":"access","jti":jti or secrets.token_urlsafe(18)},s.jwt_secret_key,algorithm=s.jwt_algorithm)
def decode_access_token(token: str) -> dict:
    s=get_settings()
    if len(s.jwt_secret_key)<32: raise RuntimeError("JWT_SECRET_KEY no está configurada correctamente")
    payload=jwt.decode(token,s.jwt_secret_key,algorithms=[s.jwt_algorithm])
    if payload.get("type")!="access": raise jwt.InvalidTokenError("Tipo de token incorrecto")
    return payload
