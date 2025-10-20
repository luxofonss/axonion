from dependency_injector.wiring import Provide
from fastapi import APIRouter, Depends

from core.container import Container
from core.middleware import inject
from models.project import Project
from schema.project_schema import UpsertProject, IndexProjectRequest
from schema.target_node_schema import NodeRelationshipRequest, NodeRelationshipListResponse
from schema.node_query_schema import NodeQueryRequest, NodeQueryResponse
from services.project_service import ProjectService
from services.neo4j_service import Neo4jService

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


@router.post("/{project_id}/nodes/left-targets", response_model=NodeRelationshipListResponse)
@inject
def get_left_target_nodes(
    project_id: int,
    request: NodeRelationshipRequest,
    neo4j_service: Neo4jService = Depends(Provide[Container.neo4j_service])
):
    """
    Get all left target nodes (incoming relationships) for a list of target nodes.
    
    This traverses relationships in reverse to find nodes that call, implement, extend, or use the target nodes.
    """
    # Validate that all target nodes belong to the specified project
    for node in request.target_nodes:
        if node.project_id != str(project_id):
            raise ValueError(f"Target node project_id {node.project_id} does not match URL project_id {project_id}")
    
    results = neo4j_service.get_left_target_nodes(
        target_nodes=request.target_nodes,
        max_level=request.max_level,
        min_level=request.min_level
    )
    
    return NodeRelationshipListResponse(
        results=results,
        total_count=len(results)
    )


@router.post("/{project_id}/nodes/related", response_model=NodeRelationshipListResponse)
@inject
def get_related_nodes(
    project_id: int,
    request: NodeRelationshipRequest,
    neo4j_service: Neo4jService = Depends(Provide[Container.neo4j_service])
):
    """
    Get all nodes related to a list of target nodes by traversing relationships.
    
    This finds all nodes that the target nodes call, implement, extend, or use.
    """
    # Validate that all target nodes belong to the specified project
    for node in request.target_nodes:
        if node.project_id != str(project_id):
            raise ValueError(f"Target node project_id {node.project_id} does not match URL project_id {project_id}")
    
    results = neo4j_service.get_related_nodes(
        target_nodes=request.target_nodes,
        max_level=request.max_level,
        min_level=request.min_level
    )
    
    return NodeRelationshipListResponse(
        results=results,
        total_count=len(results)
    )


@router.post("/{project_id}/nodes/query", response_model=NodeQueryResponse)
@inject
def get_nodes_by_condition(
    project_id: int,
    request: NodeQueryRequest,
    neo4j_service: Neo4jService = Depends(Provide[Container.neo4j_service])
):
    """
    Get nodes by various conditions including project_id, branch, pull_request_id, etc.
    
    This allows flexible querying of nodes with multiple optional filters.
    """
    # Validate that request project_id matches URL project_id
    if request.project_id != project_id:
        raise ValueError(f"Request project_id {request.project_id} does not match URL project_id {project_id}")
    
    nodes = neo4j_service.get_nodes_by_condition(
        project_id=request.project_id,
        branch=request.branch,
        pull_request_id=request.pull_request_id,
        class_name=request.class_name,
        method_name=request.method_name
    )
    
    return NodeQueryResponse(
        nodes=nodes,
        total_count=len(nodes)
    )