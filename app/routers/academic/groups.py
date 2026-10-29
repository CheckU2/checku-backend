from app.routers.academic.generic import make_router
router = make_router("groups", "groups", {"name", "capacity", "is_active", "period_id", "career_id", "code"}, {"super_admin", "admin", "career_admin", "tutor"})
