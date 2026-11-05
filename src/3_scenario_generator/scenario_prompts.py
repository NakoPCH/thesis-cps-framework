"""scenario_prompts.py

Prompt templates and builders for Stage 3 (Scenario & Fault Generation).
Designed to be 100% domain-agnostic across any Cyber-Physical System (CPS)
environment, dynamically grounding scenarios in the active .goal specification
and physical world model.
"""

from pathlib import Path
from typing import Any, Optional

SCHEMA_FILE_PATH = Path(__file__).resolve().parent / "scenario_schema.yaml"
CODE_FENCE = "`" * 3


def load_scenario_schema() -> str:
    """Loads the reference scenario_schema.yaml template from disk."""
    if SCHEMA_FILE_PATH.exists():
        return SCHEMA_FILE_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"Scenario schema file not found at: {SCHEMA_FILE_PATH}"
    )


# ==============================================================================
# CATEGORY-SPECIFIC DESIGN INSTRUCTIONS (DOMAIN-AGNOSTIC)
# ==============================================================================
CATEGORY_INSTRUCTIONS: dict[str, str] = {
    "operational": """
TARGET CATEGORY: "operational" (Operational Stress & Goal Boundary Violation)
------------------------------------------------------------------------------
CONCEPT:
An operational scenario models a physically realistic run where all hardware,
sensors, and communication links function normally (no broken devices or cyber
attacks), BUT environmental conditions, heavy workload, or natural resource
consumption cause one or more requirements/thresholds in the Goal-DSL (.goal)
specification to fail, remain uncompleted, or trigger an explicit fatal goal.

HOW TO DESIGN THIS SCENARIO:
- Inspect the provided .goal specification and identify numerical thresholds,
  safety bounds, invariants, unachieved achievement targets, or fatal conditions.
- Select the relevant broker publisher topic and target attribute names from the World Model.
- For multidimensional metrics (such as 2D/3D coordinates or positions), declare
  independent continuous signals for each axis using sub-attribute dot notation
  (e.g., attribute: "position.x" and attribute: "position.y").
- Start signals at nominal, safe baseline values at time 0.0.
- Apply realistic rates of change (slopes) such that at least one monitored metric
  naturally drifts across a .goal threshold, breaches an invariant, or triggers
  a fatal goal/condition during the duration.
- 'discrete_events' is OPTIONAL. If your scenario does not involve discrete
  actuator commands, leave 'discrete_events: []'.
""",
    "fault": """
TARGET CATEGORY: "fault" (Hardware, Sensor, or Component Malfunction)
------------------------------------------------------------------------------
CONCEPT:
A fault scenario models non-malicious physical, hardware, sensor, actuator, or
communication failures that disrupt the system and test whether the Goal-DSL
specification detects or safely handles abnormal component behavior.

HOW TO DESIGN THIS SCENARIO:
- Inspect the .goal specification and World Model to identify critical sensors,
  internal metrics, or actuators that the mission depends on.
- Begin with nominal baseline operation starting at time 0.0.
- At a specific mid-scenario timestamp, inject one or more realistic faults:
  * Sensor Freeze / Lockup: Transition a signal to a segment with 'rate: 0.0'
    so it remains stuck at a stale value.
  * Sensor Glitch / Abrupt Discontinuity: Create a segment with an instantaneous,
    discontinuous jump in 'value' (e.g., an impossible spike, sudden zero drop-out,
    or wild out-of-bounds reading). Step-jumps are completely valid fault primitives.
  * Accelerated Degradation / Runaway Drift: Transition a signal from a mild
    nominal rate to a steep abnormal degradation slope.
  * Component / Actuator Trip: Use 'discrete_events' ONLY if an actual actuator
    topic experiences an involuntary power-off, trip, or state reset.
- If all injected faults are sensor-level (freezes, spikes, drift), leave
  'discrete_events: []'. NEVER invent diagnostic text or events on unrelated
  topics to bypass topic isolation.
""",
    "security": """
TARGET CATEGORY: "security" (Adversarial / Cyber-Physical Broker Attack)
------------------------------------------------------------------------------
CONCEPT:
A security scenario models deliberate, malicious interference over the CPS
communication broker (e.g., Man-in-the-Middle spoofing, False Data Injection,
or unauthorized command injection) aimed at violating system safety, deceiving
the monitor, or hijacking actuators.

HOW TO DESIGN THIS SCENARIO:
- Analyze the .goal specification to find high-consequence conditions, alarms,
  safety thresholds, or controllable actuators.
- Begin with normal baseline behavior at time 0.0.
- At a strategic timestamp, simulate an adversarial attack:
  * False Data Injection (FDI) / Sensor Spoofing: Hijack a continuous telemetry
    attribute by injecting an abrupt malicious jump in 'value' or a manipulated 'rate'
    (e.g., spoofing a hazard reading or pinning a sensor to a fake safe constant
    via 'rate: 0.0' to conceal an unsafe condition).
  * Unauthorized Command Injection: Use 'discrete_events' to publish malicious,
    unauthorized state overrides directly to real actuator control topics.
  * State Chatter / Oscillation Attack: Inject closely-spaced 'discrete_events'
    that rapidly toggle a device state to destabilize control.
- If the attack is purely sensor spoofing / FDI over continuous telemetry,
  leave 'discrete_events: []'.
""",
}


# ==============================================================================
# CORE SYSTEM PROMPT
# ==============================================================================
SYSTEM_PROMPT = """You are an expert Cyber-Physical Systems (CPS) Verification and Scenario Architect.
Your task is to autonomously invent and generate a complete, deterministic test scenario in YAML format to evaluate a formal Goal-DSL (.goal) specification within a given Physical World Model.

CORE PRINCIPLES & CONSTRAINTS:
- DOMAIN AGNOSTICISM & CREATIVITY:
  Adapt completely to whatever CPS environment is described in the World Model and .goal file. Actively analyze the specific thresholds, entity names, metrics, and logic inside the .goal file to craft a creative, tailored scenario rather than a generic boilerplate.
- COMPLETE DESCRIPTIVENESS:
  Every scenario must be 100% self-contained. Do not assume an external simulator fills in "normal" behavior. Every signal included in 'continuous_signals' MUST start with an initial segment at 'time: 0.0' and define piecewise linear segments across the timeline.
  Mathematical rule for continuous segments:
    value(t) = segment.value + segment.rate * (t - segment.time)
    * rate = 0.0 means a constant value.
    * rate < 0.0 means linear decay.
    * rate > 0.0 means linear growth.
    * An abrupt step-change or glitch spike at time t is modeled by starting a new segment at time t with a discontinuous 'value'.
- STRICT BROKER TOPIC & ATTRIBUTE SEPARATION:
  * In both 'continuous_signals' and 'discrete_events', the 'topic' field MUST be an exact communication broker topic declared in the World Model.
  * Both 'continuous_signals' and 'discrete_events' MUST specify the target property using the separate 'attribute' key (e.g., attribute: "battery", attribute: "state").
  * For 'discrete_events', assign the new state directly using 'value:' (e.g., value: 1 or value: true). Do NOT output a nested 'payload' dictionary.
  * For multidimensional/nested variables (such as 2D/3D coordinates or vector states), represent each scalar axis as an independent continuous signal using dot notation in 'attribute' (e.g., attribute: "position.x", attribute: "position.y").
  * NEVER invent composite topics or use DSL entity syntax in topics (do NOT write '<EntityName>.<attribute>'). Topics and attributes must always remain in their separate designated schema keys.
- FUNCTIONAL TOPIC ISOLATION:
  * A specific broker topic string may appear in 'continuous_signals' OR 'discrete_events', NEVER in both.
  * 'discrete_events' is OPTIONAL. If your scenario does not require discrete actuator commands, leave 'discrete_events: []'. NEVER invent fake events on unrelated topics.
- STRICT TEMPORAL MONOTONICITY & STEP JUMPS:
  * Within any signal's 'segments' list, 'time' values must be strictly increasing (0.0 <= t_0 < t_1 < ... <= duration).
  * All timestamps in 'continuous_signals' and 'discrete_events' must be >= 0.0 and <= duration.
  * NEVER emit two segments with the identical timestamp.
  * To model an instantaneous step-jump, glitch, or state transition at timestamp T, start the new segment directly at 'time: T' with the new target 'value'. The preceding segment implicitly terminates at T without requiring an end-marker segment.
- OUTPUT FORMAT:
  * Output ONLY valid, raw YAML adhering strictly to the provided schema structure.
  * Do NOT wrap the output in explanations, prose, or markdown commentary.
  * Do NOT copy # comments from the schema or output any commented lines. Generate pure YAML key-value data only.
"""


# ==============================================================================
# PROMPT BUILDERS
# ==============================================================================
def _format_grounding_inventory(
    world_summary: Optional[dict[str, Any]] = None,
) -> str:
    """Formats extracted entity names and topics from envpop_parser into a strict checklist."""
    if not world_summary:
        return ""

    entities = world_summary.get("all_valid_entity_names", [])
    pub_topics = world_summary.get("all_topics", {}).get("publishers", [])
    sub_topics = world_summary.get("all_topics", {}).get("subscribers", [])

    lines = ["STRICT GROUNDING INVENTORY (Extracted from World Model):"]
    if entities:
        lines.append(f"- Valid Entity Names: {', '.join(entities)}")
    if pub_topics:
        lines.append(f"- Declared Publisher Topics: {', '.join(pub_topics)}")
    if sub_topics:
        lines.append(f"- Declared Subscriber Topics: {', '.join(sub_topics)}")
    lines.append(
        "CRITICAL TOPIC & ATTRIBUTE RULES:\n"
        "1. Every 'topic' entry in your YAML must strictly match one of the declared publisher or subscriber topics above.\n"
        "2. To target an entity's attribute, place the declared publisher topic in 'topic:' and the target field name in 'attribute:'.\n"
        "3. For 2D/3D coordinates or vector quantities (e.g., position), define separate continuous signals using dot notation in 'attribute:' (e.g., attribute: 'position.x', attribute: 'position.y').\n"
        "4. 'discrete_events' is OPTIONAL. If no discrete actuator transitions are needed, write 'discrete_events: []'."
    )
    return "\n".join(lines)


def build_scenario_generation_prompt(
    category: str,
    goal_dsl_content: str,
    world_context_str: str,
    world_summary: Optional[dict[str, Any]] = None,
    schema_template: Optional[str] = None,
) -> str:
    """Constructs the generation prompt for a single scenario category."""
    cat_key = category.strip().lower()
    if cat_key not in CATEGORY_INSTRUCTIONS:
        raise ValueError(
            f"Unsupported scenario category '{category}'. "
            f"Expected one of: {list(CATEGORY_INSTRUCTIONS.keys())}"
        )

    schema_text = schema_template or load_scenario_schema()
    inventory_block = _format_grounding_inventory(world_summary)

    prompt = (
        f"Generate one complete, creative CPS test scenario for the '{cat_key}' category.\n\n"
        "================================================================================\n"
        "1. CATEGORY OBJECTIVE & DESIGN RULES\n"
        "================================================================================\n"
        f"{CATEGORY_INSTRUCTIONS[cat_key].strip()}\n\n"
        "================================================================================\n"
        "2. ACTIVE PHYSICAL WORLD MODEL\n"
        "================================================================================\n"
        f"{world_context_str.strip()}\n\n"
        f"{inventory_block}\n\n"
        "================================================================================\n"
        "3. TARGET GOAL-DSL (.goal) SPECIFICATION UNDER TEST\n"
        "================================================================================\n"
        f"{CODE_FENCE}goal\n"
        f"{goal_dsl_content.strip()}\n"
        f"{CODE_FENCE}\n\n"
        "================================================================================\n"
        "4. REQUIRED YAML SCHEMA TEMPLATE & FORMAT\n"
        "================================================================================\n"
        "Follow the structural keys shown below. Map user requirements to actual topics "
        "and attributes from Section 2 & 3:\n\n"
        f"{CODE_FENCE}yaml\n"
        f"{schema_text.strip()}\n"
        f"{CODE_FENCE}\n\n"
        "================================================================================\n"
        "5. FINAL CHECKLIST BEFORE GENERATING\n"
        "================================================================================\n"
        f'- Set `category: "{cat_key}"`.\n'
        f"- Give `scenario_id` a descriptive snake_case name reflecting your '{cat_key}' test.\n"
        "- In `continuous_signals`, use the exact broker topic in `topic:` and the specific variable in `attribute:`.\n"
        "- In `discrete_events`, format each event with `time`, `topic`, `attribute`, and `value` (do NOT output a `payload:` block).\n"
        "- For 2D/3D vectors or coordinates, use dot notation in `attribute:` (e.g., `position.x`, `position.y`).\n"
        "- If the scenario does not require actuator switching, leave `discrete_events: []`.\n"
        "- Ensure every continuous signal has an initial segment starting at `time: 0.0`.\n"
        "- Ensure all segment timestamps are strictly increasing (`0.0 <= t_0 < t_1 < ... <= duration`).\n"
        "- Do NOT copy any `#` comments from the schema or output any commented lines.\n"
        "- Output ONLY the raw YAML content.\n"
    )
    return prompt


def build_scenario_repair_prompt(
    category: str,
    broken_yaml: str,
    validation_feedback: str,
    goal_dsl_content: str,
    world_context_str: str,
    world_summary: Optional[dict[str, Any]] = None,
) -> str:
    """Constructs the self-repair prompt when scenario_validator.py detects errors."""
    cat_key = category.strip().lower()
    inventory_block = _format_grounding_inventory(world_summary)

    prompt = (
        f"Your previously generated '{cat_key}' scenario YAML failed automated validation.\n"
        "Review the exact line-level validation errors below and output the corrected YAML.\n\n"
        "================================================================================\n"
        "1. VALIDATION ERRORS TO FIX\n"
        "================================================================================\n"
        f"{validation_feedback.strip()}\n\n"
        "================================================================================\n"
        "2. PREVIOUS INVALID YAML OUTPUT\n"
        "================================================================================\n"
        f"{CODE_FENCE}yaml\n"
        f"{broken_yaml.strip()}\n"
        f"{CODE_FENCE}\n\n"
        "================================================================================\n"
        "3. REFERENCE CONTEXT (WORLD MODEL & .GOAL SPECIFICATION)\n"
        "================================================================================\n"
        f"{world_context_str.strip()}\n\n"
        f"{inventory_block}\n\n"
        "Target Goal-DSL (.goal):\n"
        f"{CODE_FENCE}goal\n"
        f"{goal_dsl_content.strip()}\n"
        f"{CODE_FENCE}\n\n"
        "================================================================================\n"
        "4. REPAIR INSTRUCTIONS\n"
        "================================================================================\n"
        f"- Fix every error listed in Section 1 while preserving the creative '{cat_key}' scenario intent.\n"
        f'- Ensure `category: "{cat_key}"` is preserved.\n'
        "- Ensure every continuous signal defines both 'topic' (declared broker topic) and 'attribute' (target field, using dot notation like 'position.x' if compound).\n"
        "- Ensure every discrete event defines 'time', 'topic', 'attribute', and 'value'.\n"
        "- If discrete events on a topic conflict with continuous telemetry, move or remove the discrete event ('discrete_events: []' is completely valid).\n"
        "- Verify that all segment timestamps are strictly increasing (`0.0 <= t_0 < t_1 < ... <= duration`).\n"
        "- Do NOT output any `#` comments or markdown explanations.\n"
        "- Output ONLY the corrected raw YAML content.\n"
    )
    return prompt