from datetime import datetime
from pathlib import Path
from loguru import logger

from core.config import configs
from repository.project_repository import ProjectRepository
from repository.branch_snapshot_repository import BranchSnapshotRepository
from services.base_service import BaseService
from services.git_service import GitService
from services.neo4j_service import Neo4jService
from schema.project_schema import UpsertProject, IndexProjectRequest, FindProject
from models.branch_snapshot import BranchSnapshot


class ProjectService(BaseService):
    def __init__(self, project_repository: ProjectRepository, git_service: GitService, neo4j_service: Neo4jService, branch_snapshot_repository: BranchSnapshotRepository):
        self.project_repository = project_repository
        self.git_service = git_service
        self.neo4j_service = neo4j_service
        self.branch_snapshot_repository = branch_snapshot_repository
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

        # Determine target files, parse_all flag, and force_reindex
        target_files = (body.target_files if body and body.target_files else None)
        parse_all = (body.parse_all if body else True)
        force_reindex = (body.force_reindex if body else False)

        # Use common indexing method
        result = self._index_branch_with_snapshot(
            project_id=project_id,
            branch=branch,
            language=language,
            target_files=target_files,
            parse_all=parse_all,
            force_reindex=force_reindex
        )

        # Update last_indexed
        return self.project_repository.update_attr(project_id, "last_indexed", datetime.utcnow())

    def _index_branch_with_snapshot(
        self,
        project_id: int,
        branch: str,
        language: str = "java",
        target_files: list[str] = None,
        parse_all: bool = True,
        force_reindex: bool = False,
        pull_request_id: str = None
    ) -> dict:
        """
        Common method to index a branch and create snapshot.
        Used by both index_project and branch_snapshot_service.
        
        Returns:
            dict: {
                "was_cached": bool,
                "commit_hash": str,
                "chunk_count": int,
                "file_count": int,
                "chunks": List[CodeChunk]
            }
        """
        project = self.get_by_id(project_id)
        
        # Ensure local repo exists and is up to date
        repo_path = project.local_path if getattr(project, 'local_path', None) else str(Path(configs.DATA_DIR) / project.name)
        self.git_service.pull_branch(repo_path, branch)

        # Get current commit hash after pulling
        current_commit = self.git_service.get_current_commit_hash(repo_path, branch)
        
        # Check if this commit has already been indexed (unless force_reindex is True)
        if not force_reindex:
            existing_snapshot = self.branch_snapshot_repository.find_by_branch_and_commit(
                project_id, branch, current_commit
            )
            
            if existing_snapshot:
                print(f"ℹ️ Branch {branch}@{current_commit[:8]} already indexed, skipping...")
                if not force_reindex:
                    print(f"   Use 'force_reindex: true' to reindex anyway")
                return {
                    "was_cached": True,
                    "commit_hash": current_commit,
                    "chunk_count": existing_snapshot.chunk_count,
                    "file_count": existing_snapshot.file_count,
                    "chunks": []
                }

        if force_reindex:
            print(f"🔄 Force reindexing {branch}@{current_commit[:8]}...")
        else:
            print(f"🚀 Starting indexing for {branch}@{current_commit[:8]}...")

        # Analyze source
        from source_atlas.analyzers.analyzer_factory import AnalyzerFactory
        
        analyzer = AnalyzerFactory.create_analyzer(language, repo_path, str(project_id), branch)
        with analyzer as a:
            chunks = a.parse_project(Path(repo_path), target_files=target_files, parse_all=parse_all)
        
        # Delete existing nodes for this project and branch before importing new ones
        logger.info(f"Deleting existing nodes for project {project_id}, branch {branch}...")
        deleted_count = self.neo4j_service.delete_branch_nodes(
            project_id=project_id,
            branch_name=branch
        )
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} existing nodes")
        else:
            logger.info("No existing nodes found to delete")
        
        # Import into Neo4j
        self.neo4j_service.import_code_chunks(chunks)

        # Create or update branch snapshot record
        try:
            # Check if snapshot exists (for force_reindex case)
            existing_snapshot = self.branch_snapshot_repository.find_by_branch_and_commit(
                project_id, branch, current_commit
            )
            
            if existing_snapshot and force_reindex:
                # Update existing snapshot
                existing_snapshot.chunk_count = len(chunks)
                existing_snapshot.file_count = len(set(chunk.file_path for chunk in chunks)) if chunks else 0
                existing_snapshot.status = "completed"
                # Note: SQLModel doesn't have direct update method, so we'll delete and recreate
                self.branch_snapshot_repository.delete(existing_snapshot.id)
                
            # Create new snapshot
            branch_snapshot = BranchSnapshot(
                project_id=project_id,
                branch_name=branch,
                commit_hash=current_commit,
                chunk_count=len(chunks),
                file_count=len(set(chunk.file_path for chunk in chunks)) if chunks else 0,
                status="completed"
            )
            
            # Insert branch snapshot
            self.branch_snapshot_repository.create(branch_snapshot)
            
            action = "Updated" if (existing_snapshot and force_reindex) else "Created"
            print(f"✅ {action} branch snapshot for {branch}@{current_commit[:8]} with {len(chunks)} chunks")
                
        except Exception as e:
            print(f"⚠️ Failed to create/update branch snapshot: {e}")
            # Don't fail the entire indexing process if snapshot creation fails

        return {
            "was_cached": False,
            "commit_hash": current_commit,
            "chunk_count": len(chunks),
            "file_count": len(set(chunk.file_path for chunk in chunks)) if chunks else 0,
            "chunks": chunks
        }

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
    
    def find_project_by_repo_name(self, repo_full_name: str):
        """
        Find a project by GitHub repository full name (e.g., 'owner/repo').
        Tries multiple common URL formats.
        
        Args:
            repo_full_name: GitHub repo in format 'owner/repo'
            
        Returns:
            Project object if found, None otherwise
        """
        # Generate candidate URLs commonly used for GitHub repos
        candidate_urls = [
            f"https://github.com/{repo_full_name}.git",
            f"https://github.com/{repo_full_name}",
            f"git@github.com:{repo_full_name}.git",
        ]
        
        # Try each candidate URL
        for url in candidate_urls:
            project = self.find_project_by_git_url(url)
            if project:
                return project
        
        return None