from fastapi import APIRouter
from api.project import router as project_router
from api.webhook import router as webhook_router

routers = APIRouter()

routers.include_router(project_router, tags=["v1"])
routers.include_router(webhook_router, tags=["webhook"])
