from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger

from repository.branch_snapshot_repository import BranchSnapshotRepository
from schema.branch_snapshot_schema import UpsertBranchSnapshot
from services.base_service import BaseService
from services.git_service import GitService
from services.neo4j_service import Neo4jService
from source_atlas.models.domain_models import CodeChunk


class BranchSnapshotService(BaseService):
    def __init__(
        self,
        branch_snapshot_repository: BranchSnapshotRepository,
        git_service: GitService,
        neo4j_service: Neo4jService,
    ):
        self.branch_snapshot_repository = branch_snapshot_repository
        self.git_service = git_service
        self.neo4j_service = neo4j_service
        super().__init__(branch_snapshot_repository)

    def parse_branch_if_needed(
        self,
        project_id: int,
        project_name: str,
        repo_path: str,
        branch_name: str,
        main_branch: str,
        language: str = "java"
    ) -> dict:
        
        # Step 1: Checkout the branch and get current commit hash
        logger.info(f"Checking branch '{branch_name}' for project {project_id}")
        self.git_service.pull_branch(repo_path, branch_name)
        current_commit = self.git_service.get_current_commit_hash(repo_path, branch_name)
        
        # Step 2: Check if this branch at this commit was already parsed
        existing_snapshot = self.branch_snapshot_repository.find_by_branch_and_commit(
            project_id, branch_name, current_commit
        )
        
        if existing_snapshot:
            logger.info(
                f"Branch '{branch_name}' at commit {current_commit[:8]} already parsed "
                f"({existing_snapshot.chunk_count} chunks). Skipping."
            )
            return {
                "was_cached": True,
                "commit_hash": current_commit,
                "chunk_count": existing_snapshot.chunk_count,
                "file_count": existing_snapshot.file_count,
            }
        
        # Step 3: Determine which files to parse
        files_to_parse = []
        if branch_name == main_branch:
            # Parsing the main branch itself - parse all files
             logger.info(f"Branch '{branch_name}' is the main branch, will parse all files")
             files_to_parse = None  # None means parse all
        else:
            # Get files that differ from the project's main_branch (NOT from PR base branch!)
            logger.info(f"Comparing branch '{branch_name}' against project main branch '{main_branch}'")
            diff_files = self.git_service.get_diff_files(repo_path, main_branch, branch_name)
            files_to_parse = diff_files if diff_files else None
            logger.info(f"Found {len(diff_files) if diff_files else 0} files different from main branch '{main_branch}'")

        # Step 4: Parse the files
        logger.info(f"Parsing branch '{branch_name}' at commit {current_commit[:8]}")
        
        if not files_to_parse:
            logger.info(f"No files to parse for branch '{branch_name}'")
            return {
                "was_cached": False,
                "commit_hash": current_commit,
                "chunk_count": 0,
                "file_count": 0,
            }

        from source_atlas.analyzers.analyzer_factory import AnalyzerFactory
        
        analyzer = AnalyzerFactory.create_analyzer(language, repo_path, str(project_id), branch_name)
        with analyzer as a:
            chunks = a.parse_project(
                Path(repo_path),
                target_files=files_to_parse,
                parse_all=False,
            )
        
        # Step 5: Import chunks into Neo4j with cross-branch relationships
        if chunks:
            self.neo4j_service.import_code_chunks_with_branch_relations(
                chunks=chunks,
                project_id=project_id,
                current_branch=branch_name
            )
            logger.info(
                f"Imported {len(chunks)} chunks into Neo4j for branch '{branch_name}' "
                f"and created BRANCH relationships to other branches"
            )
        
        # Step 6: Record the snapshot
        snapshot = UpsertBranchSnapshot(
            project_id=project_id,
            branch_name=branch_name,
            commit_hash=current_commit,
            chunk_count=len(chunks),
            file_count=len(files_to_parse) if files_to_parse else 0,
            status="completed",
        )
        self.branch_snapshot_repository.create(snapshot)
        
        logger.info(
            f"Successfully parsed and recorded snapshot for branch '{branch_name}' "
            f"at commit {current_commit[:8]} ({len(chunks)} chunks)"
        )
        
        return {
            "was_cached": False,
            "commit_hash": current_commit,
            "chunk_count": len(chunks),
            "file_count": len(files_to_parse) if files_to_parse else 0,
        }

    def get_branch_history(self, project_id: int, branch_name: str, limit: int = 10):
        """Get parsing history for a branch."""
        with self.branch_snapshot_repository.session_factory() as session:
            from models.branch_snapshot import BranchSnapshot
            
            return (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.branch_name == branch_name,
                )
                .order_by(BranchSnapshot.created_at.desc())
                .limit(limit)
                .all()
            )

