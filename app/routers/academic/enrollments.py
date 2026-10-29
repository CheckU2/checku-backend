from app.routers.academic.generic import make_router
router = make_router("enrollments", "enrollments", {'status', 'group_id', 'enrolled_at', 'student_id', 'period_id', 'career_id'})
