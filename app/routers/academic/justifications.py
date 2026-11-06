from datetime import datetime,timezone
from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException,Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize,audit
router=APIRouter(prefix="/justifications",tags=["Justificantes y mensajes"])
def dbx():
 d=get_database()
 if d is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 return d
def can_access(db,user,doc):
 role=user.get("role")
 if role in {"super_admin","admin"}: return True
 if role=="career_admin": return doc.get("career_id")==user.get("career_id")
 if role=="student": return doc.get("student_user_id")==user["_id"] or doc.get("student_id")==str(user["_id"])
 if role=="tutor": return doc.get("tutor_id")==str(user["_id"])
 if role=="teacher":
  if str(doc.get("teacher_id"))==str(user["_id"]): return True
  sid=str(doc.get("session_id", ""))
  session=db.class_sessions.find_one({"_id":ObjectId(sid)},{"teacher_id":1,"class_id":1}) if ObjectId.is_valid(sid) else None
  if not session: return False
  if str(session.get("teacher_id"))==str(user["_id"]): return True
  cid=str(session.get("class_id", ""))
  cls=db.classes.find_one({"_id":ObjectId(cid)},{"teacher_id":1}) if ObjectId.is_valid(cid) else None
  return bool(cls and str(cls.get("teacher_id"))==str(user["_id"]))
 return False
def _refs(value):
 v=str(value or "")
 return [v, ObjectId(v)] if ObjectId.is_valid(v) else [v]

def _tutor_classes(db,user):
 from app.routers.academic.classes import scope
 return list(db.classes.find(scope(db,user), {"_id":1,"teacher_id":1,"group_id":1,"subject_id":1,"career_id":1}))

def _members_for_class(db,cls):
 cid=cls["_id"]
 memberships=list(db.class_memberships.find({"class_id":{"$in":_refs(cid)}},{"student_id":1}))
 ids=[ObjectId(str(m["student_id"])) for m in memberships if ObjectId.is_valid(str(m.get("student_id","")))]
 return list(db.users.find({"_id":{"$in":ids},"role":"student"},{"full_name":1}))

@router.get("/options")
def options(user=Depends(get_current_user)):
 if user.get("role")!="tutor": raise HTTPException(403,detail={"message":"Solo tutores"})
 db=dbx(); classes=_tutor_classes(db,user)
 class_map={str(c["_id"]):c for c in classes}
 sessions=list(db.class_sessions.find({"class_id":{"$in":list(class_map)}}, {"class_id":1,"teacher_id":1,"subject_id":1,"group_id":1,"status":1,"created_at":1}).sort("created_at",-1).limit(150)) if classes else []
 student_by_class={cid:[{"id":str(u["_id"]),"name":u.get("full_name","Alumno")} for u in _members_for_class(db,c)] for cid,c in class_map.items()}
 teacher_ids=[ObjectId(str(c["teacher_id"])) for c in classes if ObjectId.is_valid(str(c.get("teacher_id","")))]
 teachers={str(t["_id"]):t.get("full_name","Docente") for t in db.users.find({"_id":{"$in":teacher_ids}},{"full_name":1})}
 subjects={str(s["_id"]):s.get("name","Materia") for s in db.subjects.find({"_id":{"$in":[ObjectId(str(c["subject_id"])) for c in classes if ObjectId.is_valid(str(c.get("subject_id","")))]}},{"name":1})}
 return {"success":True,"data":{"sessions":[{"id":str(s["_id"]),"class_id":str(s.get("class_id","")),"label":f"{subjects.get(str(s.get('subject_id')),'Materia')} · {str(s.get('created_at',''))[:10]}", "teacher_name":teachers.get(str(s.get("teacher_id")),"Docente"),"students":student_by_class.get(str(s.get("class_id")),[])} for s in sessions]}}

@router.post("",status_code=201)
def create(payload:dict,user=Depends(get_current_user)):
 if user.get("role") != "tutor": raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Solo el tutor puede solicitar justificantes"})
 db=dbx()
 student_id=str(payload.get("student_id", ""))
 session_id=str(payload.get("session_id", ""))
 if not ObjectId.is_valid(session_id) or not ObjectId.is_valid(student_id):
  raise HTTPException(422,detail={"message":"Selecciona un alumno y una sesión válidos"})
 session=db.class_sessions.find_one({"_id":ObjectId(session_id)})
 if not session: raise HTTPException(404,detail={"message":"Sesión inexistente"})
 cls=next((c for c in _tutor_classes(db,user) if str(c["_id"])==str(session.get("class_id"))),None)
 if not cls: raise HTTPException(403,detail={"message":"No tienes acceso a esta clase"})
 if not any(str(x["_id"])==student_id for x in _members_for_class(db,cls)):
  raise HTTPException(403,detail={"message":"El alumno no está inscrito en esta clase"})
 reason=str(payload.get("reason","")).strip()
 if len(reason)<5 or len(reason)>2000: raise HTTPException(422,detail={"code":"INVALID_REASON","message":"El motivo debe tener entre 5 y 2000 caracteres"})
 now=datetime.now(timezone.utc)
 doc={"student_id":student_id,"student_user_id":user["_id"] if user.get("role")=="student" else None,"tutor_id":str(user["_id"]) if user.get("role")=="tutor" else None,"teacher_id":str(cls.get("teacher_id")),"session_id":session_id,"subject_id":session.get("subject_id",cls.get("subject_id")),"career_id":cls.get("career_id",user.get("career_id")),"reason":reason,"status":"pending","created_at":now,"updated_at":now,"messages":[]}
 res=db.justifications.insert_one(doc); doc["_id"]=res.inserted_id; audit(db,user,"create","justifications",str(res.inserted_id))
 return {"success":True,"data":serialize(doc)}
@router.get("")
def listing(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),user=Depends(get_current_user)):
 db=dbx(); q={}
 if user.get("role")=="student": q["$or"]=[{"student_user_id":user["_id"]},{"student_id":str(user["_id"])}]
 elif user.get("role")=="tutor": q["tutor_id"]=str(user["_id"])
 elif user.get("role")=="teacher":
  uid=str(user["_id"])
  class_ids=[str(c["_id"]) for c in db.classes.find({"teacher_id":{"$in":_refs(uid)}},{"_id":1})]
  session_q={"$or":[{"teacher_id":{"$in":_refs(uid)}},{"class_id":{"$in":class_ids}}]}
  session_ids=[str(s["_id"]) for s in db.class_sessions.find(session_q,{"_id":1})]
  q["$or"]=[{"teacher_id":{"$in":_refs(uid)}},{"session_id":{"$in":session_ids}}]
 elif user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
 elif user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 total=db.justifications.count_documents(q); docs=list(db.justifications.find(q).sort("created_at",-1).skip((page-1)*limit).limit(limit))
 student_ids=[ObjectId(str(d["student_id"])) for d in docs if ObjectId.is_valid(str(d.get("student_id", "")))]
 names={str(u["_id"]):u.get("full_name","Alumno") for u in db.users.find({"_id":{"$in":student_ids}},{"full_name":1})}
 data=[]
 for d in docs:
  item=serialize(d); item["student_name"]=names.get(str(d.get("student_id")),"Alumno"); data.append(item)
 return {"success":True,"data":data,"meta":{"page":page,"limit":limit,"total":total}}
@router.get("/notifications")
def notifications(user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor"}: raise HTTPException(403,detail={"message":"Sin permisos"})
 db=dbx(); uid=str(user["_id"])
 if user.get("role")=="teacher":
  class_ids=[str(c["_id"]) for c in db.classes.find({"teacher_id":{"$in":_refs(uid)}},{"_id":1})]
  session_ids=[str(s["_id"]) for s in db.class_sessions.find({"$or":[{"teacher_id":{"$in":_refs(uid)}},{"class_id":{"$in":class_ids}}]},{"_id":1})]
  q={"status":{"$in":["pending","needs_information"]},"$or":[{"teacher_id":{"$in":_refs(uid)}},{"session_id":{"$in":session_ids}}]}
 else:
  q={"tutor_id":{"$in":_refs(uid)},"status":{"$in":["pending","needs_information"]}}
 return {"success":True,"data":{"pending":db.justifications.count_documents(q)}}

@router.get("/students/{student_id}/justifications")
def student_justifications(student_id:str,user=Depends(get_current_user)):
 if user.get("role")=="student" and student_id!=str(user["_id"]): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
 if user.get("role") not in {"student","tutor","teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return {"success":True,"data":[serialize(x) for x in dbx().justifications.find({"student_id":student_id}).sort("created_at",-1).limit(200)]}
@router.get("/{justification_id}")
def get_one(justification_id:str,user=Depends(get_current_user)):
 if not ObjectId.is_valid(justification_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx(); doc=db.justifications.find_one({"_id":ObjectId(justification_id)})
 if not doc or not can_access(db,user,doc): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Justificante no encontrado"})
 return {"success":True,"data":serialize(doc)}
@router.patch("/{justification_id}")
def edit(justification_id:str,payload:dict,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]
 if doc.get("status")!="pending" or user.get("role") != "tutor": raise HTTPException(409,detail={"code":"NOT_EDITABLE","message":"La solicitud ya no se puede editar"})
 reason=str(payload.get("reason","")).strip()
 if len(reason)<5 or len(reason)>2000: raise HTTPException(422,detail={"code":"INVALID_REASON","message":"Motivo inválido"})
 db=dbx(); db.justifications.update_one({"_id":ObjectId(justification_id),"status":"pending"},{"$set":{"reason":reason,"updated_at":datetime.now(timezone.utc)}})
 audit(db,user,"edit","justifications",justification_id)
 return get_one(justification_id,user)
def resolve(jid,payload,user,status):
 if user.get("role") not in {"teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 doc=get_one(jid,user)["data"]; db=dbx()
 if doc.get("status")!="pending" and doc.get("status")!="needs_information": raise HTTPException(409,detail={"code":"ALREADY_RESOLVED","message":"El justificante ya fue resuelto"})
 comment=str(payload.get("reason",payload.get("comment",""))).strip()
 if status=="rejected" and len(comment)<5: raise HTTPException(422,detail={"code":"REJECTION_REASON_REQUIRED","message":"El rechazo requiere motivo"})
 now=datetime.now(timezone.utc)
 if status=="approved":
  attendance_status=str(payload.get("attendance_status","excused"))
  if attendance_status not in {"present","excused"}:
   raise HTTPException(422,detail={"message":"Estado de asistencia inválido"})
  session_id=str(doc.get("session_id","")); student_id=str(doc.get("student_id",""))
  session=db.class_sessions.find_one({"_id":ObjectId(session_id)}) if ObjectId.is_valid(session_id) else None
  if not session: raise HTTPException(409,detail={"message":"La sesión ya no existe"})
  if user.get("role")=="teacher" and str(session.get("teacher_id"))!=str(user["_id"]):
   raise HTTPException(403,detail={"message":"Esta sesión pertenece a otro docente"})
  student=db.users.find_one({"_id":ObjectId(student_id),"role":"student"}) if ObjectId.is_valid(student_id) else None
  if not student: raise HTTPException(409,detail={"message":"El alumno ya no existe"})
  profile=db.students.find_one({"user_id":{"$in":_refs(student_id)}})
  refs=[student_id]
  if profile: refs.append(str(profile["_id"]))
  existing=db.attendance.find_one({"session_id":session_id,"student_id":{"$in":refs}})
  if existing and existing.get("status") not in {"absent","excused"}:
   raise HTTPException(409,detail={"message":"La asistencia no está marcada como falta; revisa el registro antes de justificar"})
  if existing:
   db.attendance.update_one({"_id":existing["_id"]},{"$set":{"status":attendance_status,"justification_id":jid,"updated_at":now}})
  else:
   db.attendance.insert_one({"session_id":session_id,"student_id":str(profile["_id"]) if profile else student_id,"user_id":student["_id"],"status":attendance_status,"source":"justification","justification_id":jid,"recorded_at":now,"created_at":now})
 audit(db,user,status,"justifications",jid)
 db.justifications.update_one({"_id":ObjectId(jid)},{"$set":{"status":status,"resolution_comment":comment,"resolved_by":str(user["_id"]),"resolved_at":now,"attendance_status":attendance_status if status=="approved" else None,"updated_at":now}})
 return get_one(jid,user)
@router.post("/{justification_id}/approve")
def approve(justification_id:str,payload:dict,user=Depends(get_current_user)): return resolve(justification_id,payload,user,"approved")
@router.post("/{justification_id}/reject")
def reject(justification_id:str,payload:dict,user=Depends(get_current_user)): return resolve(justification_id,payload,user,"rejected")
@router.post("/{justification_id}/cancel")
def cancel(justification_id:str,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]
 if user.get("role") not in {"tutor","super_admin","admin"} or doc.get("status")!="pending": raise HTTPException(409,detail={"code":"NOT_CANCELLABLE","message":"No se puede cancelar"})
 db=dbx(); db.justifications.update_one({"_id":ObjectId(justification_id)},{"$set":{"status":"cancelled","updated_at":datetime.now(timezone.utc)}}); audit(db,user,"cancel","justifications",justification_id)
 return get_one(justification_id,user)
@router.get("/{justification_id}/messages")
def messages(justification_id:str,user=Depends(get_current_user)): return {"success":True,"data":get_one(justification_id,user)["data"].get("messages",[])}
@router.post("/{justification_id}/messages",status_code=201)
def send_message(justification_id:str,payload:dict,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]; msg=str(payload.get("message","")).strip()
 if not 1<=len(msg)<=4000: raise HTTPException(422,detail={"code":"INVALID_MESSAGE","message":"El mensaje debe tener entre 1 y 4000 caracteres"})
 entry={"id":__import__("secrets").token_hex(12),"author_id":str(user["_id"]),"author_role":user.get("role"),"message":msg,"created_at":datetime.now(timezone.utc)}
 db=dbx(); db.justifications.update_one({"_id":ObjectId(justification_id)},{"$push":{"messages":entry},"$set":{"updated_at":datetime.now(timezone.utc)}}); audit(db,user,"message","justifications",justification_id)
 return {"success":True,"data":entry}
