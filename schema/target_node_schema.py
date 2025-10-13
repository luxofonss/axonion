from typing import List, Optional
from pydantic import BaseModel, Field, validator


class TargetNode(BaseModel):
    """Simple DTO for target node input parameters used in code."""
    class_name: str = Field(..., description="Name of the class")
    method_name: Optional[str] = Field(None, description="Name of the method (optional)")
    branch: str = Field(..., description="Branch name")
    project_id: str = Field(..., description="Project ID")

    def to_dict(self) -> dict:
        """Convert to dictionary for Neo4j queries."""
        return {
            'class_name': self.class_name,
            'method_name': self.method_name,
            'branch': self.branch,
            'project_id': self.project_id
        }


class NodeRelationshipRequest(BaseModel):
    """Request schema for node relationship queries."""
    target_nodes: List[TargetNode] = Field(..., description="List of target nodes to analyze")
    max_level: int = Field(20, ge=1, le=100, description="Maximum traversal depth")
    min_level: int = Field(1, ge=1, le=100, description="Minimum traversal depth")

    @validator('target_nodes')
    def target_nodes_must_not_be_empty(cls, v):
        if not v:
            raise ValueError('target_nodes list cannot be empty')
        return v

    @validator('max_level')
    def max_level_must_be_greater_than_min_level(cls, v, values):
        if 'min_level' in values and v < values['min_level']:
            raise ValueError('max_level must be greater than or equal to min_level')
        return v


class NodeRelationshipResponse(BaseModel):
    """Response schema for node relationship queries."""
    endpoint: dict = Field(..., description="The starting node")
    path: dict = Field(..., description="The full path traversed (structured JSON)")
    visited_nodes: List[dict] = Field(..., description="List of filtered nodes based on relationship rules")


class NodeRelationshipListResponse(BaseModel):
    """Response schema for list of node relationships."""
    results: List[NodeRelationshipResponse] = Field(..., description="List of relationship results")
    total_count: int = Field(..., description="Total number of results found")
