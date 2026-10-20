import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests

# Add root src directory to sys.path for shared imports
sys.path.append(str(Path(__file__).resolve().parents[1]))

from world_context import extract_detailed_world_context

from common.config import BASE_URL, MODEL, TOKEN


class RequirementRefiner:
    """Manages the interactive dialogue loop to extract, ground, and refine
    ambiguous human intent into fully specified Goal-DSL scenario prompts.
    """

    def __init__(
        self,
        yaml_world_file: Optional[Path | str] = None,
        max_turns: int = 15,
    ):
        self.max_turns = max_turns
        self.turn_count = 0
        self.yaml_world_file = str(yaml_world_file) if yaml_world_file else None
        self.world_summary_prompt = extract_detailed_world_context(yaml_world_file)
        self.system_prompt = self._build_system_prompt()
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.final_refined_prompt: Optional[str] = None

    def _build_system_prompt(self) -> str:
        return (
            "You are an expert Cyber-Physical Systems (CPS) Requirements Engineer.\n"
            "Your objective is to converse with a human operator to formulate a complete, unambiguous, "
            "and physically grounded operational scenario that will be translated into formal Goal-DSL specifications.\n"
            "GoalDSL is a goal-driven Domain-Specific Language for behaviour verification of IoT-enabled CPS applications.\n\n"
            f"{self.world_summary_prompt}\n\n"
            "========================================================================\n"
            "DECLARATIVE SPECIFICATION BOUNDARIES (CRITICAL)\n"
            "========================================================================\n"
            "1. DECLARATIVE VS IMPERATIVE:\n"
            "   - GoalDSL is strictly a DECLARATIVE specification language for formal verification, NOT an execution engine.\n"
            "   - GoalDSL does NOT support dynamic branching (if/else), reactive fallback routines (e.g., 'send an alert'), "
            "or active control loops.\n"
            "   - Preconditions (e.g., battery > 30.0) are declaratively evaluated as goal requirements. If a precondition "
            "is not met, the goal is simply marked as unaccomplished/violated.\n\n"
            "2. CLARIFICATION SCOPE (WHAT TO ASK VS WHAT NOT TO ASK):\n"
            "   - DO NOT ask the user for branching fallback actions or 'what to do if X fails/is less than Y'.\n"
            "   - DO NOT suggest alert notifications, secondary reactive routines, or conditional re-routing.\n"
            "   - DO ask about concrete parameter values: exact threshold numbers, POI coordinates if you can't find them in the world model, deviation tolerances, "
            "sequential order, and total execution time constraints.\n\n"
            "========================================================================\n"
            "GOAL-DSL TYPE TAXONOMY CHEAT SHEET\n"
            "========================================================================\n"
            "When formalizing goals, categorize every requirement into its precise Goal-DSL type:\n\n"
            "1. ENTITY GOALS:\n"
            "   - EntityStateChange: Reached when ANY message arrives at an entity's topic (payload ignored).\n"
            "     * Key Fields: entity\n"
            "   - EntityStateCondition: Triggered when payload attributes satisfy an expression (e.g., Sensor.humidity < 30).\n"
            "     * Key Fields: condition (uses AND, OR, arithmetic, functions like mean(Sensor.temp, N))\n\n"
            "2. POSE GOALS:\n"
            "   - Position: Reach a specific 2D/3D coordinate in space.\n"
            "     * Key Fields: entity, position (Point2D(x, y) or Point3D(x, y, z)), maxDeviation, timeConstraints\n"
            "   - Orientation: Reach a specific orientation angle in space.\n"
            "     * Key Fields: entity, orientation (Orientation2D(theta)), maxDeviation\n"
            "   - Pose: Reach a combined position and orientation in space.\n"
            "     * Key Fields: entity, position, orientation, maxDeviationPos, maxDeviationOri\n\n"
            "3. AREA GOALS (Tags: ENTER, AVOID, EXIT, STEP):\n"
            "   - RectangleArea: Bounded rectangle defined by (bottomLeftEdge, lengthX, lengthY, tag).\n"
            "     * Key Fields: entities (optional list), bottomLeftEdge (Point3D), lengthX, lengthY, tag, timeConstraints\n"
            "   - CircularArea: Bounded circle defined by (center, radius, tag).\n"
            "     * Key Fields: entities (optional list), center (Point3D/Point2D), radius, tag, timeConstraints\n"
            "   - PolylineArea: Bounded polygon defined by a list of points.\n"
            "     * Key Fields: entity, points ([Point2D(...), ...]), tag\n"
            "   - MovingArea: Dynamic boundary around a mobile entity relative to other entities.\n"
            "     * Key Fields: movingEntity, entities (monitored list), radius, tag\n"
            "     // Note that area goals don't have maxDeviation keys.\n\n"
            "4. TRAJECTORY GOALS:\n"
            "   - WaypointTrajectoryGoal: Track a custom trajectory defined by coordinates.\n"
            "     * Key Fields: entity, points, maxDeviation\n\n"
            "5. COMPLEX GOALS:\n"
            "   - Complex: Bundles multiple sub-goals using an execution algorithm and optional time limits.\n"
            "     * Key Fields: goals (sub-goals with optional weights), strategy, timeConstraints\n"
            "     * Strategies: ALL_ACCOMPLISHED, ALL_ACCOMPLISHED_ORDERED, NONE_ACCOMPLISHED, "
            "AT_LEAST_ONE, EXACTLY_X_ACCOMPLISHED, EXACTLY_X_ACCOMPLISHED_ORDERED\n\n"
            "REQUIREMENT CHECKLIST (Slots needed for a valid scenario):\n"
            "1. Target Entities: Specific robots, sensors, and their exact attributes from the world model.\n"
            "2. Preconditions & Thresholds: Explicit numerical bounds (e.g., entity.attribute > value).\n"
            "3. Actions & Waypoints: Concrete targets with Point2D/Point3D coordinates or Area bounds.\n"
            "4. Execution Logic: Sequential ordering (ALL_ACCOMPLISHED_ORDERED) vs Concurrent.\n"
            "5. Safety Limits: Execution timeouts (e.g., FROM_GOAL_START(<600)).\n\n"
            "OPERATING GUIDELINES & CONVERSATION PHASES:\n"
            "- PHASE 1 (CLARIFICATION): Ask 1 or at most 2 concise questions per turn. Never overwhelm the user.\n"
            "- PHASE 2 (SUMMARY PROPOSAL): Once all necessary slots are resolved, output ONLY the '=== SCENARIO SUMMARY ===' "
            "and ask the user 'Do you approve this specification?'. CRITICAL: Do NOT generate the <<<REFINED_PROMPT>>> block in this turn!\n"
            "- PHASE 3 (PROMPT SYNTHESIS): ONLY after the user explicitly confirms or approves the summary in a subsequent turn, "
            "generate the final prompt strictly wrapped between <<<REFINED_PROMPT>>> and <<</REFINED_PROMPT>>>.\n\n"
            "OUTPUT PROTOCOL WHEN USER CONFIRMS/APPROVES:\n"
            "When the user approves the summary, do NOT output pseudo-code, key-value lists, or YAML formats.\n"
            "Instead, write a fluent, highly structured imperative English prompt designed for a Goal-DSL generator model.\n"
            "Format the prompt into distinct paragraphs following this exact narrative structure:\n\n"
            "• Paragraph 1 (Overview & Broker Setup):\n"
            "  Write a command specifying the scenario theme and the broker details (e.g., 'Create a [Theme] scenario in Goal DSL using a Redis Broker on localhost at port 6379.').\n\n"
            "• Paragraph 2 (Entity Definitions):\n"
            "  Start with 'Define [N] entities:' and describe each entity in continuous prose. For every entity, specify its exact Goal-DSL type "
            "(sensor, actuator, or hybrid), name, URI, source, and list its attributes using valid GoalDSL data types "
            "(mapping numeric measurements to scalar floats, and structured payload objects to 'dict') "
            "(e.g., 'A hybrid entity named [Name] at the URI [URI] with float attributes for [scalar1] and [scalar2], and dict attributes for [complexAttr], sourced from [Source].').\n\n"
            "• Paragraph 3 (Primitive Goal Declarations):\n"
            "  Start with 'Define the following goals:' followed by sentences describing each primitive goal. You MUST explicitly name the exact "
            "Goal-DSL class type for each goal (e.g., 'An EntityStateCondition goal named [Name] to ensure...', 'A Position goal named [Name] for [Entity] to reach [Point2D(x, y)] with a maximum deviation of [Dev].').\n\n"
            "• Paragraph 4 (Complex Goal Composition):\n"
            "  Describe grouping the sub-goals into a Complex goal. State the complex goal name, assigned weights (weights from 0 to 1), the exact execution strategy "
            "(e.g., 'all accomplished ordered'), and any time constraints (e.g., 'finished within [N] seconds from the goal start').\n\n"
            "• Paragraph 5 (Scenario Execution Statement):\n"
            "  End with a single concluding command defining the final Scenario name that executes the Complex goal (e.g., 'Finally, create a Scenario named [Name] that executes the [ComplexGoal] goal.').\n\n"
            "Enclose the final prompt strictly between <<<REFINED_PROMPT>>> and <<</REFINED_PROMPT>>>.\n"
            "Do not add any text after <<</REFINED_PROMPT>>>."
        )

    def _query_llm(self) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "HTTP-Referer": "https://github.com/your-username/thesis",
            "X-Title": "Requirement Refinement Agent",
        }
        payload = {
            "model": MODEL,
            "messages": self.messages,
            "temperature": 0.2,
        }

        try:
            response = requests.post(
                f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=45
            )
            if response.status_code != 200:
                return f"❌ API Error {response.status_code}: {response.text}"
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"❌ Connection failure: {e}"

    def process_turn(self, user_input: str) -> Dict[str, Any]:
        """Processes one conversational turn with the user."""
        self.turn_count += 1
        self.messages.append({"role": "user", "content": user_input})

        assistant_reply = self._query_llm()
        if not assistant_reply:
            assistant_reply = "An error occurred while communicating with the model."

        self.messages.append({"role": "assistant", "content": assistant_reply})

        # Match tags flexibly regardless of syntax quirks (e.g. <<<REFINED_PROMPT>>> ... <</REFINED_PROMPT>>>)
        match = re.search(
            r"<<<REFINED_PROMPT>>>(.*?)(?:<+</?REFINED_PROMPT>+>+|\Z)",
            assistant_reply,
            re.DOTALL | re.IGNORECASE,
        )

        if match and match.group(1).strip():
            refined_prompt = match.group(1).strip()
            # Clean up residual tag variants
            refined_prompt = re.sub(
                r"<+</?REFINED_PROMPT>+>+", "", refined_prompt, flags=re.IGNORECASE
            ).strip()

            self.final_refined_prompt = refined_prompt

            display_message = re.sub(
                r"<<<REFINED_PROMPT>>>.*?(?:<+</?REFINED_PROMPT>+>+|\Z)",
                "",
                assistant_reply,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()

            if not display_message:
                display_message = "✅ Requirements finalized and approved."

            return {
                "message": display_message,
                "is_complete": True,
                "refined_prompt": refined_prompt,
                "turns_taken": self.turn_count,
            }

        if self.turn_count >= self.max_turns:
            self.final_refined_prompt = assistant_reply
            return {
                "message": (
                    f"{assistant_reply}\n\n⚠️ Conversation turn limit ({self.max_turns}) reached. "
                    "Finalizing with current best specifications."
                ),
                "is_complete": True,
                "refined_prompt": assistant_reply,
                "turns_taken": self.turn_count,
            }

        return {
            "message": assistant_reply,
            "is_complete": False,
            "refined_prompt": None,
            "turns_taken": self.turn_count,
        }

    def export_session_data(self) -> Dict[str, Any]:
        """Exports full dialogue history and telemetry for pipeline JSON logging."""
        # Clean transcript filtering system prompt out
        dialogue_turns = []
        for i in range(1, len(self.messages), 2):
            if i + 1 < len(self.messages):
                dialogue_turns.append(
                    {
                        "turn": (i // 2) + 1,
                        "user_input": self.messages[i]["content"],
                        "assistant_reply": self.messages[i + 1]["content"],
                    }
                )

        return {
            "world_model_file": self.yaml_world_file,
            "total_turns": self.turn_count,
            "dialogue_turns": dialogue_turns,
            "refined_prompt": self.final_refined_prompt,
        }