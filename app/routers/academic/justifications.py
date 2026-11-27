import base64
import binascii
from io import BytesIO
from PIL import Image, ImageOps
from pymongo.errors import DuplicateKeyError
from datetime import datetime,timezone,timedelta
from re import escape
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
 return list(db.classes.find(scope(db, user), {"teacher_id":1,"group_id":1,"subject_id":1,"career_id":1,"tutor_ids":1,"institution_id":1}))


def session_class_refs(classes):
 return [ref for cls in classes for ref in id_refs(cls["_id"])]


def request_scope(db, user):
 role=user.get("role")
 if role in {"super_admin","admin"}:
  return {}
 if role=="career_admin":
  return {"career_id":{"$in":id_refs(user.get("career_id"))}}
 if role=="student":
  query={"$or":[{"student_user_id":{"$in":id_refs(user["_id"])}},{"student_user_id":None,"student_id":{"$in":id_refs(user["_id"])}}]}
  if user.get("institution_id"):
   sessions=db.class_sessions.find({"class_id":{"$in":session_class_refs(scoped_classes(db,user))}},{"_id":1})
   query["session_id"]={"$in":[ref for session in sessions for ref in id_refs(session["_id"])]}
  return query
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
 return user.get("role") in {"tutor","career_admin","super_admin","admin"}


def review_filter(user):
 query={"status":{"$in":OPEN}}
 if user.get("role") in {"teacher","student"}:
  query["_id"]={"$in":[]}
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
 return list(db.users.find({"_id":{"$in":ids},"is_deleted":{"$ne":True}},{"full_name":1,"matricula":1}))

@router.get("/options")
def options(page:int=Query(1,ge=1),limit:int=Query(25,ge=1,le=100),
            search:str=Query("",max_length=120),date:str=Query("",max_length=10),user=Depends(get_current_user)):
 role=user.get("role")
 if role not in {"student","tutor"}: raise HTTPException(403,detail={"message":"Sin permisos"})
 db=dbx()
 classes=scoped_classes(db,user)
 def names(field, collection, label):
  ids={ObjectId(str(c[field])) for c in classes if ObjectId.is_valid(str(c.get(field,"")))}
  return {str(row["_id"]):row.get(label,"") for row in db[collection].find({"_id":{"$in":list(ids)}},{label:1})}
 subjects=names("subject_id","subjects","name")
 groups=names("group_id","groups","name")
 teachers=names("teacher_id","users","full_name")
 def selected_day():
  if not date: return None
  try: return datetime.strptime(date,"%Y-%m-%d").replace(tzinfo=LOCAL_ZONE)
  except ValueError: raise HTTPException(422,detail={"message":"Fecha de sesión inválida"})
 term=search.strip().casefold()
 class_map={str(c["_id"]):c for c in classes if not term or term in " ".join((
  subjects.get(str(c.get("subject_id")),""),groups.get(str(c.get("group_id")),""),
  teachers.get(str(c.get("teacher_id")),""))).casefold()}
 q={"class_id":{"$in":session_class_refs(class_map.values())},"status":"closed"}
 day=selected_day()
 if day:
  interval={"$gte":day.astimezone(timezone.utc),"$lt":(day+timedelta(days=1)).astimezone(timezone.utc)}
  q["$or"]=[{"started_at":interval},{"started_at":None,"created_at":interval}]
 if role=="student":
  joined={str(m["class_id"]):m.get("joined_at") for m in db.class_memberships.find(
    {"student_id":{"$in":id_refs(user["_id"])},"class_id":q["class_id"]},{"class_id":1,"joined_at":1})}
  q={"$and":[q,{"$or":[{"class_id":{"$in":id_refs(cid)},**({"ended_at":{"$gte":since}} if since else {})}
                        for cid,since in joined.items()]}]} if joined else {"_id":{"$in":[]}}
 total=db.class_sessions.count_documents(q)
 sessions=list(db.class_sessions.find(q,{"class_id":1,"subject_id":1,"created_at":1,"started_at":1})
               .sort([("created_at",-1),("_id",-1)]).skip((page-1)*limit).limit(limit))
 students={}
 if role=="tutor":
  for cid in {str(s["class_id"]) for s in sessions}:
   students[cid]=[{"id":str(u["_id"]),"name":u.get("full_name","Alumno"),"matricula":u.get("matricula")}
                  for u in _members_for_class(db,class_map[cid])]
 result=[]
 for session in sessions:
  cid=str(session["class_id"]); cls=class_map[cid]
  result.append({"id":str(session["_id"]),"class_id":cid,"label":session_label(session,cls,subjects),
                 "group_name":groups.get(str(cls.get("group_id"))),"teacher_name":teachers.get(str(cls.get("teacher_id"))),
                 "students":students.get(cid,[])})
 return {"success":True,"data":{"sessions":result},"meta":{"total":total,"page":page,"limit":limit}}

@router.post("",status_code=201)
def create(payload:dict,user=Depends(get_current_user)):
 role=user.get("role")
 if role not in {"student","tutor"}: raise HTTPException(403,detail={"message":"Sin permisos"})
 db=dbx()
 student_id=str(user["_id"] if role=="student" else payload.get("student_id", ""))
 session_id=str(payload.get("session_id", ""))
 if not ObjectId.is_valid(session_id) or not ObjectId.is_valid(student_id):
  raise HTTPException(422,detail={"message":"Selecciona un alumno y una sesión válidos"})
 session=db.class_sessions.find_one({"_id":ObjectId(session_id)})
 if not session: raise HTTPException(404,detail={"message":"Sesión inexistente"})
 cls=next((c for c in scoped_classes(db,user) if str(c["_id"])==str(session.get("class_id"))),None)
 if not cls: raise HTTPException(403,detail={"message":"No tienes acceso a esta clase"})
 if session.get("status")!="closed":
  raise HTTPException(409,detail={"message":"La sesión debe estar cerrada"})
 tutor_ids=assigned_tutor_ids(db,cls,user.get("institution_id") or cls.get("institution_id"),group_only=True)[:1] if role=="student" else [user["_id"]]
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
      "submitted_by_role":role,"submitted_by":str(user["_id"]),"created_at":now,"updated_at":now,
      "messages":[message_entry(user,"",image)] if image else []}
 res=db.justifications.insert_one(doc); doc["_id"]=res.inserted_id; audit(db,user,"create","justifications",str(res.inserted_id))
 return {"success":True,"data":serialize(doc)}
@router.get("")
def listing(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),review_pending:bool=False,matricula:str=Query("",max_length=40),
            status:str=Query("",max_length=24), origin:str=Query("",max_length=12), search:str=Query("",max_length=120),user=Depends(get_current_user)):
 db=dbx(); q=request_scope(db,user)
 pending_query={**q,**review_filter(user)}
 if review_pending:
  q=pending_query
 filters=[q]
 if user.get("role")=="student":
  if origin=="staff": filters.append({"submitted_by_role":{"$ne":"student"}})
  elif origin=="mine": filters.append({"submitted_by_role":"student"})
 if status: filters.append({"status":status})
 if matricula.strip():
  pattern={"$regex":"^"+escape(matricula.strip())+"$","$options":"i"}
  people=db.users.find({"matricula":pattern},{"_id":1})
  ids=[ref for person in people for ref in id_refs(person["_id"])]
  filters.append({"$or":[{"student_id":{"$in":ids}},{"student_user_id":{"$in":ids}}]})
 if search.strip():
  pattern={"$regex":escape(search.strip()),"$options":"i"}
  people=db.users.find({"full_name":pattern},{"_id":1})
  ids=[ref for person in people for ref in id_refs(person["_id"])]
  filters.append({"$or":[{"reason":pattern},{"student_id":{"$in":ids}},{"student_user_id":{"$in":ids}}]})
 if len(filters)>1: q={"$and":filters}
 total=db.justifications.count_documents(q)
 docs=list(db.justifications.find(q,{"messages":0}).sort("created_at",-1).skip((page-1)*limit).limit(limit))
 student_ids=[ObjectId(str(d["student_id"])) for d in docs if ObjectId.is_valid(str(d.get("student_id", "")))]
 people={str(u["_id"]):u for u in db.users.find({"_id":{"$in":student_ids}},{"full_name":1,"matricula":1})}
 data=[]
 for d in docs:
  person=people.get(str(d.get("student_id")),{})
  item=student_summary(d) if user.get("role")=="student" else serialize(d); item["student_name"]=person.get("full_name","Alumno"); item["student_matricula"]=person.get("matricula"); item["can_resolve"]=can_resolve(user,d); data.append(item)
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
 return {"success":True,"data":[student_summary(x) if user.get("role")=="student" else serialize(x) for x in db.justifications.find(q,{"messages":0}).sort("created_at",-1).limit(200)]}

def student_summary(doc):
 keys=("reason","status","created_at","resolved_at","submitted_by_role")
 return serialize({"_id":doc["_id"], **{key:doc[key] for key in keys if key in doc}})


def student_detail(db, user, doc):
 role=doc.get("submitted_by_role")
 own_request=role=="student"
 author=doc.get("submitted_by")
 if not author:
  if own_request: author=user["_id"]
  elif role=="tutor": author=doc.get("tutor_id")
  elif role=="teacher": author=doc.get("teacher_id")
 ids=[ObjectId(str(value)) for value in (author,doc.get("teacher_id"),doc.get("resolved_by"))
      if ObjectId.is_valid(str(value))]
 names={str(person["_id"]):person.get("full_name") for person in db.users.find({"_id":{"$in":ids}},{"full_name":1})}
 session_id=doc.get("session_id")
 session=db.class_sessions.find_one({"_id":ObjectId(str(session_id))},{"started_at":1,"created_at":1,"subject_id":1}) if ObjectId.is_valid(str(session_id)) else None
 subject_id=(session or {}).get("subject_id") or doc.get("subject_id")
 subject=db.subjects.find_one({"_id":ObjectId(str(subject_id))},{"name":1}) if ObjectId.is_valid(str(subject_id)) else None
 visible_messages=[{key: entry[key] for key in ("id","author_role","message","image","created_at") if key in entry}
                   for entry in doc.get("messages",[]) if own_request and (
                       (entry.get("author_role")=="student" and str(entry.get("author_id"))==str(user["_id"]))
                       or (entry.get("author_role")=="tutor" and entry.get("student_visible") is True))]
 session_date=(session or {}).get("started_at") or (session or {}).get("created_at")
 detail=student_summary(doc)
 detail.update({
  "submitted_by_name":names.get(str(author)),
  "teacher_name":names.get(str(doc.get("teacher_id"))),
  "resolved_by_name":names.get(str(doc.get("resolved_by"))),
  "subject_name":(subject or {}).get("name"),
  "session_at":as_utc(session_date) if isinstance(session_date,datetime) else None,
  "messages":visible_messages,
 })
 return detail


@router.get("/{justification_id}")
def get_one(justification_id:str,user=Depends(get_current_user)):
 if not ObjectId.is_valid(justification_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=dbx(); doc=db.justifications.find_one({"_id":ObjectId(justification_id)})
 if not doc or not can_access(db,user,doc): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Justificante no encontrado"})
 if user.get("role")=="student": return {"success":True,"data":student_detail(db,user,doc)}
 return {"success":True,"data":dict(serialize(doc),can_resolve=can_resolve(user,doc))}
@router.patch("/{justification_id}")
def edit(justification_id:str,payload:dict,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]
 if doc.get("status")!="pending" or user.get("role") != "tutor" or doc.get("submitted_by_role")!="tutor": raise HTTPException(409,detail={"code":"NOT_EDITABLE","message":"La solicitud ya no se puede editar"})
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
 if user.get("role") not in {"tutor","super_admin","admin"} or doc.get("status")!="pending" or (user.get("role")=="tutor" and doc.get("submitted_by_role")!="tutor"): raise HTTPException(409,detail={"code":"NOT_CANCELLABLE","message":"No se puede cancelar"})
 db=dbx(); db.justifications.update_one({"_id":ObjectId(justification_id)},{"$set":{"status":"cancelled","updated_at":datetime.now(timezone.utc)}}); audit(db,user,"cancel","justifications",justification_id)
 return get_one(justification_id,user)
@router.get("/{justification_id}/messages")
def messages(justification_id:str,user=Depends(get_current_user)): return {"success":True,"data":get_one(justification_id,user)["data"].get("messages",[])}
@router.post("/{justification_id}/messages",status_code=201)
def send_message(justification_id:str,payload:dict,user=Depends(get_current_user)):
 doc=get_one(justification_id,user)["data"]
 if user.get("role")=="student" and doc.get("submitted_by_role")!="student":
  raise HTTPException(403,detail={"message":"Solo puedes escribir en tus propias solicitudes"})
 if user.get("role") not in {"student","tutor","career_admin","super_admin","admin"}:
  raise HTTPException(403,detail={"message":"Sin permisos"})
 msg=str(payload.get("message","")).strip()
 image=image_data(payload.get("image"))
 if len(msg)>4000 or not (msg or image):
  raise HTTPException(422,detail={"message":"Escribe un mensaje o adjunta una imagen"})
 entry=message_entry(user,msg,image)
 if doc.get("submitted_by_role")=="student" and user.get("role")=="tutor":
  entry["student_visible"]=payload.get("student_visible") is True
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
