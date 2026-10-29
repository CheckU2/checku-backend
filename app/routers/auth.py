from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
import time, secrets, base64, re
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pymongo.errors import PyMongoError
from app.config import get_settings
from app.database import get_database
from app.dependencies import get_current_user
from app.schemas.auth import LoginRequest, RegisterRequest
from app.security import verify_password, create_access_token, hash_password, token_hash, opaque_token
from app.services.users import create_user, public_user
from app.services.email import send_password_reset_email

router=APIRouter(prefix="/auth",tags=["Autenticación"])
_attempts=defaultdict(deque)
def rate_limit(request: Request, key: str, maximum: int=10, window: int=60):
    now=time.time(); bucket=_attempts[f"{request.client.host if request.client else 'unknown'}:{key}"]
    while bucket and bucket[0] < now-window: bucket.popleft()
    if len(bucket)>=maximum: raise HTTPException(429,detail={"code":"RATE_LIMITED","message":"Demasiadas solicitudes; inténtalo más tarde"})
    bucket.append(now)
def issue_tokens(db,user):
    settings=get_settings(); now=datetime.now(timezone.utc)
    refresh=opaque_token()
    db.refresh_sessions.insert_one({"user_id":user["_id"],"token_hash":token_hash(refresh),"created_at":now,
       "expires_at":now+timedelta(days=settings.refresh_token_expire_days),"revoked_at":None})
    access=create_access_token(str(user["_id"]),user["role"])
    return {"access_token":access,"refresh_token":refresh,"token_type":"bearer","expires_in":settings.access_token_expire_minutes*60}

@router.post("/register",status_code=201)
def register(payload:RegisterRequest):
    s=get_settings()
    if not s.public_registration_enabled: raise HTTPException(403,detail={"code":"REGISTRATION_DISABLED","message":"El registro público está deshabilitado"})
    try:
        user=create_user(email=str(payload.email),password=payload.password,role="student",full_name=payload.full_name)
        return {"success":True,"data":public_user(user),"message":"Cuenta creada correctamente"}
    except ValueError as exc: raise HTTPException(409,detail={"code":"EMAIL_EXISTS","message":str(exc)})
    except RuntimeError: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    except PyMongoError: raise HTTPException(503,detail={"code":"DATABASE_ERROR","message":"No fue posible guardar la cuenta"})

@router.post("/login")
def login(payload:LoginRequest,request:Request):
    rate_limit(request,"login",8,60)
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    user=db.users.find_one({"email":str(payload.email).lower().strip()})
    if not user or not user.get("is_active",False) or not verify_password(payload.password,user.get("password_hash","")):
        raise HTTPException(401,detail={"code":"INVALID_CREDENTIALS","message":"Correo o contraseña incorrectos"})
    return {"success":True,"data":{**issue_tokens(db,user),"user":public_user(user)}}

@router.post("/refresh")
def refresh(payload:dict):
    raw=payload.get("refresh_token","")
    if not isinstance(raw,str) or len(raw)<30: raise HTTPException(401,detail={"code":"INVALID_REFRESH_TOKEN","message":"Sesión inválida o expirada"})
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    now=datetime.now(timezone.utc); old=db.refresh_sessions.find_one({"token_hash":token_hash(raw),"revoked_at":None,"expires_at":{"$gt":now}})
    if not old: raise HTTPException(401,detail={"code":"INVALID_REFRESH_TOKEN","message":"Sesión inválida o expirada"})
    # Atomically consume old token; simultaneous refresh requests cannot both succeed.
    consumed=db.refresh_sessions.update_one({"_id":old["_id"],"revoked_at":None},{"$set":{"revoked_at":now}})
    if consumed.modified_count!=1: raise HTTPException(401,detail={"code":"REFRESH_REUSED","message":"La sesión ya fue renovada"})
    user=db.users.find_one({"_id":old["user_id"],"is_active":True})
    if not user: raise HTTPException(401,detail={"code":"USER_INACTIVE","message":"Usuario no disponible"})
    return {"success":True,"data":issue_tokens(db,user)}

@router.post("/logout")
def logout(payload:dict,request:Request,user=Depends(get_current_user)):
    db=get_database(); now=datetime.now(timezone.utc)
    raw=payload.get("refresh_token")
    if raw: db.refresh_sessions.update_one({"user_id":user["_id"],"token_hash":token_hash(str(raw)),"revoked_at":None},{"$set":{"revoked_at":now}})
    auth=request.headers.get("authorization","").split()
    if len(auth)==2:
        import jwt
        from app.security import decode_access_token
        try:
            token_payload=decode_access_token(auth[1])
            if token_payload.get("jti"): db.revoked_access_tokens.update_one({"jti":token_payload["jti"]},{"$set":{"jti":token_payload["jti"],"revoked_at":now,"expires_at":datetime.fromtimestamp(token_payload["exp"],timezone.utc)}},upsert=True)
        except Exception: pass
    return {"success":True,"data":{"message":"Sesión cerrada"}}

@router.post("/logout-all")
def logout_all(user=Depends(get_current_user)):
    db=get_database(); now=datetime.now(timezone.utc)
    db.refresh_sessions.update_many({"user_id":user["_id"],"revoked_at":None},{"$set":{"revoked_at":now}})
    return {"success":True,"data":{"message":"Se revocaron las sesiones renovables"}}

@router.get("/me")
def me(user=Depends(get_current_user)): return {"success":True,"data":public_user(user)}

@router.post("/forgot-password")
def forgot_password(payload:dict,request:Request):
    rate_limit(request,"forgot",5,3600)
    email=str(payload.get("email","")).strip().lower()
    db=get_database()
    if db is not None and email:
        user=db.users.find_one({"email":email,"is_active":True})
        if user:
            s=get_settings(); raw=opaque_token(); now=datetime.now(timezone.utc)
            db.password_reset_tokens.update_many({"user_id":user["_id"],"used_at":None},{"$set":{"used_at":now}})
            db.password_reset_tokens.insert_one({"user_id":user["_id"],"token_hash":token_hash(raw),"created_at":now,
              "expires_at":now+timedelta(minutes=s.password_reset_expire_minutes),"used_at":None})
            try: send_password_reset_email(email,raw)
            except Exception:
                # Do not disclose account existence or SMTP status to the caller.
                import logging
                logging.getLogger("checku.auth").warning("Password reset email delivery failed")
                db.password_reset_tokens.update_one({"token_hash":token_hash(raw)},{"$set":{"used_at":now}})
    return {"success":True,"data":{"message":"Si la cuenta existe, recibirás instrucciones para recuperar tu contraseña."}}

@router.post("/reset-password")
def reset_password(payload:dict):
    raw=payload.get("token",""); password=payload.get("new_password","")
    if not isinstance(password,str) or len(password)<12 or len(password)>128:
        raise HTTPException(422,detail={"code":"INVALID_PASSWORD","message":"La contraseña debe tener entre 12 y 128 caracteres"})
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    now=datetime.now(timezone.utc); item=db.password_reset_tokens.find_one({"token_hash":token_hash(str(raw)),"used_at":None,"expires_at":{"$gt":now}})
    if not item: raise HTTPException(400,detail={"code":"INVALID_RESET_TOKEN","message":"Token inválido o expirado"})
    result=db.password_reset_tokens.update_one({"_id":item["_id"],"used_at":None},{"$set":{"used_at":now}})
    if result.modified_count!=1: raise HTTPException(400,detail={"code":"INVALID_RESET_TOKEN","message":"Token inválido o expirado"})
    db.users.update_one({"_id":item["user_id"]},{"$set":{"password_hash":hash_password(password),"updated_at":now}})
    db.refresh_sessions.update_many({"user_id":item["user_id"],"revoked_at":None},{"$set":{"revoked_at":now}})
    return {"success":True,"data":{"message":"Contraseña actualizada; inicia sesión nuevamente"}}

@router.patch("/change-password")
def change_password(payload:dict,user=Depends(get_current_user)):
    current=payload.get("current_password",""); new=payload.get("new_password","")
    if not verify_password(current,user.get("password_hash","")): raise HTTPException(400,detail={"code":"INVALID_PASSWORD","message":"La contraseña actual es incorrecta"})
    if not isinstance(new,str) or len(new)<12 or len(new)>128: raise HTTPException(422,detail={"code":"INVALID_PASSWORD","message":"La nueva contraseña debe tener entre 12 y 128 caracteres"})
    db=get_database(); now=datetime.now(timezone.utc)
    db.users.update_one({"_id":user["_id"]},{"$set":{"password_hash":hash_password(new),"updated_at":now}})
    db.refresh_sessions.update_many({"user_id":user["_id"],"revoked_at":None},{"$set":{"revoked_at":now}})
    return {"success":True,"data":{"message":"Contraseña actualizada; vuelve a iniciar sesión"}}


@router.patch('/profile')
def update_my_profile(payload: dict, user=Depends(get_current_user)):
    db = get_database()
    changes = {}
    if 'full_name' in payload:
        name = str(payload['full_name']).strip()
        if not 2 <= len(name) <= 120:
            raise HTTPException(422, detail={'message': 'Nombre inválido'})
        changes['full_name'] = name
    if 'bio' in payload:
        bio = str(payload['bio']).strip()
        if len(bio) > 400:
            raise HTTPException(422, detail={'message': 'La biografía no puede superar 400 caracteres'})
        changes['bio'] = bio
    if 'avatar' in payload:
        avatar = payload['avatar']
        if avatar is None:
            changes['avatar'] = None
        elif isinstance(avatar, str) and re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+', avatar):
            try:
                raw = base64.b64decode(avatar.split(',', 1)[1], validate=True)
            except Exception:
                raise HTTPException(422, detail={'message': 'Imagen inválida'})
            if len(raw) > 250000 or len(raw) < 20:
                raise HTTPException(422, detail={'message': 'La imagen debe pesar menos de 250 KB'})
            changes['avatar'] = avatar
        else:
            raise HTTPException(422, detail={'message': 'Solo se permiten imágenes PNG, JPG o WEBP'})
    if 'banner_color' in payload:
        color = str(payload['banner_color']).strip()
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise HTTPException(422, detail={'message': 'Color de portada inválido'})
        changes['banner_color'] = color
    if not changes:
        raise HTTPException(422, detail={'message': 'No hay cambios'})
    changes['updated_at'] = datetime.now(timezone.utc)
    db.users.update_one({'_id': user['_id']}, {'$set': changes})
    return {'success': True, 'data': public_user(db.users.find_one({'_id': user['_id']}))}
