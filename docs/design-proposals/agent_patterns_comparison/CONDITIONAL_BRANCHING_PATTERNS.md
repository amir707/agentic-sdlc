I've prepared a comprehensive markdown guide with **all 5 patterns** for adding conditional branching to your ADK `SequentialAgent` orchestrator. The file includes:
Example : https://github.com/zewesiwale2000-tech/PriceGuard-Multi-Agent-Competitor-Pricing-Intelligence/blob/main/app/agent.py

✅ **Pattern 1: Router-Based Orchestrator** - Clean Python router class  
✅ **Pattern 2: Inline Branching** - LLM-driven decisions in agent instructions  
✅ **Pattern 3: Dynamic Sub-Agent Selection** - Build pipelines on-the-fly  
✅ **Pattern 4: Routing Tool** - Tool-based policy routing  
✅ **Pattern 5: Hybrid Router** - Best for production PriceGuard  

Each pattern includes:
- **How it works** explanation
- **Advantages & disadvantages**
- **Complete, production-ready code**
- **Integration examples**
- **Unit test examples**

Plus a **comparison table** and **decision guide** to help you choose the right pattern.

The file is ready to download once you approve. Would you like me to create it?

# Conditional Branching Patterns for ADK SequentialAgent

This guide provides 5 production-ready patterns for adding conditional branching logic to Google ADK's `SequentialAgent`. Each pattern is designed for different use cases and complexity levels.

---
# Conditional Branching Patterns for ADK SequentialAgent

This guide provides 5 production-ready patterns for adding conditional branching logic to Google ADK's `SequentialAgent`. Each pattern is designed for different use cases and complexity levels.

---

## Pattern 1: Router-Based Orchestrator (Recommended for PriceGuard)

**Best For:** Clear decision logic, multiple distinct pipelines, maintainability

**How it works:**
1. Run analyst first
2. Extract routing signals from analysis
3. Route to appropriate `SequentialAgent` pipeline
4. Each pipeline handles different scenarios

**Advantages:**
- ✅ Clean separation of concerns
- ✅ Each pipeline is independent and testable
- ✅ Easy to add new routes
- ✅ Clear audit trail of which path was taken

**Code Implementation:**

\`\`\`python
# app/conditional_router.py

import logging
from typing import Any, Dict, Optional
from google.adk.agents import Agent, SequentialAgent
from google.adk.models import Gemini
from google.genai import types

logger = logging.getLogger(__name__)


class PriceguardConditionalRouter:
    """Routes price analysis through specialized agent pipelines based on conditions.
    
    Routing logic:
    - NO_DATA: No price found → log and exit
    - LOW_CONFIDENCE: Analyst confidence is Low → manual review pipeline
    - ALERT: High confidence + below threshold → urgent alert pipeline
    - INFO: Everything else → standard info pipeline
    """

    def __init__(
        self,
        analyst_agent: Agent,
        alert_pipeline: SequentialAgent,
        info_pipeline: SequentialAgent,
        review_pipeline: SequentialAgent,
        no_data_handler: Agent,
    ):
        """Initialize the router with all required agents and pipelines.
        
        Args:
            analyst_agent: Initial analyst that searches for prices
            alert_pipeline: SequentialAgent for high-priority alerts
            info_pipeline: SequentialAgent for informational updates
            review_pipeline: SequentialAgent for low-confidence reviews
            no_data_handler: Agent for handling no-results scenarios
        """
        self.analyst_agent = analyst_agent
        self.alert_pipeline = alert_pipeline
        self.info_pipeline = info_pipeline
        self.review_pipeline = review_pipeline
        self.no_data_handler = no_data_handler

    async def run(self, user_query: str, initial_state: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute the conditional routing pipeline.
        
        Args:
            user_query: User's price search query (e.g., "Check iPhone 15 Pro price")
            initial_state: Optional initial state dict
        
        Returns:
            Execution result from the selected pipeline
        
        Example:
            >>> router = PriceguardConditionalRouter(...)
            >>> result = await router.run("iPhone 15 Pro price below $900")
            >>> print(result["route_taken"])  # "ALERT" | "INFO" | "REVIEW" | "NO_DATA"
        """
        if initial_state is None:
            initial_state = {}

        # Step 1: Always run analyst first
        logger.info("[ConditionalRouter] Starting analyst agent...")
        analyst_result = await self.analyst_agent.run(user_query, initial_state)

        # Step 2: Extract routing signals
        analysis = analyst_result.get("price_analysis_result", {})
        
        confidence = analysis.get("confidence", "Low")
        below_threshold = analysis.get("below_threshold", False)
        found_price = bool(analysis.get("price"))
        threshold_value = analysis.get("threshold_value", None)

        logger.info(
            "[ConditionalRouter] Analysis complete: confidence=%s, below_threshold=%s, found_price=%s",
            confidence,
            below_threshold,
            found_price,
        )

        # Prepare state for downstream agents
        state_for_pipeline = {"price_analysis_result": analysis}

        # Step 3: Route based on conditions
        if not found_price:
            logger.warning("[ConditionalRouter] ROUTE: NO_DATA (no price found)")
            result = await self.no_data_handler.run(user_query, state_for_pipeline)
            return {**result, "route_taken": "NO_DATA", "routing_reason": "No price data found"}

        elif confidence == "Low":
            logger.warning("[ConditionalRouter] ROUTE: REVIEW (low confidence)")
            result = await self.review_pipeline.run(user_query, state_for_pipeline)
            return {**result, "route_taken": "REVIEW", "routing_reason": f"Low confidence (threshold: {confidence})"}

        elif confidence in ("High", "Medium") and below_threshold:
            logger.critical("[ConditionalRouter] ROUTE: ALERT (high-priority)")
            result = await self.alert_pipeline.run(user_query, state_for_pipeline)
            return {**result, "route_taken": "ALERT", "routing_reason": "Price below threshold - HIGH PRIORITY"}

        else:
            logger.info("[ConditionalRouter] ROUTE: INFO (informational)")
            result = await self.info_pipeline.run(user_query, state_for_pipeline)
            return {**result, "route_taken": "INFO", "routing_reason": "Standard informational update"}
\`\`\`

---

## Pattern 2: Inline Branching with Agent Instructions

**Best For:** Simple conditional logic, AI-driven decisions, fewer pipelines

**How it works:**
- Single router agent examines analysis
- Router agent's instruction tell it which decision to make
- Router calls different tools based on the decision

**Advantages:**
- ✅ Fewer moving parts
- ✅ Logic centralized in agent instruction
- ✅ Easier to modify without code changes
- ✅ LLM can apply nuanced decision logic

**Disadvantages:**
- ❌ Less deterministic (LLM decides)
- ❌ Harder to trace exact routing path
- ❌ Tool calls might vary

**Code Implementation:**

\`\`\`python
# app/agent_with_inline_branching.py

from google.adk.agents import Agent, SequentialAgent
from google.adk.models import Gemini
from google.genai import types
from app.tools import (
    send_to_slack,
    send_urgent_to_slack,
    send_to_qa_team,
    export_to_csv,
    export_to_pdf,
    insert_to_master_csv,
    search_prices,
)

# ── Specialist agents ────────────────────────────────────────────────────────

analyst_agent = Agent(
    name="analyst_agent",
    model=Gemini(model="gemini-flash-latest"),
    instruction="""You are the AnalystAgent for PriceGuard.
    
1. Use search_prices to find current prices
2. Extract: product name, lowest price, merchant, link
3. Compare to threshold
4. Provide confidence level: High / Medium / Low

Output as structured JSON with fields:
- product_name
- price
- merchant
- link
- threshold_value
- below_threshold
- confidence
- reasoning
""",
    tools=[search_prices],
    output_key="price_analysis_result",
)

# Router agent - decides routing based on analysis
router_agent = Agent(
    name="router_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(
            attempts=6,
            initialDelay=1.0,
            maxDelay=15.0,
            httpStatusCodes=[429, 500, 503, 504],
        ),
    ),
    instruction="""You are the RouterAgent for PriceGuard. Your job is to examine 
the price analysis and route it appropriately.

Retrieve the analysis from state: {price_analysis_result}

DECISION LOGIC:
1. If confidence is "Low" → Call send_to_qa_team with analysis for manual review
2. If confidence is "High" AND price is below threshold → Call send_urgent_to_slack with a 🚨 ALERT
3. Otherwise → Call send_to_slack with a standard price update

After routing, prepare a summary of what action was taken.
""",
    tools=[send_to_slack, send_urgent_to_slack, send_to_qa_team],
)

# Export agent - persists data
export_agent = Agent(
    name="export_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(
            attempts=6,
            initialDelay=1.0,
            maxDelay=15.0,
            httpStatusCodes=[429, 500, 503, 504],
        ),
    ),
    instruction="""You are the ExportAgent for PriceGuard.

Retrieve analysis: {price_analysis_result}

Extract and export:
1. Call insert_to_master_csv (for analytics)
2. Call export_to_csv (for human-readable snapshot)
3. Call export_to_pdf (for formatted report)

Report all three exports completed successfully.
""",
    tools=[insert_to_master_csv, export_to_csv, export_to_pdf],
)

# Pipeline - sequential execution with inline branching
root_agent = SequentialAgent(
    name="priceguard_conditional_pipeline",
    sub_agents=[analyst_agent, router_agent, export_agent],
)
\`\`\`

---

## Pattern 3: Dynamic Sub-Agent Selection

**Best For:** Variable workflows, configuration-driven behavior

**How it works:**
- Python code examines routing signals
- Dynamically selects which sub-agents to include
- Creates appropriate `SequentialAgent` on the fly

**Advantages:**
- ✅ Highly flexible
- ✅ Can build complex workflows dynamically
- ✅ No multiple pipelines to maintain
- ✅ Deterministic routing

**Code Implementation:**

\`\`\`python
# app/dynamic_agent_selection.py

import logging
from typing import Any, List
from google.adk.agents import Agent, SequentialAgent
from google.adk.models import Gemini
from google.genai import types
from app.tools import (
    send_to_slack,
    send_urgent_to_slack,
    send_to_qa_team,
    export_to_csv,
    export_to_pdf,
    insert_to_master_csv,
    search_prices,
)

logger = logging.getLogger(__name__)


def build_dynamic_pipeline(
    confidence: str,
    below_threshold: bool,
    found_price: bool,
) -> SequentialAgent:
    """Dynamically build appropriate pipeline based on routing signals.
    
    Args:
        confidence: "High" | "Medium" | "Low"
        below_threshold: True if price is below user threshold
        found_price: True if a price was found
    
    Returns:
        SequentialAgent configured for this scenario
    """
    
    if not found_price:
        logger.info("[DynamicSelection] Building NO_DATA pipeline")
        no_data_agent = Agent(
            name="no_data_agent",
            model=Gemini(model="gemini-flash-latest"),
            instruction="Price search returned no results. Suggest search refinements.",
            tools=[],
        )
        return SequentialAgent(
            name="no_data_pipeline",
            sub_agents=[no_data_agent],
        )

    elif confidence == "Low":
        logger.info("[DynamicSelection] Building LOW_CONFIDENCE pipeline")
        qa_agent = Agent(
            name="qa_review_agent",
            model=Gemini(model="gemini-flash-latest"),
            instruction="Review low-confidence price data for accuracy.",
            tools=[send_to_qa_team],
        )
        return SequentialAgent(
            name="low_confidence_pipeline",
            sub_agents=[qa_agent, export_agent],
        )

    elif confidence in ("High", "Medium") and below_threshold:
        logger.info("[DynamicSelection] Building ALERT pipeline")
        alert_agent = Agent(
            name="alert_agent",
            model=Gemini(model="gemini-flash-latest"),
            instruction="Format URGENT alert for Slack.",
            tools=[send_urgent_to_slack],
        )
        critical_export = Agent(
            name="critical_export",
            model=Gemini(model="gemini-flash-latest"),
            instruction="Flag as CRITICAL in all exports.",
            tools=[insert_to_master_csv, export_to_csv, export_to_pdf],
        )
        return SequentialAgent(
            name="alert_pipeline",
            sub_agents=[alert_agent, critical_export],
        )

    else:
        logger.info("[DynamicSelection] Building STANDARD pipeline")
        standard_agent = Agent(
            name="standard_agent",
            model=Gemini(model="gemini-flash-latest"),
            instruction="Format standard price update for Slack.",
            tools=[send_to_slack],
        )
        return SequentialAgent(
            name="standard_pipeline",
            sub_agents=[standard_agent, export_agent],
        )


class DynamicPriceguardOrchestrator:
    """Orchestrates PriceGuard with dynamically selected sub-agents."""

    def __init__(self, analyst_agent: Agent):
        self.analyst_agent = analyst_agent

    async def run(self, user_query: str) -> dict:
        """Execute with dynamic pipeline selection."""
        
        # Step 1: Run analyst
        logger.info("[DynamicOrchestrator] Running analyst...")
        analyst_result = await self.analyst_agent.run(user_query)
        analysis = analyst_result.get("price_analysis_result", {})
        
        # Step 2: Extract routing signals
        confidence = analysis.get("confidence", "Low")
        below_threshold = analysis.get("below_threshold", False)
        found_price = bool(analysis.get("price"))
        
        # Step 3: Build appropriate pipeline
        pipeline = build_dynamic_pipeline(
            confidence=confidence,
            below_threshold=below_threshold,
            found_price=found_price,
        )
        
        # Step 4: Execute pipeline
        state = {"price_analysis_result": analysis}
        result = await pipeline.run(user_query, state)
        
        return {
            **result,
            "route_selected": pipeline.name,
            "routing_signals": {
                "confidence": confidence,
                "below_threshold": below_threshold,
                "found_price": found_price,
            }
        }
\`\`\`

---

## Pattern 4: Routing Tool

**Best For:** Complex routing logic, reusable routing decisions, policy-driven behavior

**How it works:**
- Create a tool that encapsulates routing logic
- Agent calls this tool to determine next steps
- Tools can be shared and versioned

**Advantages:**
- ✅ Decouples routing from agent logic
- ✅ Easy to test routing in isolation
- ✅ Can update routing without changing agents
- ✅ Reusable across multiple workflows

**Code Implementation:**

\`\`\`python
# app/routing_tool.py

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


def route_price_analysis(
    confidence: str,
    below_threshold: bool,
    found_price: bool,
    threshold_value: float = None,
    price: float = None,
) -> dict:
    """Tool that evaluates routing based on analysis results.
    
    Args:
        confidence: "High" | "Medium" | "Low"
        below_threshold: True if price <= threshold
        found_price: True if price was found
        threshold_value: The threshold price
        price: The actual price found
    
    Returns:
        RoutingDecision dict with path and metadata
    """
    
    # Rule 1: No data found
    if not found_price:
        logger.warning("[RoutingTool] Decision: NO_DATA")
        return {
            "path": "NO_DATA",
            "priority": "LOW",
            "reason": "No price data found in search results",
            "should_escalate": False,
            "next_agent": "no_data_handler",
        }
    
    # Rule 2: Low confidence requires manual review
    if confidence == "Low":
        logger.warning("[RoutingTool] Decision: REVIEW (Low Confidence)")
        return {
            "path": "REVIEW",
            "priority": "HIGH",
            "reason": f"Low confidence ({confidence}) requires manual review",
            "should_escalate": True,
            "next_agent": "qa_review_agent",
        }
    
    # Rule 3: High confidence + below threshold = ALERT
    if confidence == "High" and below_threshold:
        savings = (threshold_value - price) if threshold_value and price else None
        reason = f"High confidence match: ${price} (threshold: ${threshold_value})"
        if savings:
            reason += f", savings: ${savings:.2f}"
        
        logger.critical("[RoutingTool] Decision: ALERT (High Priority)")
        return {
            "path": "ALERT",
            "priority": "CRITICAL",
            "reason": reason,
            "should_escalate": False,
            "next_agent": "urgent_alert_agent",
        }
    
    # Rule 4: Medium confidence + below threshold = elevated INFO
    if confidence == "Medium" and below_threshold:
        logger.info("[RoutingTool] Decision: INFO (Medium Confidence Alert)")
        return {
            "path": "INFO",
            "priority": "HIGH",
            "reason": "Medium confidence match below threshold",
            "should_escalate": False,
            "next_agent": "standard_alert_agent",
        }
    
    # Rule 5: Default = standard info
    logger.info("[RoutingTool] Decision: INFO (Standard)")
    return {
        "path": "INFO",
        "priority": "NORMAL",
        "reason": "Standard price update",
        "should_escalate": False,
        "next_agent": "standard_agent",
    }
\`\`\`

---
## Pattern 5: Hybrid Router + SequentialAgent (RECOMMENDED FOR PRICEGUARD)

**Best For:** Production systems, clear separation, maintainability, observability

**How it works:**
- Python-level router determines conditions
- Each specialized pipeline is a complete `SequentialAgent`
- Clean deterministic routing
- Each path is independently testable

**Advantages:**
- ✅ **Best of all worlds**
- ✅ Deterministic routing (no LLM variance)
- ✅ Each pipeline independent and testable
- ✅ Clear audit trail
- ✅ Easy to add new routes
- ✅ Excellent observability
- ✅ Production-ready

**Code Implementation:**

\`\`\`python
# app/hybrid_orchestrator.py

import logging
from enum import Enum
from typing import Any, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class RoutePath(str, Enum):
    """Enumeration of routing paths."""
    ALERT = "ALERT"
    INFO = "INFO"
    REVIEW = "REVIEW"
    NO_DATA = "NO_DATA"


@dataclass
class RoutingDecision:
    """Decision made by the router."""
    path: RoutePath
    priority: str
    reason: str
    confidence: str
    below_threshold: bool
    found_price: bool


class HybridPriceguardOrchestrator:
    """Hybrid orchestrator combining Python logic with SequentialAgent pipelines.
    
    This is the recommended pattern for production PriceGuard deployments.
    """

    def __init__(
        self,
        analyst_agent: Any,
        alert_pipeline: Any,
        info_pipeline: Any,
        review_pipeline: Any,
        no_data_pipeline: Any,
    ):
        """Initialize orchestrator with all required agents/pipelines."""
        self.analyst_agent = analyst_agent
        self.alert_pipeline = alert_pipeline
        self.info_pipeline = info_pipeline
        self.review_pipeline = review_pipeline
        self.no_data_pipeline = no_data_pipeline

    def _make_routing_decision(
        self, analysis: Dict[str, Any]
    ) -> RoutingDecision:
        """Determine routing based on analysis results.
        
        This is where all business logic for routing lives.
        Easy to test. Easy to modify. Deterministic.
        """
        
        confidence = analysis.get("confidence", "Low")
        below_threshold = analysis.get("below_threshold", False)
        found_price = bool(analysis.get("price"))
        threshold_value = analysis.get("threshold_value")
        price = analysis.get("price")

        # Decision Rule 1: No price found
        if not found_price:
            logger.warning(
                "[HybridOrchestrator] NO_DATA detected: search returned no results"
            )
            return RoutingDecision(
                path=RoutePath.NO_DATA,
                priority="LOW",
                reason="No price data found in search results",
                confidence=confidence,
                below_threshold=below_threshold,
                found_price=found_price,
            )

        # Decision Rule 2: Low confidence requires manual review
        if confidence == "Low":
            logger.warning(
                "[HybridOrchestrator] REVIEW detected: confidence=%s", confidence
            )
            return RoutingDecision(
                path=RoutePath.REVIEW,
                priority="HIGH",
                reason=f"Low analyst confidence ({confidence}) requires QA review",
                confidence=confidence,
                below_threshold=below_threshold,
                found_price=found_price,
            )

        # Decision Rule 3: High confidence + below threshold = CRITICAL ALERT
        if confidence == "High" and below_threshold:
            savings = (threshold_value - price) if threshold_value and price else None
            reason = f"🚨 HIGH CONFIDENCE MATCH: ${price} below threshold ${threshold_value}"
            if savings:
                reason += f" (SAVINGS: ${savings:.2f})"
            
            logger.critical(
                "[HybridOrchestrator] ALERT detected: price=%.2f, threshold=%.2f, savings=%.2f",
                price, threshold_value, savings or 0,
            )
            return RoutingDecision(
                path=RoutePath.ALERT,
                priority="CRITICAL",
                reason=reason,
                confidence=confidence,
                below_threshold=below_threshold,
                found_price=found_price,
            )

        # Decision Rule 4: Medium confidence + below threshold = elevated priority
        if confidence == "Medium" and below_threshold:
            logger.info(
                "[HybridOrchestrator] INFO (elevated): confidence=%s, price=%.2f",
                confidence, price,
            )
            return RoutingDecision(
                path=RoutePath.INFO,
                priority="HIGH",
                reason=f"Medium confidence match at ${price} below ${threshold_value}",
                confidence=confidence,
                below_threshold=below_threshold,
                found_price=found_price,
            )

        # Decision Rule 5: Default = standard info
        logger.info(
            "[HybridOrchestrator] INFO (standard): price=%.2f, threshold=%.2f",
            price, threshold_value,
        )
        return RoutingDecision(
            path=RoutePath.INFO,
            priority="NORMAL",
            reason=f"Price update: ${price}",
            confidence=confidence,
            below_threshold=below_threshold,
            found_price=found_price,
        )

    async def run(
        self, user_query: str, initial_state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Execute the complete orchestration flow."""
        
        if initial_state is None:
            initial_state = {}

        # Step 1: Always run analyst first
        logger.info(
            "[HybridOrchestrator] Starting pipeline for query: %s", user_query
        )
        analyst_result = await self.analyst_agent.run(user_query, initial_state)
        analysis = analyst_result.get("price_analysis_result", {})

        # Step 2: Make routing decision (deterministic)
        decision = self._make_routing_decision(analysis)
        logger.info(
            "[HybridOrchestrator] Routing decision: %s (priority=%s)",
            decision.path.value, decision.priority,
        )

        # Prepare state for selected pipeline
        state_for_pipeline = {"price_analysis_result": analysis}

        # Step 3: Execute selected pipeline
        logger.info(
            "[HybridOrchestrator] Executing %s pipeline", decision.path.value
        )

        if decision.path == RoutePath.NO_DATA:
            pipeline_result = await self.no_data_pipeline.run(
                user_query, state_for_pipeline
            )
        elif decision.path == RoutePath.REVIEW:
            pipeline_result = await self.review_pipeline.run(
                user_query, state_for_pipeline
            )
        elif decision.path == RoutePath.ALERT:
            pipeline_result = await self.alert_pipeline.run(
                user_query, state_for_pipeline
            )
        else:  # INFO
            pipeline_result = await self.info_pipeline.run(
                user_query, state_for_pipeline
            )

        logger.info("[HybridOrchestrator] Pipeline %s completed", decision.path.value)

        # Step 4: Return result with routing metadata
        return {
            **pipeline_result,
            "route_taken": decision.path.value,
            "routing_priority": decision.priority,
            "routing_reason": decision.reason,
            "routing_signals": {
                "confidence": decision.confidence,
                "below_threshold": decision.below_threshold,
                "found_price": decision.found_price,
            },
        }
\`\`\`

---
## Summary

- **Pattern 1**: Simple router class
- **Pattern 2**: LLM decides via instruction
- **Pattern 3**: Dynamic sub-agent selection
- **Pattern 4**: Tool-based routing
- **Pattern 5**: Hybrid (BEST) - deterministic routing + specialized pipelines

For **PriceGuard in production**, use **Pattern 5 (Hybrid Router)** for:
- ✅ Deterministic routing
- ✅ Clear audit trails
- ✅ Excellent testability
- ✅ Production-ready observability