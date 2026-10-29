from datetime import datetime,timezone
from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException,Query
from pymongo import DESCENDING
from app.database import get_database
from app.dependencies import get_current_user,require_roles
from app.schemas.user import UserCreate
from app.services.users import create_user,ensure_user_indexes,public_user
from app.routers.academic.generic import audit
from pymongo.errors import DuplicateKeyError
router=APIRouter(prefix="/users",tags=["Usuarios"])
def dbx():
 d=get_database()
 if d is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 return d
def scoped_query(user,target=None):
 q={}
 if target is not None: q["_id"]=target
 if user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
 elif user.get("role") not in {"super_admin","admin"}:
  if target is not None and target!=user["_id"]: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Usuario no encontrado"})
  q["_id"]=user["_id"]
 return q
@router.post("",status_code=201)
def create_user_route(payload:UserCreate,admin=Depends(require_roles("super_admin","admin"))):
 role=payload.role
 career_id=payload.career_id
 if admin.get("role")=="career_admin":
  role=payload.role
  career_id=admin.get("career_id")
  if role=="super_admin": raise HTTPException(403,detail={"code":"FORBIDDEN","message":"No puedes asignar ese rol"})
 # Al crear un docente o tutor, se crea también su perfil académico.
 employee_id=(payload.employee_id or "").strip()
 if role in {"teacher","tutor"} and not employee_id:
  raise HTTPException(422,detail={"code":"EMPLOYEE_REQUIRED","message":"El número de empleado es obligatorio para docentes y tutores"})
 if role not in {"teacher","tutor"} and employee_id:
  raise HTTPException(422,detail={"code":"EMPLOYEE_ROLE","message":"El número de empleado solo corresponde a docentes o tutores"})
 if career_id and not ObjectId.is_valid(career_id):
  raise HTTPException(422,detail={"code":"INVALID_ID","message":"La carrera seleccionada no es válida"})
 db=dbx()
 if career_id and not db.careers.find_one({"_id":ObjectId(career_id),"is_deleted":{"$ne":True}}):
  raise HTTPException(422,detail={"code":"CAREER_NOT_FOUND","message":"La carrera seleccionada no existe"})
 collection={"teacher":"teachers","tutor":"tutors"}.get(role)
 if collection and db[collection].find_one({"employee_id":employee_id,"is_deleted":{"$ne":True}}):
  raise HTTPException(409,detail={"code":"EMPLOYEE_EXISTS","message":"Ya existe un perfil con ese número de empleado"})
 try:
  user=create_user(email=str(payload.email),password=payload.password,role=role,full_name=payload.full_name,career_id=career_id)
 except ValueError as exc: raise HTTPException(409,detail={"code":"EMAIL_EXISTS","message":str(exc)})
 except RuntimeError: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 if collection:
  now=datetime.now(timezone.utc)
  profile={"user_id":str(user["_id"]),"employee_id":employee_id,"is_active":True,"is_deleted":False,"created_at":now,"updated_at":now}
  if career_id: profile["career_id"]=career_id
  if role=="teacher" and payload.department: profile["department"]=payload.department.strip()
  try:
   db[collection].insert_one(profile)
  except DuplicateKeyError:
   db.users.delete_one({"_id":user["_id"]})
   raise HTTPException(409,detail={"code":"EMPLOYEE_EXISTS","message":"El número de empleado ya está registrado"})
  except Exception:
   db.users.delete_one({"_id":user["_id"]})
   raise
 audit(db,admin,"create","users",str(user["_id"]))
 return {"success":True,"data":public_user(user)}

@router.get("")
def list_users(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),user=Depends(get_current_user)):
 if user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx(); q=scoped_query(user); total=db.users.count_documents(q)
 docs=db.users.find(q,{"password_hash":0}).sort("created_at",DESCENDING).skip((page-1)*limit).limit(limit)
 return {"success":True,"data":[public_user(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
@router.get("/{user_id}")
def get_user(user_id:str,current=Depends(get_current_user)):
 if not ObjectId.is_valid(user_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx(); user=db.users.find_one(scoped_query(current,ObjectId(user_id)),{"password_hash":0})
 if not user: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Usuario no encontrado"})
 return {"success":True,"data":public_user(user)}
@router.patch("/{user_id}")
def patch_user(user_id:str,payload:dict,current=Depends(get_current_user)):
 if not ObjectId.is_valid(user_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 target=ObjectId(user_id); admin=current.get("role") in {"super_admin","admin"}
 if not admin and target!=current["_id"]: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Usuario no encontrado"})
 allowed={"full_name"} if not admin else {"full_name","career_id"}
 data={k:v for k,v in payload.items() if k in allowed}
 if not data: raise HTTPException(422,detail={"code":"NO_FIELDS","message":"No hay campos modificables"})
 if current.get("role")=="career_admin": data.pop("career_id",None)
 data["updated_at"]=datetime.now(timezone.utc); db=dbx(); q=scoped_query(current,target)
 res=db.users.update_one(q,{"$set":data})
 if not res.matched_count: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Usuario no encontrado"})
 audit(db,current,"update","users",user_id)
 return {"success":True,"data":public_user(db.users.find_one(q))}
@router.patch("/{user_id}/status")
def status_user(user_id:str,payload:dict,current=Depends(require_roles("super_admin","admin"))):
 if not ObjectId.is_valid(user_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 active=payload.get("is_active")
 if not isinstance(active,bool): raise HTTPException(422,detail={"code":"INVALID_STATUS","message":"is_active debe ser booleano"})
 target=ObjectId(user_id); db=dbx(); q=scoped_query(current,target)
 if target==current["_id"] and not active: raise HTTPException(409,detail={"code":"SELF_DEACTIVATION","message":"No puedes desactivar tu propia cuenta"})
 res=db.users.update_one(q,{"$set":{"is_active":active,"updated_at":datetime.now(timezone.utc)}})
 if not res.matched_count: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Usuario no encontrado"})
 if not active: db.refresh_sessions.update_many({"user_id":target,"revoked_at":None},{"$set":{"revoked_at":datetime.now(timezone.utc)}})
 audit(db,current,"status_change","users",user_id)
 return {"success":True,"data":public_user(db.users.find_one(q))}
@router.delete("/{user_id}")
def delete_user(user_id:str,current=Depends(require_roles("super_admin","admin"))):
 if not ObjectId.is_valid(user_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 target=ObjectId(user_id)
 if target==current["_id"]: raise HTTPException(409,detail={"code":"SELF_DEACTIVATION","message":"No puedes desactivar tu propia cuenta"})
 db=dbx(); q=scoped_query(current,target); res=db.users.update_one(q,{"$set":{"is_active":False,"deleted_at":datetime.now(timezone.utc),"updated_at":datetime.now(timezone.utc)}})
 if not res.matched_count: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Usuario no encontrado"})
 db.refresh_sessions.update_many({"user_id":target,"revoked_at":None},{"$set":{"revoked_at":datetime.now(timezone.utc)}})
 audit(db,current,"soft_delete","users",user_id)
 return {"success":True,"data":{"id":user_id,"deleted":True}}


@router.post("/me/avatar")
async def upload_avatar(file: __import__("fastapi").UploadFile, current=Depends(get_current_user)):
    import io, secrets
    from pathlib import Path
    from PIL import Image, UnidentifiedImageError
    from app.config import get_settings
    settings=__import__("app.config",fromlist=["get_settings"]).get_settings()
    raw=await file.read(settings.max_avatar_bytes+1)
    if not raw or len(raw)>settings.max_avatar_bytes:
        raise HTTPException(413,detail={"code":"FILE_TOO_LARGE","message":"La imagen está vacía o supera el tamaño permitido"})
    try:
        image=Image.open(io.BytesIO(raw))
        image.verify()
        image=Image.open(io.BytesIO(raw))
        if image.format not in {"JPEG","PNG","WEBP"}: raise ValueError()
        image=image.convert("RGB")
        out=io.BytesIO(); image.save(out,format="JPEG",quality=88,optimize=True)
    except Exception:
        raise HTTPException(422,detail={"code":"INVALID_IMAGE","message":"Solo se permiten imágenes JPEG, PNG o WebP válidas"})
    directory=Path(settings.upload_dir).resolve(); directory.mkdir(parents=True,exist_ok=True)
    filename=f"{secrets.token_hex(24)}.jpg"; path=directory/filename
    path.write_bytes(out.getvalue())
    db=dbx(); old=db.users.find_one({"_id":current["_id"]})
    db.users.update_one({"_id":current["_id"]},{"$set":{"avatar_path":str(path),"avatar_content_type":"image/jpeg","updated_at":datetime.now(timezone.utc)}})
    if old and old.get("avatar_path"):
        try:
            Path(old["avatar_path"]).unlink(missing_ok=True)
        except OSError: pass
    return {"success":True,"data":{"message":"Foto actualizada","avatar_url":"/api/v1/users/me/avatar"}}

@router.get("/me/avatar")
def get_avatar(current=Depends(get_current_user)):
    from pathlib import Path
    from fastapi.responses import FileResponse
    db=dbx(); user=db.users.find_one({"_id":current["_id"]})
    path=user.get("avatar_path") if user else None
    if not path or not Path(path).is_file(): raise HTTPException(404,detail={"code":"AVATAR_NOT_FOUND","message":"Foto no encontrada"})
    return FileResponse(path,media_type="image/jpeg",headers={"Cache-Control":"private, max-age=300"})

@router.delete("/me/avatar")
def delete_avatar(current=Depends(get_current_user)):
    from pathlib import Path
    db=dbx(); user=db.users.find_one({"_id":current["_id"]})
    if user and user.get("avatar_path"):
        try: Path(user["avatar_path"]).unlink(missing_ok=True)
        except OSError: pass
    db.users.update_one({"_id":current["_id"]},{"$unset":{"avatar_path":"","avatar_content_type":""},"$set":{"updated_at":datetime.now(timezone.utc)}})
    return {"success":True,"data":{"deleted":True}}
