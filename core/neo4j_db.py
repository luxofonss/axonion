from neo4j import GraphDatabase
from core.config import configs


class Neo4jDB:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            configs.APP_NEO4J_URL,
            auth=(configs.APP_NEO4J_USER, configs.APP_NEO4J_PASSWORD),
            max_connection_lifetime=configs.NEO4J_MAX_CONNECTION_LIFETIME,
            max_connection_pool_size=configs.NEO4J_MAX_CONNECTION_POOL_SIZE,
            connection_timeout=configs.NEO4J_CONNECTION_TIMEOUT
        )

    def close(self):
        self.driver.close()


