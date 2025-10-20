from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger

from repository.branch_snapshot_repository import BranchSnapshotRepository
from schema.branch_snapshot_schema import UpsertBranchSnapshot
from services.base_service import BaseService
from services.git_service import GitService
from services.neo4j_service import Neo4jService
from source_atlas.models.domain_models import CodeChunk


class BranchSnapshotService(BaseService):
    def __init__(
        self,
        branch_snapshot_repository: BranchSnapshotRepository,
        git_service: GitService,
        neo4j_service: Neo4jService,
    ):
        self.branch_snapshot_repository = branch_snapshot_repository
        self.git_service = git_service
        self.neo4j_service = neo4j_service
        self._project_service = None  # Will be injected to avoid circular dependency
        super().__init__(branch_snapshot_repository)
    
    def set_project_service(self, project_service):
        """Inject project_service to avoid circular dependency"""
        self._project_service = project_service

    def parse_branch_if_needed(
        self,
        project_id: int,
        repo_path: str,
        branch_name: str,
        main_branch: str,
        base_branch: str = None,
        language: str = "java",
        pull_request_id: str = None
    ) -> dict:
        
        # Step 1: Checkout the branch and get current commit hash
        logger.info(f"Checking branch '{branch_name}' for project {project_id}")
        self.git_service.pull_branch(repo_path, branch_name)
        current_commit = self.git_service.get_current_commit_hash(repo_path, branch_name)
        
        # Step 2: Check if this branch at this commit was already parsed
        existing_snapshot = self.branch_snapshot_repository.find_by_branch_and_commit(
            project_id, branch_name, current_commit
        )
        
        if existing_snapshot:
            logger.info(
                f"Branch '{branch_name}' at commit {current_commit[:8]} already parsed "
                f"({existing_snapshot.chunk_count} chunks). Skipping."
            )
            return {
                "was_cached": True,
                "commit_hash": current_commit,
                "chunk_count": existing_snapshot.chunk_count,
                "file_count": existing_snapshot.file_count,
                "chunks": []
            }
        
        # Step 2.1: Always clean up old nodes and snapshots before parsing to ensure fresh data
        logger.info(f"Cleaning up old nodes and snapshots for branch '{branch_name}' before parsing...")
        

        # Step 3: Handle main branch vs feature branch differently
        if branch_name == main_branch:
            # Parsing the main branch itself - use project_service for full indexing
            logger.info(f"Branch '{branch_name}' is the main branch, using full indexing")
            
            if not self._project_service:
                raise RuntimeError("ProjectService not injected. Call set_project_service() first.")
            
            # Use project_service common method for main branch
            result = self._project_service._index_branch_with_snapshot(
                project_id=project_id,
                branch=branch_name,
                language=language,
                target_files=None,  # Parse all files for main branch
                parse_all=True,
                force_reindex=False,
                pull_request_id=pull_request_id
            )
            
            return result
        else:
            # Get files that differ from the project's main_branch (NOT from PR base branch!)
                    # Delete all nodes for this project_id and branch (regardless of pull_request_id)
            deleted_count = self.neo4j_service.delete_branch_nodes(
                project_id=project_id,
                branch_name=branch_name
            )
            logger.info(f"Deleted {deleted_count} old nodes for branch '{branch_name}'")
            

            logger.info(f"Comparing branch '{branch_name}' against project main branch '{main_branch}'")
            diff_files = self.git_service.get_diff_files(repo_path, main_branch, branch_name)
            files_to_parse = diff_files if diff_files else None
            logger.info(f"Found {len(diff_files) if diff_files else 0} files different from main branch '{main_branch}'")

        # Step 4: Parse the files
        logger.info(f"Parsing branch '{branch_name}' at commit {current_commit[:8]}")
        
        if not files_to_parse:
            logger.info(f"No files to parse for branch '{branch_name}'")
            
            # Nếu không có file thay đổi nhưng không phải main branch, 
            # vẫn copy toàn bộ nodes từ main branch
            if branch_name != main_branch:
                logger.info(f"No changed files, copying all nodes from '{main_branch}' to '{branch_name}'")
                copied_count = self.neo4j_service.copy_unchanged_nodes_from_main(
                    project_id=project_id,
                    main_branch=main_branch,
                    current_branch=branch_name,
                    changed_chunks=[]  # Không có chunks thay đổi
                )
                logger.info(f"Copied {copied_count} nodes from main branch (no changes)")
            
            return {
                "was_cached": False,
                "commit_hash": current_commit,
                "chunk_count": 0,
                "file_count": 0,
                "chunks": []
            }

        from source_atlas.analyzers.analyzer_factory import AnalyzerFactory
        
        analyzer = AnalyzerFactory.create_analyzer(language, repo_path, str(project_id), branch_name)
        with analyzer as a:
            chunks = a.parse_project(
                Path(repo_path),
                target_files=files_to_parse,
                parse_all=False,
            )
        
        if chunks:
            # Step 1: Import ONLY changed chunk nodes (no relationships yet)
            logger.info(f"Step 1: Importing {len(chunks)} changed chunk nodes...")
            self.neo4j_service.import_changed_chunk_nodes_only(
                chunks=chunks,
                main_branch=main_branch,
                base_branch=base_branch,
                pull_request_id=pull_request_id
            )
            
            # Step 2: Copy unchanged nodes from main branch
            if branch_name != main_branch:
                logger.info(f"Step 2: Copying unchanged nodes from '{main_branch}' to '{branch_name}'...")
                copied_count = self.neo4j_service.copy_unchanged_nodes_from_main(
                    project_id=project_id,
                    main_branch=main_branch,
                    current_branch=branch_name,
                    changed_chunks=chunks
                )
                logger.info(f"Copied {copied_count} unchanged nodes with internal and cross relationships")
            
            # Step 3: Import relationships from changed chunks
            # Now all target nodes exist (both changed and copied), so relationships can be created
            logger.info(f"Step 3: Creating relationships for changed chunks...")
            self.neo4j_service.import_changed_chunk_relationships(
                chunks=chunks,
                current_branch=branch_name
            )
            logger.info(f"Successfully imported {len(chunks)} changed chunks with all relationships")
        
        # Step 6: Record the snapshot
        snapshot = UpsertBranchSnapshot(
            project_id=project_id,
            branch_name=branch_name,
            commit_hash=current_commit,
            chunk_count=len(chunks),
            file_count=len(files_to_parse) if files_to_parse else 0,
            status="completed",
        )
        self.branch_snapshot_repository.create(snapshot)
        
        logger.info(
            f"Successfully parsed and recorded snapshot for branch '{branch_name}' "
            f"at commit {current_commit[:8]} ({len(chunks)} chunks)"
        )
        
        return {
            "was_cached": False,
            "commit_hash": current_commit,
            "chunk_count": len(chunks),
            "file_count": len(files_to_parse) if files_to_parse else 0,
            "chunks": chunks
        }

    def get_brach_diff_nodes(self, target_nodes):
        """
        Analyze impact of changed nodes by finding:
        - left_target_nodes: nodes that depend on the target_nodes (incoming dependencies)  
        - related_nodes: nodes that the target_nodes depend on (outgoing dependencies)
        
        Args:
            target_nodes: List of TargetNode objects representing changed nodes
            
        Returns:
            dict: {
                'left_target_nodes': List[Dict] - nodes depending on target_nodes
                'related_nodes': List[Dict] - nodes that target_nodes depend on
            }
            
        All returned nodes are unique (no duplicates).
        """
        logger.info("Analyzing left target nodes (incoming dependencies)...")
        left_results = self.neo4j_service.get_left_target_nodes(
            target_nodes=target_nodes,
            max_level=20
        )
        
        # Extract unique left target nodes using set for deduplication
        left_target_nodes_set = set()
        for result in left_results:
            for node in result['visited_nodes']:
                node_key = (
                    node.get('class_name'),
                    node.get('method_name'), 
                    node.get('branch'),
                    str(node.get('project_id'))  # Ensure string for consistency
                )
                left_target_nodes_set.add(node_key)
        
        # Convert to list of dicts for consistency
        left_target_nodes = [
            {
                'class_name': node[0],
                'method_name': node[1],
                'branch': node[2],
                'project_id': node[3]
            }
            for node in left_target_nodes_set
        ]
        
        logger.info(f"Found {len(left_target_nodes)} unique left target nodes")
        
        # Get related nodes (outgoing dependencies from original target_nodes)
        logger.info("Analyzing related nodes (outgoing dependencies from target_nodes)...")
        related_results = self.neo4j_service.get_related_nodes(
            target_nodes=target_nodes,
            max_level=20
        )
        
        # Extract unique related nodes using set for deduplication
        related_nodes_set = set()
        for result in related_results:
            for node in result['visited_nodes']:
                node_key = (
                    node.get('class_name'),
                    node.get('method_name'),
                    node.get('branch'),
                    str(node.get('project_id'))  # Ensure string for consistency
                )
                related_nodes_set.add(node_key)
        
        # Convert to list of dicts
        related_nodes = [
            {
                'class_name': node[0],
                'method_name': node[1],
                'branch': node[2],
                'project_id': node[3]
            }
            for node in related_nodes_set
        ]
        
        logger.info(f"Found {len(related_nodes)} unique related nodes")
        
        # Calculate total unique affected nodes (avoid double counting)
        all_nodes_set = set()
        for node in left_target_nodes:
            node_key = (node['class_name'], node['method_name'], node['branch'], node['project_id'])
            all_nodes_set.add(node_key)
        for node in related_nodes:
            node_key = (node['class_name'], node['method_name'], node['branch'], node['project_id'])
            all_nodes_set.add(node_key)
        
        total_affected = len(all_nodes_set)
        logger.info(
            f"Impact analysis complete: "
            f"{len(left_target_nodes)} left target nodes, "
            f"{len(related_nodes)} related nodes, "
            f"{total_affected} total unique affected nodes"
        )
        
        return {
            'left_target_nodes': left_target_nodes,
            'related_nodes': related_nodes
        }

    def get_branch_history(self, project_id: int, branch_name: str, limit: int = 10):
        """Get parsing history for a branch."""
        with self.branch_snapshot_repository.session_factory() as session:
            from models.branch_snapshot import BranchSnapshot
            
            return (
                session.query(BranchSnapshot)
                .filter(
                    BranchSnapshot.project_id == project_id,
                    BranchSnapshot.branch_name == branch_name,
                )
                .order_by(BranchSnapshot.created_at.desc())
                .limit(limit)
                .all()
            )
    
    def delete_pull_request_nodes(self, project_id: int, pull_request_id: str):
        """
        Delete all nodes belonging to a specific pull request.
        This is useful when a PR is closed or merged.
        """
        return self.neo4j_service.delete_pull_request_nodes(project_id, pull_request_id)
    
    def delete_branch_nodes(self, project_id: int, branch_name: str, pull_request_id: str = None):
        """
        Delete all nodes belonging to a specific branch.
        This is useful when re-parsing a branch with different commit hash.
        
        Args:
            project_id: Project ID
            branch_name: Branch name to delete nodes for
            pull_request_id: Optional PR ID. If provided, only delete nodes with this PR ID.
                           If None, delete nodes without PR ID (main branch nodes).
        """
        return self.neo4j_service.delete_branch_nodes(project_id, branch_name, pull_request_id)

