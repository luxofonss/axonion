from typing import List, Optional
from pydantic import BaseModel, Field


class NodeQueryRequest(BaseModel):
    """Request schema for querying nodes by conditions."""
    project_id: int = Field(..., description="Project ID to filter by")
    branch: str = Field(..., description="Branch name to filter by")
    pull_request_id: Optional[str] = Field(None, description="Optional pull request ID to filter by")
    class_name: Optional[str] = Field(None, description="Optional class name to filter by (exact match)")
    method_name: Optional[str] = Field(None, description="Optional method name to filter by (exact match)")


class NodeQueryResponse(BaseModel):
    """Response schema for node query results."""
    nodes: List[dict] = Field(..., description="List of nodes matching the conditions")
    total_count: int = Field(..., description="Total number of nodes found")
