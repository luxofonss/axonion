from pydantic import BaseModel
from typing import Optional, Dict, Any


class WebhookResponse(BaseModel):
    """Response schema for webhook endpoints"""
    status: str
    message: str
    data: Optional[Dict[str, Any]] = None


class GitHubWebhookPayload(BaseModel):
    """Base schema for GitHub webhook payloads"""
    action: Optional[str] = None
    repository: Optional[Dict[str, Any]] = None
    installation: Optional[Dict[str, Any]] = None


class PushEventPayload(GitHubWebhookPayload):
    """Schema for GitHub push event payload"""
    after: str
    before: str
    commits: list
    pusher: Dict[str, Any]


class PullRequestEventPayload(GitHubWebhookPayload):
    """Schema for GitHub pull request event payload"""
    pull_request: Dict[str, Any]
    action: str
