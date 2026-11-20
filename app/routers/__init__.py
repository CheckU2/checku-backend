from fastapi import APIRouter, Depends
from app.services.tenant_guard import legacy_only
from app.routers import system, auth, users, institutions
from app.routers.academic import (
 students,teachers,tutors,groups,subjects,enrollments,assignments,schedules,sessions,
 attendance,justifications,proximity,incidents,reports,audit_logs,careers,periods,classes,career_management
)
api_router=APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(institutions.router)
for module in (classes,careers,periods,students,teachers,tutors,groups,subjects,enrollments,assignments,schedules,sessions,attendance,justifications,proximity,incidents,reports,audit_logs,career_management):
 api_router.include_router(module.router, dependencies=[Depends(legacy_only)])
