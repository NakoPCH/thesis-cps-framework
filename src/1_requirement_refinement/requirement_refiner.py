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
        self.world_summary_prompt = extract_detailed_world_context(yaml_world_file)
        self.system_prompt = self._build_system_prompt()
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def _build_system_prompt(self) -> str:
        return (
            "You are an expert Cyber-Physical Systems (CPS) Requirements Engineer.\n"
            "Your objective is to converse with a human operator to formulate a complete, unambiguous, "
            "and physically grounded operational scenario that will be translated into formal Goal-DSL specifications.\n"
            "GoalDSL is a goal-driven Domain-Specific Language for behaviour verification of IoT-enabled CPS applications.\n\n"
            f"{self.world_summary_prompt}\n\n"
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
            "     * Key Fields: movingEntity, entities (monitored list), radius, tag\n\n"
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
            "OPERATING GUIDELINES:\n"
            "- Ask 1 or at most 2 concise questions per turn. Never overwhelm the user.\n"
            "- If the user names non-existent devices, point out the real available devices.\n"
            "- Once all necessary slots are resolved, output a structured summary starting with '=== SCENARIO SUMMARY ===' "
            "and ask the user to confirm ('Do you approve this specification?').\n\n"
            "OUTPUT PROTOCOL WHEN USER CONFIRMS/APPROVES:\n"
            "When the user approves the summary, do NOT output pseudo-code or raw code. "
            "Generate a structured natural language prompt designed for a Goal-DSL generator model.\n"
            "The prompt MUST follow this exact structure:\n"
            "1. Broker Setup: Broker type (Redis by default), host ('localhost'), port (6379).\n"
            "2. Entity Definitions: Name, type (sensor, actuator, hybrid), exact URI, broker source, and typed attributes.\n"
            "3. Goal Definitions:\n"
            "   - For EACH goal, you MUST explicitly state its exact Goal-DSL Type from the taxonomy above "
            "(e.g., Type: EntityStateCondition, Type: Position, Type: CircularArea, Type: Complex, etc.).\n"
            "   - Explicitly list all required properties: targets, expressions, tags (ENTER/AVOID), coordinates, "
            "tolerances, sub-goal weights, execution strategies, and time constraints.\n"
            "4. Scenario Definition: Scenario name, main Complex goal to execute, and concurrency setting.\n\n"
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

        # Match tags regardless of whether the closing slash is present
        match = re.search(
            r"<<<REFINED_PROMPT>>>(.*?)(?:<<</?REFINED_PROMPT>>>|\Z)",
            assistant_reply,
            re.DOTALL,
        )

        if match and match.group(1).strip():
            refined_prompt = match.group(1).strip()
            # Clean up any stray tag fragments left behind
            refined_prompt = re.sub(
                r"<<</?REFINED_PROMPT>?>?", "", refined_prompt
            ).strip()

            display_message = re.sub(
                r"<<<REFINED_PROMPT>>>.*?(?:<<</?REFINED_PROMPT>>>|\Z)",
                "",
                assistant_reply,
                flags=re.DOTALL,
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