"""Multi-institution primitives. Legacy academic routes remain single-tenant until audited."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from bson import ObjectId
from fastapi import HTTPException
from app.database import get_database

def now_utc():
    return datetime.now(timezone.utc)

def valid_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise HTTPException(422, detail={"code":"INVALID_TIMEZONE","message":"Zona horaria IANA inválida"})
    return value

def institution(db, identifier, active=True):
    if not isinstance(identifier,str) or not ObjectId.is_valid(identifier):
        raise HTTPException(422, detail={"code":"INVALID_INSTITUTION","message":"Institución inválida"})
    query={"_id":ObjectId(identifier)}
    if active: query["status"]="active"
    doc=db.institutions.find_one(query)
    if not doc: raise HTTPException(404, detail={"code":"INSTITUTION_NOT_FOUND","message":"Institución no disponible"})
    return doc

def institution_career_refs(db, user):
    institution_id=user.get("institution_id")
    if not institution_id:
        return None
    query={"institution_id":institution_id,"is_deleted":{"$ne":True}}
    career_id=user.get("career_id")
    if career_id:
        if not ObjectId.is_valid(str(career_id)):
            return []
        query["_id"]=ObjectId(str(career_id))
    careers=db.careers.find(query,{"_id":1})
    return [value for career in careers for value in (str(career["_id"]),career["_id"])]

def career_belongs_to_user_institution(db,user,career_id):
    institution_id=user.get("institution_id")
    if not institution_id:
        return True
    if not ObjectId.is_valid(str(career_id)):
        return False
    return db.careers.find_one({
        "_id":ObjectId(str(career_id)),
        "institution_id":institution_id,
        "is_deleted":{"$ne":True},
    }) is not None

def public_institution(doc):
    return {"id":str(doc["_id"]),"name":doc["name"],"slug":doc["slug"],"period_type":doc["period_type"]}

def check_platform(user):
    if user.get("role") not in ("super_admin","admin","platform_super_admin") or user.get("institution_id"):
        raise HTTPException(403,detail={"code":"FORBIDDEN","message":"Solo administración de plataforma"})

def is_institution_admin(db,user,institution_id):
    if user.get("role") in ("admin","super_admin","platform_super_admin") and not user.get("institution_id"):
        return True
    if user.get("institution_id") and user["institution_id"] != institution_id:
        return False
    if user.get("active_context") and user.get("role") not in ("institution_owner", "institution_admin"):
        return False
    return db.role_assignments.find_one({
        "institution_id":institution_id,
        "user_id":str(user["_id"]),
        "role":{"$in":["institution_owner","institution_admin"]},
        "scope_type":"institution",
        "status":"active",
    }) is not None
