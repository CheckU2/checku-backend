from datetime import datetime, timezone
import hashlib
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit
router=APIRouter(prefix="/attendance",tags=["Asistencia"])
def dbx():
 d=get_database()
 if d is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 return d
def checkin(payload,user,field):
 if user.get("role")!="student": raise HTTPException(403,detail={"code":"STUDENT_ONLY","message":"Solo un alumno puede registrar su asistencia"})
 sid=str(payload.get("session_id", "")); value=str(payload.get(field, "")).strip()
 if field == "qr_token" and value.startswith("{"):
  import json
  try:
   qr_payload=json.loads(value)
   sid=str(qr_payload.get("session_id", sid))
   value=str(qr_payload.get("qr_token", ""))
  except (ValueError, TypeError):
   raise HTTPException(422,detail={"message":"El contenido del QR no es válido"})
 if not value: raise HTTPException(422,detail={"code":"INVALID_CHALLENGE","message":"Introduce el PIN o escanea el QR"})
 db=dbx(); now=datetime.now(timezone.utc)
 if not ObjectId.is_valid(sid):
  digest=hashlib.sha256(value.encode()).hexdigest()
  key="token_hash" if field=="qr_token" else "code_hash"
  challenges=list(db.qr_challenges.find({key:digest,"revoked_at":None,"expires_at":{"$gt":now}}))
  if len(challenges)!=1: raise HTTPException(400,detail={"message":"Código inválido, expirado o ambiguo. Solicita un código nuevo"})
  sid=challenges[0]["session_id"]
 session=db.class_sessions.find_one({"_id":ObjectId(sid),"status":"active"})
 if not session: raise HTTPException(409,detail={"code":"SESSION_NOT_ACTIVE","message":"La clase no está activa"})
 # Resolve student profile ID if the institution uses a separate students collection.
 profile=db.students.find_one({"user_id":str(user["_id"])}) or db.students.find_one({"user_id":user["_id"]})
 student_id=str(profile["_id"]) if profile else str(user["_id"])
 student_refs=[student_id,str(user["_id"]),user["_id"]]
 if profile: student_refs.append(profile["_id"])
 class_id=str(session.get("class_id", ""))
 enrolled=db.class_memberships.find_one({"class_id":ObjectId(class_id),"student_id":str(user["_id"])}) if ObjectId.is_valid(class_id) else None
 if not enrolled: raise HTTPException(403,detail={"code":"NOT_ENROLLED","message":"No estás inscrito en esta clase"})
 ch=db.qr_challenges.find_one({"session_id":sid,"revoked_at":None,"expires_at":{"$gt":now}})
 if not ch: raise HTTPException(410,detail={"code":"CHALLENGE_EXPIRED","message":"El código QR o código temporal expiró"})
 digest=hashlib.sha256(value.encode()).hexdigest()
 if ch.get("attempts",0)>=10: raise HTTPException(429,detail={"code":"TOO_MANY_ATTEMPTS","message":"Demasiados intentos"})
 valid=digest==ch.get("token_hash") if field=="qr_token" else digest==ch.get("code_hash")
 if not valid:
  db.qr_challenges.update_one({"_id":ch["_id"]},{"$inc":{"attempts":1}})
  raise HTTPException(400,detail={"code":"INVALID_CHALLENGE","message":"Código inválido"})
 doc={"session_id":sid,"student_id":student_id,"user_id":user["_id"],"status":"present","recorded_at":now,"created_at":now,"source":"qr" if field=="qr_token" else "code"}
 try:
  res=db.attendance.insert_one(doc); doc["_id"]=res.inserted_id
 except Exception as exc:
  from pymongo.errors import DuplicateKeyError
  if isinstance(exc,DuplicateKeyError):
   existing=db.attendance.find_one({"session_id":sid,"student_id":student_id})
   return {"success":True,"data":serialize(existing),"meta":{"idempotent":True}}
  raise
 audit(db,user,"check_in","attendance",str(doc["_id"]))
 result=serialize(doc)
 from app.routers.academic.sessions import details
 result["class"] = details(db,session)
 return {"success":True,"data":result}
@router.post("/qr/check-in",status_code=201)
def qr_checkin(payload:dict,user=Depends(get_current_user)): return checkin(payload,user,"qr_token")
@router.post("/code/check-in",status_code=201)
def code_checkin(payload:dict,user=Depends(get_current_user)): return checkin(payload,user,"code")
@router.post("/qr/validate")
def validate(payload:dict,user=Depends(get_current_user)):
 sid=str(payload.get("session_id","")); token=str(payload.get("qr_token",""))
 if user.get("role")!="student": raise HTTPException(403,detail={"code":"STUDENT_ONLY","message":"Solo alumnos pueden validar asistencia"})
 db=dbx()
 if not ObjectId.is_valid(sid): raise HTTPException(422,detail={"code":"INVALID_CHALLENGE","message":"Desafío inválido"})
 ch=db.qr_challenges.find_one({"session_id":sid,"token_hash":hashlib.sha256(token.encode()).hexdigest(),"revoked_at":None,"expires_at":{"$gt":datetime.now(timezone.utc)}})
 return {"success":True,"data":{"valid":bool(ch)}}
@router.get("/me")
def my_attendance(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),user=Depends(get_current_user)):
 db=dbx()
 if user.get("role")=="student":
  p=db.students.find_one({"user_id":str(user["_id"])}) or db.students.find_one({"user_id":user["_id"]})
  ids=[str(p["_id"]),str(user["_id"])] if p else [str(user["_id"])]
  q={"student_id":{"$in":ids}}
 elif user.get("role") in {"super_admin","admin","career_admin","teacher","tutor"}:
  q={}
  if user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
  if user.get("role")=="teacher": q["teacher_id"]=str(user["_id"])
  if user.get("role")=="tutor": q["tutor_id"]=str(user["_id"])
 else: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 total=db.attendance.count_documents(q); docs=db.attendance.find(q).sort("recorded_at",-1).skip((page-1)*limit).limit(limit)
 return {"success":True,"data":[serialize(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
@router.get("/session/{session_id}")
def by_session(session_id:str,user=Depends(get_current_user)):
 db=dbx()
 if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 session=db.class_sessions.find_one({"_id":ObjectId(session_id)})
 if not session: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
 if user.get("role")=="teacher" and session.get("teacher_id")!=str(user["_id"]): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 if user.get("role")=="career_admin" and session.get("career_id")!=user.get("career_id"): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 if user.get("role")=="student": raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return {"success":True,"data":[serialize(x) for x in db.attendance.find({"session_id":session_id}).limit(500)]}
@router.get("/{attendance_id}")
def get_attendance(attendance_id:str,user=Depends(get_current_user)):
 if not ObjectId.is_valid(attendance_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx(); doc=db.attendance.find_one({"_id":ObjectId(attendance_id)})
 if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
 if user.get("role")=="student" and doc.get("user_id")!=user["_id"] and doc.get("student_id")!=str(user["_id"]): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
 if user.get("role") not in {"student","teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return {"success":True,"data":serialize(doc)}
@router.patch("/{attendance_id}")
def correct_attendance(attendance_id:str,payload:dict,user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 reason=str(payload.get("reason","")).strip(); status=payload.get("status")
 if len(reason)<5 or status not in {"present","late","absent","excused","pending_review","invalidated"}: raise HTTPException(422,detail={"code":"INVALID_CORRECTION","message":"Estado o motivo inválido"})
 if not ObjectId.is_valid(attendance_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx(); doc=db.attendance.find_one({"_id":ObjectId(attendance_id)})
 if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
 session=db.class_sessions.find_one({"_id":ObjectId(doc["session_id"])}) if ObjectId.is_valid(doc.get("session_id","")) else None
 if user.get("role")=="teacher" and (not session or session.get("teacher_id")!=str(user["_id"])): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db.attendance.update_one({"_id":doc["_id"]},{"$set":{"status":status,"updated_at":datetime.now(timezone.utc)}})
 audit(db,user,"correct_attendance","attendance",attendance_id)
 db.audit_logs.insert_one({"actor_id":user["_id"],"action":"attendance_correction_reason","entity_id":attendance_id,"reason":reason,"created_at":datetime.now(timezone.utc)})
 return {"success":True,"data":serialize(db.attendance.find_one({"_id":doc["_id"]}))}
@router.post("/manual",status_code=201)
def manual(payload:dict,user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 sid=str(payload.get("session_id","")); student=str(payload.get("student_id","")); status=payload.get("status","present"); reason=str(payload.get("reason","")).strip()
 if not ObjectId.is_valid(sid) or len(student)<1 or len(reason)<5 or status not in {"present","late","absent","excused"}: raise HTTPException(422,detail={"code":"INVALID_DATA","message":"Datos de asistencia manual inválidos"})
 db=dbx(); session=db.class_sessions.find_one({"_id":ObjectId(sid)})
 if not session: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
 if user.get("role")=="teacher" and session.get("teacher_id")!=str(user["_id"]): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 now=datetime.now(timezone.utc); doc={"session_id":sid,"student_id":student,"status":status,"recorded_at":now,"manual":True,"reason":reason,"created_by":str(user["_id"])}
 db.attendance.update_one({"session_id":sid,"student_id":student},{"$set":doc},upsert=True)
 audit(db,user,"manual_attendance","attendance",sid)
 return {"success":True,"data":serialize(db.attendance.find_one({"session_id":sid,"student_id":student}))}
@router.get("/student/{student_id}")
def student_attendance(student_id:str,user=Depends(get_current_user)):
 if user.get("role")=="student" and student_id!=str(user["_id"]): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
 if user.get("role") not in {"student","teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return {"success":True,"data":[serialize(x) for x in dbx().attendance.find({"student_id":student_id}).limit(500)]}
@router.get("/group/{group_id}")
def group_attendance(group_id:str,user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx(); sessions=[str(s["_id"]) for s in db.class_sessions.find({"group_id":group_id},{"_id":1}).limit(500)]
 return {"success":True,"data":[serialize(x) for x in db.attendance.find({"session_id":{"$in":sessions}}).limit(1000)]}
@router.get("/summary")
def summary(user=Depends(get_current_user)):
 docs=list(dbx().attendance.find({"user_id":user["_id"] if user.get("role")=="student" else {"$exists":True}}).limit(5000))
 total=len(docs); present=sum(1 for x in docs if x.get("status") in {"present","late","excused"})
 return {"success":True,"data":{"sessions_evaluated":total,"present_or_excused":present,"absences":sum(1 for x in docs if x.get("status")=="absent"),"attendance_percentage":round(100*present/total,2) if total else None,"minimum_percentage":80,"at_risk":bool(total and present/total<0.8)}}
