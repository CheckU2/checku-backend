from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
from fastapi import FastAPI,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import get_settings
from app.database import connect_database,close_database,get_database,ensure_indexes
from app.routers import api_router
from app.services.bootstrap import ensure_bootstrap_admin
settings=get_settings()
@asynccontextmanager
async def lifespan(app:FastAPI):
 connect_database()
 try:
  db=get_database()
  if db is not None:
   try: ensure_indexes(db)
   except Exception: pass
  try:
   ensure_bootstrap_admin()
  except Exception:
   # Readiness endpoint reports dependency failures; liveness should remain available.
   pass
  yield
 finally: close_database()
app=FastAPI(title=settings.app_name,description="API REST de CheckU para aplicaciones web y móvil.",version="1.0.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=settings.cors_origin_list,allow_credentials=True,allow_methods=["GET","POST","PATCH","PUT","DELETE","OPTIONS"],allow_headers=["Authorization","Content-Type"])
@app.exception_handler(Exception)
async def unhandled_exception_handler(request:Request,exc:Exception):
 if settings.app_env.lower()=="development": raise exc
 return JSONResponse(status_code=500,content={"success":False,"error":{"code":"INTERNAL_ERROR","message":"Ocurrió un error interno"}})
app.include_router(api_router,prefix=settings.api_v1_prefix)
@app.get("/",tags=["Sistema"])
def root(): return {"success":True,"data":{"name":settings.app_name,"docs":"/docs" if settings.app_env.lower()!="production" else "OpenAPI deshabilitado por política de despliegue","timezone":settings.institutional_timezone}}
