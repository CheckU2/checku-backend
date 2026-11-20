from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize, audit
from app.routers.academic.classes import id_refs, scope as class_scope
from app.routers.academic.sessions import student_session_query
from app.routers.academic.proximity import verify_checkin_location
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
 session_scope=student_session_query(db,user) if user.get("institution_id") else {}
 if not ObjectId.is_valid(sid):
  digest=hashlib.sha256(value.encode()).hexdigest()
  key="token_hash" if field=="qr_token" else "code_hash"
  challenge_query={key:digest,"revoked_at":None,"expires_at":{"$gt":now}}
  if session_scope:
   challenge_query["session_id"]={"$in":[str(item["_id"]) for item in db.class_sessions.find({**session_scope,"status":"active"},{"_id":1})]}
  challenges=list(db.qr_challenges.find(challenge_query))
  if len(challenges)!=1: raise HTTPException(400,detail={"message":"Código inválido, expirado o ambiguo. Solicita un código nuevo"})
  sid=challenges[0]["session_id"]
 session=db.class_sessions.find_one({"_id":ObjectId(sid),"status":"active",**session_scope})
 if not session: raise HTTPException(409,detail={"code":"SESSION_NOT_ACTIVE","message":"La clase no está activa"})
 # Resolve student profile ID if the institution uses a separate students collection.
 profile=db.students.find_one({"user_id":str(user["_id"])}) or db.students.find_one({"user_id":user["_id"]})
 student_id=str(profile["_id"]) if profile else str(user["_id"])
 student_refs=[student_id,str(user["_id"]),user["_id"]]
 if profile: student_refs.append(profile["_id"])
 class_id=str(session.get("class_id", ""))
 enrolled=db.class_memberships.find_one({"class_id":{"$in":id_refs(class_id)},"student_id":{"$in":id_refs(user["_id"])}}) if ObjectId.is_valid(class_id) else None
 if not enrolled: raise HTTPException(403,detail={"code":"NOT_ENROLLED","message":"No estás inscrito en esta clase"})
 ch=db.qr_challenges.find_one({"session_id":sid,"revoked_at":None,"expires_at":{"$gt":now}})
 if not ch: raise HTTPException(410,detail={"code":"CHALLENGE_EXPIRED","message":"El código QR o código temporal expiró"})
 digest=hashlib.sha256(value.encode()).hexdigest()
 if ch.get("attempts",0)>=10: raise HTTPException(429,detail={"code":"TOO_MANY_ATTEMPTS","message":"Demasiados intentos"})
 valid=digest==ch.get("token_hash") if field=="qr_token" else digest==ch.get("code_hash")
 if not valid:
  db.qr_challenges.update_one({"_id":ch["_id"]},{"$inc":{"attempts":1}})
  raise HTTPException(400,detail={"code":"INVALID_CHALLENGE","message":"Código inválido"})
 if session.get("gps_checkin_required"):
  verify_checkin_location(session, payload.get("location"))
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
def my_attendance(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),status:str|None=None,user=Depends(get_current_user)):
 db=dbx()
 q=scoped_attendance(db,user,str(user["_id"]) if user.get("role")=="student" else None)
 if status:
  q["status"] = status
 total=db.attendance.count_documents(q); docs=db.attendance.find(q).sort("recorded_at",-1).skip((page-1)*limit).limit(limit)
 return {"success":True,"data":[serialize(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
def scoped_attendance_sessions(db, user, group_id=None):
 query=class_scope(db,user)
 if group_id is not None:
  query={"$and":[query,{"group_id":{"$in":id_refs(group_id)}}]}
 classes=db.classes.find(query,{"_id":1})
 return {"class_id":{"$in":[ref for cls in classes for ref in id_refs(cls["_id"])]}}


def scoped_attendance(db, user, student_id=None, group_id=None):
 role=user.get("role")
 if role=="student" and str(user["_id"])!=student_id:
  raise HTTPException(404,detail={"message":"Registro no encontrado"})
 sessions=db.class_sessions.find(scoped_attendance_sessions(db,user,group_id),{"_id":1})
 query={"session_id":{"$in":[ref for row in sessions for ref in id_refs(row["_id"])]}}
 if student_id:
  query.update(student_attendance_filter(db,student_id))
 return query


def student_attendance_filter(db, student_id):
 refs=id_refs(student_id)
 profiles=db.students.find({"user_id":{"$in":refs}},{"_id":1})
 keys=refs+[ref for profile in profiles for ref in id_refs(profile["_id"])]
 return {"$or":[{"student_id":{"$in":keys}},{"user_id":{"$in":refs}}]}


def is_automatic_absence(record):
 return record.get("status")=="absent" and record.get("created_by")=="system"


def attendance_for_student(db, session_id, student_id):
 query={"session_id":{"$in":id_refs(session_id)},**student_attendance_filter(db,student_id)}
 rows=list(db.attendance.find(query).sort([("updated_at",-1),("recorded_at",-1)]))
 return next((row for row in rows if not is_automatic_absence(row)), rows[0] if rows else None)


def as_utc(value):
 return value.replace(tzinfo=timezone.utc) if not value.tzinfo else value


@router.get("/calendar")
def attendance_calendar(
 year:int=Query(...,ge=2000,le=2100), month:int=Query(...,ge=1,le=12),
 student_id:str|None=None, user=Depends(get_current_user),
):
 role=user.get("role")
 if role not in {"student","teacher","tutor"}:
  raise HTTPException(403,detail={"message":"Sin permisos"})
 if role=="student":
  if student_id and student_id!=str(user["_id"]):
   raise HTTPException(404,detail={"message":"Alumno no encontrado"})
  student_id=str(user["_id"])
 if not ObjectId.is_valid(student_id or ""):
  raise HTTPException(422,detail={"message":"Selecciona un alumno válido"})
 db=dbx()
 classes=list(db.classes.find(class_scope(db,user),{"subject_id":1,"group_id":1}))
 class_map={str(cls["_id"]):cls for cls in classes}
 memberships=db.class_memberships.find({
  "student_id":{"$in":id_refs(student_id)},
  "class_id":{"$in":[ref for cls in classes for ref in id_refs(cls["_id"])]},
 })
 enrollment={str(member["class_id"]):member.get("joined_at") for member in memberships}
 if not enrollment:
  if role=="student":
   return {"success":True,"data":[]}
  raise HTTPException(404,detail={"message":"Alumno no encontrado en tus clases"})
 zone=ZoneInfo("America/Mexico_City")
 start=datetime(year,month,1,tzinfo=zone).astimezone(timezone.utc)
 end=datetime(year+(month==12),month%12+1,1,tzinfo=zone).astimezone(timezone.utc)
 interval={"$gte":start,"$lt":end}
 sessions=list(db.class_sessions.find({
  "class_id":{"$in":[ref for cid in enrollment for ref in id_refs(cid)]},
  "status":{"$in":["active","closed"]},
  "$or":[{"started_at":interval},{"started_at":None,"created_at":interval}],
 }).sort("started_at",1))
 records=db.attendance.find({
  "session_id":{"$in":[ref for row in sessions for ref in id_refs(row["_id"])]},
  **student_attendance_filter(db,student_id),
 }).sort([("updated_at",1),("recorded_at",1)])
 by_session={}
 for row in records:
  sid=str(row["session_id"])
  if sid not in by_session or not is_automatic_absence(row) or is_automatic_absence(by_session[sid]):
   by_session[sid]=row
 subject_ids={str(cls.get("subject_id")) for cls in classes}
 subjects={str(row["_id"]):row.get("name") for row in db.subjects.find({
  "_id":{"$in":[ObjectId(ref) for ref in subject_ids if ObjectId.is_valid(ref)]},
 },{"name":1})}
 data=[]
 for session in sessions:
  cid=str(session["class_id"]); sid=str(session["_id"])
  start_time=as_utc(session.get("started_at") or session["created_at"])
  joined=enrollment[cid]
  if joined and as_utc(joined)>as_utc(session.get("ended_at") or datetime.now(timezone.utc)):
   continue
  record=by_session.get(sid)
  state=(record or {}).get("status") or ("absent" if session["status"]=="closed" else "pending")
  subject=subjects.get(str(session.get("subject_id") or class_map[cid].get("subject_id"))) or "Clase"
  data.append({"id":sid,"date":start_time.astimezone(zone).date().isoformat(),"subject":subject,
               "started_at":start_time,"status":state})
 return {"success":True,"data":data}


@router.get("/session/{session_id}")
def by_session(session_id:str,user=Depends(get_current_user)):
 db=dbx()
 if not ObjectId.is_valid(session_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 session=db.class_sessions.find_one({"_id":ObjectId(session_id)})
 if not session: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Sesión no encontrada"})
 if user.get("role")=="student" or not db.class_sessions.find_one({"_id":session["_id"],**scoped_attendance_sessions(db,user)}):
  raise HTTPException(404,detail={"message":"Sesión no encontrada"})
 return {"success":True,"data":[serialize(x) for x in db.attendance.find({"session_id":{"$in":id_refs(session_id)}}).limit(500)]}
@router.get("/summary")
def summary(user=Depends(get_current_user)):
 db=dbx()
 docs=list(db.attendance.find(scoped_attendance(db,user,str(user["_id"]) if user.get("role")=="student" else None)).limit(5000))
 total=len(docs); present=sum(1 for x in docs if x.get("status") in {"present","late","excused"})
 return {"success":True,"data":{"sessions_evaluated":total,"present_or_excused":present,"absences":sum(1 for x in docs if x.get("status")=="absent"),"attendance_percentage":round(100*present/total,2) if total else None,"minimum_percentage":80,"at_risk":bool(total and present/total<0.8)}}

@router.get("/{attendance_id}")
def get_attendance(attendance_id:str,user=Depends(get_current_user)):
 if not ObjectId.is_valid(attendance_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx()
 query=scoped_attendance(db,user,str(user["_id"]) if user.get("role")=="student" else None)
 doc=db.attendance.find_one({"_id":ObjectId(attendance_id),**query})
 if not doc: raise HTTPException(404,detail={"message":"Registro no encontrado"})
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
 db=dbx()
 return {"success":True,"data":[serialize(x) for x in db.attendance.find(scoped_attendance(db,user,student_id)).limit(500)]}
@router.get("/group/{group_id}")
def group_attendance(group_id:str,user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx(); query=scoped_attendance(db,user,group_id=group_id)
 return {"success":True,"data":[serialize(x) for x in db.attendance.find(query).limit(1000)]}
