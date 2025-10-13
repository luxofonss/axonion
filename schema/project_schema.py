from datetime import datetime

from pydantic import BaseModel

from schema.base_schema import ModelBaseInfo, FindBase
from util.schema import AllOptional


class BaseProject(BaseModel):
    name: str
    description: str
    git_url: str
    main_branch: str
    source: str
    local_path: str
    latest_commit: str
    status: int
    last_indexed: datetime
    languages: str  # JSON string, required
    valid_extensions: str | None = "[]"  # JSON string

    class Config:
        orm_mode: True


class Project(ModelBaseInfo, BaseProject, metaclass=AllOptional): ...

class FindProject(FindBase, BaseProject, metaclass=AllOptional): ...

class UpsertProject(BaseProject, metaclass=AllOptional): ...


class IndexProjectRequest(BaseModel):
    branch: str | None = None
    language: str | None = None
    target_files: list[str] | None = None  # Optional list of files to parse (can be partial paths)
    parse_all: bool = True  # If True, parse all files; if False, only parse target_files