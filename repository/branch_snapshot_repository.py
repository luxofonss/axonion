from contextlib import AbstractContextManager
from typing import Callable, Optional

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

