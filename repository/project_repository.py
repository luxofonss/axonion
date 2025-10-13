from contextlib import AbstractContextManager
from typing import Callable

from sqlalchemy.orm import Session

from models.project import Project
from repository.base_repository import BaseRepository


class ProjectRepository(BaseRepository):
    def __init__(self, session_factory: Callable[..., AbstractContextManager[Session]]):
        self.session_factory = session_factory
        super().__init__(session_factory, Project)

    def read_by_git_url(self, git_url: str):
        with self.session_factory() as session:
            return session.query(Project).filter(Project.git_url == git_url).first()