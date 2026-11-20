import base64
import binascii
from io import BytesIO
from PIL import Image, ImageOps
from pymongo.errors import DuplicateKeyError
from datetime import datetime,timezone
from secrets import token_hex
from zoneinfo import ZoneInfo
from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException,Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize,audit
from app.routers.academic.attendance import attendance_for_student, as_utc
from app.routers.academic.classes import assigned_tutor_ids, id_refs, scope
router=APIRouter(prefix="/justifications",tags=["Justificantes y mensajes"])
def dbx():
 d=get_database()
 if d is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 return d
OPEN = ("pending", "needs_information")
LOCAL_ZONE = ZoneInfo("America/Mexico_City")


def scoped_classes(db, user):
 return list(db.classes.find(scope(db, user), {"teacher_id":1,"group_id":1,"subject_id":1,"career_id":1,"tutor_ids":1}))


def session_class_refs(classes):
 return [ref for cls in classes for ref in id_refs(cls["_id"])]


def request_scope(db, user):
 role=user.get("role")
 if role in {"super_admin","admin"}:
  return {}
 if role=="career_admin":
  return {"career_id":{"$in":id_refs(user.get("career_id"))}}
 if role=="student":
  return {"$or":[{"student_user_id":{"$in":id_refs(user["_id"])}},{"student_id":{"$in":id_refs(user["_id"])}}]}
 if role in {"teacher","tutor"}:
  sessions=db.class_sessions.find({"class_id":{"$in":session_class_refs(scoped_classes(db,user))}}, {"_id":1})
  query={"session_id":{"$in":[ref for session in sessions for ref in id_refs(session["_id"])]}}
  if role=="tutor":
   query["$or"]=[{"tutor_id":{"$in":id_refs(user["_id"])}},{"tutor_ids":{"$in":id_refs(user["_id"])}}]
  return query
 raise HTTPException(403,detail={"message":"Sin permisos"})


def can_access(db, user, doc):
 return db.justifications.find_one({"$and":[{"_id":doc["_id"]},request_scope(db,user)]},{"_id":1}) is not None


def can_resolve(user, doc):
 role=user.get("role")
 if doc.get("submitted_by_role")=="teacher":
  return role=="tutor" or role in {"super_admin","admin","career_admin"}
 return role in {"teacher","career_admin","super_admin","admin"}


def review_filter(user):
 role=user.get("role")
 query={"status":{"$in":OPEN}}
 if role=="teacher":
  query["submitted_by_role"]={"$ne":"teacher"}
 elif role=="tutor":
  query["submitted_by_role"]="teacher"
 return query


def session_label(session, cls, subjects):
 subject=subjects.get(str(session.get("subject_id") or cls.get("subject_id")), "Materia")
 date=session.get("started_at") or session.get("created_at")
 return f"{subject} · {as_utc(date).astimezone(LOCAL_ZONE):%d/%m/%Y %H:%M}" if isinstance(date,datetime) else subject


def image_data(value):
 if value is None:
  return None
 if not isinstance(value,str) or len(value)>1_400_000:
  raise HTTPException(422,detail={"message":"La imagen debe pesar hasta 1 MB"})
 try:
  with Image.open(BytesIO(base64.b64decode(value,validate=True))) as image:
   if image.format not in {"JPEG","PNG","WEBP"} or image.width*image.height>16_000_000:
    raise ValueError()
   image=ImageOps.exif_transpose(image).convert("RGB")
   image.thumbnail((1280,1280))
   output=BytesIO()
   image.save(output,format="JPEG",quality=82)
 except (ValueError, OSError, binascii.Error, Image.DecompressionBombError) as exc:
  raise HTTPException(422,detail={"message":"Selecciona una imagen JPG, PNG o WebP válida"}) from exc
 return "data:image/jpeg;base64,"+base64.b64encode(output.getvalue()).decode("ascii")


def message_entry(user, message, image):
 return {"id":token_hex(12),"author_id":str(user["_id"]),"author_role":user.get("role"),
         "message":message,"image":image,"created_at":datetime.now(timezone.utc)}


def _members_for_class(db,cls):
 cid=cls["_id"]
 memberships=list(db.class_memberships.find({"class_id":{"$in":id_refs(cid)}},{"student_id":1}))
 ids=[ObjectId(str(m["student_id"])) for m in memberships if ObjectId.is_valid(str(m.get("student_id","")))]
 return list(db.users.find({"_id":{"$in":ids},"is_deleted":{"$ne":True}},{"full_name":1}))

@router.get("/options")
def options(user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor"}: raise HTTPException(403,detail={"message":"Sin permisos"})
 db=dbx(); classes=scoped_classes(db,user)
 class_map={str(c["_id"]):c for c in classes}
 sessions=list(db.class_sessions.find({"class_id":{"$in":session_class_refs(classes)},"status":"closed"}, {"class_id":1,"teacher_id":1,"subject_id":1,"group_id":1,"status":1,"created_at":1,"started_at":1}).sort("created_at",-1).limit(150)) if classes else []
 tutors_by_class={cid:assigned_tutor_ids(db,c,user.get("institution_id")) for cid,c in class_map.items()} if user.get("role")=="teacher" else {}
 student_by_class={cid:[{"id":str(u["_id"]),"name":u.get("full_name","Alumno")} for u in _members_for_class(db,c)] for cid,c in class_map.items()}
 teacher_ids=[ObjectId(str(c["teacher_id"])) for c in classes if ObjectId.is_valid(str(c.get("teacher_id","")))]
 teachers={str(t["_id"]):t.get("full_name","Docente") for t in db.users.find({"_id":{"$in":teacher_ids}},{"full_name":1})}
 subjects={str(s["_id"]):s.get("name","Materia") for s in db.subjects.find({"_id":{"$in":[ObjectId(str(c["subject_id"])) for c in classes if ObjectId.is_valid(str(c.get("subject_id","")))]}},{"name":1})}
 return {"success":True,"data":{"sessions":[{"id":str(s["_id"]),"class_id":str(s.get("class_id","")),"label":session_label(s,class_map[str(s["class_id"])],subjects), "teacher_name":teachers.get(str(s.get("teacher_id")),"Docente"),"has_tutor":bool(tutors_by_class.get(str(s.get("class_id")))),"students":student_by_class.get(str(s.get("class_id")),[])} for s in sessions]}}

@router.post("",status_code=201)
def create(payload:dict,user=Depends(get_current_user)):
 role=user.get("role")
 if role not in {"teacher","tutor"}: raise HTTPException(403,detail={"message":"Sin permisos"})
 db=dbx()
 student_id=str(payload.get("student_id", ""))
 session_id=str(payload.get("session_id", ""))
 if not ObjectId.is_valid(session_id) or not ObjectId.is_valid(student_id):
  raise HTTPException(422,detail={"message":"Selecciona un alumno y una sesión válidos"})
 session=db.class_sessions.find_one({"_id":ObjectId(session_id)})
 if not session: raise HTTPException(404,detail={"message":"Sesión inexistente"})
 cls=next((c for c in scoped_classes(db,user) if str(c["_id"])==str(session.get("class_id"))),None)
 if not cls: raise HTTPException(403,detail={"message":"No tienes acceso a esta clase"})
 if session.get("status")!="closed":
  raise HTTPException(409,detail={"message":"La sesión debe estar cerrada"})
 tutor_ids=assigned_tutor_ids(db,cls,user.get("institution_id")) if role=="teacher" else [user["_id"]]
 if not tutor_ids:
  raise HTTPException(409,detail={"message":"El grupo necesita un tutor asignado"})
 member=db.class_memberships.find_one({"class_id":{"$in":id_refs(cls["_id"])},"student_id":{"$in":id_refs(student_id)}})
 if not member or not db.users.find_one({"_id":ObjectId(student_id),"is_deleted":{"$ne":True}}) or (member.get("joined_at") and session.get("ended_at")
                   and as_utc(member["joined_at"])>as_utc(session["ended_at"])):
  raise HTTPException(403,detail={"message":"El alumno no estaba inscrito al cerrar esta sesión"})
 attendance=attendance_for_student(db,session_id,student_id)
 if attendance and attendance.get("status")!="absent":
  raise HTTPException(409,detail={"message":"El alumno no tiene una falta pendiente en esta sesión"})
 reason=str(payload.get("reason","")).strip()
 if len(reason)<5 or len(reason)>2000: raise HTTPException(422,detail={"code":"INVALID_REASON","message":"El motivo debe tener entre 5 y 2000 caracteres"})
 now=datetime.now(timezone.utc)
 image=image_data(payload.get("image"))
 doc={"student_id":student_id,"student_user_id":ObjectId(student_id),
      "tutor_id":str(user["_id"]) if role=="tutor" else None,"tutor_ids":list(map(str,tutor_ids)),
      "teacher_id":str(cls.get("teacher_id")),"session_id":session_id,
      "subject_id":str(session.get("subject_id") or cls.get("subject_id") or ""),
      "career_id":str(cls.get("career_id") or ""),"reason":reason,"status":"pending",
      "submitted_by_role":role,"created_at":now,"updated_at":now,
      "messages":[message_entry(user,"",image)] if image else []}
 res=db.justifications.insert_one(doc); doc["_id"]=res.inserted_id; audit(db,user,"create","justifications",str(res.inserted_id))
 return {"success":True,"data":serialize(doc)}
@router.get("")
def listing(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),review_pending:bool=False,user=Depends(get_current_user)):
 db=dbx(); q=request_scope(db,user)
 pending_query={**q,**review_filter(user)}
 if review_pending:
  q=pending_query
 total=db.justifications.count_documents(q)
 docs=list(db.justifications.find(q,{"messages":0}).sort("created_at",-1).skip((page-1)*limit).limit(limit))
 student_ids=[ObjectId(str(d["student_id"])) for d in docs if ObjectId.is_valid(str(d.get("student_id", "")))]
 names={str(u["_id"]):u.get("full_name","Alumno") for u in db.users.find({"_id":{"$in":student_ids}},{"full_name":1})}
 data=[]
 for d in docs:
  item=serialize(d); item["student_name"]=names.get(str(d.get("student_id")),"Alumno"); item["can_resolve"]=can_resolve(user,d); data.append(item)
 return {"success":True,"data":data,"meta":{"page":page,"limit":limit,"total":total,"review_pending":db.justifications.count_documents(pending_query)}}
@router.get("/notifications")
def notifications(user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor"}: raise HTTPException(403,detail={"message":"Sin permisos"})
 db=dbx(); q={**request_scope(db,user),**review_filter(user)}
 return {"success":True,"data":{"pending":db.justifications.count_documents(q)}}

@router.get("/students/{student_id}/justifications")
def student_justifications(student_id:str,user=Depends(get_current_user)):
 db=dbx()
 q={"$and":[{"student_id":{"$in":id_refs(student_id)}},request_scope(db,user)]}
 return {"success":True,"data":[serialize(x) for x in db.justifications.find(q,{"messages":0}).sort("created_at",-1).limit(200)]}

@router.get("/{justification_id}")
def get_one(justification_id:str,user=Depends(get_current_user)):
 if not ObjectId.is_valid(justification_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx(); doc=db.justifications.find_one({"_id":ObjectId(justification_id)})
 if not doc or not can_access(db,user,doc): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Justificante no encontrado"})
 return {"success":True,"data":dict(serialize(doc),can_resolve=can_resolve(user,doc))}
@router.patch("/{justification_id}")
def edit(justification_id:str,payload:dict,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]
 if doc.get("status")!="pending" or user.get("role") != "tutor" or doc.get("submitted_by_role")=="teacher": raise HTTPException(409,detail={"code":"NOT_EDITABLE","message":"La solicitud ya no se puede editar"})
 reason=str(payload.get("reason","")).strip()
 if len(reason)<5 or len(reason)>2000: raise HTTPException(422,detail={"code":"INVALID_REASON","message":"Motivo inválido"})
 db=dbx(); db.justifications.update_one({"_id":ObjectId(justification_id),"status":"pending"},{"$set":{"reason":reason,"updated_at":datetime.now(timezone.utc)}})
 audit(db,user,"edit","justifications",justification_id)
 return get_one(justification_id,user)
def resolve(jid,payload,user,status):
 doc=get_one(jid,user)["data"]; db=dbx()
 if not can_resolve(user,doc):
  raise HTTPException(403,detail={"message":"Solo el destinatario puede resolver el justificante"})
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
  if session.get("status")!="closed":
   raise HTTPException(409,detail={"message":"La sesión debe estar cerrada"})
  if user.get("role")=="tutor" and attendance_status!="excused":
   raise HTTPException(422,detail={"message":"El tutor puede marcar la falta como justificada"})
  student=db.users.find_one({"_id":ObjectId(student_id)}) if ObjectId.is_valid(student_id) else None
  member=db.class_memberships.find_one({"class_id":{"$in":id_refs(session.get("class_id"))},"student_id":{"$in":id_refs(student_id)}})
  if not student or not member:
   raise HTTPException(409,detail={"message":"El alumno ya no pertenece a la clase"})
  existing=attendance_for_student(db,session_id,student_id)
  if existing and existing.get("status") not in {"absent","excused"} and existing.get("justification_id")!=jid:
   raise HTTPException(409,detail={"message":"La asistencia no está marcada como falta"})
 fields={"status":status,"resolution_comment":comment,"resolved_by":str(user["_id"]),"resolved_at":now,
         "attendance_status":attendance_status if status=="approved" else None,"updated_at":now}
 claimed=db.justifications.update_one({"_id":ObjectId(jid),"status":{"$in":OPEN}},{"$set":fields})
 if not claimed.modified_count:
  raise HTTPException(409,detail={"message":"El justificante ya fue resuelto"})
 try:
  if status=="approved":
   values={"status":attendance_status,"justification_id":jid,"updated_at":now}
   if existing:
    changed=db.attendance.update_one({"_id":existing["_id"],"status":existing.get("status")},{"$set":values})
    if not changed.matched_count:
     raise HTTPException(409,detail={"message":"La asistencia cambió. Actualiza e inténtalo de nuevo"})
   else:
    db.attendance.insert_one(dict(values,session_id=session_id,student_id=student_id,user_id=student["_id"],
                                  source="justification",recorded_at=now,created_at=now))
 except (DuplicateKeyError, HTTPException):
  db.justifications.update_one({"_id":ObjectId(jid),"resolved_at":now},{"$set":{"status":doc["status"],"updated_at":now},
                               "$unset":{key:"" for key in ("resolution_comment","resolved_by","resolved_at","attendance_status")}})
  raise HTTPException(409,detail={"message":"La asistencia cambió. Actualiza e inténtalo de nuevo"})
 audit(db,user,status,"justifications",jid)
 return get_one(jid,user)
@router.post("/{justification_id}/approve")
def approve(justification_id:str,payload:dict,user=Depends(get_current_user)): return resolve(justification_id,payload,user,"approved")
@router.post("/{justification_id}/reject")
def reject(justification_id:str,payload:dict,user=Depends(get_current_user)): return resolve(justification_id,payload,user,"rejected")
@router.post("/{justification_id}/cancel")
def cancel(justification_id:str,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]
 if user.get("role") not in {"tutor","super_admin","admin"} or doc.get("status")!="pending" or (user.get("role")=="tutor" and doc.get("submitted_by_role")=="teacher"): raise HTTPException(409,detail={"code":"NOT_CANCELLABLE","message":"No se puede cancelar"})
 db=dbx(); db.justifications.update_one({"_id":ObjectId(justification_id)},{"$set":{"status":"cancelled","updated_at":datetime.now(timezone.utc)}}); audit(db,user,"cancel","justifications",justification_id)
 return get_one(justification_id,user)
@router.get("/{justification_id}/messages")
def messages(justification_id:str,user=Depends(get_current_user)): return {"success":True,"data":get_one(justification_id,user)["data"].get("messages",[])}
@router.post("/{justification_id}/messages",status_code=201)
def send_message(justification_id:str,payload:dict,user=Depends(get_current_user)):
 get_one(justification_id,user)
 if user.get("role") not in {"teacher","tutor","career_admin","super_admin","admin"}:
  raise HTTPException(403,detail={"message":"Sin permisos"})
 msg=str(payload.get("message","")).strip()
 image=image_data(payload.get("image"))
 if len(msg)>4000 or not (msg or image):
  raise HTTPException(422,detail={"message":"Escribe un mensaje o adjunta una imagen"})
 entry=message_entry(user,msg,image)
 query={"_id":ObjectId(justification_id),"messages.199":{"$exists":False}}
 if image:
  query["$expr"]={"$lt":[{"$size":{"$filter":{"input":{"$ifNull":["$messages",[]]},"as":"item",
                   "cond":{"$ne":[{"$ifNull":["$$item.image",None]},None]}}}},6]}
 db=dbx()
 result=db.justifications.update_one(query,{"$push":{"messages":entry},"$set":{"updated_at":entry["created_at"]}})
 if not result.matched_count:
  raise HTTPException(409,detail={"message":"La conversación alcanzó su límite de mensajes o imágenes"})
 audit(db,user,"message","justifications",justification_id)
 return {"success":True,"data":entry}
