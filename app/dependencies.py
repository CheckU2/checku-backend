from bson import ObjectId
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from app.database import get_database
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)
def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)):
    if credentials is None:
        raise HTTPException(401, detail={"code":"AUTH_REQUIRED","message":"Autenticación requerida"})
    try:
        payload=decode_access_token(credentials.credentials); uid=payload.get("sub")
        if not uid or not ObjectId.is_valid(uid): raise ValueError("sub inválido")
    except (jwt.InvalidTokenError, ValueError, RuntimeError):
        raise HTTPException(401, detail={"code":"INVALID_TOKEN","message":"Token inválido o expirado"})
    db=get_database()
    if db is None: raise HTTPException(503, detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    if payload.get("jti") and db.revoked_access_tokens.find_one({"jti":payload["jti"]}):
        raise HTTPException(401, detail={"code":"TOKEN_REVOKED","message":"Sesión revocada"})
    user=db.users.find_one({"_id":ObjectId(uid),"is_active":True})
    if not user: raise HTTPException(401, detail={"code":"USER_INACTIVE_OR_MISSING","message":"Usuario no disponible"})
    return user

def require_roles(*roles: str):
    def checker(user=Depends(get_current_user)):
        # Legacy role alias is supported only for existing admin records.
        role=user.get("role")
        allowed=set(roles)
        if "super_admin" in allowed: allowed.add("admin")
        if role not in allowed: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"No tienes permisos para esta operación"})
        return user
    return checker
