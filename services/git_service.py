import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import re

import httpx
from fastapi import HTTPException
from loguru import logger

from core.config import configs


class GitService:
    def __init__(self) -> None:
        self.data_dir = Path(configs.DATA_DIR)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to ensure data directory exists: {str(e)}")
            raise


    def clone_repository(self, repo_url: str, folder_name: Optional[str] = None, branch: Optional[str] = None) -> str:
        try:
            try:
                from git import Repo
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Git library not available: {str(e)}")

            if not folder_name:
                # derive from URL (take last path segment without .git)
                last = repo_url.rstrip("/").split("/")[-1]
                folder_name = last[:-4] if last.endswith(".git") else last

            target_path = self.data_dir / folder_name
            if target_path.exists() and any(target_path.iterdir()):
                logger.info(f"Target path already exists and is not empty: {target_path}")
                return str(target_path)

            logger.info(f"Cloning {repo_url} into {target_path}")
            repo = Repo.clone_from(repo_url, str(target_path))

            if branch:
                logger.info(f"Checking out branch {branch}")
                repo.git.checkout(branch)

            return str(target_path)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Clone failed: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")

    def pull_branch(self, repo_path: str, branch: str) -> Dict[str, Any]:
        try:
            from git import Repo

            repo = Repo(str(repo_path))
            # Ensure branch exists locally and track remote
            origin = repo.remotes.origin
            origin.fetch()
            logger.info(f"Fetching latest from origin in {repo_path}")

            # Checkout branch (create if needed tracking origin/branch)
            if repo.active_branch.name != branch:
                # If branch doesn't exist locally, create tracking
                if branch not in [h.name for h in repo.heads]:
                    repo.git.checkout('-B', branch, f'origin/{branch}')
                else:
                    repo.git.checkout(branch)

            pull_info = origin.pull(branch)
            summary = [str(p) for p in pull_info]
            logger.info(f"Pulled branch {branch}: {summary}")
            return {"status": "success", "branch": branch, "summary": summary}
        except Exception as e:
            logger.error(f"Pull failed: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to pull branch: {str(e)}")

    @staticmethod
    def _gh_headers(token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_pull_request_commits(self, owner: str, repo: str, pr_number: int, token: str) -> List[Dict[str, Any]]:
        base = f"https://api.github.com/repos/{owner}/{repo}"
        commits_url = f"{base}/pulls/{pr_number}/commits"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # List commits in PR
                r = await client.get(commits_url, headers=self._gh_headers(token))
                r.raise_for_status()
                commit_shas = [c["sha"] for c in r.json()]

                results: List[Dict[str, Any]] = []
                for sha in commit_shas:
                    cr = await client.get(f"{base}/commits/{sha}", headers=self._gh_headers(token))
                    cr.raise_for_status()
                    data = cr.json()
                    results.append(
                        {
                            "sha": data.get("sha"),
                            "author": data.get("commit", {}).get("author", {}),
                            "message": data.get("commit", {}).get("message"),
                            "files": [
                                {
                                    "filename": f.get("filename"),
                                    "status": f.get("status"),
                                    "additions": f.get("additions"),
                                    "deletions": f.get("deletions"),
                                    "changes": f.get("changes"),
                                    "patch": f.get("patch"),
                                }
                                for f in data.get("files", [])
                            ],
                        }
                    )

                return results
        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub API error: {e.response.status_code} - {e.response.text}")
            raise HTTPException(status_code=500, detail=f"GitHub API error: {e.response.status_code}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="GitHub API timeout")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to GitHub API: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error retrieving PR commits: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get PR commits: {str(e)}")

    async def get_pull_request_files(self, owner: str, repo: str, pr_number: int, token: str) -> List[Dict[str, Any]]:
        """Return the final net file diffs for a PR (head vs base).

        Mirrors the shape used elsewhere: filename, status, additions, deletions, changes, patch.
        """
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers=self._gh_headers(token))
                r.raise_for_status()
                files = r.json() or []
                return [
                    {
                        "filename": f.get("filename"),
                        "status": f.get("status"),
                        "additions": f.get("additions"),
                        "deletions": f.get("deletions"),
                        "changes": f.get("changes"),
                        "patch": f.get("patch"),
                    }
                    for f in files
                ]
        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub API error: {e.response.status_code} - {e.response.text}")
            raise HTTPException(status_code=500, detail=f"GitHub API error: {e.response.status_code}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="GitHub API timeout")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to GitHub API: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error retrieving PR files: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get PR files: {str(e)}")

    async def get_commit_content(self, owner: str, repo: str, sha: str, token: str) -> Dict[str, Any]:
        url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, headers=self._gh_headers(token))
                r.raise_for_status()
                data = r.json()
                return {
                    "sha": data.get("sha"),
                    "author": data.get("commit", {}).get("author", {}),
                    "message": data.get("commit", {}).get("message"),
                    "files": [
                        {
                            "filename": f.get("filename"),
                            "status": f.get("status"),
                            "additions": f.get("additions"),
                            "deletions": f.get("deletions"),
                            "changes": f.get("changes"),
                            "patch": f.get("patch"),
                        }
                        for f in data.get("files", [])
                    ],
                }
        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub API error: {e.response.status_code} - {e.response.text}")
            raise HTTPException(status_code=500, detail=f"GitHub API error: {e.response.status_code}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="GitHub API timeout")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to GitHub API: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error retrieving commit: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get commit content: {str(e)}")

    def get_current_commit_hash(self, repo_path: str, branch: str) -> str:
        """Get the current commit hash for a specific branch."""
        try:
            from git import Repo
            
            repo = Repo(str(repo_path))
            # Ensure we're on the correct branch first
            if repo.active_branch.name != branch:
                repo.git.checkout(branch)
            
            commit_hash = repo.head.commit.hexsha
            logger.info(f"Current commit hash for {branch}: {commit_hash}")
            return commit_hash
        except Exception as e:
            logger.error(f"Failed to get commit hash for branch {branch}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get commit hash: {str(e)}")

    def get_diff_files(self, repo_path: str, base_branch: str, compare_branch: str) -> List[str]:
        """Get list of files that differ between two branches."""
        try:
            from git import Repo
            
            repo = Repo(str(repo_path))
            
            # Get the diff between the two branches
            diff_output = repo.git.diff('--name-only', f'{base_branch}...{compare_branch}')
            
            if not diff_output.strip():
                logger.info(f"No differences found between {base_branch} and {compare_branch}")
                return []
            
            # Split by newlines and filter out empty strings
            changed_files = [f for f in diff_output.split('\n') if f.strip()]
            
            logger.info(f"Found {len(changed_files)} changed files between {base_branch} and {compare_branch}")
            return changed_files
        except Exception as e:
            logger.error(f"Failed to get diff files: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to get diff files: {str(e)}")