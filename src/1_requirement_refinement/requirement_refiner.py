import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

# Add root src directory to sys.path for shared imports
sys.path.append(str(Path(__file__).resolve().parents[1]))

# Import shared configurations
from common.config import BASE_URL, MODEL, TOKEN, WORLD_MODELS_DIR

# Import Tier-2 parser from constraint generator
sys.path.append(str(Path(__file__).resolve().parents[1] / "2_constraint_generator"))
try:
    from envpop_parser import parse_envpop_model
except ImportError:
    parse_envpop_model = None


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
        self.world_model = None
        self.world_summary_prompt = "No world model provided. Infer plausible cyber-physical entities."

        # Load and summarize world model if available
        if yaml_world_file and os.path.exists(yaml_world_file) and parse_envpop_model:
            try:
                self.world_model = parse_envpop_model(yaml_world_file)
                self.world_summary_prompt = (
                    "ACTIVE 2D PHYSICAL WORLD CONSTRAINTS:\n"
                    f"- Sensors: {list(self.world_model['sensors'].keys())}\n"
                    f"- Actuators / Robots: {list(self.world_model['actuators'].keys())}\n"
                    f"- Locations / POIs: {self.world_model['locations']}\n"
                    f"- Monitored Metrics: {list(self.world_model['entities_by_metric'].keys())}\n"
                    "CRITICAL: The user's system ONLY has these devices. You must strictly guide "
                    "the user to bind their scenario to these physical assets."
                )
            except Exception as e:
                print(f"⚠️ Warning: Could not parse world model: {e}")

        self.system_prompt = self._build_system_prompt()
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def _build_system_prompt(self) -> str:
        return (
            "You are an expert Cyber-Physical Systems (CPS) Requirements Engineer.\n"
            "Your objective is to converse with a human operator to formulate a complete, unambiguous, "
            "and physically grounded operational scenario that will be translated into formal Goal-DSL specifications.\n\n"
            f"{self.world_summary_prompt}\n\n"
            "REQUIREMENT CHECKLIST (Slots needed for a valid scenario):\n"
            "1. Target Entities: Specific robots, actuators, or sensors involved.\n"
            "2. Preconditions & Thresholds: Explicit numerical bounds (e.g., battery > 20.0, humidity < 40.0).\n"
            "3. Actions & Waypoints: Concrete targets or POIs (e.g., RefillStation, Plant coordinates).\n"
            "4. Execution Logic: Sequential (ALL_ACCOMPLISHED_ORDERED) vs Concurrent.\n"
            "5. Safety Limits: Timeouts (e.g., max 600s) or safe shutdown limits.\n\n"
            "OPERATING GUIDELINES:\n"
            "- Ask 1 or at most 2 concise, focused questions per turn. Never overwhelm the user.\n"
            "- If the user names non-existent devices, point out the real available devices and propose an alternative.\n"
            "- For minor details (e.g., standard timeouts or tolerances), infer reasonable defaults and state what you assumed.\n"
            "- Once all necessary slots are resolved, output a structured summary starting with '=== SCENARIO SUMMARY ===' "
            "and ask the user to confirm (e.g., 'Do you approve this specification?').\n"
            "- When the user explicitly approves/confirms the summary, output the final prompt for the compiler wrapped "
            "strictly between <<<REFINED_PROMPT>>> and <<</REFINED_PROMPT>>> tags."
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
        """Processes one conversational turn with the user.

        Returns a dictionary containing:
          - 'message': The assistant's textual response.
          - 'is_complete': Boolean indicating if refinement is finalized.
          - 'refined_prompt': The final prompt string (if finalized), else None.
        """
        self.turn_count += 1
        self.messages.append({"role": "user", "content": user_input})

        assistant_reply = self._query_llm()
        if not assistant_reply:
            assistant_reply = "An error occurred while communicating with the reasoning model."

        self.messages.append({"role": "assistant", "content": assistant_reply})

        # Check if the model produced the final verified output tag
        match = re.search(
            r"<<<REFINED_PROMPT>>>(.*?)<<</REFINED_PROMPT>>>",
            assistant_reply,
            re.DOTALL,
        )

        if match:
            refined_prompt = match.group(1).strip()
            # Clean up the output message presented to the user
            display_message = re.sub(
                r"<<<REFINED_PROMPT>>>.*?<<</REFINED_PROMPT>>>",
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

        # Check turn budget safety
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