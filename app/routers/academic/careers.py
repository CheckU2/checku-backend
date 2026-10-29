from app.routers.academic.generic import make_router
router = make_router("careers", "careers", {"name", "code", "description", "is_active"}, {"super_admin", "admin"})
