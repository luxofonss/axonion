from datetime import datetime

from sqlmodel import Field, SQLModel
from sqlalchemy import DateTime, func


class BaseModel(SQLModel):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"server_default": func.now()}
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()}
)