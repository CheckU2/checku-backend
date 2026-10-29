from app.routers.academic.generic import make_router
router = make_router("teachers", "teachers", {'user_id', 'is_active', 'department', 'career_id', 'employee_id'})
