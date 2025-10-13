from fastapi import APIRouter, Request, Header, HTTPException, Depends
from typing import Optional, Dict, Any

from services.webhook_service import GitHubWebhookService
from schema.webhook_schema import WebhookResponse

from loguru import logger

router = APIRouter(prefix="/api/v1", tags=["webhook"])


def get_webhook_service() -> GitHubWebhookService:
    """Dependency to get webhook service instance"""
    return GitHubWebhookService()


@router.post("/github/webhook", response_model=WebhookResponse)
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
    webhook_service: GitHubWebhookService = Depends(get_webhook_service)
):
    try:
        # Get raw body for signature verification
        body = await request.body()
        
        # Verify signature
        webhook_service.verify_signature(body, x_hub_signature_256)
        
        # Parse JSON payload
        payload = await request.json()
        logger.info(f"payload: {payload}")

        if not x_github_event:
            raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")
        
        # Handle the webhook event
        result = await webhook_service.handle_webhook(x_github_event, payload)
        
        return WebhookResponse(
            status=result.get("status", "success"),
            message=result.get("message", "Webhook processed successfully"),
            data=result
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("/github/webhook/health")
async def webhook_health():
    """Health check endpoint for webhook service"""
    return {"status": "healthy", "service": "github-webhook"}
