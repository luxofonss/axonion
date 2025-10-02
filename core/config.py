import os
from typing import List

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()


class Configs(BaseSettings):

    PROJECT_ROOT: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "axonion")

    API: str = "/api"

    # data dir
    DATA_DIR: str = os.getenv(
        "DATA_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )

    # date
    DATETIME_FORMAT: str = "%Y-%m-%dT%H:%M:%S"
    DATE_FORMAT: str = "%Y-%m-%d"

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = ["*"]

    # database
    DB: str = os.getenv("DB", "postgresql")
    DB_USER: str = os.getenv("DB_USER", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "admin")
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: str = os.getenv("DB_PORT", "5432")
    DB_ENGINE: str = os.getenv("DB_ENGINE", "postgresql")
    DB_DATABASE: str = os.getenv("DB_DATABASE", "axonion")

    DATABASE_URI_FORMAT: str = "{db_engine}://{user}:{password}@{host}:{port}/{database}"

    DATABASE_URI: str = "{db_engine}://{user}:{password}@{host}:{port}/{database}".format(
        db_engine=DB_ENGINE,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_DATABASE,
    )

    # GitHub Webhook Configuration
    GITHUB_APP_ID: str = os.getenv("GITHUB_APP_ID", "")
    GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "supersecret123")
    GITHUB_PRIVATE_KEY_PATH: str = os.getenv("GITHUB_PRIVATE_KEY_PATH", "private-key.pem")

    # Neo4j
    APP_NEO4J_URL: str = os.getenv("APP_NEO4J_URL", "bolt://localhost:7687")
    APP_NEO4J_USER: str = os.getenv("APP_NEO4J_USER", "neo4j")
    APP_NEO4J_PASSWORD: str = os.getenv("APP_NEO4J_PASSWORD", "your_password")
    APP_NEO4J_DATABASE: str = os.getenv("APP_NEO4J_DATABASE", "neo4j")
    NEO4J_MAX_CONNECTION_LIFETIME: int = int(os.getenv("NEO4J_MAX_CONNECTION_LIFETIME", "30"))
    NEO4J_MAX_CONNECTION_POOL_SIZE: int = int(os.getenv("NEO4J_MAX_CONNECTION_POOL_SIZE", "50"))
    NEO4J_CONNECTION_TIMEOUT: float = float(os.getenv("NEO4J_CONNECTION_TIMEOUT", "30.0"))

    class Config:
        case_sensitive = True


configs = Configs()
