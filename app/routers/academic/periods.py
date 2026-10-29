from app.routers.academic.generic import make_router
router = make_router("academic-periods","academic_periods",{"name","code","start_date","end_date","career_id","is_active"})
