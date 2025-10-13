from sqlmodel import Field, UniqueConstraint

from models.base_model import BaseModel


class BranchSnapshot(BaseModel, table=True):
    """Track parsed branches with their commit hashes to avoid redundant parsing."""
    
    __tablename__ = "branch_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "branch_name", "commit_hash", name="unique_branch_commit"),
    )
    
    project_id: int = Field(foreign_key="project.id", index=True)
    branch_name: str = Field(index=True)
    commit_hash: str = Field(index=True)
    chunk_count: int = Field(default=0)
    file_count: int = Field(default=0)
    status: str = Field(default="completed")  # "parsing", "completed", "failed"

