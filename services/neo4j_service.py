from typing import List, Dict, Tuple

from loguru import logger

from core.neo4j_db import Neo4jDB
from source_atlas.models.domain_models import CodeChunk, ChunkType
from schema.target_node_schema import TargetNode


def _escape_for_cypher(text):
    if text is None:
        return ""
    text = text.replace('\\', '\\\\')
    text = text.replace('"', '\\"')
    text = text.replace('\n', '\\n')
    text = text.replace('\t', '\\t')
    text = text.replace('\r', '\\r')
    return text


def _path_to_json(path):
    """
    Convert Neo4j Path object to structured JSON format.
    
    Args:
        path: Neo4j Path object
        
    Returns:
        dict: Structured path representation
    """
    if not path:
        return None
    
    nodes = list(path.nodes)
    relationships = list(path.relationships)
    
    # Build path structure
    path_data = {
        "start_node": dict(nodes[0]) if nodes else None,
        "end_node": dict(nodes[-1]) if nodes else None,
        "total_length": len(relationships),
        "nodes": [dict(node) for node in nodes],
        "relationships": [],
        "path_summary": []
    }
    
    # Build relationships with direction and type
    for i, rel in enumerate(relationships):
        start_node = nodes[i] if i < len(nodes) else None
        end_node = nodes[i + 1] if i + 1 < len(nodes) else None
        
        rel_data = {
            "relationship_type": rel.type,
            "start_node": dict(start_node) if start_node else None,
            "end_node": dict(end_node) if end_node else None,
            "properties": dict(rel) if rel else {}
        }
        path_data["relationships"].append(rel_data)
        
        # Build path summary for easier reading
        summary_item = {
            "step": i + 1,
            "from": {
                "class_name": start_node.get("class_name") if start_node else None,
                "method_name": start_node.get("method_name") if start_node else None,
                "node_type": list(start_node.labels)[0] if start_node and hasattr(start_node, 'labels') else None
            } if start_node else None,
            "relationship": rel.type,
            "to": {
                "class_name": end_node.get("class_name") if end_node else None,
                "method_name": end_node.get("method_name") if end_node else None,
                "node_type": list(end_node.labels)[0] if end_node and hasattr(end_node, 'labels') else None
            } if end_node else None
        }
        path_data["path_summary"].append(summary_item)
    
    return path_data


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

    def generate_cypher_from_chunks(self, chunks: List[CodeChunk], batch_size: int = 100, main_branch: str = None, base_branch: str = None, pull_request_id: str = None) -> List[Tuple[str, Dict]]:
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
                
                node_data_item = {
                    'node_type': node_type,
                    'file_path': file_path,
                    'class_name': class_name,
                    'content': content,
                    'ast_hash': chunk.ast_hash,
                    'project_id': str(chunk.project_id),
                    'branch': chunk.branch
                }
                
                # Add pull_request_id if branch is not main_branch
                if pull_request_id and chunk.branch != main_branch:
                    node_data_item['pull_request_id'] = pull_request_id
                
                node_data.append(node_data_item)
                
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
                    
                    method_node_data_item = {
                        'node_type': method_node_type,
                        'file_path': method_file_path,
                        'class_name': method_class_name,
                        'method_name': method_name,
                        'content': method_content,
                        'ast_hash': method.ast_hash,
                        'project_id': str(method.project_id),
                        'branch': method.branch
                    }
                    
                    # Add pull_request_id if branch is not main_branch
                    if pull_request_id and method.branch != main_branch:
                        method_node_data_item['pull_request_id'] = pull_request_id
                    
                    node_data.append(method_node_data_item)

            # Delete existing nodes - include pull_request_id in matching if provided
            if class_nodes_to_delete:
                if pull_request_id:
                    # Delete class nodes by branch and pull_request_id
                    delete_class_query = """
                    UNWIND $nodes AS node
                    MATCH (n {class_name: node.class_name, project_id: node.project_id, branch: node.branch, pull_request_id: node.pull_request_id})
                    WHERE n.method_name IS NULL
                    DETACH DELETE n
                    """
                    # Add pull_request_id to each node for deletion
                    for node in class_nodes_to_delete:
                        node['pull_request_id'] = pull_request_id
                else:
                    # Delete class nodes by branch only (for main branch or when no pull_request_id)
                    delete_class_query = """
                    UNWIND $nodes AS node
                    MATCH (n {class_name: node.class_name, project_id: node.project_id, branch: node.branch})
                    WHERE n.method_name IS NULL AND n.pull_request_id IS NULL
                    DETACH DELETE n
                    """
                all_queries.append((delete_class_query, {'nodes': class_nodes_to_delete}))
            
            if method_nodes_to_delete:
                if pull_request_id:
                    # Delete method nodes by branch and pull_request_id
                    delete_method_query = """
                    UNWIND $nodes AS node
                    MATCH (n {class_name: node.class_name, method_name: node.method_name, project_id: node.project_id, branch: node.branch, pull_request_id: node.pull_request_id})
                    WHERE n.method_name IS NOT NULL
                    DETACH DELETE n
                    """
                    # Add pull_request_id to each node for deletion
                    for node in method_nodes_to_delete:
                        node['pull_request_id'] = pull_request_id
                else:
                    # Delete method nodes by branch only (for main branch or when no pull_request_id)
                    delete_method_query = """
                    UNWIND $nodes AS node
                    MATCH (n {class_name: node.class_name, method_name: node.method_name, project_id: node.project_id, branch: node.branch})
                    WHERE n.method_name IS NOT NULL AND n.pull_request_id IS NULL
                    DETACH DELETE n
                    """
                all_queries.append((delete_method_query, {'nodes': method_nodes_to_delete}))
            
            # Create new nodes with smart duplicate checking
            if main_branch and base_branch:
                batch_query = """
                UNWIND $nodes AS node
                // Tìm node tương ứng trong main_branch
                OPTIONAL MATCH (main_node {
                    class_name: node.class_name,
                    project_id: node.project_id,
                    branch: $main_branch,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END
                })
                
                // Tìm node tương ứng trong base_branch
                OPTIONAL MATCH (base_node {
                    class_name: node.class_name,
                    project_id: node.project_id,
                    branch: $base_branch,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END
                })
                
                // Điều kiện lọc - chỉ tạo node mới khi thỏa mãn các điều kiện
                WITH node, main_node, base_node
                WHERE 
                    // ✅ TH1: Có base_branch, so sánh với base_branch bằng AST hash
                    (base_node IS NOT NULL AND node.ast_hash <> base_node.ast_hash)
                    OR
                    // ✅ TH2: Không có base_branch, so sánh với main_branch bằng AST hash
                    (base_node IS NULL AND main_node IS NOT NULL AND node.ast_hash <> main_node.ast_hash)
                    OR
                    // ✅ TH3: Node hoàn toàn mới - chưa tồn tại ở cả base và main
                    (base_node IS NULL AND main_node IS NULL)
                
                // Tạo node mới
                CALL apoc.create.node([node.node_type], {
                    file_path: node.file_path,
                    class_name: node.class_name,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END,
                    content: node.content,
                    ast_hash: node.ast_hash,
                    project_id: node.project_id,
                    branch: node.branch,
                    pull_request_id: CASE WHEN node.pull_request_id IS NOT NULL THEN node.pull_request_id ELSE null END
                }) YIELD node AS created_node
                RETURN count(created_node) AS created_count
                """
                all_queries.append((batch_query, {'nodes': node_data, 'main_branch': main_branch, 'base_branch': base_branch}))
            elif main_branch:
                # Fallback logic khi chỉ có main_branch
                batch_query = """
                UNWIND $nodes AS node
                OPTIONAL MATCH (main_node {
                    class_name: node.class_name,
                    project_id: node.project_id,
                    branch: $main_branch,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END
                })
                WHERE main_node IS NULL OR main_node.ast_hash <> node.ast_hash
                CALL apoc.create.node([node.node_type], {
                    file_path: node.file_path,
                    class_name: node.class_name,
                    method_name: CASE WHEN node.method_name IS NOT NULL THEN node.method_name ELSE null END,
                    content: node.content,
                    ast_hash: node.ast_hash,
                    project_id: node.project_id,
                    branch: node.branch,
                    pull_request_id: CASE WHEN node.pull_request_id IS NOT NULL THEN node.pull_request_id ELSE null END
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
                    ast_hash: node.ast_hash,
                    project_id: node.project_id,
                    branch: node.branch,
                    pull_request_id: CASE WHEN node.pull_request_id IS NOT NULL THEN node.pull_request_id ELSE null END
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
                    # Use base_branch first, then fallback to main_branch
                    call_query = """
                    UNWIND $relationships AS rel
                    MATCH (source {class_name: rel.source_class, method_name: rel.source_method, project_id: rel.project_id, branch: rel.branch})
                    OPTIONAL MATCH (target_base {method_name: rel.target_method, project_id: rel.project_id, branch: $base_branch})
                    OPTIONAL MATCH (target_main {method_name: rel.target_method, project_id: rel.project_id, branch: $main_branch})
                    WITH source, COALESCE(target_base, target_main) AS target
                    WHERE target IS NOT NULL
                    MERGE (source)-[:CALL]->(target)
                    """
                    all_queries.append((call_query, {'relationships': call_rels, 'base_branch': base_branch, 'main_branch': main_branch}))
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
                        OPTIONAL MATCH (source_base {class_name: rel.source_class, project_id: rel.project_id, branch: $base_branch})
                        WHERE source_base.method_name IS NULL
                        OPTIONAL MATCH (source_main {class_name: rel.source_class, project_id: rel.project_id, branch: $main_branch})
                        WHERE source_main.method_name IS NULL
                        WITH rel, COALESCE(source_base, source_main) AS source
                        WHERE source IS NOT NULL
                        OPTIONAL MATCH (target_base {class_name: rel.target_class, project_id: rel.project_id, branch: $base_branch})
                        WHERE target_base.method_name IS NULL
                        OPTIONAL MATCH (target_main {class_name: rel.target_class, project_id: rel.project_id, branch: $main_branch})
                        WHERE target_main.method_name IS NULL
                        WITH source, COALESCE(target_base, target_main) AS target
                        WHERE target IS NOT NULL
                        MERGE (source)-[:IMPLEMENT]->(target)
                        """
                        all_queries.append((class_implement_query, {'relationships': class_implement_rels, 'base_branch': base_branch, 'main_branch': main_branch}))
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
                        OPTIONAL MATCH (source_base {method_name: rel.source_method, project_id: rel.project_id, branch: $base_branch})
                        OPTIONAL MATCH (source_main {method_name: rel.source_method, project_id: rel.project_id, branch: $main_branch})
                        WITH rel, COALESCE(source_base, source_main) AS source
                        WHERE source IS NOT NULL
                        OPTIONAL MATCH (target_base {class_name: rel.target_class, method_name: rel.target_method, project_id: rel.project_id, branch: $base_branch})
                        OPTIONAL MATCH (target_main {class_name: rel.target_class, method_name: rel.target_method, project_id: rel.project_id, branch: $main_branch})
                        WITH source, COALESCE(target_base, target_main) AS target
                        WHERE target IS NOT NULL
                        MERGE (source)-[:IMPLEMENT]->(target)
                        """
                        all_queries.append((method_implement_query, {'relationships': method_implement_rels, 'base_branch': base_branch, 'main_branch': main_branch}))
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
                    OPTIONAL MATCH (target_base {class_name: rel.target_class, project_id: rel.project_id, branch: $base_branch})
                    WHERE target_base.method_name IS NULL
                    OPTIONAL MATCH (target_main {class_name: rel.target_class, project_id: rel.project_id, branch: $main_branch})
                    WHERE target_main.method_name IS NULL
                    WITH source, COALESCE(target_base, target_main) AS target
                    WHERE target IS NOT NULL
                    MERGE (source)-[:USE]->(target)
                    """
                    all_queries.append((use_query, {'relationships': use_rels, 'base_branch': base_branch, 'main_branch': main_branch}))
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

    def delete_pull_request_nodes(self, project_id: int, pull_request_id: str):
        """
        Delete all nodes belonging to a specific pull request.
        This is useful when a PR is closed or merged.
        """
        delete_query = """
        MATCH (n {project_id: $project_id, pull_request_id: $pull_request_id})
        DETACH DELETE n
        RETURN count(n) as deleted_count
        """
        
        try:
            with self.db.driver.session() as session:
                result = session.run(delete_query, {
                    'project_id': str(project_id),
                    'pull_request_id': pull_request_id
                })
                record = result.single()
                deleted_count = record['deleted_count'] if record else 0
                logger.info(f"Deleted {deleted_count} nodes for PR {pull_request_id} in project {project_id}")
                return deleted_count
        except Exception as e:
            logger.error(f"Failed to delete PR nodes: {str(e)}")
            raise e

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

    def import_code_chunks(self, chunks: List[CodeChunk], batch_size: int = 50, main_branch: str = None, base_branch: str = None, pull_request_id: str = None):
        self.create_indexes()
        queries_with_params = self.generate_cypher_from_chunks(chunks, batch_size, main_branch, base_branch, pull_request_id)
        self.execute_queries_batch(queries_with_params)

    def import_code_chunks_with_branch_relations(
        self, 
        chunks: List[CodeChunk], 
        project_id: int,
        current_branch: str,
        main_branch: str,
        base_branch: str = None,
        batch_size: int = 50,
        pull_request_id: str = None
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
        self.import_code_chunks(chunks, batch_size, main_branch, base_branch, pull_request_id)
        
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
        target_nodes: List[TargetNode],
        max_level: int = 20,
        min_level: int = 1,
        relationship_filter: str = "CALL>|<IMPLEMENT|<EXTEND|USE>|<BRANCH"
    ) -> List[Dict]:
        """
        Get all nodes related to a list of target nodes by traversing relationships.
        
        Args:
            target_nodes: List of TargetNode DTO objects
            max_level: Maximum traversal depth (default: 20)
            relationship_filter: Relationship filter for APOC (default: "CALL>|<IMPLEMENT|<EXTEND|USE>|<BRANCH")
            min_level: Minimum traversal depth (default: 1)
            
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
          relationshipFilter: "CALL>|<IMPLEMENT|<EXTEND|USE>|<BRANCH",
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
            [i IN range(0, size(rel_list) - 1) |
            CASE 
                WHEN type(rel_list[i]) = 'BRANCH'
                    AND node_list[i + 1].branch = 'develop'
                    AND node_list[i].branch = 'main'
                THEN node_list[i+1]
                ELSE null
            END
            ] AS exclude_nodes
        
        WITH endpoint, path, node_list, rel_list, exclude_nodes,
             [i IN range(0, size(rel_list)-1) |
                CASE
                  WHEN type(rel_list[i]) = 'CALL'
                       AND node_list[i+1].method_name IS NOT NULL
                  THEN node_list[i+1]
                  
                  WHEN type(rel_list[i]) IN ['IMPLEMENT', 'EXTEND', 'BRANCH']
                  THEN node_list[i+1]
                  
                  WHEN type(rel_list[i]) = 'USE'
                       AND node_list[i+1].method_name IS NULL
                  THEN node_list[i+1]
                  ELSE null
                END
             ] AS filtered_nodes
        
        RETURN endpoint, path,
               [node IN filtered_nodes WHERE node IS NOT NULL AND NOT node IN exclude_nodes] AS visited_nodes
        ORDER BY path
        """
        
        params = {
            'targets': [node.to_dict() for node in target_nodes],
            'relationship_filter': relationship_filter,
            'min_level': min_level,
            'max_level': max_level
        }
        
        with self.db.driver.session() as session:
            result = session.run(query, params)
            return [
                {
                    'endpoint': dict(record['endpoint']),
                    'path': _path_to_json(record['path']),  # Convert Neo4j Path to structured JSON
                    'visited_nodes': [dict(node) for node in record['visited_nodes']]
                }
                for record in result
            ]

    def get_left_target_nodes(
        self,
        target_nodes: List[TargetNode],
        max_level: int = 20,
        min_level: int = 1 # at least one relation with other nodes
    ) -> List[Dict]:
        """
        Get all left target nodes (incoming relationships) for a list of target nodes.
        
        This traverses relationships in reverse to find nodes that call, implement, extend, or use the target nodes.
        
        Args:
            target_nodes: List of TargetNode DTO objects
            max_level: Maximum traversal depth (default: 20)
            min_level: Minimum traversal depth (default: 1)
            
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
          (
            (t.method_name IS NULL AND endpoint.method_name IS NULL)
            OR (t.method_name = endpoint.method_name)
          )
        )
        
        CALL apoc.path.expandConfig(endpoint, {
          relationshipFilter: '<CALL|IMPLEMENT>|EXTEND>|<USE',
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
            'targets': [node.to_dict() for node in target_nodes],
            'min_level': min_level,
            'max_level': max_level
        }
        
        with self.db.driver.session() as session:
            result = session.run(query, params)
            return [
                {
                    'endpoint': dict(record['endpoint']),
                    'path': _path_to_json(record['path']),  # Convert Neo4j Path to structured JSON
                    'visited_nodes': [dict(node) for node in record['visited_nodes']]
                }
                for record in result
            ]

    def get_nodes_by_condition(
    self,
    project_id: int,
    branch: str,
    pull_request_id: str = None,
    node_types: List[str] = None,
    class_name: str = None,
    method_name: str = None
    ) -> List[Dict]:
        """
        Get all nodes by various conditions.
        
        Args:
            project_id: Project ID to filter by
            branch: Branch name to filter by
            pull_request_id: Optional pull request ID to filter by
            node_types: Optional list of node types to filter by (e.g., ['ClassNode', 'MethodNode', 'EndpointNode', 'ConfigurationNode'])
            class_name: Optional class name to filter by (supports partial matching with CONTAINS)
            method_name: Optional method name to filter by (supports partial matching with CONTAINS)
            
        Returns:
            List of dictionaries containing node properties
        """
        where_conditions = [
            "n.project_id = $project_id",
            "n.branch = $branch"
        ]
        
        params = {
            'project_id': str(project_id),
            'branch': branch
        }
        
        # Add pull_request_id filter if provided
        if pull_request_id is not None:
            where_conditions.append("n.pull_request_id = $pull_request_id")
            params['pull_request_id'] = pull_request_id
        else:
            where_conditions.append("n.pull_request_id IS NULL")
        
        # Add node type filter if provided
        if node_types:
            # Sử dụng ANY để check label thay vì labels(n)[0]
            node_type_conditions = " OR ".join([f"$node_type_{i} IN labels(n)" for i in range(len(node_types))])
            where_conditions.append(f"({node_type_conditions})")
            for i, node_type in enumerate(node_types):
                params[f'node_type_{i}'] = node_type
        
        # Add class_name filter if provided (chỉ filter khi property tồn tại)
        if class_name:
            where_conditions.append("(n.class_name IS NOT NULL AND n.class_name CONTAINS $class_name)")
            params['class_name'] = class_name
        
        # Add method_name filter if provided (chỉ filter khi property tồn tại)
        if method_name:
            where_conditions.append("(n.method_name IS NOT NULL AND n.method_name CONTAINS $method_name)")
            params['method_name'] = method_name
        
        query = f"""
        MATCH (n)
        WHERE {' AND '.join(where_conditions)}
        RETURN n
        ORDER BY 
            CASE WHEN n.class_name IS NOT NULL THEN n.class_name ELSE '' END,
            CASE WHEN n.method_name IS NOT NULL THEN n.method_name ELSE '' END
        """
        
        try:
            with self.db.driver.session() as session:
                result = session.run(query, params)
                nodes = []
                for record in result:
                    node_data = record['n']
                    node = dict(node_data)
                    
                    # Add node type information - lấy label đầu tiên
                    labels = list(node_data.labels) if hasattr(node_data, 'labels') and node_data.labels else []
                    node['node_type'] = labels[0] if labels else None
                    node['all_labels'] = labels  # Thêm tất cả labels để debug
                    
                    nodes.append(node)
                
                logger.info(
                    f"Retrieved {len(nodes)} nodes for project_id={project_id}, "
                    f"branch={branch}, pull_request_id={pull_request_id}, "
                    f"node_types={node_types}, class_name={class_name}, method_name={method_name}"
                )
                return nodes
                
        except Exception as e:
            logger.error(f"Failed to get nodes by condition: {str(e)}", exc_info=True)
            raise