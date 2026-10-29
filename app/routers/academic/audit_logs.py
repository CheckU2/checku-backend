from bson import ObjectId
from fastapi import APIRouter,Depends,HTTPException,Query
from app.database import get_database
from app.dependencies import get_current_user
from app.routers.academic.generic import serialize
router=APIRouter(prefix="/audit-logs",tags=["Auditoría"])
def admin(user):
 if user.get("role") not in {"super_admin","career_admin","admin"}: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
@router.get("")
def listing(page:int=Query(1,ge=1),limit:int=Query(50,ge=1,le=100),user=Depends(get_current_user)):
 admin(user); db=get_database(); q={}
 if user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
 total=db.audit_logs.count_documents(q); docs=db.audit_logs.find(q).sort("created_at",-1).skip((page-1)*limit).limit(limit)
 return {"success":True,"data":[serialize(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
@router.get("/{log_id}")
def get_one(log_id:str,user=Depends(get_current_user)):
 admin(user)
 if not ObjectId.is_valid(log_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
 db=get_database(); q={"_id":ObjectId(log_id)}
 if user.get("role")=="career_admin": q["career_id"]=user.get("career_id")
 doc=db.audit_logs.find_one(q)
 if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
 return {"success":True,"data":serialize(doc)}
