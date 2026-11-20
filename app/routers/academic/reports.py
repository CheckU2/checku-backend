from datetime import datetime,timezone,timedelta
from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException,Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize
from app.routers.academic.attendance import scoped_attendance
from app.routers.academic.justifications import request_scope
from app.routers.academic.sessions import student_session_query, teacher_session_query
router=APIRouter(prefix="/reports",tags=["Reportes"])
def dbx():
 d=get_database()
 if d is None: raise HTTPException(503,detail={"code":"DATABASE_UNAVAILABLE","message":"Base de datos no disponible"})
 return d
def scope(user):
 role=user.get("role")
 if role in {"super_admin","admin"}: return {}
 if role=="career_admin": return {"career_id":user.get("career_id")}
 if role=="teacher": return {"teacher_id":str(user["_id"])}
 if role=="tutor": return {"tutor_id":str(user["_id"])}
 if role=="student": return {"user_id":user["_id"]}
 raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
def teacher_session_refs(db,user):
 sessions=db.class_sessions.find(teacher_session_query(db,user),{"_id":1})
 return [value for session in sessions for value in (str(session["_id"]),session["_id"])]
@router.get("/attendance")
def attendance_report(user=Depends(get_current_user),days:int=Query(90,ge=1,le=366)):
 db=dbx()
 q={"session_id":{"$in":teacher_session_refs(db,user)}} if user.get("role")=="teacher" else scope(user)
 since=datetime.now(timezone.utc)-timedelta(days=days)
 q["recorded_at"]={"$gte":since}
 pipeline=[{"$match":q},{"$group":{"_id":"$status","count":{"$sum":1}}}]
 counts={x["_id"]:x["count"] for x in db.attendance.aggregate(pipeline)}
 total=sum(counts.values()); present=counts.get("present",0)+counts.get("late",0)+counts.get("excused",0)
 return {"success":True,"data":{"period_days":days,"sessions_evaluated":total,"statuses":counts,"attendance_percentage":round(100*present/total,2) if total else None,"minimum_percentage":80,"at_risk":bool(total and present/total<.8)}}
@router.get("/students/{student_id}/attendance")
def student_report(student_id:str,user=Depends(get_current_user)):
 if user.get("role")=="student" and student_id!=str(user["_id"]): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Reporte no encontrado"})
 if user.get("role") not in {"student","teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx()
 docs=list(db.attendance.find(scoped_attendance(db,user,student_id)).limit(5000))
 total=len(docs); present=sum(1 for d in docs if d.get("status") in {"present","late","excused"})
 return {"success":True,"data":{"student_id":student_id,"sessions_evaluated":total,"attendance_percentage":round(100*present/total,2) if total else None,"absences":sum(1 for d in docs if d.get("status")=="absent"),"at_risk":bool(total and present/total<.8)}}
@router.get("/groups/{group_id}/attendance")
def group_report(group_id:str,user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx()
 pipeline=[{"$match":scoped_attendance(db,user,group_id=group_id)},{"$group":{"_id":"$status","count":{"$sum":1}}}]
 return {"success":True,"data":{"group_id":group_id,"statuses":{x["_id"]:x["count"] for x in db.attendance.aggregate(pipeline)}}}
@router.get("/teachers/{teacher_id}/sessions")
def teacher_sessions(teacher_id:str,user=Depends(get_current_user)):
 if user.get("role")=="teacher" and teacher_id!=str(user["_id"]): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Reporte no encontrado"})
 if user.get("role") not in {"teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx()
 query=teacher_session_query(db,user) if user.get("role")=="teacher" else {"teacher_id":teacher_id}
 return {"success":True,"data":[serialize(x) for x in db.class_sessions.find(query).sort("started_at",-1).limit(500)]}
@router.get("/justifications")
def justification_report(user=Depends(get_current_user)):
 db=dbx()
 q=request_scope(db,user)
 return {"success":True,"data":[serialize(x) for x in db.justifications.find(q,{"messages":0}).sort("created_at",-1).limit(500)]}
@router.get("/dashboard")
def dashboard(user=Depends(get_current_user)):
 if user.get("role")=="student":
  uid=str(user["_id"])
  db=dbx()
  if user.get("institution_id"):
   from app.routers.academic.classes import id_refs
   session_query=student_session_query(db,user)
   session_ids=[row["_id"] for row in db.class_sessions.find(session_query,{"_id":1})]
   refs=[ref for session_id in session_ids for ref in id_refs(session_id)]
   return {"success":True,"data":{
    "sessions":len(session_ids),
    "active_sessions":db.class_sessions.count_documents({**session_query,"status":"active"}),
    "attendance_records":db.attendance.count_documents({"session_id":{"$in":refs},"user_id":{"$in":id_refs(uid)}}),
    "pending_justifications":db.justifications.count_documents({"session_id":{"$in":refs},"status":"pending","$or":[{"student_user_id":{"$in":id_refs(uid)}},{"student_id":{"$in":id_refs(uid)}}]})
   }}
  class_ids=[str(x["class_id"]) for x in db.class_memberships.find({"student_id":uid},{"class_id":1})]
  session_query={"class_id":{"$in":class_ids}} if class_ids else {"_id":{"$in":[]}}
  return {"success":True,"data":{
   "sessions":db.class_sessions.count_documents(session_query),
   "active_sessions":db.class_sessions.count_documents({**session_query,"status":"active"}),
   "attendance_records":db.attendance.count_documents({"user_id":user["_id"]}),
   "pending_justifications":db.justifications.count_documents({"status":"pending","$or":[{"student_user_id":user["_id"]},{"student_id":uid}]})
  }}
 db=dbx()
 if user.get("role")=="teacher":
  q=teacher_session_query(db,user)
  session_refs=teacher_session_refs(db,user)
  attendance_query={"session_id":{"$in":session_refs}}
  justification_query={"status":"pending","session_id":{"$in":session_refs}}
  return {"success":True,"data":{
   "sessions":db.class_sessions.count_documents(q),
   "active_sessions":db.class_sessions.count_documents({**q,"status":"active"}),
   "attendance_records":db.attendance.count_documents(attendance_query),
   "pending_justifications":db.justifications.count_documents(justification_query)
  }}
 q=scope(user)
 return {"success":True,"data":{"sessions":db.class_sessions.count_documents(q),"active_sessions":db.class_sessions.count_documents({**q,"status":"active"}),"attendance_records":db.attendance.count_documents(q),"pending_justifications":db.justifications.count_documents({**q,"status":"pending"})}}


@router.get("/academic")
def academic(days:int=Query(90,ge=0,le=366),group_id:str|None=None,class_id:str|None=None,
             student_id:str|None=None,career_id:str|None=None,user=Depends(get_current_user)):
 from app.services.attendance_analysis import academic_report
 return {"success":True,"data":academic_report(dbx(),user,days,group_id,class_id,student_id,career_id)}


@router.get("/academic/export")
def export_academic(days:int=Query(90,ge=0,le=366),group_id:str|None=None,class_id:str|None=None,
                    user=Depends(get_current_user)):
 from fastapi import Response
 from app.services.attendance_analysis import academic_report
 from app.services.attendance_workbook import workbook
 if not group_id and not class_id:
  raise HTTPException(422,detail={"message":"Selecciona un grupo o una materia para exportar"})
 report=academic_report(dbx(),user,days,group_id,class_id,metadata=True)
 if len(report["sessions"])>16377:
  raise HTTPException(422,detail={"message":"Reduce el rango de fechas para exportar a Excel"})
 return Response(workbook(report),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 headers={"Content-Disposition":'attachment; filename="checku-asistencia.xlsx"'})
