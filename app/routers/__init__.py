from fastapi import APIRouter
from app.routers import system, auth, users
from app.routers.academic import (
 students,teachers,tutors,groups,subjects,enrollments,assignments,schedules,sessions,
 attendance,justifications,proximity,incidents,reports,audit_logs,careers,periods,classes
)
api_router=APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
for module in (classes,careers,periods,students,teachers,tutors,groups,subjects,enrollments,assignments,schedules,sessions,attendance,justifications,proximity,incidents,reports,audit_logs):
 api_router.include_router(module.router)
