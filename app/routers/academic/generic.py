from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import DESCENDING
from app.database import get_database
from app.dependencies import get_current_user

ROLE_ADMIN={"super_admin","career_admin","admin"}
CONFIG={
 "careers":({"name","code","description","is_active"},{"super_admin","career_admin","admin"}),
 "academic_periods":({"name","code","start_date","end_date","career_id","is_active"},{"super_admin","career_admin","admin"}),
 "groups":({"name","code","career_id","period_id","capacity","is_active"},{"super_admin","career_admin","admin"}),
 "subjects":({"name","code","description","career_id","credits","is_active"},{"super_admin","career_admin","admin"}),
 "students":({"student_id","user_id","career_id","group_id","period_id","enrollment_status"},{"super_admin","career_admin","admin"}),
 "teachers":({"user_id","career_id","employee_id","department","is_active"},{"super_admin","career_admin","admin"}),
 "tutors":({"user_id","career_id","employee_id","is_active"},{"super_admin","career_admin","admin"}),
 "enrollments":({"student_id","group_id","period_id","career_id","status","enrolled_at"},{"super_admin","career_admin","admin"}),
 "teaching_assignments":({"teacher_id","subject_id","group_id","period_id","career_id","is_active"},{"super_admin","career_admin","admin"}),
 "tutor_assignments":({"tutor_id","student_id","group_id","career_id","is_active"},{"super_admin","career_admin","admin"}),
 "schedules":({"teacher_id","subject_id","group_id","period_id","career_id","weekday","start_time","end_time","room","is_active"},{"super_admin","career_admin","admin"}),
 "incidents":({"title","description","category","status","career_id","student_id","session_id","priority"},{"super_admin","career_admin","admin","teacher","tutor"}),
}
def make_router(prefix,collection,fields,admin_roles=None):
    r=APIRouter(prefix="/"+prefix,tags=[prefix.replace("-"," ").title()])
    admin_roles=admin_roles or ROLE_ADMIN
    def scope(user):
        query={}
        role=user.get("role")
        if role in {"career_admin", "tutor"} and collection == "groups":
            if not user.get("career_id"): raise HTTPException(403, detail={"message":"Tu cuenta necesita una carrera asignada"})
            query["career_id"] = str(user["career_id"])
        elif role=="career_admin":
            if not user.get("career_id"): raise HTTPException(403,detail={"code":"SCOPE_MISSING","message":"La cuenta no tiene carrera asignada"})
            query["career_id"]=user["career_id"]
        elif role=="student":
            query["user_id"]=user["_id"]
        elif role=="teacher":
            query["teacher_id"]=user["_id"]
        elif role=="tutor":
            query["tutor_id"]=user["_id"]
        return query
    @r.get("")
    def list_items(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),user=Depends(get_current_user)):
        db=get_database(); q=scope(user)
        if user.get("role") not in admin_roles and (collection == "careers" or user.get("role") not in {"student","teacher","tutor"}):
            raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        total=db[collection].count_documents(q)
        docs=db[collection].find(q).sort("created_at",DESCENDING).skip((page-1)*limit).limit(limit)
        return {"success":True,"data":[serialize(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
    @r.post("",status_code=201)
    def create_item(payload:dict,user=Depends(get_current_user)):
        if user.get("role") not in admin_roles: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        data={k:v for k,v in payload.items() if k in fields}
        if not data: raise HTTPException(422,detail={"code":"EMPTY_DATA","message":"No hay campos permitidos"})
        if user.get("role") in {"career_admin", "tutor"} and collection == "groups":
            data["career_id"] = str(user.get("career_id"))
        elif user.get("role")=="career_admin":
            data["career_id"]=user.get("career_id")
        now=datetime.now(timezone.utc); data.update({"created_at":now,"updated_at":now,"is_deleted":False})
        db=get_database()
        # Validate ObjectId-like references before persistence.
        for k,v in list(data.items()):
            if k.endswith("_id") and isinstance(v,str) and k not in {"student_id","employee_id"}:
                if not ObjectId.is_valid(v): raise HTTPException(422,detail={"code":"INVALID_ID","message":f"Identificador inválido: {k}"})
        try: result=db[collection].insert_one(data)
        except Exception as exc:
            from pymongo.errors import DuplicateKeyError
            if isinstance(exc,DuplicateKeyError): raise HTTPException(409,detail={"code":"DUPLICATE_RESOURCE","message":"El registro ya existe"})
            raise
        data["_id"]=result.inserted_id
        if collection == "groups" and user.get("role") == "tutor":
            profile = db.tutors.find_one({"user_id":str(user["_id"])})
            db.tutor_assignments.update_one(
                {"tutor_id":str(profile["_id"]) if profile else str(user["_id"]), "group_id":str(result.inserted_id)},
                {"$setOnInsert":{"career_id":str(user["career_id"]),"is_active":True,"created_at":now}}, upsert=True)
        audit(db,user,"create",collection,str(result.inserted_id))
        return {"success":True,"data":serialize(data)}
    @r.get("/{item_id}")
    def get_item(item_id:str,user=Depends(get_current_user)):
        if not ObjectId.is_valid(item_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
        q={"_id":ObjectId(item_id),**scope(user)}
        doc=get_database()[collection].find_one(q)
        if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
        return {"success":True,"data":serialize(doc)}
    @r.patch("/{item_id}")
    def update_item(item_id:str,payload:dict,user=Depends(get_current_user)):
        if user.get("role") not in admin_roles: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        if not ObjectId.is_valid(item_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
        data={k:v for k,v in payload.items() if k in fields and k!="career_id"}
        if not data: raise HTTPException(422,detail={"code":"EMPTY_DATA","message":"No hay campos modificables"})
        data["updated_at"]=datetime.now(timezone.utc)
        q={"_id":ObjectId(item_id),**scope(user)}
        result=get_database()[collection].update_one(q,{"$set":data})
        if result.matched_count==0: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
        audit(get_database(),user,"update",collection,item_id)
        return {"success":True,"data":serialize(get_database()[collection].find_one(q))}
    @r.delete("/{item_id}")
    def delete_item(item_id:str,user=Depends(get_current_user)):
        if user.get("role") not in admin_roles: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        if not ObjectId.is_valid(item_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
        q={"_id":ObjectId(item_id),**scope(user)}
        res=get_database()[collection].update_one(q,{"$set":{"is_deleted":True,"is_active":False,"updated_at":datetime.now(timezone.utc)}})
        if not res.matched_count: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
        audit(get_database(),user,"soft_delete",collection,item_id)
        return {"success":True,"data":{"id":item_id,"deleted":True}}
    return r
def serialize(doc):
    out={}
    for k,v in doc.items():
        if k.startswith("_") and k!="_id": continue
        out["id" if k=="_id" else k]=str(v) if isinstance(v,ObjectId) else v
    return out
def audit(db,user,action,entity,entity_id):
    db.audit_logs.insert_one({"actor_id":user["_id"],"actor_role":user.get("role"),"career_id":user.get("career_id"),"action":action,"entity":entity,"entity_id":entity_id,"created_at":datetime.now(timezone.utc)})
