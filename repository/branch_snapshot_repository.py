from contextlib import AbstractContextManager
from typing import Callable, Optional, List, Dict, Any

from sqlalchemy.orm import Session

from models.branch_snapshot import BranchSnapshot
from repository.base_repository import BaseRepository


class BranchSnapshotRepository(BaseRepository):
    def __init__(self, session_factory: Callable[..., AbstractContextManager[Session]]):
        self.session_factory = session_factory
        super().__init__(session_factory, BranchSnapshot)

    def find_by_branch_and_commit(
        self, project_id: int, branch_name: str, commit_hash: str
    ) -> Optional[BranchSnapshot]:
        """Check if a specific branch at a specific commit has been parsed."""
        with self.session_factory() as session:
            return (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.branch_name == branch_name,
                    BranchSnapshot.commit_hash == commit_hash,
                    BranchSnapshot.status == "completed"
                )
                .first()
            )

    def get_latest_for_branch(
        self, project_id: int, branch_name: str
    ) -> Optional[BranchSnapshot]:
        """Get the most recently parsed snapshot for a branch."""
        with self.session_factory() as session:
            return (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.branch_name == branch_name,
                    BranchSnapshot.status == "completed"
                )
                .order_by(BranchSnapshot.created_at.desc())
                .first()
            )

    def find_by_branch(self, project_id: int, branch_name: str) -> list[BranchSnapshot]:
        """Get all snapshots for a specific branch."""
        with self.session_factory() as session:
            return (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.branch_name == branch_name
                )
                .order_by(BranchSnapshot.created_at.desc())
                .all()
            )

    def find_by_pr_and_project(self, project_id: int, pull_request_id: str) -> Optional[BranchSnapshot]:
        """Find branch snapshot by project and pull request ID."""
        with self.session_factory() as session:
            return (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.pull_request_id == pull_request_id,
                    BranchSnapshot.status == "completed"
                )
                .first()
            )

    def upsert_changed_nodes(
        self, 
        project_id: int, 
        pull_request_id: str, 
        branch_name: str, 
        commit_hash: str, 
        changed_nodes: List[Dict[str, Any]]
    ) -> BranchSnapshot:
        """Update or insert changed nodes for a pull request."""
        with self.session_factory() as session:
            # Try to find existing snapshot
            existing = (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.pull_request_id == pull_request_id
                )
                .first()
            )
            
            if existing:
                # Update existing
                existing.branch_name = branch_name
                existing.commit_hash = commit_hash
                existing.changed_nodes = changed_nodes
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return existing
            else:
                # Create new
                new_snapshot = BranchSnapshot(
                    project_id=project_id,
                    branch_name=branch_name,
                    commit_hash=commit_hash,
                    pull_request_id=pull_request_id,
                    changed_nodes=changed_nodes,
                    chunk_count=len(changed_nodes),
                    file_count=0,  # We don't track file count for changed nodes
                    status="completed"
                )
                session.add(new_snapshot)
                session.commit()
                session.refresh(new_snapshot)
                return new_snapshot

    def delete_by_pr(self, project_id: int, pull_request_id: str) -> int:
        """Delete branch snapshot by project and pull request ID."""
        with self.session_factory() as session:
            deleted_count = (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.pull_request_id == pull_request_id
                )
                .delete()
            )
            session.commit()
            return deleted_count

