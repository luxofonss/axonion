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
            
            # Create JWT (valid for 5 minutes to be safe)
            now = int(time.time())
            exp_time = now + (5 * 60)  # 5 minutes from now
            
            payload = {
                "iat": now,
                "exp": exp_time,
                "iss": self.app_id,
            }
            
            # Validate payload
            if exp_time - now > 600:  # More than 10 minutes
                logger.error(f"JWT expiration too far in future: {exp_time - now}s")
                raise HTTPException(status_code=500, detail="JWT expiration time invalid")
            
            from datetime import datetime
            logger.info(f"Creating JWT: iat={now}, exp={exp_time}, duration={exp_time-now}s")
            logger.info(f"Current time: {datetime.fromtimestamp(now)}")
            logger.info(f"Expiry time: {datetime.fromtimestamp(exp_time)}")
            
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
                        expiry = datetime.now() + timedelta(hours=8)
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
            
            # Handle PR closed action - delete all nodes for this branch
            # Note: GitHub sends "closed" action with merged=true when PR is merged
            is_merged = payload.get("pull_request", {}).get("merged", False)
            if action == "closed":
                container = Container()
                project_service = container.project_service()
                branch_snapshot_service = container.branch_snapshot_service()
                
                # Get branch name and find project
                branch_name = payload.get("pull_request", {}).get("head", {}).get("ref", "unknown")
                found_project = project_service.find_project_by_repo_name(repo)
                
                # Base response data
                action_type = "merged" if is_merged else "closed"
                response_data = {
                    "pr_number": pr_number,
                    "branch_name": branch_name,
                    "repository": repo,
                    "action": action,
                    "is_merged": is_merged,
                    "action_type": action_type
                }
                
                if not found_project:
                    logger.warning(f"Project not found for repo {repo}, cannot delete PR nodes")
                    return {**response_data, "status": "warning", "message": f"Project not found for repo {repo}"}
                
                try:
                    # Delete all nodes for this branch
                    deleted_count = branch_snapshot_service.delete_branch_nodes(
                        project_id=found_project.id,
                        branch_name=branch_name
                    )
                    
                    # Also delete changed nodes record from database
                    branch_snapshot_repository = container.branch_snapshot_repository()
                    deleted_changed_nodes = branch_snapshot_repository.delete_by_pr(
                        project_id=found_project.id,
                        pull_request_id=str(pr_number)
                    )
                    
                    logger.info(f"Deleted {deleted_count} nodes and {deleted_changed_nodes} changed nodes records for {action_type} PR #{pr_number} branch '{branch_name}'")
                    return {
                        **response_data,
                        "status": "success",
                        "message": f"Deleted {deleted_count} nodes for {action_type} PR #{pr_number} branch '{branch_name}'",
                        "deleted_nodes_count": deleted_count,
                        "deleted_changed_nodes_records": deleted_changed_nodes
                    }
                except Exception as e:
                    logger.error(f"Failed to delete nodes for {action_type} PR: {str(e)}")
                    return {**response_data, "status": "error", "message": f"Failed to delete nodes: {str(e)}"}
            
            # Get token for github app comment
            token = await self.get_installation_token(installation_id)

            # Comment on PR using GitService
            container = Container()
            git_service = container.git_service()
            
            comment_body = git_service.generate_simple_comment(
                "Xin chào! Tôi đã nhận được yêu cầu và đang tiến hành phân tích."
            )
            
            try:
                await git_service.post_github_comment(
                    repo=repo,
                    pr_number=pr_number,
                    comment_body=comment_body,
                    github_token=token,
                    timeout=self.http_timeout
                )
            except Exception as e:
                logger.error(f"Failed to post comment: {str(e)}")
                # Don't fail the webhook for comment errors

            # Find project by repository name using ProjectService
            container = Container()
            project_service = container.project_service()
            found_project = project_service.find_project_by_repo_name(repo)

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
            
            # Get commit messages from PR to detect Jira issues
            pr_commits = await git_service.get_pull_request_commits(owner_name[0], owner_name[1], pr_number, token)
            commit_messages = [commit.get("message", "") for commit in pr_commits if commit.get("message")]
            
            # Extract Jira issues from commit messages and PR title/description
            from services.jira_service import JiraService
            jira_service = JiraService()
            jira_issues = jira_service.extract_and_fetch_jira_issues(
                commit_messages=commit_messages,
                pr_title=payload["pull_request"].get("title", ""),
                pr_description=payload["pull_request"].get("body", "")
            )

            extensions = [".java"]
            if found_project and getattr(found_project, 'valid_extensions', None):
                parsed = json.loads(getattr(found_project, 'valid_extensions') or "['.java']")
                if isinstance(parsed, list):
                    # Normalize to lowercase and ensure leading dot
                    extensions = [e.lower() if e.startswith('.') else f".{e.lower()}" for e in parsed if isinstance(e, str) and e]
            if extensions:
                pr_files = [f for f in pr_files if isinstance(f.get("filename"), str) and f.get("filename").lower().endswith(tuple(extensions))]

            pr_files_name = [f.get("filename") for f in pr_files if isinstance(f.get("filename"), str)]

            # Parse branches with caching if project exists
            base_result = None
            head_result = None
            left_target_nodes = []
            related_nodes = []
            
            if found_project and base_branch and head_branch:
                try:
                    # Get language from project (default to java)
                    languages = json.loads(getattr(found_project, 'languages', '["java"]') or '["java"]')
                    language = languages[0] if isinstance(languages, list) and languages else "java"
                    
                    repo_path = found_project.local_path if getattr(found_project, 'local_path', None) else str(Path(configs.DATA_DIR) / found_project.name)
                    
                    # Get branch_snapshot_service from container
                    branch_snapshot_service = container.branch_snapshot_service()
                    project_service = container.project_service()
                    
                    # Setup circular dependency
                    branch_snapshot_service.set_project_service(project_service)
                    
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

                    # Parse head branch (comparing to project's main branch)
                    logger.info(f"Parsing PR head branch '{head_branch}' (comparing to main: '{found_project.main_branch}')")
                    head_result = branch_snapshot_service.parse_branch_if_needed(
                        project_id=found_project.id,
                        repo_path=repo_path,
                        branch_name=head_branch,
                        main_branch=found_project.main_branch,  # Project's main branch for consistency
                        base_branch=base_branch,  # Pass base_branch for fallback logic
                        language=language,
                        pull_request_id=str(pr_number)  # Pass PR number as pull_request_id for head branch
                    )

                    # Get changed nodes - either from DB (if cached) or from Neo4j (if newly parsed)
                    branch_snapshot_repository = container.branch_snapshot_repository()
                    neo4j_service = container.neo4j_service()
                    
                    # First try to get from database
                    stored_snapshot = branch_snapshot_repository.find_by_pr_and_project(
                        project_id=found_project.id,
                        pull_request_id=str(pr_number)
                    )
                    
                    if stored_snapshot and not head_result.get('was_cached', True):
                        # If we have stored nodes but just parsed new data, update the stored nodes
                        changed_nodes = neo4j_service.get_nodes_by_condition(
                            project_id=found_project.id,
                            branch=head_branch,
                            pull_request_id=str(pr_number)
                        )
                        
                        # Save/update changed nodes in database
                        branch_snapshot_repository.upsert_changed_nodes(
                            project_id=found_project.id,
                            pull_request_id=str(pr_number),
                            branch_name=head_branch,
                            commit_hash=head_result.get('commit_hash', ''),
                            changed_nodes=changed_nodes
                        )
                        logger.info(f"Updated {len(changed_nodes)} changed nodes in database for PR {pr_number}")
                        
                    elif stored_snapshot:
                        # Use stored changed nodes
                        changed_nodes = stored_snapshot.changed_nodes or []
                        logger.info(f"Retrieved {len(changed_nodes)} changed nodes from database for PR {pr_number}")
                        
                    else:
                        # Get from Neo4j and save to database
                        changed_nodes = neo4j_service.get_nodes_by_condition(
                            project_id=found_project.id,
                            branch=head_branch,
                            pull_request_id=str(pr_number)
                        )
                        
                        # Save changed nodes in database for future use
                        if changed_nodes:
                            branch_snapshot_repository.upsert_changed_nodes(
                                project_id=found_project.id,
                                pull_request_id=str(pr_number),
                                branch_name=head_branch,
                                commit_hash=head_result.get('commit_hash', '') if head_result else '',
                                changed_nodes=changed_nodes
                            )
                            logger.info(f"Saved {len(changed_nodes)} changed nodes to database for PR {pr_number}")
                        else:
                            logger.info(f"No changed nodes found for PR {pr_number}")
                    
                    logger.info(f"Using {len(changed_nodes)} changed nodes for PR {pr_number} analysis")
                    
                    # Convert changed_nodes to the format expected by get_brach_diff_nodes
                    target_nodes = [
                        {
                            'class_name': node.get('class_name'),
                            'method_name': node.get('method_name'),
                            'branch': node.get('branch'),
                            'project_id': node.get('project_id')
                        }
                        for node in changed_nodes
                    ]
                    
                    diff_analysis = branch_snapshot_service.get_brach_diff_nodes(
                        target_nodes=target_nodes
                    )
                    left_target_nodes = diff_analysis['left_target_nodes']
                    related_nodes = diff_analysis['related_nodes']
                    logger.info(f"left_target_nodes: {len(left_target_nodes)} nodes")
                    logger.info(f"related_nodes: {len(related_nodes)} nodes")
                    
                    logger.info(
                        f"Branch parsing complete - "
                        f"Base: {'cached' if base_result['was_cached'] else 'parsed'} ({base_result['chunk_count']} chunks), "
                        f"Head: {'cached' if head_result['was_cached'] else 'parsed'} ({head_result['chunk_count']} chunks)"
                    )
                    
                    
                except Exception as e:
                    logger.error(f"Failed to parse branches: {str(e)}", exc_info=True)
                    # Don't fail the webhook - just log and continue

            # Always perform LangGraph code review analysis (even if parsing failed)
            logger.info("Starting LangGraph code review analysis...")
            review_result = None
            try:
                # Get git diff using local git service (if we have repo info)
                git_diff = ""
                if found_project and 'repo_path' in locals() and base_branch and head_branch:
                    try:
                        git_diff = git_service.get_diff_content(
                            repo_path=repo_path,
                            base_branch=base_branch,
                            target_branch=head_branch
                        )
                    except Exception as e:
                        logger.warning(f"Failed to get local git diff, will use GitHub API: {str(e)}")
                        # Fallback to GitHub API
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            diff_response = await client.get(
                                f"https://api.github.com/repos/{owner_name[0]}/{owner_name[1]}/pulls/{pr_number}",
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github.v3.diff",
                                    "X-GitHub-Api-Version": "2022-11-28"
                                }
                            )
                            diff_response.raise_for_status()
                            git_diff = diff_response.text
                else:
                    # Use GitHub API if no local repo
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        diff_response = await client.get(
                            f"https://api.github.com/repos/{owner_name[0]}/{owner_name[1]}/pulls/{pr_number}",
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": "application/vnd.github.v3.diff",
                                "X-GitHub-Api-Version": "2022-11-28"
                            }
                        )
                        diff_response.raise_for_status()
                        git_diff = diff_response.text
                
                # Prepare Jira tickets as string (reuse existing jira_issues)
                jira_tickets_text = ""
                if jira_issues:
                    jira_tickets_text = "Jira Requirements:\n"
                    for issue in jira_issues:
                        jira_tickets_text += f"\n**{issue.get('key', 'Unknown')}**: {issue.get('summary', 'No summary')}\n"
                        jira_tickets_text += f"Description: {issue.get('description', 'No description')}\n"
                        if issue.get('fields', {}).get('customfield_10000'):  # Acceptance criteria field
                            jira_tickets_text += f"Acceptance Criteria: {issue['fields']['customfield_10000']}\n"
                        jira_tickets_text += "---\n"
                else:
                    jira_tickets_text = "No Jira tickets found for this PR."
                
                # Create LangGraph service
                from services.langgraph_service import LangGraphCodeReviewService, CodeReviewRequest
                langgraph_service = LangGraphCodeReviewService(
                    jira_service=jira_service,
                    git_service=git_service,
                    branch_snapshot_service=branch_snapshot_service if 'branch_snapshot_service' in locals() else None
                )
                
                # Create review request (use available values)
                project_id = found_project.id if found_project else 0
                review_request = CodeReviewRequest(
                    project_id=project_id,
                    pull_request_id=str(pr_number),
                    repository_url=repo,
                    branch_name=head_branch if head_branch else payload["pull_request"].get("head", {}).get("ref", ""),
                    base_branch=base_branch if base_branch else payload["pull_request"].get("base", {}).get("ref", "main"),
                    pr_title=payload["pull_request"].get("title", ""),
                    pr_description=payload["pull_request"].get("body", ""),
                    commit_sha=payload["pull_request"].get("head", {}).get("sha", "")
                )
                
                # Use changed nodes if available, otherwise empty lists
                analysis_left_nodes = left_target_nodes if left_target_nodes else []
                analysis_related_nodes = related_nodes if related_nodes else []
                
                # Run LangGraph analysis
                review_result = langgraph_service.run_review(
                    request=review_request,
                    jira_tickets=jira_tickets_text,
                    git_diff=git_diff,
                    left_target_nodes=analysis_left_nodes,
                    related_nodes=analysis_related_nodes
                )
                
                logger.info("LangGraph code review analysis completed successfully")
                
            except Exception as e:
                logger.error(f"Failed to perform LangGraph analysis: {str(e)}")
                review_result = {
                    "code_style_issues": [],
                    "requirement_compliance": {"compliance_score": 0.0, "error": str(e)},
                    "recommendations": [f"Code review analysis failed: {str(e)}"],
                    "left_target_nodes": left_target_nodes if left_target_nodes else [],
                    "related_nodes": related_nodes if related_nodes else []
                }

            # Prepare serializable response
            response_data = {
                "status": "success",
                "message": f"Commented on PR #{pr_number} in {repo}",
                "pr_number": pr_number,
                "repository": repo,
                "action": action,
                "changes": pr_files,
                "pr_files_name": pr_files_name,
                "jira_issues": jira_issues,
                "jira_issues_count": len(jira_issues)
            }
            
            # Add analysis results if available (convert to serializable format)
            analysis_left_nodes = analysis_left_nodes if 'analysis_left_nodes' in locals() else []
            analysis_related_nodes = analysis_related_nodes if 'analysis_related_nodes' in locals() else []
            
            if analysis_left_nodes or analysis_related_nodes:
                # Calculate total unique affected nodes
                all_nodes_set = set()
                for node in analysis_left_nodes:
                    node_key = (node['class_name'], node['method_name'], node['branch'], node['project_id'])
                    all_nodes_set.add(node_key)
                for node in analysis_related_nodes:
                    node_key = (node['class_name'], node['method_name'], node['branch'], node['project_id'])
                    all_nodes_set.add(node_key)
                
                response_data["analysis_summary"] = {
                    "left_target_nodes_count": len(analysis_left_nodes),
                    "related_nodes_count": len(analysis_related_nodes),
                    "total_affected": len(all_nodes_set)
                }
            else:
                response_data["analysis_summary"] = {
                    "left_target_nodes_count": 0,
                    "related_nodes_count": 0,
                    "total_affected": 0
                }
            
            # Add LangGraph code review results if available
            if review_result:
                response_data["code_review"] = {
                    "code_style_issues": review_result.get("code_style_issues", []),
                    "code_style_issues_count": len(review_result.get("code_style_issues", [])),
                    "requirement_compliance": review_result.get("requirement_compliance", {}),
                    "compliance_score": review_result.get("requirement_compliance", {}).get("compliance_score", 0.0),
                    "recommendations": review_result.get("recommendations", []),
                    "recommendations_count": len(review_result.get("recommendations", []))
                }
            else:
                response_data["code_review"] = {
                    "code_style_issues": [],
                    "code_style_issues_count": 0,
                    "requirement_compliance": {},
                    "compliance_score": 0.0,
                    "recommendations": [],
                    "recommendations_count": 0
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
    