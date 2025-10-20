import re
from typing import Dict, List, Optional
from loguru import logger
from markdownify import markdownify as md
from atlassian import Jira


class JiraService:
    """Service to interact with Jira API for fetching requirements and tickets."""
    
    def __init__(self, jira_url: str = None, username: str = None, api_token: str = None):
        from core.config import configs
        
        self.jira_url = configs.JIRA_URL
        self.username = configs.JIRA_USERNAME
        self.api_token = configs.JIRA_API_TOKEN
        
        # Debug config values
        logger.info(f"JIRA_URL from config: '{self.jira_url}'")
        logger.info(f"JIRA_USERNAME from config: '{self.username}'")
        logger.info(f"JIRA_API_TOKEN from config: '{self.api_token[:10] if self.api_token else 'None'}...'")
        
        if not all([self.jira_url, self.username, self.api_token]):
            logger.warning("Jira credentials not fully configured. Some features may not work.")
            logger.warning(f"Missing: URL={not bool(self.jira_url)}, USERNAME={not bool(self.username)}, TOKEN={not bool(self.api_token)}")
            self.client = None
        else:
            # Initialize Atlassian Jira client
            self.client = Jira(
                url=self.jira_url,
                username=self.username,
                password=self.api_token,
                cloud=True
            )
            logger.info(f"Jira client initialized for: {self.jira_url}")
    
    def get_ticket_by_key(self, ticket_key: str, include_comments: bool = True) -> Optional[Dict]:        
        try:
            issue = self.client.issue(ticket_key)
            if issue:
                return self._format_ticket(issue, include_comments=include_comments)
            else:
                logger.warning(f"Could not fetch ticket {ticket_key}")
                return None
        except Exception as e:
            logger.error(f"Error fetching Jira ticket {ticket_key}: {str(e)}")
            return None
    
    def extract_ticket_keys_from_text(self, text: str) -> List[str]:
        """Extract Jira ticket keys from text (e.g., PR title, branch name, commit messages)."""
        # Common Jira ticket pattern: PROJECT-123, PROJ-456, etc.
        pattern = r'\b[A-Z][A-Z0-9]+-\d+\b'
        matches = re.findall(pattern, text, re.IGNORECASE)
        
        # Convert to uppercase and remove duplicates
        ticket_keys = list(set([match.upper() for match in matches]))
        
        if ticket_keys:
            logger.info(f"Found Jira ticket keys: {ticket_keys}")
        
        return ticket_keys
    
    def get_tickets_from_pr_info(self, pr_title: str, pr_description: str = "", branch_name: str = "") -> List[Dict]:
        """Extract and fetch Jira tickets from PR information."""
        all_text = f"{pr_title} {pr_description} {branch_name}"
        ticket_keys = self.extract_ticket_keys_from_text(all_text)
        
        tickets = []
        for key in ticket_keys:
            ticket = self.get_ticket_by_key(key)
            if ticket:
                tickets.append(ticket)
        
        return tickets
    
    def extract_and_fetch_jira_issues(
        self, 
        commit_messages: List[str], 
        pr_title: str = "", 
        pr_description: str = ""
    ) -> List[Dict]:
        """Extract Jira issue keys from commit messages and fetch their content."""
        # Filter out None values and ensure all items are strings
        clean_commit_messages = [msg for msg in commit_messages if msg is not None]
        clean_pr_title = pr_title or ""
        clean_pr_description = pr_description or ""
        
        all_text = " ".join(clean_commit_messages + [clean_pr_title, clean_pr_description])
        
        # Extract Jira issue keys
        issue_keys = self.extract_ticket_keys_from_text(all_text)
        
        if not issue_keys:
            logger.info("No Jira issue keys found in commit messages or PR info")
            return []
        
        logger.info(f"Found Jira issue keys: {issue_keys}")
        
        # Fetch issue details from Jira
        jira_issues = []
        for issue_key in issue_keys:
            try:
                issue_data = self.get_ticket_by_key(issue_key)
                if issue_data:
                    jira_issues.append(issue_data)
                    logger.info(f"Successfully fetched Jira issue: {issue_key}")
                else:
                    logger.warning(f"Could not fetch Jira issue: {issue_key}")
            except Exception as e:
                logger.error(f"Error fetching Jira issue {issue_key}: {str(e)}")
        
        logger.info(f"Successfully fetched {len(jira_issues)} Jira issues")
        return jira_issues
    
    def _format_ticket(self, raw_ticket: Dict, include_comments: bool = True) -> Dict:
        """Format raw Jira ticket data into a cleaner structure."""
        fields = raw_ticket.get("fields", {})
        
        # Convert description to markdown
        description_raw = fields.get("description", "")
        description_markdown = self._convert_to_markdown(description_raw)
        
        formatted_ticket = {
            "key": raw_ticket.get("key"),
            "summary": fields.get("summary", ""),
            "description": description_markdown,
            "description_raw": description_raw,  # Keep raw for debugging
            "status": fields.get("status", {}).get("name", "Unknown"),
            "priority": fields.get("priority", {}).get("name", "Unknown"),
            "issue_type": fields.get("issuetype", {}).get("name", "Unknown"),
            "assignee": fields.get("assignee", {}).get("displayName", "Unassigned") if fields.get("assignee") else "Unassigned",
            "reporter": fields.get("reporter", {}).get("displayName", "Unknown") if fields.get("reporter") else "Unknown",
            "components": [comp.get("name", "") for comp in fields.get("components", [])],
            "labels": fields.get("labels", []),
            "raw_fields": fields  # Keep raw data for custom fields
        }
        
        # Add comments if requested
        if include_comments and "comment" in fields:
            comments = fields.get("comment", {}).get("comments", [])
            formatted_comments = []
            
            for comment in comments:
                comment_body = comment.get("body", "")
                comment_markdown = self._convert_to_markdown(comment_body)
                
                formatted_comments.append({
                    "id": comment.get("id"),
                    "author": comment.get("author", {}).get("displayName", "Unknown"),
                    "created": comment.get("created", ""),
                    "updated": comment.get("updated", ""),
                    "body": comment_markdown,
                    "body_raw": comment_body  # Keep raw for debugging
                })
            
            formatted_ticket["comments"] = formatted_comments
            formatted_ticket["comments_count"] = len(formatted_comments)
        
        return formatted_ticket
    
    def extract_requirements_from_ticket(self, ticket: Dict) -> Dict:
        """Extract structured requirements from Jira ticket."""
        description = ticket.get("description", "")
        
        # If description is Atlassian Document Format (ADF), extract text
        if isinstance(description, list):
            description = self._extract_text_from_adf(description)
        
        requirements = {
            "functional_requirements": [],
            "technical_requirements": [],
            "acceptance_criteria": [],
            "business_rules": [],
            "constraints": []
        }
        
        # Simple text parsing - can be enhanced with more sophisticated NLP
        lines = description.split('\n') if isinstance(description, str) else []
        
        current_section = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Detect sections
            lower_line = line.lower()
            if any(keyword in lower_line for keyword in ['acceptance criteria', 'ac:', 'criteria:']):
                current_section = 'acceptance_criteria'
                continue
            elif any(keyword in lower_line for keyword in ['technical requirement', 'tech req']):
                current_section = 'technical_requirements'
                continue
            elif any(keyword in lower_line for keyword in ['functional requirement', 'func req']):
                current_section = 'functional_requirements'
                continue
            elif any(keyword in lower_line for keyword in ['business rule', 'rule:']):
                current_section = 'business_rules'
                continue
            elif any(keyword in lower_line for keyword in ['constraint', 'limitation']):
                current_section = 'constraints'
                continue
            
            # Add content to current section
            if current_section and line.startswith(('-', '*', '•', '1.', '2.', '3.')):
                requirements[current_section].append(line.lstrip('-*•123456789. '))
        
        # If no structured sections found, put everything in functional_requirements
        if not any(requirements.values()) and description:
            requirements['functional_requirements'] = [description]
        
        return requirements
    
    def _extract_text_from_adf(self, adf_content) -> str:
        """Extract plain text from Atlassian Document Format (ADF)."""
        if not adf_content:
            return ""
            
        if isinstance(adf_content, str):
            return adf_content
            
        if isinstance(adf_content, dict):
            return self._extract_text_from_adf_dict(adf_content)
        elif isinstance(adf_content, list):
            text_parts = []
            for item in adf_content:
                text_parts.append(self._extract_text_from_adf_dict(item))
            return " ".join(text_parts)
        
        return str(adf_content)
    
    def _extract_text_from_adf_dict(self, adf_node: Dict) -> str:
        """Extract text from ADF dictionary node."""
        if not isinstance(adf_node, dict):
            return str(adf_node)
        
        text_parts = []
        
        def extract_text_recursive(node):
            if isinstance(node, dict):
                # Handle text nodes
                if node.get('type') == 'text':
                    text_parts.append(node.get('text', ''))
                # Handle other node types with content
                elif 'content' in node:
                    for child in node['content']:
                        extract_text_recursive(child)
                # Handle nodes with text directly
                elif 'text' in node:
                    text_parts.append(node['text'])
            elif isinstance(node, list):
                for item in node:
                    extract_text_recursive(item)
        
        extract_text_recursive(adf_node)
        return ' '.join(text_parts).strip()
    
    def _convert_to_markdown(self, content) -> str:
        """Convert Jira content (ADF or HTML) to Markdown format using markdownify."""
        if not content:
            return ""
        
        # Handle ADF (Atlassian Document Format)
        if isinstance(content, dict) and content.get("type"):
            # Convert ADF to HTML first, then to Markdown
            html_content = self._adf_to_html(content)
            return md(html_content, heading_style="ATX").strip()
        elif isinstance(content, list):
            # Handle list of ADF nodes
            html_content = self._adf_to_html({"type": "doc", "content": content})
            return md(html_content, heading_style="ATX").strip()
        elif isinstance(content, str):
            # Handle HTML or plain text
            if content.strip().startswith('<'):
                # Looks like HTML, convert directly
                return md(content, heading_style="ATX").strip()
            else:
                # Plain text, return as is
                return content.strip()
        
        return str(content)
    
    def _adf_to_html(self, adf_node: Dict) -> str:
        """Convert ADF (Atlassian Document Format) to HTML for markdownify processing."""
        if not isinstance(adf_node, dict):
            return str(adf_node)
        
        node_type = adf_node.get("type", "")
        content = adf_node.get("content", [])
        text = adf_node.get("text", "")
        attrs = adf_node.get("attrs", {})
        marks = adf_node.get("marks", [])
        
        # Handle text nodes
        if node_type == "text":
            result = text
            # Apply marks (formatting)
            for mark in marks:
                mark_type = mark.get("type", "")
                if mark_type == "strong":
                    result = f"<strong>{result}</strong>"
                elif mark_type == "em":
                    result = f"<em>{result}</em>"
                elif mark_type == "code":
                    result = f"<code>{result}</code>"
                elif mark_type == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    result = f'<a href="{href}">{result}</a>'
            return result
        
        # Handle block nodes
        elif node_type == "paragraph":
            content_html = "".join([self._adf_to_html(child) for child in content])
            return f"<p>{content_html}</p>"
        
        elif node_type == "heading":
            level = attrs.get("level", 1)
            content_html = "".join([self._adf_to_html(child) for child in content])
            return f"<h{level}>{content_html}</h{level}>"
        
        elif node_type == "bulletList":
            items_html = "".join([self._adf_to_html(item) for item in content])
            return f"<ul>{items_html}</ul>"
        
        elif node_type == "orderedList":
            items_html = "".join([self._adf_to_html(item) for item in content])
            return f"<ol>{items_html}</ol>"
        
        elif node_type == "listItem":
            content_html = "".join([self._adf_to_html(child) for child in content])
            return f"<li>{content_html}</li>"
        
        elif node_type == "codeBlock":
            language = attrs.get("language", "")
            content_text = "".join([self._adf_to_html(child) for child in content])
            return f'<pre><code class="language-{language}">{content_text}</code></pre>'
        
        elif node_type == "blockquote":
            content_html = "".join([self._adf_to_html(child) for child in content])
            return f"<blockquote>{content_html}</blockquote>"
        
        elif node_type == "rule":
            return "<hr>"
        
        elif node_type == "hardBreak":
            return "<br>"
        
        elif node_type == "table":
            rows_html = "".join([self._adf_to_html(row) for row in content])
            return f"<table>{rows_html}</table>"
        
        elif node_type == "tableRow":
            cells_html = "".join([self._adf_to_html(cell) for cell in content])
            return f"<tr>{cells_html}</tr>"
        
        elif node_type == "tableCell":
            content_html = "".join([self._adf_to_html(child) for child in content])
            return f"<td>{content_html}</td>"
        
        # Handle doc and other container nodes
        elif node_type in ["doc"]:
            return "".join([self._adf_to_html(child) for child in content])
        
        # Fallback for unknown nodes
        else:
            if content:
                return "".join([self._adf_to_html(child) for child in content])
            elif text:
                return text
            else:
                return ""
    
    def get_project_info(self, project_key: str) -> Optional[Dict]:
        """Get project information by key."""
        if not self.client:
            logger.error("Jira client not initialized")
            return None
            
        try:
            # Use atlassian-python-api to get project
            project = self.client.project(project_key)
            return project
        except Exception as e:
            logger.error(f"Error fetching project {project_key}: {str(e)}")
            return None
    
    def search_tickets(self, jql: str, max_results: int = 50) -> List[Dict]:
        """Search tickets using JQL (Jira Query Language)."""
        if not self.client:
            logger.error("Jira client not initialized")
            return []
            
        try:
            # Use atlassian-python-api JQL search
            result = self.client.jql(jql, limit=max_results)
            if result and "issues" in result:
                return [self._format_ticket(ticket) for ticket in result["issues"]]
            return []
        except Exception as e:
            logger.error(f"Error searching Jira tickets: {str(e)}")
            return []
