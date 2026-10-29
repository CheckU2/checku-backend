from app.routers.academic.generic import make_router
router = make_router("assignments", "teaching_assignments", {'subject_id', 'teacher_id', 'group_id', 'is_active', 'period_id', 'career_id'})
