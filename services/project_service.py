from datetime import datetime
from pathlib import Path

from core.config import configs
from repository.project_repository import ProjectRepository
from services.base_service import BaseService
from services.git_service import GitService
from services.neo4j_service import Neo4jService
from schema.project_schema import UpsertProject, IndexProjectRequest, FindProject


class ProjectService(BaseService):
    def __init__(self, project_repository: ProjectRepository, git_service: GitService, neo4j_service: Neo4jService):
        self.project_repository = project_repository
        self.git_service = git_service
        self.neo4j_service = neo4j_service
        super().__init__(project_repository)

    def add(self, schema: UpsertProject):
        """Create a new project and clone its repository at the main branch."""
        if not schema.git_url:
            raise ValueError("git_url is required")
        if not schema.languages:
            raise ValueError("languages is required (JSON string, can be multiple)")

        # Determine branch and folder name
        main_branch = schema.main_branch or "main"
        folder_name = None
        if schema.name:
            folder_name = schema.name
        else:
            last = schema.git_url.rstrip("/").split("/")[-1]
            folder_name = last[:-4] if last.endswith(".git") else last

        # Clone repository
        repo_path = self.git_service.clone_repository(schema.git_url, folder_name=folder_name, branch=main_branch)

        # Determine latest commit hash
        latest_commit = ""
        try:
            from git import Repo

            repo = Repo(repo_path)
            latest_commit = repo.head.commit.hexsha
        except Exception:
            latest_commit = ""

        # Determine source: use provided or infer from git_url
        src = (schema.source or "").strip().lower() if schema.source else None
        if not src:
            host = schema.git_url.split("://")[-1].split("/")[0]
            host_l = host.lower()
            if "github" in host_l:
                src = "github"
            elif "gitlab" in host_l:
                src = "gitlab"
            elif "bitbucket" in host_l:
                src = "bitbucket"
            else:
                src = "other"

        # Build complete payload
        payload = UpsertProject(
            name=schema.name or folder_name,
            description=schema.description or "",
            git_url=schema.git_url,
            main_branch=main_branch,
            source=src,
            local_path=str(Path(repo_path)),
            latest_commit=latest_commit,
            status=schema.status or 1,
            last_indexed=schema.last_indexed or datetime.utcnow(),
            languages=schema.languages,
            valid_extensions=schema.valid_extensions or "[]",
        )

        return self.project_repository.create(payload)

    def index_project(self, project_id: int, body: IndexProjectRequest | None = None):
        project = self.get_by_id(project_id)

        # Determine language and branch
        branch = (body.branch if body and body.branch else project.main_branch)
        language = (body.language if body and body.language else "java")

        # Determine target files and parse_all flag
        target_files = (body.target_files if body and body.target_files else None)
        parse_all = (body.parse_all if body else True)

        # Ensure local repo exists and is up to date
        repo_path = project.local_path if getattr(project, 'local_path', None) else str(Path(configs.DATA_DIR) / project.name)
        self.git_service.pull_branch(repo_path, branch)

        # Analyze source
        from source_atlas.analyzers.analyzer_factory import AnalyzerFactory
        # Use persisted local_path for analysis
        repo_path = project.local_path if getattr(project, 'local_path', None) else str(Path(configs.DATA_DIR) / project.name)

        analyzer = AnalyzerFactory.create_analyzer(language, repo_path, str(project.id), branch)
        with analyzer as a:
            chunks = a.parse_project(Path(repo_path), target_files=target_files, parse_all=parse_all)

        # Import into Neo4j
        self.neo4j_service.import_code_chunks(chunks)

        # Update last_indexed
        return self.project_repository.update_attr(project_id, "last_indexed", datetime.utcnow())

    def ensure_repo_and_pull_branches(self, project_id: int, branches: list[str]) -> dict:
        project = self.get_by_id(project_id)

        # Resolve repo_path: prefer persisted local_path, otherwise derive from configs and name
        repo_path = project.local_path if getattr(project, 'local_path', None) else str(Path(configs.DATA_DIR) / project.name)

        # If directory doesn't exist, clone using stored git_url and main_branch
        repo_dir = Path(repo_path)
        if not repo_dir.exists() or not any(repo_dir.iterdir()):
            repo_path = self.git_service.clone_repository(project.git_url, folder_name=project.name, branch=project.main_branch)

        results: dict[str, dict] = {}
        for br in branches:
            try:
                results[br] = self.git_service.pull_branch(repo_path, br)
            except Exception as e:
                results[br] = {"status": "error", "error": str(e)}

        return {"repo_path": repo_path, "branches": results}

    def find_project_by_git_url(self, git_url: str):
        """Find a project by exact git_url. Returns first match or None."""
        try:
            return self.project_repository.read_by_git_url(git_url)
        except Exception:
            return None