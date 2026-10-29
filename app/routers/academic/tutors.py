from app.routers.academic.generic import make_router
router = make_router("tutors", "tutors", {'career_id', 'user_id', 'employee_id', 'is_active'})
