from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import DESCENDING
from app.database import get_database
from app.dependencies import get_current_user
from app.services.academic_catalogs import (
    CATALOGS, catalog_create_data, catalog_scope, validate_group_period, validate_period_dates,
)

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
    def institutional_catalog(user):
        return user.get("institution_id") and collection in CATALOGS and not (
            collection == "groups" and user.get("role") == "tutor"
        )

    def scope(user, *, write=False):
        if institutional_catalog(user):
            return catalog_scope(get_database(), user, collection, write=write)
        query={}
        role=user.get("role")
        if role in {"career_admin", "tutor"} and collection == "groups":
            from app.routers.academic.classes import id_refs, managed_career, tutor_group_ids

            db = get_database()
            career = managed_career(db, user, None)
            query.update(career_id={"$in": id_refs(career)}, is_deleted={"$ne": True})
            if user.get("institution_id"):
                query["institution_id"] = {"$in": [None, user["institution_id"]]}
            if role == "tutor":
                query["_id"] = {"$in": [ObjectId(group_id) for group_id in tutor_group_ids(db, user)
                                         if ObjectId.is_valid(group_id)]}
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
    def list_items(page:int=Query(1,ge=1),limit:int=Query(20,ge=1,le=100),search:str|None=Query(None,max_length=80),career_id:str|None=None,user=Depends(get_current_user)):
        db=get_database(); q=scope(user)
        if not institutional_catalog(user) and user.get("role") not in admin_roles and (collection == "careers" or user.get("role") not in {"student","teacher","tutor"}):
            raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        filters = [q]
        if search and search.strip():
            from re import escape
            columns = {"name", "code", "title", "status"} & fields
            if columns:
                filters.append({"$or": [{key: {"$regex": escape(search.strip()), "$options": "i"}} for key in sorted(columns)]})
        if career_id and "career_id" in fields:
            if not ObjectId.is_valid(career_id):
                raise HTTPException(422,detail={"message":"Selecciona una carrera válida"})
            filters.append({"career_id":{"$in":[career_id,ObjectId(career_id)]}})
        q = {"$and": filters} if len(filters) > 1 else q
        total=db[collection].count_documents(q)
        docs=db[collection].find(q).sort("created_at",DESCENDING).skip((page-1)*limit).limit(limit)
        return {"success":True,"data":[serialize(x) for x in docs],"meta":{"page":page,"limit":limit,"total":total}}
    @r.post("",status_code=201)
    def create_item(payload:dict,user=Depends(get_current_user)):
        if not institutional_catalog(user) and user.get("role") not in admin_roles: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        data={k:v for k,v in payload.items() if k in fields}
        if not data: raise HTTPException(422,detail={"code":"EMPTY_DATA","message":"No hay campos permitidos"})
        db=get_database()
        if institutional_catalog(user):
            catalog_create_data(db, user, collection, data)
            if collection == "academic_periods":
                validate_period_dates(data)
            if collection == "groups":
                validate_group_period(db, user, data, require_active=True)
        elif user.get("role") in {"career_admin", "tutor"} and collection == "groups":
            data["career_id"] = str(user.get("career_id"))
        elif user.get("role")=="career_admin":
            data["career_id"]=user.get("career_id")
        now=datetime.now(timezone.utc); data.update({"created_at":now,"updated_at":now,"is_deleted":False})
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
        q={"$and": [{"_id": ObjectId(item_id)}, scope(user)]}
        doc=get_database()[collection].find_one(q)
        if not doc: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
        return {"success":True,"data":serialize(doc)}
    @r.patch("/{item_id}")
    def update_item(item_id:str,payload:dict,user=Depends(get_current_user)):
        if not institutional_catalog(user) and user.get("role") not in admin_roles: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        if not ObjectId.is_valid(item_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
        data={k:v for k,v in payload.items() if k in fields and k!="career_id"}
        if not data: raise HTTPException(422,detail={"code":"EMPTY_DATA","message":"No hay campos modificables"})
        data["updated_at"]=datetime.now(timezone.utc)
        q={"$and": [{"_id": ObjectId(item_id)}, scope(user, write=True)]}
        db=get_database()
        if institutional_catalog(user) and collection == "academic_periods" and {"start_date", "end_date"} & data.keys():
            period = db.academic_periods.find_one(q)
            if not period:
                raise HTTPException(404, detail={"code":"NOT_FOUND","message":"Periodo no encontrado"})
            validate_period_dates({**period, **data})
        if institutional_catalog(user) and collection == "groups" and "period_id" in data:
            group = db.groups.find_one(q)
            if not group:
                raise HTTPException(404, detail={"code":"NOT_FOUND","message":"Grupo no encontrado"})
            if str(group.get("period_id")) != str(data["period_id"]):
                validate_group_period(db, user, {**group, **data}, require_active=True)
        result=db[collection].update_one(q,{"$set":data})
        if result.matched_count==0: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
        audit(get_database(),user,"update",collection,item_id)
        return {"success":True,"data":serialize(get_database()[collection].find_one(q))}
    @r.delete("/{item_id}")
    def delete_item(item_id:str,user=Depends(get_current_user)):
        if not institutional_catalog(user) and user.get("role") not in admin_roles: raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Sin permisos"})
        if not ObjectId.is_valid(item_id): raise HTTPException(400,detail={"code":"INVALID_ID","message":"Identificador inválido"})
        q={"$and": [{"_id": ObjectId(item_id)}, scope(user, write=True)]}
        res=get_database()[collection].update_one(q,{"$set":{"is_deleted":True,"is_active":False,"updated_at":datetime.now(timezone.utc)}})
        if not res.matched_count: raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Registro no encontrado"})
        audit(get_database(),user,"soft_delete",collection,item_id)
        return {"success":True,"data":{"id":item_id,"deleted":True}}
    return r
def serialize(doc):
    out={}
    for k,v in doc.items():
        if k.startswith("_") and k!="_id": continue
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        out["id" if k=="_id" else k]=str(v) if isinstance(v,ObjectId) else v
    return out
def audit(db,user,action,entity,entity_id):
    db.audit_logs.insert_one({"actor_id":user["_id"],"actor_role":user.get("role"),"institution_id":user.get("institution_id"),"career_id":user.get("career_id"),"action":action,"entity":entity,"entity_id":entity_id,"created_at":datetime.now(timezone.utc)})
