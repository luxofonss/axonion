from sqlmodel import Field

from models.base_model import BaseModel


class Project(BaseModel, table=True):
    name: str = Field()
    description: str = Field()
    git_url: str = Field(unique=True)
    main_branch: str = Field(default="main")
    source: str = Field()
    local_path: str = Field()
    latest_commit: str = Field()
    status: int = Field(default=1)
    last_indexed: str = Field(unique=True)
    languages: str = Field()
    valid_extensions: str = Field(default="[]")
