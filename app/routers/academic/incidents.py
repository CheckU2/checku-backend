from app.routers.academic.generic import make_router
router = make_router("incidents", "incidents", {'category', 'career_id', 'priority', 'status', 'session_id', 'title', 'student_id', 'description'})
