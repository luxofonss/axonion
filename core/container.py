from dependency_injector import containers, providers

from core.config import configs
from core.database import Database
from repository.project_repository import ProjectRepository
from repository.branch_snapshot_repository import BranchSnapshotRepository
from services.project_service import ProjectService
from services.git_service import GitService
from services.neo4j_service import Neo4jService
from services.branch_snapshot_service import BranchSnapshotService


class Container(containers.DeclarativeContainer):
    wiring_config = containers.WiringConfiguration(
        modules=[
            "api.project",
        ]
    )

    db = providers.Singleton(Database, db_url=configs.DATABASE_URI)

    project_repository = providers.Factory(ProjectRepository, session_factory=db.provided.session)
    
    branch_snapshot_repository = providers.Factory(BranchSnapshotRepository, session_factory=db.provided.session)

    git_service = providers.Factory(GitService)

    neo4j_service = providers.Singleton(Neo4jService)

    branch_snapshot_service = providers.Factory(
        BranchSnapshotService,
        branch_snapshot_repository=branch_snapshot_repository,
        git_service=git_service,
        neo4j_service=neo4j_service,
    )

    project_service = providers.Factory(
        ProjectService,
        project_repository=project_repository,
        git_service=git_service,
        neo4j_service=neo4j_service,
    )

