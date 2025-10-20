from typing import Dict, List, Optional, TypedDict, Annotated
import operator
from dataclasses import dataclass
from loguru import logger
import json
import os

from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage, AIMessage
from langchain_anthropic import ChatAnthropic

from services.jira_service import JiraService
from services.git_service import GitService
from services.branch_snapshot_service import BranchSnapshotService
from schema.target_node_schema import TargetNode
from core.config import configs


@dataclass
class CodeReviewRequest:
    """Request for code review."""
    project_id: int
    pull_request_id: str
    repository_url: str
    branch_name: str
    base_branch: str
    pr_title: str
    pr_description: str = ""
    commit_sha: str = ""


class CodeReviewState(TypedDict):
    """State for code review workflow."""
    # Input
    request: CodeReviewRequest
    
    # Jira requirements (string format)
    jira_tickets: str
    
    # Git diff analysis
    git_diff: str
    
    # Context analysis
    needs_additional_context: bool
    target_nodes: List[Dict]
    
    # Neo4j analysis
    left_target_nodes: List[Dict]
    related_nodes: List[Dict]
    
    # Review results
    code_style_issues: List[Dict]
    requirement_compliance: Dict
    recommendations: List[str]
    
    # Messages for LLM interaction
    messages: Annotated[List, operator.add]


class LangGraphCodeReviewService:
    """LangGraph-based code review service."""
    
    def __init__(
        self,
        jira_service: JiraService,
        git_service: GitService,
        branch_snapshot_service: BranchSnapshotService,
        anthropic_api_key: str = None
    ):
        self.jira_service = jira_service
        self.git_service = git_service
        self.branch_snapshot_service = branch_snapshot_service
        
        # Initialize Claude LLM
        api_key = anthropic_api_key or getattr(configs, 'ANTHROPIC_API_KEY', None)
        if not api_key:
            raise ValueError("Anthropic API key is required. Set ANTHROPIC_API_KEY in config or pass anthropic_api_key parameter.")
        
        self.llm = ChatAnthropic(
            model="claude-sonnet-4-5-20250929",
            api_key=os.getenv("CLAUDE_API_KEY"),
            temperature=0.1,
            max_tokens=4000
        )
        
        # Build the workflow graph
        self.workflow = self._build_workflow()
    
    def _build_workflow(self) -> StateGraph:
        """Build the LangGraph workflow for code review."""
        workflow = StateGraph(CodeReviewState)
        
        # Add nodes
        workflow.add_node("check_additional_context", self._check_additional_context)
        workflow.add_node("analyze_code_impact", self._analyze_code_impact)
        workflow.add_node("review_code_style", self._review_code_style)
        workflow.add_node("check_requirement_compliance", self._check_requirement_compliance)
        
        # Define the flow - start with check_additional_context
        workflow.set_entry_point("check_additional_context")
        
        # Conditional edge based on whether additional context is needed
        workflow.add_conditional_edges(
            "check_additional_context",
            self._should_analyze_impact,
            {
                "analyze_impact": "analyze_code_impact",
                "skip_impact": "review_code_style"
            }
        )
        
        workflow.add_edge("analyze_code_impact", "review_code_style")
        workflow.add_edge("review_code_style", "check_requirement_compliance")
        workflow.add_edge("check_requirement_compliance", END)
        
        return workflow.compile()
    
    def fetch_jira_requirements(self, pr_title: str, pr_description: str, branch_name: str) -> str:
        """Fetch requirements from Jira tickets (called externally)."""
        logger.info("Fetching Jira requirements...")
        
        # Extract Jira tickets from PR info
        jira_tickets = self.jira_service.get_tickets_from_pr_info(
            pr_title=pr_title,
            pr_description=pr_description,
            branch_name=branch_name
        )
        
        # Convert tickets to string format
        if not jira_tickets:
            return "No Jira tickets found for this PR."
        
        jira_text = "Jira Requirements:\n"
        for ticket in jira_tickets:
            jira_text += f"\n**{ticket.get('key', 'Unknown')}**: {ticket.get('summary', 'No summary')}\n"
            jira_text += f"Description: {ticket.get('description', 'No description')}\n"
            if ticket.get('acceptance_criteria'):
                jira_text += f"Acceptance Criteria: {ticket.get('acceptance_criteria')}\n"
            jira_text += "---\n"
        
        return jira_text
    
    def analyze_git_diff(self, repo_path: str, base_branch: str, target_branch: str) -> str:
        """Analyze git diff from pull request (called externally)."""
        logger.info("Analyzing git diff...")
        
        try:
            # Get git diff between base branch and current branch
            git_diff = self.git_service.get_diff_content(
                repo_path=repo_path,
                base_branch=base_branch,
                target_branch=target_branch
            )
            
            logger.info("Git diff analysis completed")
            return git_diff or ""
            
        except Exception as e:
            logger.error(f"Failed to analyze git diff: {str(e)}")
            return ""
    
    def _check_additional_context(self, state: CodeReviewState) -> CodeReviewState:
        """Node 3: Check if we need additional context from related methods/classes."""
        logger.info("Checking if additional context is needed...")
        
        git_diff = state["git_diff"]
        
        # Simple heuristics to determine if we need more context
        needs_context = False
        target_nodes = []
        
        # Check if diff contains method calls or class references
        if any(keyword in git_diff.lower() for keyword in ['new ', 'class ', 'def ', 'function', 'method']):
            needs_context = True
            logger.info("New methods/classes detected, need additional context")
        
        # Check diff size - if it's large, we might need context
        if len(git_diff) > 5000:  # Large diff
            needs_context = True
            logger.info("Large diff detected, need additional context")
        
        # Extract potential target nodes from git diff (simplified)
        # This is a basic implementation - in practice, you'd parse the diff more carefully
        lines = git_diff.split('\n')
        for line in lines:
            if line.startswith('+++') and '.java' in line:
                # Extract class name from file path
                file_path = line.replace('+++', '').strip()
                if file_path.endswith('.java'):
                    class_name = file_path.split('/')[-1].replace('.java', '')
                    target_nodes.append({
                        'class_name': class_name,
                        'method_name': None,
                        'project_id': state["request"].project_id,
                        'branch': state["request"].branch_name
                    })
        
        state["needs_additional_context"] = needs_context
        state["target_nodes"] = target_nodes
        state["messages"].append(HumanMessage(
            content=f"Additional context needed: {needs_context}, found {len(target_nodes)} target nodes"
        ))
        
        return state
    
    def _should_analyze_impact(self, state: CodeReviewState) -> str:
        """Conditional function to determine if we should analyze code impact."""
        return "analyze_impact" if state["needs_additional_context"] else "skip_impact"
    
    def _analyze_code_impact(self, state: CodeReviewState) -> CodeReviewState:
        """Node 4: Analyze code impact using left_target_nodes and related nodes."""
        logger.info("Analyzing code impact...")
        
        target_nodes = state["target_nodes"]
        request = state["request"]
        
        if not target_nodes:
            state["left_target_nodes"] = []
            state["related_nodes"] = []
            state["impact_analysis"] = {"total_affected": 0}
            return state
        
        try:
            # Convert to TargetNode objects
            target_node_objects = [
                TargetNode(
                    class_name=node["class_name"],
                    method_name=node.get("method_name"),
                    project_id=node["project_id"],
                    branch=node["branch"]
                )
                for node in target_nodes
            ]
            
            # Get impact analysis
            impact_result = self.branch_snapshot_service.get_brach_diff_nodes(target_node_objects)
            
            state["left_target_nodes"] = impact_result.get("left_target_nodes", [])
            state["related_nodes"] = impact_result.get("related_nodes", [])
            
            total_affected = len(state["left_target_nodes"]) + len(state["related_nodes"])
            state["messages"].append(HumanMessage(
                content=f"Impact analysis: {total_affected} total affected nodes"
            ))
            
            logger.info(f"Impact analysis completed: {total_affected} affected nodes")
            
        except Exception as e:
            logger.error(f"Failed to analyze code impact: {str(e)}")
            state["left_target_nodes"] = []
            state["related_nodes"] = []
            state["messages"].append(HumanMessage(content=f"Impact analysis failed: {str(e)}"))
        
        return state
    
    def _review_code_style(self, state: CodeReviewState) -> CodeReviewState:
        """Node 5: Review code style and quality using Claude LLM."""
        logger.info("Reviewing code style with Claude LLM...")
        
        git_diff = state["git_diff"]
        
        try:
            # Get affected nodes info
            left_target_nodes = state.get("left_target_nodes", [])
            related_nodes = state.get("related_nodes", [])
            
            # Analyze affected endpoints/APIs/consumers
            affected_apis = []
            affected_jobs = []
            affected_consumers = []
            
            for node in left_target_nodes + related_nodes:
                class_name = node.get('class_name', '').lower()
                method_name = node.get('method_name', '').lower() if node.get('method_name') else ''
                
                # Detect API endpoints
                if any(keyword in class_name for keyword in ['controller', 'endpoint', 'api', 'rest']):
                    affected_apis.append(f"{node.get('class_name')}.{node.get('method_name', 'N/A')}")
                
                # Detect jobs/schedulers
                if any(keyword in class_name for keyword in ['job', 'scheduler', 'task', 'worker']):
                    affected_jobs.append(f"{node.get('class_name')}.{node.get('method_name', 'N/A')}")
                
                # Detect consumers/listeners
                if any(keyword in class_name for keyword in ['consumer', 'listener', 'handler', 'processor']):
                    affected_consumers.append(f"{node.get('class_name')}.{node.get('method_name', 'N/A')}")
            
            # Prepare enhanced prompt for Claude
            affected_components_text = ""
            if affected_apis:
                affected_components_text += f"\n**Affected APIs/Endpoints:**\n" + "\n".join(f"- {api}" for api in affected_apis[:10])
            if affected_jobs:
                affected_components_text += f"\n**Affected Jobs/Schedulers:**\n" + "\n".join(f"- {job}" for job in affected_jobs[:10])
            if affected_consumers:
                affected_components_text += f"\n**Affected Consumers/Listeners:**\n" + "\n".join(f"- {consumer}" for consumer in affected_consumers[:10])
            
            prompt = f"""
You are a senior code reviewer. Please analyze the following git diff for code style and quality issues.

Git Diff:
```
{git_diff}
```

Impact Analysis:
- Total affected nodes: {len(left_target_nodes + related_nodes)}
- Nodes depending on changes: {len(left_target_nodes)}
- Nodes used by changes: {len(related_nodes)}
{affected_components_text}

Please identify code style issues and return them in the following JSON format:
{{
    "code_style_issues": [
        {{
            "type": "issue_type",
            "severity": "low|medium|high",
            "description": "Description of the issue",
            "line": "line_number_if_applicable",
            "suggestion": "Suggested fix"
        }}
    ]
}}

Focus on:
- Code formatting and style
- Naming conventions
- Code complexity
- Best practices
- Potential bugs or issues
- Impact on APIs, jobs, and consumers
- Breaking changes that might affect dependent components
"""

            # Call Claude LLM
            response = self.llm.invoke([HumanMessage(content=prompt)])
            
            # Parse response
            try:
                import re
                json_match = re.search(r'\{.*\}', response.content, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                    code_style_issues = result.get("code_style_issues", [])
                else:
                    code_style_issues = []
            except (json.JSONDecodeError, AttributeError):
                logger.warning("Failed to parse Claude response, using fallback")
                code_style_issues = [{
                    "type": "analysis_error",
                    "severity": "low",
                    "description": "Could not parse LLM response for detailed analysis",
                    "suggestion": "Manual review recommended"
                }]
            
        except Exception as e:
            logger.error(f"Failed to review code style with Claude: {str(e)}")
            code_style_issues = [{
                "type": "review_error",
                "severity": "medium",
                "description": f"Code style review failed: {str(e)}",
                "suggestion": "Manual review required"
            }]
        
        state["code_style_issues"] = code_style_issues
        state["messages"].append(HumanMessage(
            content=f"Code style review completed: {len(code_style_issues)} issues found"
        ))
        
        logger.info(f"Code style review: {len(code_style_issues)} issues found")
        return state
    
    def _check_requirement_compliance(self, state: CodeReviewState) -> CodeReviewState:
        """Node 6: Check if code implements requirements from Jira using Claude LLM."""
        logger.info("Checking requirement compliance with Claude LLM...")
        
        jira_tickets = state["jira_tickets"]
        git_diff = state["git_diff"]
        
        try:
            # Prepare prompt for Claude
            prompt = f"""
You are a senior code reviewer. Please analyze if the following code changes comply with the Jira requirements.

Jira Requirements:
{jira_tickets}

Git Diff:
```
{git_diff}
```

Please analyze the compliance and return the result in the following JSON format:
{{
    "compliant_requirements": [
        {{
            "requirement": "requirement description",
            "confidence": 0.8,
            "evidence": "evidence from code changes"
        }}
    ],
    "missing_requirements": [
        {{
            "requirement": "requirement description",
            "reason": "why it's missing or unclear"
        }}
    ],
    "unclear_requirements": [
        {{
            "requirement": "requirement description",
            "reason": "why it's unclear"
        }}
    ],
    "compliance_score": 0.75
}}

Focus on:
- Whether the code changes address the Jira requirements
- Missing functionality that should be implemented
- Unclear or ambiguous implementations
"""

            # Call Claude LLM
            response = self.llm.invoke([HumanMessage(content=prompt)])
            
            # Parse response
            try:
                import re
                json_match = re.search(r'\{.*\}', response.content, re.DOTALL)
                if json_match:
                    compliance_result = json.loads(json_match.group())
                else:
                    compliance_result = {
                        "compliant_requirements": [],
                        "missing_requirements": [],
                        "unclear_requirements": [],
                        "compliance_score": 0.0
                    }
            except (json.JSONDecodeError, AttributeError):
                logger.warning("Failed to parse Claude response for compliance check")
                compliance_result = {
                    "compliant_requirements": [],
                    "missing_requirements": [{
                        "requirement": "Analysis failed",
                        "reason": "Could not parse LLM response"
                    }],
                    "unclear_requirements": [],
                    "compliance_score": 0.0
                }
                
        except Exception as e:
            logger.error(f"Failed to check requirement compliance with Claude: {str(e)}")
            compliance_result = {
                "compliant_requirements": [],
                "missing_requirements": [{
                    "requirement": "Compliance check failed",
                    "reason": f"Error: {str(e)}"
                }],
                "unclear_requirements": [],
                "compliance_score": 0.0
            }
        
        state["requirement_compliance"] = compliance_result
        state["messages"].append(HumanMessage(
            content=f"Requirement compliance: {compliance_result['compliance_score']:.2%}"
        ))
        
        logger.info(f"Requirement compliance: {compliance_result['compliance_score']:.2%}")
        
        # Generate recommendations based on analysis
        recommendations = self._generate_recommendations(state)
        state["recommendations"] = recommendations
        
        return state
    
    def _generate_recommendations(self, state: CodeReviewState) -> List[str]:
        """Generate recommendations based on analysis results."""
        logger.info("Generating recommendations...")
        
        recommendations = []
        
        # Code style recommendations
        code_style_issues = state.get("code_style_issues", [])
        if code_style_issues:
            high_severity_issues = [issue for issue in code_style_issues if issue.get("severity") == "high"]
            if high_severity_issues:
                recommendations.append(f"Address {len(high_severity_issues)} high-severity code style issues")
            
            medium_severity_issues = [issue for issue in code_style_issues if issue.get("severity") == "medium"]
            if medium_severity_issues:
                recommendations.append(f"Consider fixing {len(medium_severity_issues)} medium-severity code style issues")
        
        # Requirement compliance recommendations
        requirement_compliance = state.get("requirement_compliance", {})
        compliance_score = requirement_compliance.get("compliance_score", 0)
        
        if compliance_score < 0.5:
            recommendations.append("Low requirement compliance - review Jira requirements and ensure all are addressed")
        elif compliance_score < 0.8:
            recommendations.append("Moderate requirement compliance - verify all requirements are fully implemented")
        
        missing_reqs = requirement_compliance.get("missing_requirements", [])
        if missing_reqs:
            recommendations.append(f"Address {len(missing_reqs)} potentially missing requirements")
        
        # Impact analysis recommendations
        left_target_nodes = state.get("left_target_nodes", [])
        related_nodes = state.get("related_nodes", [])
        total_affected = len(left_target_nodes) + len(related_nodes)
        
        if total_affected > 20:
            recommendations.append("High impact change - consider comprehensive testing and gradual rollout")
        elif total_affected > 10:
            recommendations.append("Medium impact change - ensure adequate testing coverage")
        
        # General recommendations
        if not recommendations:
            recommendations.append("Code changes look good - no major issues identified")
        
        logger.info(f"Generated {len(recommendations)} recommendations")
        return recommendations
    
    def run_review(self, request: CodeReviewRequest, jira_tickets: str, git_diff: str, left_target_nodes: List[Dict] = None, related_nodes: List[Dict] = None) -> Dict:
        """Run the complete code review workflow."""
        logger.info(f"Starting code review for PR {request.pull_request_id}")
        
        # Initialize state
        initial_state = {
            "request": request,
            "jira_tickets": jira_tickets,
            "git_diff": git_diff,
            "needs_additional_context": False,
            "target_nodes": [],
            "left_target_nodes": left_target_nodes or [],
            "related_nodes": related_nodes or [],
            "code_style_issues": [],
            "requirement_compliance": {},
            "recommendations": [],
            "messages": []
        }
        
        # Run the workflow
        result = self.workflow.invoke(initial_state)
        
        # Return the final results
        return {
            "code_style_issues": result["code_style_issues"],
            "requirement_compliance": result["requirement_compliance"],
            "recommendations": result["recommendations"],
            "left_target_nodes": result["left_target_nodes"],
            "related_nodes": result["related_nodes"]
        }
