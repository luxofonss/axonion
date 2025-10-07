import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict

from loguru import logger
from tree_sitter import Language, Parser, Node

from source_atlas.lsp.lsp_service import LSPService
from source_atlas.models.domain_models import CodeChunk, ChunkType
from source_atlas.models.domain_models import Method
from source_atlas.utils.common import convert


@dataclass
class ClassParsingContext:
    package: str
    class_name: str
    full_class_name: str
    is_nested: bool
    is_config: bool
    import_mapping: Dict[str, str] = None
    parent_class: Optional[str] = None


class BaseCodeAnalyzer(ABC):

    def __init__(self, language: Language, parser: Parser, project_id: str, branch: str):
        self.language = language
        self.parser = parser
        self.comment_remover = None
        self.project_id = project_id
        self.branch = branch
        self.cached_nodes = {}
        self.methods_cache = {}
        self.lsp_service: LSPService = None

    def parse_project(self, root: Path, target_files: Optional[List[str]] = None, parse_all: bool = True, export_output: bool = True) -> List[CodeChunk]:
        logger.info(f"Starting analysis for project '{self.project_id}' at {root}")

        code_files = self._get_code_files(root)
        if not code_files:
            logger.warning("No source files found")
            return []

        # Filter files if parse_all is False and target_files is provided
        if not parse_all and target_files:
            code_files = self._filter_files_by_targets(code_files, target_files)

        if not code_files:
            logger.warning("No files to process after filtering")
            return []

        logger.info(f"Found {len(code_files)} source files to process")
        chunks: List[CodeChunk] = []

        # Build cache for all files (needed for cross-references)
        self.build_source_cache(root)

        # Process files sequentially (no threading for LSP compatibility)
        logger.info("Processing files sequentially")
        for i, file in enumerate(code_files, 1):
            try:
                file_chunks = self.process_file(file)
                chunks.extend(file_chunks)
                logger.debug(f"[{i}/{len(code_files)}] Completed processing: {file}")
            except Exception as e:
                logger.error(f"Error processing {file}: {e}", exc_info=True)

        logger.info(f"Extracted {len(chunks)} code chunks total")

        # Export to file if requested
        if export_output:
            output_path = Path("source_atlas/output") / str(self.project_id) / self.branch
            self.export_chunks(chunks, output_path)

        # Return chunks; higher layers (services) will handle persistence/indexing
        return chunks

    def process_file(self, file_path: Path) -> List[CodeChunk]:
        try:
            content = self._read_file_content(file_path)
            if not content.strip():
                return []

            content = self.comment_remover.remove_comments(content)

            tree = self.parser.parse(bytes(content, 'utf8'))
            class_nodes = self._extract_all_class_nodes(tree.root_node)

            chunks = []
            for class_node in class_nodes:
                chunk = self._parse_class_node(class_node, content, str(file_path), tree.root_node)
                if chunk:
                    chunks.append(chunk)
            return chunks

        except Exception as e:
            logger.error(f"Error processing file {file_path}: {e}")
            return []

    def _parse_class_node(self, class_node: Node, content: str, file_path: str, root_node: Node) -> Optional[CodeChunk]:
        try:
            with self.lsp_service.open_file(file_path):
                class_name = self._extract_class_name(class_node, content)
                package = self._extract_package(root_node, content)
                full_class_name = self._build_full_class_name(class_name, package, class_node, content, root_node)
                context = self.cached_nodes.get(full_class_name)
                if not context:
                    return None

                # Lazy evaluation: only compute implements if class is interface/abstract
                # This avoids expensive LSP calls for regular classes
                implements = []
                if self._should_check_implements(class_node, content):
                    implements = self._extract_implements_with_lsp(class_node, file_path, content)
                
                methods = self._extract_class_methods(
                    class_node, content, implements,
                    context.full_class_name, file_path, context.import_mapping
                )

                # Compute AST hash for the class content
                class_content = content[class_node.start_byte:class_node.end_byte]
                ast_hash = self.compute_ast_hash(class_content)

                return CodeChunk(
                    package=context.package,
                    class_name=context.class_name,
                    full_class_name=context.full_class_name,
                    file_path=file_path,
                    content=class_content,
                    ast_hash=ast_hash,
                    implements=implements,
                    methods=methods,
                    is_nested=context.is_nested,
                    parent_class=context.parent_class,
                    type=ChunkType.CONFIGURATION if context.is_config else ChunkType.REGULAR,
                    project_id=self.project_id,
                    branch=self.branch
                )
        except Exception as e:
            logger.error(f"Error parsing class node: {e}")
            return None
    
    def _should_check_implements(self, class_node: Node, content: str) -> bool:
        """
        Determine if we should check for implementations.
        Only check for interfaces and abstract classes to save LSP calls.
        Override in subclasses for language-specific logic.
        """
        return True  # Default: always check (subclass can optimize)

    def _build_class_context(self, class_node: Node, content: str, root_node: Node) -> Optional[ClassParsingContext]:
        class_name = self._extract_class_name(class_node, content)
        logger.info(f"class_name {class_name}")
        if not class_name:
            return None

        package = self._extract_package(root_node, content)
        is_nested = self._is_nested_class(class_node, root_node)
        full_class_name = self._build_full_class_name(class_name, package, class_node, content, root_node)
        parent_class = self._get_parent_class(class_node, content, package) if is_nested else None,
        is_config = self._is_config_node(class_node, content)
        import_mapping = self.build_import_mapping(root_node, content)
        return ClassParsingContext(
            package=package,
            class_name=class_name,
            full_class_name=full_class_name,
            is_nested=is_nested,
            parent_class=parent_class,
            is_config = is_config,
            import_mapping=import_mapping
        )

    def _read_file_content(self, file_path: Path) -> str:
        """Read file content with encoding fallback."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='latin1') as f:
                return f.read()

    def export_chunks(self, chunks: List[CodeChunk], output_path: Path) -> None:
        """Export chunks to JSON file"""
        if not chunks:
            logger.warning("No chunks to export")
            return
            
        logger.info(f"Exporting {len(chunks)} chunks to {output_path}")
        output_path.mkdir(parents=True, exist_ok=True)
        
        chunks_data = [convert(c) for c in chunks]
        chunks_file = output_path / "chunks.json"
        
        with open(chunks_file, "w", encoding="utf-8") as f:
            json.dump(chunks_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ Exported {len(chunks)} chunks to: {chunks_file}")

    @abstractmethod
    def _get_code_files(self, root: Path) -> List[Path]:
        """Return a list of relevant source files."""
        pass

    @abstractmethod
    def _extract_all_class_nodes(self, root_node: Node) -> List[Node]:
        pass

    @abstractmethod
    def _extract_implements_with_lsp(self, class_node: Node, file_path: str, content: str) -> List[str]:
        pass

    @abstractmethod
    def _extract_class_methods(self, class_node: Node, content: str,
                               implements: List[str],
                               full_class_name: str, file_path: str, import_mapping: Dict[str, str]) -> List[Method]:
        pass

    @abstractmethod
    def _extract_class_name(self, class_node: Node, content: str) -> Optional[str]:
        pass

    @abstractmethod
    def _extract_package(self, root_node: Node, content: str) -> str:
        pass

    @abstractmethod
    def _is_nested_class(self, class_node: Node, root_node: Node) -> bool:
        pass

    @abstractmethod
    def _build_full_class_name(self, class_name: str, package: str, class_node: Node,
                               content: str, root_node: Node) -> str:
        pass

    @abstractmethod
    def _get_parent_class(self, class_node: Node, content: str, package: str) -> Optional[str]:
        pass

    @abstractmethod
    def _is_config_node(self, class_node: Node, content: str):
        pass

    @abstractmethod
    def build_import_mapping(self, root_node: Node, content: str) -> Dict[str, str]:
        pass

    @abstractmethod
    def compute_ast_hash(self, code: str) -> str:
        """
        Compute AST hash for the specific language.
        Should be implemented by each language-specific analyzer.
        """
        pass

    def _filter_files_by_targets(self, code_files: List[Path], target_files: Optional[List[str]]) -> List[Path]:
        """
        Filter code files based on target_files list.
        Returns files where the code file path ends with one of the target file paths.
        
        Args:
            code_files: List of all code files
            target_files: List of target file paths to filter by
            
        Returns:
            Filtered list of code files
        """
        if not target_files:
            return code_files
            
        filtered_files = []
        for code_file in code_files:
            code_file_str = str(code_file).replace('\\', '/')
            # Check if any target file path matches the end of this code file
            for target in target_files:
                target_normalized = target.replace('\\', '/')
                if code_file_str.endswith(target_normalized):
                    filtered_files.append(code_file)
                    break
        
        logger.info(f"Filtered to {len(filtered_files)} files based on target_files")
        return filtered_files

    def build_source_cache(self, root) -> Dict[str, ClassParsingContext]:
        # Get all code files and filter by target_files if provided
        code_files = self._get_code_files(root)
        cached_nodes = {}

        # Process cache building sequentially (no threading for LSP compatibility)
        logger.info("Building source cache sequentially")
        for i, file in enumerate(code_files, 1):
            try:
                cache_data = self.process_class_cache_file(file)
                cached_nodes.update(cache_data)
                logger.debug(f"[{i}/{len(code_files)}] Cached: {file}")
            except Exception as e:
                logger.error(f"Error caching {file}: {e}", exc_info=True)

        self.cached_nodes = cached_nodes
        logger.info(f"Cache built with {len(cached_nodes)} classes")
        return cached_nodes

    def process_class_cache_file(self, file_path) -> Dict[str, ClassParsingContext]:
        content = self._read_file_content(file_path)
        if not content.strip():
            return {}

        content = self.comment_remover.remove_comments(content)

        tree = self.parser.parse(bytes(content, 'utf8'))
        class_nodes = self._extract_all_class_nodes(tree.root_node)
        chunks = {}
        for class_node in class_nodes:
            context = self._build_class_context(class_node, content, tree.root_node)
            if context:
                chunks[context.full_class_name] = context
        return chunks

    def _build_knowledge_graph(self, chunks: List[CodeChunk]):
        # Deprecated: handled by service layer
        return