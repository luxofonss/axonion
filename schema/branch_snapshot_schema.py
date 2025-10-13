from pydantic import BaseModel

from schema.base_schema import ModelBaseInfo, FindBase
from util.schema import AllOptional


class BaseBranchSnapshot(BaseModel):
    project_id: int
    branch_name: str
    commit_hash: str
    chunk_count: int
    file_count: int
    status: str

    class Config:
        orm_mode: True


class BranchSnapshot(ModelBaseInfo, BaseBranchSnapshot, metaclass=AllOptional): ...


class FindBranchSnapshot(FindBase, BaseBranchSnapshot, metaclass=AllOptional): ...


class UpsertBranchSnapshot(BaseBranchSnapshot, metaclass=AllOptional): ...

