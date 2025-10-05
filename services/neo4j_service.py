from typing import List, Dict, Tuple

from loguru import logger

from core.neo4j_db import Neo4jDB
from source_atlas.models.domain_models import CodeChunk, ChunkType


def _escape_for_cypher(text):
    if text is None:
        return ""
    text = text.replace('\\', '\\\\')
    text = text.replace('"', '\\"')
    text = text.replace('\n', '\\n')
    text = text.replace('\t', '\\t')
    text = text.replace('\r', '\\r')
    return text


class Neo4jService:
    def __init__(self, db: Neo4jDB | None = None):
        self.db = db or Neo4jDB()

    def create_indexes(self):
        indexes = [
            # Composite indexes for nodes
            "CREATE INDEX IF NOT EXISTS FOR (n:EndpointNode) ON (n.class_name, n.method_name, n.project_id, n.branch)",
            "CREATE INDEX IF NOT EXISTS FOR (n:MethodNode) ON (n.class_name, n.method_name, n.project_id, n.branch)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ClassNode) ON (n.class_name, n.project_id, n.branch)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ConfigurationNode) ON (n.class_name, n.project_id, n.branch)",
            # Project and branch indexes
            "CREATE INDEX IF NOT EXISTS FOR (n:EndpointNode) ON (n.project_id, n.branch)",
            "CREATE INDEX IF NOT EXISTS FOR (n:MethodNode) ON (n.project_id, n.branch)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ClassNode) ON (n.project_id, n.branch)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ConfigurationNode) ON (n.project_id, n.branch)",
        ]
        with self.db.driver.session() as session:
            for index_query in indexes:
                try:
                    session.run(index_query)
                except Exception as e:
                    logger.error(f"Error creating index: {str(e)}")

    def generate_cypher_from_chunks(self, chunks: List[CodeChunk], batch_size: int = 100, main_branch: str = None) -> List[Tuple[str, Dict]]:
        all_queries = []
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i+batch_size]
            node_data = []
            class_nodes_to_delete = []
            method_nodes_to_delete = []
            
            for chunk in batch:
                file_path = chunk.file_path
                class_name = chunk.full_class_name
                content = _escape_for_cypher(chunk.content)
                node_type = "ConfigurationNode" if chunk.type == ChunkType.CONFIGURATION else "ClassNode"
                
                # Collect class node for deletion
                class_nodes_to_delete.append({
                    'class_name': class_name,
                    'project_id': str(chunk.project_id),
                    'branch': chunk.branch
                })
                
                node_data.append({
                    'node_type': node_type,
                    'file_path': file_path,
                    'class_name': class_name,
                    'content': content,
                    'project_id': str(chunk.project_id),
                    'branch': chunk.branch
                })
                
                for method in chunk.methods:
                    method_file_path = chunk.file_path
                    method_class_name = chunk.full_class_name
                    method_name = method.name
                    method_body = _escape_for_cypher(method.body)
                    method_field_access = str(method.field_access)
                    method_content = method_body + " " + method_field_access
                    if method.type == ChunkType.CONFIGURATION:
                        method_node_type = "ConfigurationNode"
                    elif method.type == ChunkType.ENDPOINT:
                        method_node_type = "EndpointNode"
                    else:
                        method_node_type = "MethodNode"
                    
                    # Collect method node for deletion
                    method_nodes_to_delete.append({
                        'class_name': method_class_name,
                        'method_name': method_name,
                        'project_id': str(method.project_id),
                        'branch': method.branch
                    })
                    
                    node_data.append({
                        'node_type': method_node_type,
                        'file_path': method_file_path,
                        'class_name': method_class_name,
                        'method_name': method_name,
                        'content': method_content,
                        'project_id': str(method.project_id),
                        'branch': method.branch
                    })

            # Delete existing class nodes first
            if class_nodes_to_delete:
                delete_class_query = """
                UNWIND $nodes AS node
                MATCH (n {class_name: node.class_name, project_id: node.project_id, branch: node.branch})
                WHERE n.method_name IS NULL
                DETACH DELETE n
                """
                all_queries.append((delete_class_query, {'nodes': class_nodes_to_delete}))
            
            # Delete existing method nodes
            if method_nodes_to_delete:
                delete_method_query = """
                UNWIND $nodes AS node
                MATCH (n {class_name: node.class_name, method_name: node.method_name, project_id: node.project_id, branch: node.branch})
                WHERE n.method_name IS NOT NULL
                DETACH DELETE n
                """
                all_queries.append((delete_method_query, {'nodes': method_nodes_to_delete}))
            
            # Create new nodes with duplicate checking against main_branch
            if main_branch:
                batch_query = """
                UNWIND $nodes AS node
                OPTIONAL MATCH (main_node {
                    class_name: node.class_name,
                    project_id: node.project_id,
                    branch: $main_branch,
                    content: node.content,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END
                })
                WHERE main_node IS NULL
                CALL apoc.create.node([node.node_type], {
                    file_path: node.file_path,
                    class_name: node.class_name,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END,
                    content: node.content,
                    project_id: node.project_id,
                    branch: node.branch
                }) YIELD node AS created_node
                RETURN count(created_node)
                """
                all_queries.append((batch_query, {'nodes': node_data, 'main_branch': main_branch}))
            else:
                batch_query = """
                UNWIND $nodes AS node
                CALL apoc.create.node([node.node_type], {
                    file_path: node.file_path,
                    class_name: node.class_name,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END,
                    content: node.content,
                    project_id: node.project_id,
                    branch: node.branch
                }) YIELD node AS created_node
                RETURN count(created_node)
                """
                all_queries.append((batch_query, {'nodes': node_data}))

        # Relationships
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i+batch_size]
            call_rels = []
            implement_rels = []
            use_rels = []
            for chunk in batch:
                chunk_class_name = chunk.full_class_name
                chunk_project_id = str(chunk.project_id)
                chunk_branch = chunk.branch
                for impl in chunk.implements:
                    implement_rels.append({
                        'source_class': impl,
                        'target_class': chunk_class_name,
                        'project_id': chunk_project_id,
                        'branch': chunk_branch
                    })
                for method in chunk.methods:
                    method_name = method.name
                    for call in method.method_calls:
                        call_name = call.name
                        if call_name:
                            call_rels.append({
                                'source_class': chunk_class_name,
                                'source_method': method_name,
                                'target_method': call_name,
                                'project_id': chunk_project_id,
                                'branch': chunk_branch
                            })
                    for inheritance in method.inheritance_info:
                        if inheritance:
                            implement_rels.append({
                                'source_method': inheritance,
                                'target_class': chunk_class_name,
                                'target_method': method_name,
                                'project_id': chunk_project_id,
                                'branch': chunk_branch
                            })
                    for used_type in method.used_types:
                        if used_type:
                            use_rels.append({
                                'source_class': chunk_class_name,
                                'source_method': method_name,
                                'target_class': used_type,
                                'project_id': chunk_project_id,
                                'branch': chunk_branch
                            })

            if call_rels:
                if main_branch:
                    # If main_branch is provided, use fallback logic
                    call_query = """
                    UNWIND $relationships AS rel
                    MATCH (source {class_name: rel.source_class, method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                    OPTIONAL MATCH (target_current {method_name: rel.target_method, project_id: rel.project_id, branch: rel.branch})
                    OPTIONAL MATCH (target_main {method_name: rel.target_method, project_id: rel.project_id, branch: $main_branch})
                    WITH source, COALESCE(target_current, target_main) AS target
                    WHERE target IS NOT NULL
                    MERGE (source)-[:CALL]->(target)
                    """
                    all_queries.append((call_query, {'relationships': call_rels, 'main_branch': main_branch}))
                else:
                    call_query = """
                    UNWIND $relationships AS rel
                    MATCH (source {class_name: rel.source_class, method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                    MATCH (target {method_name: rel.target_method, project_id: rel.project_id, branch: rel.branch})
                    MERGE (source)-[:CALL]->(target)
                    """
                    all_queries.append((call_query, {'relationships': call_rels}))

            if implement_rels:
                class_implement_rels = [rel for rel in implement_rels if 'source_method' not in rel]
                if class_implement_rels:
                    if main_branch:
                        class_implement_query = """
                        UNWIND $relationships AS rel
                        OPTIONAL MATCH (source_current {class_name: rel.source_class, project_id: rel.project_id, branch: rel.branch})
                        WHERE source_current.method_name IS NULL
                        OPTIONAL MATCH (source_main {class_name: rel.source_class, project_id: rel.project_id, branch: $main_branch})
                        WHERE source_main.method_name IS NULL
                        WITH rel, COALESCE(source_current, source_main) AS source
                        WHERE source IS NOT NULL
                        OPTIONAL MATCH (target_current {class_name: rel.target_class, project_id: rel.project_id, branch: rel.branch})
                        WHERE target_current.method_name IS NULL
                        OPTIONAL MATCH (target_main {class_name: rel.target_class, project_id: rel.project_id, branch: $main_branch})
                        WHERE target_main.method_name IS NULL
                        WITH source, COALESCE(target_current, target_main) AS target
                        WHERE target IS NOT NULL
                        MERGE (source)-[:IMPLEMENT]->(target)
                        """
                        all_queries.append((class_implement_query, {'relationships': class_implement_rels, 'main_branch': main_branch}))
                    else:
                        class_implement_query = """
                        UNWIND $relationships AS rel
                        MATCH (source {class_name: rel.source_class, project_id: rel.project_id, branch: rel.branch})
                        WHERE source.method_name IS NULL
                        MATCH (target {class_name: rel.target_class, project_id: rel.project_id, branch: rel.branch})
                        WHERE target.method_name IS NULL
                        MERGE (source)-[:IMPLEMENT]->(target)
                        """
                        all_queries.append((class_implement_query, {'relationships': class_implement_rels}))

                method_implement_rels = [rel for rel in implement_rels if 'source_method' in rel]
                if method_implement_rels:
                    if main_branch:
                        method_implement_query = """
                        UNWIND $relationships AS rel
                        OPTIONAL MATCH (source_current {method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                        OPTIONAL MATCH (source_main {method_name: rel.source_method, project_id: rel.project_id, branch: $main_branch})
                        WITH rel, COALESCE(source_current, source_main) AS source
                        WHERE source IS NOT NULL
                        OPTIONAL MATCH (target_current {class_name: rel.target_class, method_name: rel.target_method, project_id: rel.project_id, branch: rel.branch})
                        OPTIONAL MATCH (target_main {class_name: rel.target_class, method_name: rel.target_method, project_id: rel.project_id, branch: $main_branch})
                        WITH source, COALESCE(target_current, target_main) AS target
                        WHERE target IS NOT NULL
                        MERGE (source)-[:IMPLEMENT]->(target)
                        """
                        all_queries.append((method_implement_query, {'relationships': method_implement_rels, 'main_branch': main_branch}))
                    else:
                        method_implement_query = """
                        UNWIND $relationships AS rel
                        MATCH (source {method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                        MATCH (target {class_name: rel.target_class, method_name: rel.target_method, project_id: rel.project_id, branch: rel.branch})
                        MERGE (source)-[:IMPLEMENT]->(target)
                        """
                        all_queries.append((method_implement_query, {'relationships': method_implement_rels}))

            if use_rels:
                if main_branch:
                    use_query = """
                    UNWIND $relationships AS rel
                    MATCH (source {class_name: rel.source_class, method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                    OPTIONAL MATCH (target_current {class_name: rel.target_class, project_id: rel.project_id, branch: rel.branch})
                    WHERE target_current.method_name IS NULL
                    OPTIONAL MATCH (target_main {class_name: rel.target_class, project_id: rel.project_id, branch: $main_branch})
                    WHERE target_main.method_name IS NULL
                    WITH source, COALESCE(target_current, target_main) AS target
                    WHERE target IS NOT NULL
                    MERGE (source)-[:USE]->(target)
                    """
                    all_queries.append((use_query, {'relationships': use_rels, 'main_branch': main_branch}))
                else:
                    use_query = """
                    UNWIND $relationships AS rel
                    MATCH (source {class_name: rel.source_class, method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                    MATCH (target {class_name: rel.target_class, project_id: rel.project_id, branch: rel.branch})
                    WHERE target.method_name IS NULL
                    MERGE (source)-[:USE]->(target)
                    """
                    all_queries.append((use_query, {'relationships': use_rels}))

        return all_queries

    def execute_queries_batch(self, queries_with_params: List[Tuple[str, Dict]], max_retries: int = 3):
        with self.db.driver.session() as session:
            for i, (query, params) in enumerate(queries_with_params):
                retry_count = 0
                while retry_count < max_retries:
                    try:
                        result = session.run(query, params)
                        result.consume()
                        break
                    except Exception as e:
                        retry_count += 1
                        if retry_count >= max_retries:
                            raise e

    def import_code_chunks(self, chunks: List[CodeChunk], batch_size: int = 50, main_branch: str = None):
        self.create_indexes()
        queries_with_params = self.generate_cypher_from_chunks(chunks, batch_size, main_branch)
        self.execute_queries_batch(queries_with_params)

    def import_code_chunks_with_branch_relations(
        self, 
        chunks: List[CodeChunk], 
        project_id: int,
        current_branch: str,
        main_branch: str,
        batch_size: int = 50
    ):
        """
        Import code chunks and create BRANCH relationships to corresponding nodes in main branch.
        
        This links nodes representing the same entity between current branch and main branch:
        - Method X in develop -> BRANCH -> Method X in main
        - Class Y in feature/auth -> BRANCH -> Class Y in main
        
        Args:
            chunks: List of code chunks to import
            project_id: Project ID
            current_branch: Branch name being imported
            main_branch: Main branch name to create relationships with
            batch_size: Batch size for queries
        """
        # Step 1: Import the chunks normally
        self.import_code_chunks(chunks, batch_size, main_branch)
        
        if not chunks:
            logger.info("No chunks to create branch relationships for")
            return
        
        logger.info(
            f"Creating BRANCH relationships for {len(chunks)} chunks "
            f"(project_id={project_id}, current_branch={current_branch}, main_branch={main_branch})"
        )
        
        # Step 2: Create BRANCH relationships between current branch and main branch nodes
        branch_rel_queries = self._generate_branch_relationships(
            chunks, project_id, current_branch, main_branch, batch_size
        )
        
        if branch_rel_queries:
            self.execute_queries_batch(branch_rel_queries)
            logger.info(f"Created BRANCH relationships from '{current_branch}' to '{main_branch}'")
    
    def _generate_branch_relationships(
        self,
        chunks: List[CodeChunk],
        project_id: int,
        current_branch: str,
        main_branch: str,
        batch_size: int = 100
    ) -> List[Tuple[str, Dict]]:
        """
        Generate queries to create BRANCH relationships between current branch nodes and main branch nodes.
        
        Links corresponding nodes (same class/method name, same project) from current branch to main branch.
        Uses existing 'branch' property of nodes to identify and link them.
        """
        all_queries = []
        
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i+batch_size]
            
            # Collect all class and method identifiers
            class_nodes = []
            method_nodes = []
            
            for chunk in batch:
                # Class-level node
                class_nodes.append({
                    'class_name': chunk.full_class_name,
                    'project_id': str(project_id),
                    'current_branch': current_branch,
                    'main_branch': main_branch
                })
                
                # Method-level nodes
                for method in chunk.methods:
                    method_nodes.append({
                        'class_name': chunk.full_class_name,
                        'method_name': method.name,
                        'project_id': str(project_id),
                        'current_branch': current_branch,
                        'main_branch': main_branch
                    })
            
            # Create BRANCH relationships for class nodes
            if class_nodes:
                class_branch_query = """
                UNWIND $nodes AS node
                MATCH (current:ClassNode {
                    class_name: node.class_name, 
                    project_id: node.project_id, 
                    branch: node.current_branch
                })
                WHERE current.method_name IS NULL
                MATCH (main:ClassNode {
                    class_name: node.class_name, 
                    project_id: node.project_id,
                    branch: node.main_branch
                })
                WHERE main.method_name IS NULL
                MERGE (current)-[:BRANCH]->(main)
                """
                all_queries.append((class_branch_query, {'nodes': class_nodes}))
            
            # Create BRANCH relationships for method nodes (works for MethodNode, EndpointNode, ConfigurationNode)
            if method_nodes:
                method_branch_query = """
                UNWIND $nodes AS node
                MATCH (current {
                    class_name: node.class_name,
                    method_name: node.method_name,
                    project_id: node.project_id,
                    branch: node.current_branch
                })
                WHERE current.method_name IS NOT NULL
                MATCH (main {
                    class_name: node.class_name,
                    method_name: node.method_name,
                    project_id: node.project_id,
                    branch: node.main_branch
                })
                WHERE main.method_name IS NOT NULL
                MERGE (current)-[:BRANCH]->(main)
                """
                all_queries.append((method_branch_query, {'nodes': method_nodes}))
        
        return all_queries

    def get_related_nodes(
        self,
        target_nodes: List[Dict[str, str]],
        max_level: int = 20,
        relationship_filter: str = "CALL>|<IMPLEMENT|<EXTEND|USE>",
        min_level: int = 0
    ) -> List[Dict]:
        """
        Get all nodes related to a list of target nodes by traversing relationships.
        
        Args:
            target_nodes: List of target nodes, each dict should have:
                - class_name: str
                - method_name: str | None
                - branch: str
                - project_id: str
            max_level: Maximum traversal depth (default: 20)
            relationship_filter: Relationship filter for APOC (default: "CALL>|<IMPLEMENT|<EXTEND|USE>")
            min_level: Minimum traversal depth (default: 0)
            
        Returns:
            List of dicts containing:
                - endpoint: The starting node
                - path: The full path traversed
                - visited_nodes: List of filtered nodes based on relationship rules
        """
        query = """
        WITH $targets AS targets
        
        MATCH (endpoint)
        WHERE endpoint.project_id = $targets[0].project_id
        AND any(t IN targets WHERE
          t.class_name = endpoint.class_name AND
          t.branch = endpoint.branch AND
          (
            (t.method_name IS NULL AND endpoint.method_name IS NULL)
            OR (t.method_name = endpoint.method_name)
          )
        )
        
        CALL apoc.path.expandConfig(endpoint, {
          relationshipFilter: $relationship_filter,
          minLevel: $min_level,
          maxLevel: $max_level,
          bfs: true,
          uniqueness: "NODE_GLOBAL",
          filterStartNode: false
        }) YIELD path
        
        WITH endpoint, path,
             nodes(path) AS node_list,
             relationships(path) AS rel_list
        
        WITH endpoint, path, node_list, rel_list,
             [i IN range(0, size(rel_list)-1) |
                CASE
                  WHEN type(rel_list[i]) = 'CALL'
                       AND node_list[i+1].method_name IS NOT NULL
                  THEN node_list[i+1]
                  
                  WHEN type(rel_list[i]) IN ['IMPLEMENT', 'EXTEND']
                  THEN node_list[i+1]
                  
                  WHEN type(rel_list[i]) = 'USE'
                       AND node_list[i+1].method_name IS NULL
                  THEN node_list[i+1]
                  ELSE null
                END
             ] AS filtered_nodes
        
        RETURN endpoint, path,
               [node IN filtered_nodes WHERE node IS NOT NULL] AS visited_nodes
        ORDER BY path
        """
        
        params = {
            'targets': target_nodes,
            'relationship_filter': relationship_filter,
            'min_level': min_level,
            'max_level': max_level
        }
        
        with self.db.driver.session() as session:
            result = session.run(query, params)
            return [
                {
                    'endpoint': dict(record['endpoint']),
                    'path': record['path'],
                    'visited_nodes': [dict(node) for node in record['visited_nodes']]
                }
                for record in result
            ]

    def get_left_target_nodes(
        self,
        target_nodes: List[Dict[str, str]],
        max_level: int = 20,
        relationship_filter: str = "<CALL|IMPLEMENT>|EXTEND>|<USE",
        min_level: int = 0
    ) -> List[Dict]:
        """
        Get all left target nodes (incoming relationships) for a list of target nodes.
        
        This traverses relationships in reverse to find nodes that call, implement, extend, or use the target nodes.
        
        Args:
            target_nodes: List of target nodes, each dict should have:
                - class_name: str
                - method_name: str | None
                - branch: str
                - project_id: str
            max_level: Maximum traversal depth (default: 20)
            relationship_filter: Relationship filter for APOC (default: "<CALL|IMPLEMENT>|EXTEND>|<USE")
            min_level: Minimum traversal depth (default: 0)
            
        Returns:
            List of dicts containing:
                - endpoint: The starting node
                - path: The full path traversed
                - visited_nodes: List of filtered nodes based on relationship rules
        """
        query = """
        WITH $targets AS targets
        
        MATCH (endpoint)
        WHERE endpoint.project_id = $targets[0].project_id
        AND any(t IN targets WHERE
          t.class_name = endpoint.class_name AND
          t.branch = endpoint.branch AND
          (
            (t.method_name IS NULL AND endpoint.method_name IS NULL)
            OR (t.method_name = endpoint.method_name)
          )
        )
        
        CALL apoc.path.expandConfig(endpoint, {
          relationshipFilter: $relationship_filter,
          minLevel: $min_level,
          maxLevel: $max_level,
          bfs: true,
          uniqueness: "NODE_GLOBAL",
          filterStartNode: false
        }) YIELD path
        
        WITH endpoint, path,
             nodes(path) AS node_list,
             relationships(path) AS rel_list
        
        WITH endpoint, path, node_list, rel_list,
             [i IN range(0, size(rel_list)-1) |
                CASE
                  WHEN type(rel_list[i]) = 'CALL'
                       AND node_list[i+1].method_name IS NOT NULL
                  THEN node_list[i+1]
                  
                  WHEN type(rel_list[i]) IN ['IMPLEMENT', 'EXTEND']
                  THEN node_list[i+1]
                  
                  WHEN type(rel_list[i]) = 'USE'
                       AND node_list[i+1].method_name IS NULL
                  THEN node_list[i+1]
                  ELSE null
                END
             ] AS filtered_nodes
        
        RETURN endpoint, path,
               [node IN filtered_nodes WHERE node IS NOT NULL] AS visited_nodes
        ORDER BY path
        """
        
        params = {
            'targets': target_nodes,
            'relationship_filter': relationship_filter,
            'min_level': min_level,
            'max_level': max_level
        }
        
        with self.db.driver.session() as session:
            result = session.run(query, params)
            return [
                {
                    'endpoint': dict(record['endpoint']),
                    'path': record['path'],
                    'visited_nodes': [dict(node) for node in record['visited_nodes']]
                }
                for record in result
            ]
