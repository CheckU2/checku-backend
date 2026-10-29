from app.routers.academic.generic import make_router
router = make_router("schedules", "schedules", {'start_time', 'career_id', 'room', 'subject_id', 'weekday', 'teacher_id', 'group_id', 'is_active', 'end_time', 'period_id'})
