from datetime import datetime,timezone,timedelta
from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException,Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize
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
@router.get("/attendance")
def attendance_report(user=Depends(get_current_user),days:int=Query(90,ge=1,le=366)):
 db=dbx(); q=scope(user)
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
 docs=list(dbx().attendance.find({"student_id":student_id}).limit(5000))
 total=len(docs); present=sum(1 for d in docs if d.get("status") in {"present","late","excused"})
 return {"success":True,"data":{"student_id":student_id,"sessions_evaluated":total,"attendance_percentage":round(100*present/total,2) if total else None,"absences":sum(1 for d in docs if d.get("status")=="absent"),"at_risk":bool(total and present/total<.8)}}
@router.get("/groups/{group_id}/attendance")
def group_report(group_id:str,user=Depends(get_current_user)):
 if user.get("role") not in {"teacher","tutor","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 db=dbx(); ss=[str(s["_id"]) for s in db.class_sessions.find({"group_id":group_id},{"_id":1}).limit(500)]
 pipeline=[{"$match":{"session_id":{"$in":ss}}},{"$group":{"_id":"$status","count":{"$sum":1}}}]
 return {"success":True,"data":{"group_id":group_id,"statuses":{x["_id"]:x["count"] for x in db.attendance.aggregate(pipeline)}}}
@router.get("/teachers/{teacher_id}/sessions")
def teacher_sessions(teacher_id:str,user=Depends(get_current_user)):
 if user.get("role")=="teacher" and teacher_id!=str(user["_id"]): raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Reporte no encontrado"})
 if user.get("role") not in {"teacher","career_admin","super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return {"success":True,"data":[serialize(x) for x in dbx().class_sessions.find({"teacher_id":teacher_id}).sort("started_at",-1).limit(500)]}
@router.get("/justifications")
def justification_report(user=Depends(get_current_user)):
 q={}
 if user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
 elif user.get("role")=="teacher": q["teacher_id"]=str(user["_id"])
 elif user.get("role")=="tutor": q["tutor_id"]=str(user["_id"])
 elif user.get("role") not in {"super_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
 return {"success":True,"data":[serialize(x) for x in dbx().justifications.find(q).sort("created_at",-1).limit(500)]}
@router.get("/dashboard")
def dashboard(user=Depends(get_current_user)):
 db=dbx(); q=scope(user)
 return {"success":True,"data":{"sessions":db.class_sessions.count_documents(q),"active_sessions":db.class_sessions.count_documents({**q,"status":"active"}),"attendance_records":db.attendance.count_documents(q),"pending_justifications":db.justifications.count_documents({**q,"status":"pending"})}}
