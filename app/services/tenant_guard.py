"""Fail-closed guard for legacy routes that have not been tenant-audited."""
from fastapi import Depends, HTTPException, Request
from app.config import get_settings
from app.dependencies import get_current_user
from app.services.academic_catalogs import CATALOG_PATHS, INSTITUTION_ADMINS

STUDENT_ROUTES = {
    ("GET", "/sessions/active"),
    ("GET", "/sessions/{session_id}"),
    ("POST", "/proximity/sessions/{session_id}/events"),
    ("GET", "/classes"),
    ("POST", "/classes/join"),
    ("GET", "/classes/{class_id}/community"),
    ("GET", "/attendance/me"),
    ("GET", "/attendance/calendar"),
    ("POST", "/attendance/code/check-in"),
    ("POST", "/attendance/qr/check-in"),
    ("GET", "/reports/dashboard"),
}

TEACHER_ROUTES = {
    ("GET", "/proximity/sessions/{session_id}/events"),
    ("GET", "/classes"),
    ("GET", "/classes/options"),
    ("POST", "/classes"),
    ("POST", "/classes/subjects"),
    ("GET", "/classes/{class_id}/community"),
    ("GET", "/classes/{class_id}/students"),
    ("PATCH", "/classes/{class_id}"),
    ("POST", "/classes/{class_id}/invite-tutor"),
    ("GET", "/sessions"),
    ("GET", "/sessions/active"),
    ("POST", "/sessions"),
    ("GET", "/sessions/{session_id}"),
    ("GET", "/sessions/{session_id}/status"),
    ("GET", "/sessions/{session_id}/report"),
    ("POST", "/sessions/{session_id}/start"),
    ("POST", "/sessions/{session_id}/end"),
    ("POST", "/sessions/{session_id}/qr/rotate"),
    ("POST", "/sessions/{session_id}/qr/revoke"),
    ("GET", "/reports/dashboard"),
    ("GET", "/reports/attendance"),
    ("GET", "/reports/justifications"),
    ("GET", "/reports/teachers/{teacher_id}/sessions"),
    ("GET", "/justifications"),
    ("GET", "/justifications/notifications"),
    ("GET", "/justifications/{justification_id}"),
    ("GET", "/justifications/{justification_id}/messages"),
    ("POST", "/justifications/{justification_id}/approve"),
    ("POST", "/justifications/{justification_id}/reject"),
    ("POST", "/justifications/{justification_id}/messages"),
}

JUSTIFICATION_ROUTES = {
    ("GET", "/justifications"),
    ("GET", "/justifications/options"),
    ("GET", "/justifications/notifications"),
    ("GET", "/justifications/{justification_id}"),
    ("GET", "/justifications/{justification_id}/messages"),
    ("POST", "/justifications"),
    ("POST", "/justifications/{justification_id}/approve"),
    ("POST", "/justifications/{justification_id}/reject"),
    ("POST", "/justifications/{justification_id}/messages"),
}
ACADEMIC_REPORT_ROUTES = {
    ("GET", "/reports/academic"), ("GET", "/reports/academic/export"),
}
TEACHER_ROUTES |= ACADEMIC_REPORT_ROUTES | JUSTIFICATION_ROUTES | {("GET", "/attendance/calendar")}

GROUP_ROSTER_ROUTES = {
    ("GET", "/groups"),
    ("GET", "/groups/{item_id}"),
    ("GET", "/classes"),
    ("GET", "/classes/{class_id}/community"),
    ("GET", "/classes/{class_id}/students"),
    ("GET", "/career-management/groups/{group_id}/members"),
}

CAREER_ADMIN_ROUTES = GROUP_ROSTER_ROUTES | ACADEMIC_REPORT_ROUTES | {
    ("GET", "/classes/options"),
    ("POST", "/classes"),
    ("POST", "/classes/subjects"),
    ("PATCH", "/classes/{class_id}"),
    ("GET", "/career-management/overview"),
    ("GET", "/career-management/people"),
    ("POST", "/career-management/groups/{group_id}/tutor"),
}


def legacy_only(request: Request, user=Depends(get_current_user)):
    if user.get('institution_id'):
        route = request.scope.get("route")
        prefix = get_settings().api_v1_prefix.rstrip("/")
        route_path = getattr(route, "path", "")
        # FastAPI can expose the original or the prefixed template for included routers.
        if prefix and route_path.startswith(f"{prefix}/"):
            route_path = route_path[len(prefix):]
        role = user.get("role")
        if role in INSTITUTION_ADMINS | {"career_admin"} and any(
            (request.method in {"GET", "POST"} and route_path == f"/{path}")
            or (request.method in {"GET", "PATCH", "DELETE"} and route_path == f"/{path}/{{item_id}}")
            for path in CATALOG_PATHS
        ):
            return user
        is_student = user.get("role") == "student" and user.get("account_type", "student") == "student"
        is_allowed_student_route = any(
            request.method == method and route_path == path
            for method, path in STUDENT_ROUTES
        )
        is_teacher = user.get("role") == "teacher"
        is_allowed_teacher_route = any(
            request.method == method and route_path == path
            for method, path in TEACHER_ROUTES
        )
        if (is_student and is_allowed_student_route) or (is_teacher and is_allowed_teacher_route):
            return user
        staff_routes = {
            "career_admin": CAREER_ADMIN_ROUTES, "tutor": GROUP_ROSTER_ROUTES | JUSTIFICATION_ROUTES | ACADEMIC_REPORT_ROUTES | {
                ("GET", "/attendance/calendar"), ("GET", "/classes/options"), ("POST", "/classes"),
                ("POST", "/classes/subjects"), ("POST", "/classes/groups"),
            },
            **dict.fromkeys(INSTITUTION_ADMINS, ACADEMIC_REPORT_ROUTES | {
                ("GET", "/classes"), ("GET", "/classes/{class_id}/community"),
                ("GET", "/classes/{class_id}/students"),
                ("GET", "/career-management/groups/{group_id}/members"),
            }),
        }
        if any(request.method == method and route_path == path
               for method, path in staff_routes.get(user.get("role"), ())):
            return user
        raise HTTPException(403, detail={'code':'TENANT_ROUTE_NOT_READY', 'message':'Tu rol activo no tiene acceso a esta operación.'})
    return user
