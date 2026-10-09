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
            "and physically grounded operational scenario that will be translated into formal Goal-DSL specifications.\n\n"
            f"{self.world_summary_prompt}\n\n"
            "REQUIREMENT CHECKLIST (Slots needed for a valid scenario):\n"
            "1. Target Entities: Specific robots, sensors, and their exact attributes from the world model.\n"
            "2. Preconditions & Thresholds: Explicit numerical bounds (e.g., entity.attribute > value).\n"
            "3. Actions & Waypoints: Concrete targets with Point2D(x, y) coordinates from the POI list.\n"
            "4. Execution Logic: Sequential ordering (ALL_ACCOMPLISHED_ORDERED) vs Concurrent.\n"
            "5. Safety Limits: Execution timeouts (e.g., max 600s).\n\n"
            "OPERATING GUIDELINES:\n"
            "- Ask 1 or at most 2 concise questions per turn. Never overwhelm the user.\n"
            "- If the user names non-existent devices, point out the real available devices.\n"
            "- Once all necessary slots are resolved, output a structured summary starting with '=== SCENARIO SUMMARY ===' "
            "and ask the user to confirm ('Do you approve this specification?').\n\n"
            "OUTPUT PROTOCOL WHEN USER CONFIRMS/APPROVES:\n"
            "When the user approves the summary, do NOT output pseudo-code or YAML configs. "
            "Instead, generate a structured natural language prompt designed for a Goal-DSL compiler.\n"
            "The prompt MUST follow this exact structure:\n"
            "1. Broker Setup: Broker type (Redis by default), host ('localhost'), port (6379).\n"
            "2. Entity Definitions: Name, type (sensor, actuator, hybrid), exact URI, broker source, and typed attributes.\n"
            "3. Goal Definitions:\n"
            "   - EntityStateCondition goals: Goal name and condition expression (<Entity>.<attribute> <op> <value>).\n"
            "   - Position goals: Goal name, target entity, Point2D(x, y) coordinates, and maximum deviation tolerance.\n"
            "   - Complex goal: Goal name, list of sub-goals with unique weights (e.g., 0.1, 0.2), execution strategy, and time constraint.\n"
            "4. Scenario Definition: Scenario name, complex goal to execute, and concurrency setting.\n\n"
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