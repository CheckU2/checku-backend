from fastapi import APIRouter
from app.database import ping_database

router = APIRouter(tags=["Sistema"])


@router.get("/health")
def health():
    return {"success": True, "data": {"status": "healthy"}}


@router.get("/ready")
def ready():
    database_ready = ping_database()
    if not database_ready:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail={
                "success": False,
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "MongoDB no está configurado o no responde.",
                },
            },
        )
    return {"success": True, "data": {"status": "ready", "database": "connected"}}
