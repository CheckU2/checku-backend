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
 if role=="teacher": return doc.get("teacher_id")==str(user["_id"])
 return False
@router.post("",status_code=201)
def create(payload:dict,user=Depends(get_current_user)):
 if user.get("role") != "tutor": raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Solo el tutor puede solicitar justificantes"})
 student_id=str(payload.get("student_id",user["_id"]))
 if user.get("role")=="student" and student_id!=str(user["_id"]): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"No puedes solicitar por otro alumno"})
 if user.get("role")=="tutor":
  db=dbx()
  if not db.tutor_assignments.find_one({"tutor_id":str(user["_id"]),"student_id":student_id,"is_active":{"$ne":False}}): raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Alumno no asignado a este tutor"})
 else: db=dbx()
 reason=str(payload.get("reason","")).strip()
 if len(reason)<5 or len(reason)>2000: raise HTTPException(422,detail={"code":"INVALID_REASON","message":"El motivo debe tener entre 5 y 2000 caracteres"})
 now=datetime.now(timezone.utc)
 session_id=str(payload.get("session_id",""))
 session=db.class_sessions.find_one({"_id":ObjectId(session_id)}) if ObjectId.is_valid(session_id) else None
 if not session: raise HTTPException(422,detail={"code":"INVALID_SESSION","message":"Debes indicar una sesión válida"})
 if user.get("role")=="student" and session.get("group_id") is not None:
  profile=db.students.find_one({"user_id":str(user["_id"])}) or db.students.find_one({"user_id":user["_id"]})
  student_refs=[str(user["_id"])]
  if profile: student_refs.append(str(profile["_id"]))
  if not db.enrollments.find_one({"student_id":{"$in":student_refs},"group_id":session.get("group_id"),"status":{"$nin":["inactive","cancelled"]}}):
   raise HTTPException(403,detail={"code":"NOT_ENROLLED","message":"No estás inscrito en esa clase"})
 doc={"student_id":student_id,"student_user_id":user["_id"] if user.get("role")=="student" else None,"tutor_id":str(user["_id"]) if user.get("role")=="tutor" else None,"teacher_id":session.get("teacher_id"),"session_id":session_id,"subject_id":session.get("subject_id",payload.get("subject_id")),"career_id":session.get("career_id",user.get("career_id")),"reason":reason,"status":"pending","created_at":now,"updated_at":now,"messages":[]}
 res=db.justifications.insert_one(doc); doc["_id"]=res.inserted_id; audit(db,user,"create","justifications",str(res.inserted_id))
 return {"success":True,"data":serialize(doc)}
@router.get("")
def listing(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),user=Depends(get_current_user)):
 db=dbx(); q={}
 if user.get("role")=="student": q["$or"]=[{"student_user_id":user["_id"]},{"student_id":str(user["_id"])}]
 elif user.get("role")=="tutor": q["tutor_id"]=str(user["_id"])
 elif user.get("role")=="teacher": q["teacher_id"]=str(user["_id"])
 elif user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
 elif user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 total=db.justifications.count_documents(q); docs=db.justifications.find(q).sort("created_at",-1).skip((page-1)*limit).limit(limit)
 return {"success":True,"data":[serialize(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
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
 now=datetime.now(timezone.utc); db.justifications.update_one({"_id":ObjectId(jid)},{"$set":{"status":status,"resolution_comment":comment,"resolved_by":str(user["_id"]),"resolved_at":now,"updated_at":now}})
 audit(db,user,status,"justifications",jid)
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
