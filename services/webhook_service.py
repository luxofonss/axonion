import hmac
import hashlib
import jwt
import time
import httpx
from typing import Dict, Any, Optional
import json
from pathlib import Path
from fastapi import HTTPException
from datetime import datetime, timedelta

from loguru import logger

from core.config import configs
from core.container import Container


class GitHubWebhookService:
    def __init__(self):
        self.app_id = configs.GITHUB_APP_ID
        self.webhook_secret = configs.GITHUB_WEBHOOK_SECRET.encode()
        self.private_key_path = configs.GITHUB_PRIVATE_KEY_PATH
        self._private_key_cache: Optional[str] = None
        self._token_cache: Dict[int, tuple[str, datetime]] = {}  # installation_id -> (token, expiry)
        self.http_timeout = 30.0

    def _load_private_key(self) -> str:
        """Load and cache private key"""
        if self._private_key_cache is None:
            try:
                key_path = Path(self.private_key_path)
                if not key_path.exists():
                    raise FileNotFoundError(f"Private key not found at {self.private_key_path}")
                if not key_path.is_file():
                    raise ValueError(f"Private key path is not a file: {self.private_key_path}")
                
                self._private_key_cache = key_path.read_text()
                logger.info("Private key loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load private key: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Failed to load private key: {str(e)}")
        
        return self._private_key_cache

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify GitHub webhook signature"""
        if not signature:
            logger.warning("Webhook signature missing")
            raise HTTPException(status_code=400, detail="Missing signature")
        
        try:
            mac = hmac.new(self.webhook_secret, msg=payload, digestmod=hashlib.sha256)
            expected = "sha256=" + mac.hexdigest()
            
            if not hmac.compare_digest(expected, signature):
                logger.warning("Webhook signature verification failed")
                raise HTTPException(status_code=400, detail="Invalid signature")
            
            logger.debug("Webhook signature verified successfully")
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error verifying signature: {str(e)}")
            raise HTTPException(status_code=500, detail="Signature verification error")

    async def get_installation_token(self, installation_id: int) -> str:
        """Get GitHub installation access token (with caching)"""
        # Check cache
        if installation_id in self._token_cache:
            token, expiry = self._token_cache[installation_id]
            # Return cached token if it has at least 5 minutes left
            if datetime.now() < expiry - timedelta(minutes=5):
                logger.debug(f"Using cached token for installation {installation_id}")
                return token
        
        try:
            # Load private key
            private_key = self._load_private_key()
            
            # Create JWT (valid for 10 minutes)
            now = int(time.time())
            payload = {
                "iat": now,
                "exp": now + (10 * 60),
                "iss": self.app_id,
            }
            
            try:
                encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")
            except Exception as e:
                logger.error(f"Failed to encode JWT: {str(e)}")
                raise HTTPException(status_code=500, detail=f"JWT encoding failed: {str(e)}")

            # Get installation token
            try:
                async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                    try:
                        response = await client.post(
                            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                            headers={
                                "Authorization": f"Bearer {encoded_jwt}",
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"
                            }
                        )
                        response.raise_for_status()
                        token_data = response.json()
                        token = token_data["token"]
                        
                        # Cache token (expires in 1 hour)
                        expiry = datetime.now() + timedelta(hours=1)
                        self._token_cache[installation_id] = (token, expiry)
                        
                        logger.info(f"Successfully obtained installation token for {installation_id}")
                        return token
                        
                    except httpx.HTTPStatusError as e:
                        logger.error(f"GitHub API error getting installation token: {e.response.status_code} - {e.response.text}")
                        raise HTTPException(
                            status_code=500, 
                            detail=f"GitHub API error: {e.response.status_code}"
                        )
                    except httpx.TimeoutException:
                        logger.error("Timeout getting installation token")
                        raise HTTPException(status_code=504, detail="GitHub API timeout")
                    except httpx.RequestError as e:
                        logger.error(f"Request error getting installation token: {str(e)}")
                        raise HTTPException(status_code=502, detail="Failed to connect to GitHub API")
            except Exception as e:
                logger.error(f"Unexpected error in async client for token: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Failed to get installation token: {str(e)}")
                    
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Unexpected error getting installation token: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get installation token: {str(e)}")


    async def handle_pull_request_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle GitHub pull request event"""
        try:
            # Validate payload
            if "action" not in payload:
                raise ValueError("Missing 'action' field in pull request payload")
            if "pull_request" not in payload or "number" not in payload["pull_request"]:
                raise ValueError("Missing pull request information in payload")
            if "repository" not in payload or "full_name" not in payload["repository"]:
                raise ValueError("Missing repository information in payload")
            if "installation" not in payload or "id" not in payload["installation"]:
                raise ValueError("Missing installation information in payload")
            
            action = payload["action"]
            if action not in ("opened", "synchronize", "reopened", "closed"):
                logger.info(f"Ignoring pull request action: {action}")
                return {"status": "ignored", "message": f"Action '{action}' not handled"}

            pr_number = payload["pull_request"]["number"]
            repo = payload["repository"]["full_name"]
            installation_id = payload["installation"]["id"]
            
            logger.info(f"Handling pull request event: PR #{pr_number} in {repo} (action: {action})")
            
            # Handle PR closed action - delete nodes for this PR
            if action == "closed":
                container = Container()
                project_service = container.project_service()
                branch_snapshot_service = container.branch_snapshot_service()
                
                # Find project by repo
                owner, name = repo.split("/", 1)
                candidate_urls = [
                    f"https://github.com/{repo}.git",
                    f"https://github.com/{repo}",
                    f"git@github.com:{repo}.git",
                ]
                
                found_project = None
                for url in candidate_urls:
                    fp = project_service.find_project_by_git_url(url)
                    if fp:
                        found_project = fp
                        break
                
                if found_project:
                    try:
                        deleted_count = branch_snapshot_service.delete_pull_request_nodes(
                            project_id=found_project.id,
                            pull_request_id=str(pr_number)
                        )
                        logger.info(f"Deleted {deleted_count} nodes for closed PR #{pr_number}")
                        return {
                            "status": "success",
                            "message": f"Deleted {deleted_count} nodes for closed PR #{pr_number}",
                            "pr_number": pr_number,
                            "repository": repo,
                            "action": action,
                            "deleted_nodes_count": deleted_count
                        }
                    except Exception as e:
                        logger.error(f"Failed to delete nodes for closed PR: {str(e)}")
                        return {
                            "status": "error",
                            "message": f"Failed to delete nodes for closed PR: {str(e)}",
                            "pr_number": pr_number,
                            "repository": repo,
                            "action": action
                        }
                else:
                    logger.warning(f"Project not found for repo {repo}, cannot delete PR nodes")
                    return {
                        "status": "warning",
                        "message": f"Project not found for repo {repo}, cannot delete PR nodes",
                        "pr_number": pr_number,
                        "repository": repo,
                        "action": action
                    }
            
            token = await self.get_installation_token(installation_id)

            # Comment on PR
            comment_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
            logger.debug(f"Comment URL: {comment_url}")
            
            try:
                async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                    try:
                        response = await client.post(
                            comment_url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"
                            },
                            json={"body": "👋 Xin chào! Tôi đã nhận được yêu cầu và đang tiến hành phân tích."}
                        )
                        response.raise_for_status()
                        logger.info(f"Successfully commented on PR #{pr_number}")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"Failed to comment on PR: {e.response.status_code} - {e.response.text}")
                        raise HTTPException(
                            status_code=500,
                            detail=f"Failed to comment on PR: {e.response.status_code}"
                        )
                    except httpx.TimeoutException:
                        logger.error("Timeout commenting on PR")
                        raise HTTPException(status_code=504, detail="GitHub API timeout")
                    except httpx.RequestError as e:
                        logger.error(f"Request error commenting on PR: {str(e)}")
                        raise HTTPException(status_code=502, detail="Failed to connect to GitHub API")
            except Exception as e:
                logger.error(f"Unexpected error in async client: {str(e)}")
                # Don't fail the webhook for comment errors

            # get project info from database by git_url (constructed from repo full_name)
            # repo like "owner/name"; attempt to find existing project
            owner, name = repo.split("/", 1)
            # Try https and ssh git URLs commonly stored
            candidate_urls = [
                f"https://github.com/{repo}.git",
                f"https://github.com/{repo}",
                f"git@github.com:{repo}.git",
            ]

            container = Container()
            project_service = container.project_service()
            found_project = None
            for url in candidate_urls:
                fp = project_service.find_project_by_git_url(url)
                if fp:
                    found_project = fp
                    break

            head_branch = ""
            base_branch = ""

            if not found_project:
                logger.warning("Project not found by git_url; skipping local pulls")
            else:
                base_branch = payload["pull_request"].get("base", {}).get("ref", found_project.main_branch or "main")
                head_branch = payload["pull_request"].get("head", {}).get("ref", "")
                project_service.ensure_repo_and_pull_branches(found_project.id, [b for b in [base_branch, head_branch] if b])

            target_branch = payload["pull_request"].get("base", {}).get("ref", "main")
            logger.info(f"Target branch for PR #{pr_number} is '{target_branch}'")

            request_branch = payload["pull_request"].get("head", {}).get("ref", "")
            logger.info(f"Source branch for PR #{pr_number} is '{request_branch}'")

            # get diff files via GitHub API for the PR
            owner_name = repo.split("/")
            # Use final PR diff files to avoid false positives from per-commit diffs
            pr_files: list[dict[str, Any]] = await project_service.git_service.get_pull_request_files(owner_name[0], owner_name[1], pr_number, token)

            extensions = []
            if found_project and getattr(found_project, 'valid_extensions', None):
                parsed = json.loads(getattr(found_project, 'valid_extensions') or "[]")
                if isinstance(parsed, list):
                    # Normalize to lowercase and ensure leading dot
                    extensions = [e.lower() if e.startswith('.') else f".{e.lower()}" for e in parsed if isinstance(e, str) and e]
            if extensions:
                pr_files = [f for f in pr_files if isinstance(f.get("filename"), str) and f.get("filename").lower().endswith(tuple(extensions))]

            pr_files_name = [f.get("filename") for f in pr_files if isinstance(f.get("filename"), str)]

            # Parse branches with caching if project exists
            base_result = None
            head_result = None
            left_results = None
            related_results = None
            
            if found_project and base_branch and head_branch:
                try:
                    # Get language from project (default to java)
                    languages = json.loads(getattr(found_project, 'languages', '["java"]') or '["java"]')
                    language = languages[0] if isinstance(languages, list) and languages else "java"
                    
                    repo_path = found_project.local_path if getattr(found_project, 'local_path', None) else str(Path(configs.DATA_DIR) / found_project.name)
                    
                    # Get branch_snapshot_service from container
                    branch_snapshot_service = container.branch_snapshot_service()
                    
                    # IMPORTANT: We always pass found_project.main_branch (from Project table)
                    # This is the baseline for diff comparison, NOT the PR's base_branch
                    # Example: PR from feature-branch -> develop, but we compare both against 'main'
                    
                    # Parse base branch if it not merged to main branch yet
                    logger.info(f"Parsing PR base branch '{base_branch}' (comparing to main: '{found_project.main_branch}')")
                    base_result = branch_snapshot_service.parse_branch_if_needed(
                        project_id=found_project.id,
                        repo_path=repo_path,
                        branch_name=base_branch,
                        main_branch=found_project.main_branch,  # Project's main branch, NOT PR base!
                        base_branch=base_branch,  # Pass base_branch for fallback logic
                        language=language
                    )

                    # Initialize target_nodes list
                    base_result['target_nodes'] = []
                    
                    # Step 5.1: Prepare target nodes from chunks
                    for chunk in base_result['chunks']:
                        # Add class node
                        base_result['target_nodes'].append({
                            'class_name': chunk.full_class_name,
                            'method_name': None,
                            'branch': base_branch,
                            'project_id': str(found_project.id)
                        })
                        # Add method nodes
                        for method in chunk.methods:
                            base_result['target_nodes'].append({
                                'class_name': chunk.full_class_name,
                                'method_name': method.name,
                                'branch': base_branch,
                                'project_id': str(found_project.id)
                            })
                    
                    logger.info(f"Prepared {len(base_result['target_nodes'])} target nodes for analysis")
                    
                    # Parse head branch with base branch to compare diff
                    logger.info(f"Parsing PR head branch '{head_branch}' (comparing to main: '{found_project.main_branch}')")
                    head_result = branch_snapshot_service.parse_branch_if_needed(
                        project_id=found_project.id,
                        repo_path=repo_path,
                        branch_name=head_branch,
                        main_branch=base_branch,
                        base_branch=base_branch,  # Pass base_branch for fallback logic
                        language=language,
                        pull_request_id=str(pr_number)  # Pass PR number as pull_request_id for head branch
                    )

                    # Initialize target_nodes list
                    head_result['target_nodes'] = []
                    
                    # Step 5.1: Prepare target nodes from chunks
                    for chunk in head_result['chunks']:
                        # Add class node
                        head_result['target_nodes'].append({
                            'class_name': chunk.full_class_name,
                            'method_name': None,
                            'branch': head_branch,
                            'project_id': str(found_project.id)
                        })
                        # Add method nodes
                        for method in chunk.methods:
                            head_result['target_nodes'].append({
                                'class_name': chunk.full_class_name,
                                'method_name': method.name,
                                'branch': head_branch,
                                'project_id': str(found_project.id)
                            })
                    
                    logger.info(f"Prepared {len(head_result['target_nodes'])} target nodes for analysis")

                    # we have to check if target node's in head_branch content different from base_branch
                    # if not different then not insert to neo4j
                    # get_brach_diff_nodes handles both left and related nodes analysis and returns results
                    diff_analysis = branch_snapshot_service.get_brach_diff_nodes(head_result['target_nodes'])
                    left_results = diff_analysis['left_results']
                    related_results = diff_analysis['related_results']
                    
                    logger.info(
                        f"Branch parsing complete - "
                        f"Base: {'cached' if base_result['was_cached'] else 'parsed'} ({base_result['chunk_count']} chunks), "
                        f"Head: {'cached' if head_result['was_cached'] else 'parsed'} ({head_result['chunk_count']} chunks)"
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to parse branches: {str(e)}", exc_info=True)
                    # Don't fail the webhook - just log and continue

            # Prepare serializable response
            response_data = {
                "status": "success",
                "message": f"Commented on PR #{pr_number} in {repo}",
                "pr_number": pr_number,
                "repository": repo,
                "action": action,
                "changes": pr_files,
                "pr_files_name": pr_files_name
            }
            
            # Add analysis results if available (convert to serializable format)
            if left_results is not None:
                if isinstance(left_results, dict):
                    response_data["analysis_summary"] = {
                        "left_target_nodes_count": len(left_results.get('left_target_nodes', [])),
                        "related_nodes_count": len(left_results.get('related_nodes', [])),
                        "total_affected": left_results.get('total_affected', 0)
                    }
                else:
                    response_data["analysis_summary"] = {
                        "left_target_nodes_count": 0,
                        "related_nodes_count": 0,
                        "total_affected": 0
                    }
            else:
                response_data["analysis_summary"] = {
                    "left_target_nodes_count": 0,
                    "related_nodes_count": 0,
                    "total_affected": 0
                }
            
            # Clean up base_result and head_result to be serializable
            if base_result:
                response_data["base_branch_result"] = {
                    "was_cached": base_result.get('was_cached', False),
                    "commit_hash": base_result.get('commit_hash', ''),
                    "chunk_count": base_result.get('chunk_count', 0),
                    "file_count": base_result.get('file_count', 0),
                    "target_nodes_count": len(base_result.get('target_nodes', []))
                }
            else:
                response_data["base_branch_result"] = None
                
            if head_result:
                response_data["head_branch_result"] = {
                    "was_cached": head_result.get('was_cached', False),
                    "commit_hash": head_result.get('commit_hash', ''),
                    "chunk_count": head_result.get('chunk_count', 0),
                    "file_count": head_result.get('file_count', 0),
                    "target_nodes_count": len(head_result.get('target_nodes', []))
                }
            else:
                response_data["head_branch_result"] = None
            
            return response_data
            
        except HTTPException:
            raise
        except ValueError as e:
            logger.error(f"Invalid pull request payload: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Unexpected error handling pull request event: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to handle pull request event: {str(e)}")

    async def handle_webhook(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Main webhook handler"""
        logger.info(f"Received webhook event: {event_type}")
        
        if event_type == "pull_request":
            return await self.handle_pull_request_event(payload)
        else:
            logger.info(f"Ignoring event type: {event_type}")
            return {
                "status": "ignored",
                "message": f"Event type '{event_type}' not handled"
            }