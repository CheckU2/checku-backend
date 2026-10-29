from app.routers.academic.generic import make_router
router = make_router("subjects", "subjects", {'name', 'credits', 'is_active', 'description', 'career_id', 'code'})
