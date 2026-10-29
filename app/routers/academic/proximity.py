from datetime import datetime,timezone
from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize,audit
router=APIRouter(prefix="/proximity",tags=["Proximidad"])
def dbx():
 d=get_database()
 if d is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 return d
def auth_session(db,sid,user):
 if not ObjectId.is_valid(sid): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 s=db.class_sessions.find_one({"_id":ObjectId(sid)})
 if not s: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
 if user.get("role") not in {"super_admin","admin"} and s.get("teacher_id")!=str(user["_id"]): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return s
@router.post("/sessions/{session_id}/activate")
def activate(session_id:str,payload:dict,user=Depends(get_current_user)):
 db=dbx(); s=auth_session(db,session_id,user)
 if s.get("status")!="active": raise HTTPException(409,detail={"code":"SESSION_NOT_ACTIVE","message":"La clase no está activa"})
 profile=payload.get("profile","standard")
 if profile not in {"conservative","standard","strict"}: raise HTTPException(422,detail={"code":"INVALID_PROFILE","message":"Perfil no permitido"})
 db.class_sessions.update_one({"_id":s["_id"]},{"$set":{"proximity_enabled":True,"proximity_profile":profile,"proximity_activated_at":datetime.now(timezone.utc)}})
 audit(db,user,"proximity_activate","class_sessions",session_id)
 return {"success":True,"data":{"session_id":session_id,"enabled":True,"profile":profile}}
@router.post("/sessions/{session_id}/deactivate")
def deactivate(session_id:str,user=Depends(get_current_user)):
 db=dbx(); s=auth_session(db,session_id,user); db.class_sessions.update_one({"_id":s["_id"]},{"$set":{"proximity_enabled":False,"proximity_deactivated_at":datetime.now(timezone.utc)}})
 return {"success":True,"data":{"enabled":False}}
@router.post("/sessions/{session_id}/challenge")
def challenge(session_id:str,user=Depends(get_current_user)):
 db=dbx(); s=auth_session(db,session_id,user)
 if not s.get("proximity_enabled"): raise HTTPException(409,detail={"code":"PROXIMITY_DISABLED","message":"Proximidad desactivada"})
 token=__import__("secrets").token_urlsafe(24); now=datetime.now(timezone.utc)
 db.proximity_challenges.insert_one({"session_id":session_id,"token_hash":__import__("hashlib").sha256(token.encode()).hexdigest(),"created_at":now,"expires_at":now.replace(second=now.second)+__import__("datetime").timedelta(seconds=60)})
 return {"success":True,"data":{"challenge":token,"expires_in_seconds":60}}
@router.post("/sessions/{session_id}/check-in")
def proximity_checkin(session_id:str,payload:dict,user=Depends(get_current_user)):
 if user.get("role")!="student": raise HTTPException(403,detail={"code":"STUDENT_ONLY","message":"Solo alumnos pueden reportar proximidad"})
 db=dbx(); s=db.class_sessions.find_one({"_id":ObjectId(session_id),"status":"active"}) if ObjectId.is_valid(session_id) else None
 if not s or not s.get("proximity_enabled"): raise HTTPException(409,detail={"code":"PROXIMITY_DISABLED","message":"Proximidad no disponible"})
 return {"success":True,"data":{"accepted":True,"review_required":True,"message":"La proximidad es evidencia auxiliar y requiere múltiples lecturas."}}
@router.post("/sessions/{session_id}/events",status_code=201)
def event(session_id:str,payload:dict,user=Depends(get_current_user)):
 if user.get("role")!="student": raise HTTPException(403,detail={"code":"STUDENT_ONLY","message":"Solo alumnos pueden enviar eventos"})
 db=dbx(); s=db.class_sessions.find_one({"_id":ObjectId(session_id),"status":"active"}) if ObjectId.is_valid(session_id) else None
 if not s or not s.get("proximity_enabled"): raise HTTPException(409,detail={"code":"PROXIMITY_DISABLED","message":"Proximidad no disponible"})
 kind=payload.get("kind"); confidence=payload.get("confidence",0)
 if kind not in {"inside","outside","return","uncertain"} or not isinstance(confidence,(int,float)) or not 0<=confidence<=1: raise HTTPException(422,detail={"code":"INVALID_EVENT","message":"Evento inválido"})
 now=datetime.now(timezone.utc); doc={"session_id":session_id,"student_id":str(user["_id"]),"kind":kind,"confidence":confidence,"source":str(payload.get("source","unknown"))[:40],"status":"pending_review","created_at":now}
 res=db.proximity_events.insert_one(doc); doc["_id"]=res.inserted_id
 return {"success":True,"data":serialize(doc)}
@router.get("/sessions/{session_id}/status")
def status(session_id:str,user=Depends(get_current_user)):
 db=dbx(); s=auth_session(db,session_id,user)
 return {"success":True,"data":{"enabled":s.get("proximity_enabled",False),"profile":s.get("proximity_profile")}}
@router.get("/sessions/{session_id}/events")
def events(session_id:str,user=Depends(get_current_user)):
 db=dbx(); auth_session(db,session_id,user)
 return {"success":True,"data":[serialize(x) for x in db.proximity_events.find({"session_id":session_id}).sort("created_at",-1).limit(500)]}
