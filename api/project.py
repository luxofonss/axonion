from dependency_injector.wiring import Provide
from fastapi import APIRouter, Depends

from core.container import Container
from core.middleware import inject
from models.project import Project
from schema.project_schema import UpsertProject, IndexProjectRequest
from services.project_service import ProjectService

router = APIRouter(prefix="/project", tags=["project"])

@router.post("", response_model=Project)
@inject
def create_project(
       project: UpsertProject,
       service: ProjectService = Depends(Provide[Container.project_service])
):
    return service.add(project)


@router.post("/{project_id}/index", response_model=Project)
@inject
def index_project(
       project_id: int,
       body: IndexProjectRequest | None = None,
       service: ProjectService = Depends(Provide[Container.project_service])
):
    return service.index_project(project_id, body)