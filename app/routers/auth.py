from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from math import ceil
import time, secrets, base64, re
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pymongo.errors import DuplicateKeyError, PyMongoError
from app.config import get_settings
from app.database import get_database
from app.dependencies import get_current_user, get_current_identity
from app.schemas.auth import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResendVerificationRequest,
    VerificationCodeRequest,
    VerifyEmailRequest,
)
from app.schemas.password_policy import password_policy_error
from app.security import verify_password, create_access_token, hash_password, token_hash, opaque_token
from app.services.users import public_user
from app.services.email import send_password_reset_email
from app.services.email_verification import (
    CODE_EXPIRY_MINUTES,
    RESEND_COOLDOWN_SECONDS,
    send_verification_code,
    verify_email_code,
)
from app.services.email_change import confirm_email_change, request_email_change
from app.services.password_change import confirm_password_change, request_password_change
from app.services.institutions import institution, valid_timezone
from app.services.public_registration import (
    complete_registration, confirm_registration, find_pending_registration, resend_registration_code, start_registration,
)
from app.services.authorization import apply_user_context, resolve_user_context, user_contexts

router=APIRouter(prefix="/auth",tags=["Autenticación"])
_attempts=defaultdict(deque)
def rate_limit(request: Request, key: str, maximum: int=10, window: int=60):
    now=time.time(); bucket=_attempts[f"{request.client.host if request.client else 'unknown'}:{key}"]
    while bucket and bucket[0] < now-window: bucket.popleft()
    if len(bucket)>=maximum: raise HTTPException(429,detail={"code":"RATE_LIMITED","message":"Demasiadas solicitudes; inténtalo más tarde"})
    bucket.append(now)
def issue_tokens(db,user,context=None):
    settings=get_settings(); now=datetime.now(timezone.utc)
    if context is not None:
        context=resolve_user_context(db,user,context)
    refresh=opaque_token()
    db.refresh_sessions.insert_one({"user_id":user["_id"],"token_hash":token_hash(refresh),"created_at":now,
       "expires_at":now+timedelta(days=settings.refresh_token_expire_days),"revoked_at":None,"context":context})
    access=create_access_token(str(user["_id"]),user["role"],context=context)
    return {"access_token":access,"refresh_token":refresh,"token_type":"bearer","expires_in":settings.access_token_expire_minutes*60}

def context_display_name(db, context):
    scope = context.get("scope_type")
    identifier = context.get("scope_id")
    institution_id = context.get("institution_id")
    if scope not in {"group", "career", "class"} or not institution_id or not ObjectId.is_valid(identifier):
        return None
    collection = getattr(db, {"group": "groups", "career": "careers", "class": "classes"}[scope])
    record = collection.find_one(
        {"_id": ObjectId(identifier), "institution_id": institution_id, "is_deleted": {"$ne": True}},
        {"name": 1, "subject_name": 1, "code": 1},
    )
    if not record:
        return None
    return record.get("name") or record.get("subject_name") or record.get("code")


def auth_user_response(db,user,context=None):
    contexts=user_contexts(db,user)
    active_context=resolve_user_context(db,user,context) if context is not None else None
    response_user=apply_user_context(user,active_context) if active_context else user
    result=public_user(response_user)
    result["contexts"] = [{**item, "scope_name": context_display_name(db, item)} for item in contexts]
    result["active_context"]=active_context
    return result

def verification_timing(expires_at, sent_at):
    now = datetime.now(timezone.utc)

    def remaining(deadline):
        if deadline is None:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0, ceil((deadline - now).total_seconds()))

    return {
        "expires_in_seconds": remaining(expires_at),
        "resend_in_seconds": remaining(sent_at + timedelta(seconds=RESEND_COOLDOWN_SECONDS)) if sent_at else 0,
    }


def new_verification_timing(sent_at):
    return verification_timing(sent_at + timedelta(minutes=CODE_EXPIRY_MINUTES), sent_at)


def validate_registration_scope(db, settings, account_type, institution_id, career_id):
    if account_type not in ("student", "teacher"):
        raise HTTPException(422, detail={"code": "INVALID_ACCOUNT_TYPE", "message": "Tipo de cuenta inválido"})
    if institution_id:
        institution(db, institution_id)
        if not settings.multi_institution_registration_enabled:
            raise HTTPException(403, detail={"code": "FEATURE_DISABLED", "message": "Registro multiinstitucional aún no habilitado"})
        if account_type == "student" and not career_id:
            raise HTTPException(422, detail={"code": "CAREER_REQUIRED", "message": "Selecciona tu carrera"})
        if career_id and (
            not ObjectId.is_valid(career_id)
            or not db.careers.find_one({"_id": ObjectId(career_id), "institution_id": institution_id, "is_deleted": {"$ne": True}})
        ):
            raise HTTPException(422, detail={"code": "INVALID_CAREER", "message": "Carrera no pertenece a la institución"})
    elif career_id or account_type == "teacher":
        raise HTTPException(422, detail={"code": "INSTITUTION_REQUIRED", "message": "Selecciona una institución"})


@router.post("/register", status_code=202)
def register(payload: RegisterRequest, request: Request):
    settings = get_settings()
    rate_limit(request, "register", 10, 3600)
    if not settings.public_registration_enabled:
        raise HTTPException(403, detail={"code": "REGISTRATION_DISABLED", "message": "El registro público está deshabilitado"})
    if len(settings.jwt_secret_key) < 32:
        raise HTTPException(503, detail={"code": "AUTH_UNAVAILABLE", "message": "Registro temporalmente no disponible"})
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"code": "DATABASE_UNAVAILABLE", "message": "Base de datos no disponible"})
    email = str(payload.email).lower().strip()
    try:
        validate_registration_scope(db, settings, payload.account_type, payload.institution_id, payload.career_id)
        if db.users.find_one({"email": email}, {"_id": 1}):
            raise HTTPException(409, detail={"code": "EMAIL_EXISTS", "message": "Ya existe un usuario con ese correo"})
        zone = valid_timezone(payload.timezone) if payload.timezone else None
        result = start_registration(
            db, email, full_name=payload.full_name, password=payload.password, account_type=payload.account_type,
            institution_id=payload.institution_id, career_id=payload.career_id, timezone_name=zone,
        )
    except PyMongoError:
        raise HTTPException(503, detail={"code": "DATABASE_ERROR", "message": "No se pudo guardar la solicitud de registro"})
    if result is None:
        raise HTTPException(429, detail={"code": "REGISTRATION_PENDING", "message": "Ya hay un registro pendiente. Usa el código anterior o espera un minuto para volver a solicitarlo."})
    sent, challenge = result
    return {
        "success": True,
        "data": {
            "email": email, "email_verification_required": True, "verification_email_sent": sent,
            **verification_timing(challenge["expires_at"] if sent else None, challenge["sent_at"]),
        },
        "message": "Confirma tu correo para crear tu cuenta.",
    }


@router.post("/verify-email")
def verify_email(payload: VerifyEmailRequest, request: Request):
    rate_limit(request, "verify-email", 30, 60)
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"code": "DATABASE_UNAVAILABLE", "message": "Base de datos no disponible"})
    email = str(payload.email).lower().strip()
    settings = get_settings()

    def validate_scope(details):
        if not settings.public_registration_enabled:
            raise HTTPException(403, detail={"code": "REGISTRATION_DISABLED", "message": "El registro público está deshabilitado"})
        validate_registration_scope(db, settings, details["account_type"], details.get("institution_id"), details.get("career_id"))

    try:
        verified = confirm_registration(db, email, payload.code, validate_scope) or verify_email_code(db, email, payload.code)
    except DuplicateKeyError:
        raise HTTPException(409, detail={"code": "EMAIL_EXISTS", "message": "Ya existe un usuario con ese correo"})
    except PyMongoError:
        raise HTTPException(503, detail={"code": "DATABASE_ERROR", "message": "No se pudo completar el registro. Intenta de nuevo."})
    if not verified:
        raise HTTPException(400, detail={"code": "INVALID_VERIFICATION_CODE", "message": "Código inválido, vencido o sin intentos disponibles."})
    return {"success": True, "data": {"message": "Correo confirmado. Ya puedes iniciar sesión."}}


@router.post("/resend-verification")
def resend_verification(payload: ResendVerificationRequest, request: Request):
    rate_limit(request, "resend-verification", 10, 3600)
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"code": "DATABASE_UNAVAILABLE", "message": "Base de datos no disponible"})
    email = str(payload.email).lower().strip()
    if not get_settings().public_registration_enabled or not resend_registration_code(db, email):
        send_verification_code(db, email)
    return {"success": True, "data": {"message": "Si puedes verificar tu cuenta, recibirás un código."}}


@router.post("/login")
def login(payload:LoginRequest,request:Request):
    rate_limit(request,"login",8,60)
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    email = str(payload.email).lower().strip()
    user = db.users.find_one({"email": email})
    if user and user.get("registration_pending") and verify_password(payload.password, user["password_hash"]):
        validate_registration_scope(db, get_settings(), user["account_type"], user.get("institution_id"), user.get("career_id"))
        try:
            complete_registration(db, user)
        except PyMongoError:
            raise HTTPException(503, detail={"code": "DATABASE_ERROR", "message": "No se pudo completar el registro. Intenta de nuevo."})
        user = db.users.find_one({"_id": user["_id"], "email": email})
    if user is None:
        pending = find_pending_registration(db, email)
        if pending and verify_password(payload.password, pending["payload"]["password_hash"]):
            challenge = pending["challenge"]
            expires_at = pending["expires_at"] if pending.get("verified_at") else challenge["expires_at"]
            raise HTTPException(403, detail={
                "code": "EMAIL_NOT_VERIFIED",
                "message": "Confirma tu correo para completar el registro.",
                **verification_timing(expires_at, challenge["sent_at"]),
            })
    if not user or not user.get("is_active",False) or not verify_password(payload.password,user.get("password_hash","")):
        raise HTTPException(401,detail={"code":"INVALID_CREDENTIALS","message":"Correo o contraseña incorrectos"})
    if "email_verified_at" in user and user["email_verified_at"] is None:
        challenge = user.get("email_verification") or {}
        raise HTTPException(403, detail={
            "code": "EMAIL_NOT_VERIFIED",
            "message": "Confirma tu correo para iniciar sesión.",
            **verification_timing(challenge.get("expires_at"), challenge.get("sent_at")),
        })
    contexts=user_contexts(db,user)
    context=contexts[0] if len(contexts)==1 else None
    return {"success":True,"data":{**issue_tokens(db,user,context),"user":auth_user_response(db,user,context)}}

@router.post("/context")
def select_context(payload:dict,user=Depends(get_current_identity)):
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    base_user=db.users.find_one({"_id":user["_id"],"is_active":True})
    if not base_user: raise HTTPException(401,detail={"code":"USER_INACTIVE_OR_MISSING","message":"Usuario no disponible"})
    context=resolve_user_context(db,base_user,payload)
    return {"success":True,"data":{**issue_tokens(db,base_user,context),"user":auth_user_response(db,base_user,context)}}

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
    context=old.get("context")
    return {"success":True,"data":issue_tokens(db,user,context)}

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
def me(user=Depends(get_current_user)):
    db=get_database()
    context=user.get("active_context")
    return {"success":True,"data":auth_user_response(db,user,context)}

@router.post("/forgot-password")
def forgot_password(payload:dict,request:Request):
    rate_limit(request,"forgot",5,3600)
    email=str(payload.get("email","")).strip().lower()
    db=get_database()
    if db is not None and email:
        now = datetime.now(timezone.utc)
        user=db.users.find_one({"email":email,"is_active":True})
        if user:
            s=get_settings(); raw=opaque_token()
            db.password_reset_tokens.update_many({"user_id":user["_id"],"used_at":None},{"$set":{"used_at":now}})
            db.password_reset_tokens.insert_one({"user_id":user["_id"],"token_hash":token_hash(raw),"created_at":now,
              "expires_at":now+timedelta(minutes=s.password_reset_expire_minutes),"used_at":None})
            try: send_password_reset_email(email,raw)
            except Exception as error:
                # Do not disclose account existence or SMTP status to the caller.
                import logging
                logging.getLogger("checku.auth").warning(
                    "Password reset email delivery failed (%s, SMTP status=%s)",
                    type(error).__name__,
                    getattr(error, "smtp_code", None),
                )
                db.password_reset_tokens.update_one({"token_hash":token_hash(raw)},{"$set":{"used_at":now}})
    return {"success":True,"data":{"message":"Si la cuenta existe, recibirás instrucciones para recuperar tu contraseña."}}

@router.post("/reset-password")
def reset_password(payload:dict):
    raw=payload.get("token",""); password=payload.get("new_password","")
    password_error = password_policy_error(password)
    if password_error:
        raise HTTPException(422, detail={"code": "INVALID_PASSWORD", "message": password_error})
    db=get_database()
    if db is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
    now=datetime.now(timezone.utc); item=db.password_reset_tokens.find_one({"token_hash":token_hash(str(raw)),"used_at":None,"expires_at":{"$gt":now}})
    if not item: raise HTTPException(400,detail={"code":"INVALID_RESET_TOKEN","message":"Token inválido o expirado"})
    result=db.password_reset_tokens.update_one({"_id":item["_id"],"used_at":None},{"$set":{"used_at":now}})
    if result.modified_count!=1: raise HTTPException(400,detail={"code":"INVALID_RESET_TOKEN","message":"Token inválido o expirado"})
    changed = db.users.update_one(
        {"_id": item["user_id"], "$or": [
            {"email_changed_at": {"$exists": False}},
            {"email_changed_at": {"$lte": item["created_at"]}},
        ]},
        {"$set": {"password_hash": hash_password(password), "updated_at": now},
         "$unset": {"password_change": "", "email_change": ""}},
    )
    if changed.matched_count != 1:
        raise HTTPException(400, detail={"code": "INVALID_RESET_TOKEN", "message": "Token inválido o expirado"})
    db.refresh_sessions.update_many({"user_id":item["user_id"],"revoked_at":None},{"$set":{"revoked_at":now}})
    return {"success":True,"data":{"message":"Contraseña actualizada; inicia sesión nuevamente"}}

@router.patch("/change-password", status_code=202)
def change_password(payload: ChangePasswordRequest, request: Request, user=Depends(get_current_identity)):
    rate_limit(request, "change-password", 10, 60)
    if not verify_password(payload.current_password, user.get("password_hash", "")):
        raise HTTPException(400, detail={"code": "INVALID_PASSWORD", "message": "La contraseña actual es incorrecta"})
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"code": "DATABASE_UNAVAILABLE", "message": "Base de datos no disponible"})
    sent_at = datetime.now(timezone.utc)
    result = request_password_change(db, user, payload.new_password)
    if result == "delivery_failed":
        raise HTTPException(503, detail={"code": "VERIFICATION_EMAIL_FAILED", "message": "No se pudo enviar el código. Inténtalo de nuevo."})
    if result == "unavailable":
        raise HTTPException(429, detail={"code": "PASSWORD_CHANGE_UNAVAILABLE", "message": "Espera 60 segundos antes de solicitar otro código."})
    return {"success": True, "data": {"verification_required": True, "message": "Enviamos un código al correo de tu cuenta.", **new_verification_timing(sent_at)}}


@router.post("/change-password/confirm")
def confirm_change_password(payload: VerificationCodeRequest, request: Request, user=Depends(get_current_identity)):
    rate_limit(request, "confirm-password-change", 30, 60)
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"code": "DATABASE_UNAVAILABLE", "message": "Base de datos no disponible"})
    if not confirm_password_change(db, user, payload.code):
        raise HTTPException(400, detail={
            "code": "INVALID_VERIFICATION_CODE",
            "message": "Código inválido, vencido o sin intentos disponibles.",
        })
    return {"success": True, "data": {"message": "Contraseña actualizada. Vuelve a iniciar sesión."}}


@router.post("/email-change", status_code=202)
def change_email(payload: ChangeEmailRequest, request: Request, user=Depends(get_current_identity)):
    rate_limit(request, "email-change", 10, 60)
    if not verify_password(payload.current_password, user.get("password_hash", "")):
        raise HTTPException(400, detail={"message": "La contraseña actual es incorrecta."})
    email = str(payload.email).lower().strip()
    if email == user["email"] and user.get("email_verified_at"):
        raise HTTPException(400, detail={"message": "Este correo ya está verificado."})
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"message": "Base de datos no disponible."})
    if db.users.find_one({"email": email, "_id": {"$ne": user["_id"]}}, {"_id": 1}):
        raise HTTPException(409, detail={"message": "Ya existe una cuenta con ese correo."})
    sent_at = datetime.now(timezone.utc)
    result = request_email_change(db, user, email)
    if result == "unavailable":
        raise HTTPException(429, detail={"message": "Espera 60 segundos antes de solicitar otro código."})
    if result == "delivery_failed":
        raise HTTPException(503, detail={"message": "No se pudo enviar el código. Inténtalo de nuevo."})
    return {"success": True, "data": {"email": email, "message": "Enviamos un código al correo indicado.", **new_verification_timing(sent_at)}}


@router.post("/email-change/confirm")
def confirm_change_email(payload: VerifyEmailRequest, request: Request, user=Depends(get_current_identity)):
    rate_limit(request, "confirm-email-change", 30, 60)
    db = get_database()
    if db is None:
        raise HTTPException(503, detail={"message": "Base de datos no disponible."})
    try:
        verified = confirm_email_change(db, user, str(payload.email).lower().strip(), payload.code)
    except DuplicateKeyError:
        raise HTTPException(409, detail={"message": "Ya existe una cuenta con ese correo."})
    if not verified:
        raise HTTPException(400, detail={"message": "Código inválido, vencido o sin intentos disponibles."})
    return {"success": True, "data": {"message": "Correo verificado y guardado."}}


@router.patch('/profile')
def update_my_profile(payload: dict, user=Depends(get_current_user)):
    db = get_database()
    changes = {}
    if 'timezone' in payload:
        changes['timezone'] = valid_timezone(payload['timezone'])
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
