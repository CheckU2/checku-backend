"""Resolve user contexts and permissions from active database assignments."""
from bson import ObjectId
from fastapi import HTTPException


ROLE_ALIASES = {
    "admin": "platform_super_admin",
    "super_admin": "platform_super_admin",
    "career_admin": "career_director",
}

ROLE_PERMISSIONS = {
    "platform_super_admin": {
        "institutions.manage",
        "owners.assign",
        "platform.audit",
    },
    "institution_owner": {
        "institution.manage",
        "members.read",
        "roles.assign",
        "roles.revoke",
        "staff.review",
        "staff.approve",
        "academic.manage",
        "reports.read",
        "audit.read",
    },
    "institution_admin": {
        "institution.read",
        "members.read",
        "roles.assign",
        "staff.review",
        "staff.approve",
        "academic.manage",
        "reports.read",
        "audit.read",
    },
    "career_director": {
        "career.manage",
        "staff.review",
        "staff.approve",
        "tutors.assign",
        "classes.manage",
        "reports.read",
    },
    "teacher": {"classes.read", "sessions.manage", "attendance.manage", "reports.read"},
    "tutor": {"tutorados.read", "attendance.read", "justifications.manage"},
    "student": {"classes.read", "classes.join", "attendance.self", "profile.self"},
}


def canonical_role(role):
    return ROLE_ALIASES.get(role, role)


def _context(role, institution_id=None, institution_name=None, scope_type="institution", scope_id=None, career_id=None):
    normalized_role = canonical_role(role)
    return {
        "institution_id": institution_id,
        "institution_name": institution_name,
        "role": normalized_role,
        "scope_type": scope_type,
        "scope_id": str(scope_id) if scope_id is not None else None,
        "career_id": str(career_id or scope_id) if career_id or scope_type == "career" else None,
        "permissions": sorted(ROLE_PERMISSIONS.get(normalized_role, set())),
    }


def user_contexts(db, user):
    contexts = []
    user_id = str(user["_id"])
    base_role = user.get("role")

    if base_role in {"admin", "super_admin", "platform_super_admin"}:
        contexts.append(_context("platform_super_admin", scope_type="platform"))

    memberships = db.institution_memberships.find({
        "user_id": {"$in": [user_id, user["_id"]]},
        "status": "active",
    })
    for membership in memberships:
        institution_id = str(membership.get("institution_id", ""))
        if not ObjectId.is_valid(institution_id):
            continue
        institution = db.institutions.find_one({
            "_id": ObjectId(institution_id),
            "status": "active",
        })
        if not institution:
            continue

        assignments = list(db.role_assignments.find({
            "institution_id": institution_id,
            "user_id": {"$in": [user_id, user["_id"]]},
            "status": "active",
        }))
        for assignment in assignments:
            role = assignment.get("role")
            if canonical_role(role) not in ROLE_PERMISSIONS:
                continue
            contexts.append(_context(
                role,
                institution_id,
                institution.get("name"),
                assignment.get("scope_type", "institution"),
                assignment.get("scope_id"),
                assignment.get("career_id"),
            ))

        account_type = user.get("account_type", base_role)
        if account_type == "student" and not any(canonical_role(x.get("role")) == "student" for x in assignments):
            contexts.append(_context(
                "student",
                institution_id,
                institution.get("name"),
                "career" if user.get("career_id") else "institution",
                user.get("career_id"),
            ))

    if not contexts and not user.get("institution_id") and base_role:
        legacy_role = canonical_role(base_role)
        if legacy_role in ROLE_PERMISSIONS:
            contexts.append(_context(legacy_role, scope_type="legacy"))

    unique = {}
    for item in contexts:
        key = (
            item["institution_id"],
            item["role"],
            item["scope_type"],
            item["scope_id"],
        )
        unique[key] = item
    return list(unique.values())


def resolve_user_context(db, user, requested):
    if not isinstance(requested, dict):
        raise HTTPException(422, detail={"code": "INVALID_CONTEXT", "message": "Selecciona un contexto válido"})
    for context in user_contexts(db, user):
        if all(
            requested.get(key) == context.get(key)
            for key in ("institution_id", "role", "scope_type", "scope_id")
        ):
            return context
    raise HTTPException(403, detail={"code": "CONTEXT_NOT_AVAILABLE", "message": "El rol o institución ya no está disponible"})


def apply_user_context(user, context):
    result = dict(user)
    result["_base_role"] = user.get("role")
    result["active_context"] = context
    role = context["role"]
    result["role"] = {
        "platform_super_admin": "super_admin",
        "career_director": "career_admin",
    }.get(role, role)
    if context.get("institution_id"):
        result["institution_id"] = context["institution_id"]
        result.pop("career_id", None)
    if context.get("career_id"):
        result["career_id"] = context["career_id"]
    return result
