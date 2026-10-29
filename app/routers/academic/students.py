from app.routers.academic.generic import make_router
router = make_router("students", "students", {'group_id', 'user_id', 'student_id', 'period_id', 'career_id', 'enrollment_status'})
