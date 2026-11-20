"""Institution and career boundaries for academic catalogs."""
from datetime import date

from bson import ObjectId
from fastapi import HTTPException

from app.services.institutions import institution_career_refs

INSTITUTION_ADMINS = {"institution_owner", "institution_admin"}
CATALOGS = {"careers", "academic_periods", "groups", "subjects"}
CATALOG_PATHS = {"careers", "academic-periods", "groups", "subjects"}


def catalog_error(code, message, status=422):
    raise HTTPException(status, detail={"code": code, "message": message})


def catalog_scope(db, user, collection, *, write=False):
    role = user["role"]
    tenant = user["institution_id"]
    active = {"is_deleted": {"$ne": True}}
    if collection not in CATALOGS or role not in INSTITUTION_ADMINS | {"career_admin", "teacher", "tutor"}:
        catalog_error("FORBIDDEN", "Tu rol no tiene acceso a este catálogo.", 403)
    if write and (role in {"teacher", "tutor"} or (role == "career_admin" and collection == "careers")):
        catalog_error("FORBIDDEN", "Tu rol solo puede consultar este catálogo.", 403)
    if role == "career_admin" and not user.get("career_id"):
        catalog_error("CAREER_REQUIRED", "Selecciona un contexto de director con una carrera asignada.", 403)
    refs = institution_career_refs(db, user)
    if collection == "careers":
        return {**active, "institution_id": tenant, "_id": {"$in": refs}}
    career_scope = {"career_id": {"$in": refs},
                    "$or": [{"institution_id": tenant}, {"institution_id": None}]}
    if collection == "academic_periods" and (not write or role in INSTITUTION_ADMINS):
        return {**active, "$or": [career_scope, {"institution_id": tenant, "career_id": None}]}
    return {**active, **career_scope}


def catalog_create_data(db, user, collection, data):
    catalog_scope(db, user, collection, write=True)
    data["institution_id"] = user["institution_id"]
    if collection == "careers":
        return
    assigned = user.get("career_id") if user["role"] == "career_admin" else None
    career = assigned or data.get("career_id")
    if assigned and data.get("career_id") and str(data["career_id"]) != str(assigned):
        catalog_error("CAREER_MISMATCH", "El registro debe pertenecer a tu carrera asignada.", 403)
    if not career and collection == "academic_periods" and user["role"] in INSTITUTION_ADMINS:
        data.pop("career_id", None)
    elif not career or str(career) not in institution_career_refs(db, user):
        catalog_error("INVALID_CAREER", "Selecciona una carrera de tu institución.")
    else:
        data["career_id"] = str(career)


def validate_period_dates(data):
    try:
        start = date.fromisoformat(data.get("start_date", ""))
        end = date.fromisoformat(data.get("end_date", ""))
    except (TypeError, ValueError):
        catalog_error("INVALID_PERIOD_DATES", "Indica las fechas de inicio y fin del periodo.")
    if end < start:
        catalog_error("INVALID_PERIOD_DATES", "La fecha de fin debe ser igual o posterior a la de inicio.")


def validate_group_period(db, user, data, *, require_active):
    period = data.get("period_id")
    if not isinstance(period, str) or not ObjectId.is_valid(period):
        catalog_error("INVALID_PERIOD", "Selecciona un periodo académico.")
    scoped_user = {**user, "career_id": str(data["career_id"])}
    filters = catalog_scope(db, scoped_user, "academic_periods")
    if require_active:
        filters["is_active"] = {"$ne": False}
    if not db.academic_periods.find_one({"_id": ObjectId(period), **filters}):
        catalog_error("INVALID_PERIOD", "Selecciona un periodo disponible para la carrera del grupo.")
