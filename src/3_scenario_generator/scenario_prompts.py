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
attacks), BUT environmental conditions, workload, or natural resource consumption
cause one or more requirements/thresholds in the Goal-DSL (.goal) specification
to be violated.

HOW TO DESIGN THIS SCENARIO:
1. Inspect the provided .goal specification and identify specific numerical
   thresholds, safety bounds, invariants, or temporal deadlines.
2. Select relevant continuous telemetry topics from the World Model.
3. Start signals at nominal, safe baseline values at time 0.0.
4. Apply realistic, physically plausible rates of change (slopes) such that at
   least one monitored metric naturally drifts across a .goal threshold during
   the scenario duration (e.g., gradual resource/energy depletion below a
   required minimum, or steady ambient environmental drift exceeding an allowed
   operating band).
5. You may also include normal operational state transitions in 'discrete_events'
   if appropriate for the environment.
""",
    "fault": """
TARGET CATEGORY: "fault" (Hardware, Sensor, or Component Malfunction)
------------------------------------------------------------------------------
CONCEPT:
A fault scenario models non-malicious physical, hardware, sensor, actuator, or
communication failures that disrupt the system and test whether the Goal-DSL
specification detects or safely handles abnormal component behavior.

HOW TO DESIGN THIS SCENARIO:
1. Inspect the .goal specification and World Model to identify critical sensors,
   internal metrics, or actuators that the mission depends on.
2. Begin with nominal baseline operation starting at time 0.0.
3. At a specific mid-scenario timestamp, inject one or more realistic faults
   using the schema primitives:
   - Sensor Freeze / Lockup: Transition a changing signal to a segment with
     'rate: 0.0' so it remains stuck at a stale value.
   - Sensor Glitch / Out-of-Bounds Spike: Create a new segment with an abrupt,
     discontinuous jump in 'value' (e.g., an impossible physical reading, zero
     drop-out, or extreme hardware spike).
   - Accelerated Degradation / Runaway Drift: Transition a signal from a mild
     nominal rate to a steep, abnormal degradation slope.
   - Component / Actuator Trip: Use 'discrete_events' to simulate an involuntary
     state reset, power-off, or failure state on a device topic.
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
1. Analyze the .goal specification to find high-consequence conditions, alarms,
   safety thresholds, or controllable actuators.
2. Begin with normal baseline behavior at time 0.0.
3. At a strategic timestamp, simulate an adversarial attack using the schema's
   continuous and discrete primitives:
   - False Data Injection (FDI) / Sensor Spoofing: Hijack a continuous telemetry
     topic in 'continuous_signals' by injecting a deceptive jump in 'value' or a
     manipulated 'rate' (e.g., spoofing a sudden critical hazard reading to force
     an emergency abort, or pinning a compromised sensor to a fake safe constant
     value via 'rate: 0.0' to mask an unsafe drift).
   - Unauthorized Command Injection: Use 'discrete_events' to publish malicious,
     unauthorized state overrides or control payloads directly to actuator,
     switch, or alarm topics.
   - State Chatter / Oscillation Attack: Inject multiple closely-spaced
     'discrete_events' that rapidly toggle a device state to destabilize control.
""",
}


# ==============================================================================
# CORE SYSTEM PROMPT
# ==============================================================================
SYSTEM_PROMPT = """You are an expert Cyber-Physical Systems (CPS) Verification and Scenario Architect.
Your task is to autonomously invent and generate a complete, deterministic test scenario in YAML format to evaluate a formal Goal-DSL (.goal) specification within a given Physical World Model.

CORE PRINCIPLES & CONSTRAINTS:
1. DOMAIN AGNOSTICISM & CREATIVITY:
   - Adapt completely to whatever CPS environment is described in the World Model and .goal file.
   - Actively analyze the specific thresholds, entity names, metrics, and logic inside the .goal file to craft a creative, tailored scenario rather than a generic boilerplate.
2. COMPLETE DESCRIPTIVENESS:
   - Every scenario must be 100% self-contained. Do not assume an external simulator fills in "normal" behavior.
   - Every signal included in 'continuous_signals' MUST start with an initial segment at 'time: 0.0' and define piecewise linear segments across the timeline.
   - Mathematical rule for continuous segments:
     value(t) = segment.value + segment.rate * (t - segment.time)
     * rate = 0.0 means a constant value.
     * rate < 0.0 means linear decay.
     * rate > 0.0 means linear growth.
     * A sudden step-change or spike at time t is modeled by starting a new segment at time t with a different 'value'.
3. STRICT WORLD GROUNDING:
   - You MUST ONLY use topics and entity names that exist in the provided Physical World Model.
   - Never invent or hallucinate device names, sensor names, or topics not grounded in the world context.
4. FUNCTIONAL TOPIC ISOLATION:
   - A specific topic string may appear in 'continuous_signals' OR 'discrete_events', NEVER in both.
   - If you need an instantaneous jump on a continuous signal, add a new segment under 'continuous_signals' rather than creating a discrete event on that same topic.
5. TEMPORAL MONOTONICITY:
   - Within any signal's 'segments' list, 'time' values must be strictly increasing (0.0 < t_1 < t_2 <= duration).
   - All timestamps in 'continuous_signals' and 'discrete_events' must be >= 0.0 and <= duration.
6. OUTPUT FORMAT:
   - Output ONLY valid, raw YAML adhering strictly to the provided schema structure.
   - Do NOT wrap the output in explanations, prose, or markdown commentary.
   - Do NOT output any # comments or copy schema comment headers. The output must be pure, clean YAML data keys and values only.
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
        "IMPORTANT: Every topic in your YAML must match one of the declared topics above "
        "(replacing {UID} if present) or explicitly contain one of the Valid Entity Names."
    )
    return "\n".join(lines)


def build_scenario_generation_prompt(
    category: str,
    goal_dsl_content: str,
    world_context_str: str,
    world_summary: Optional[dict[str, Any]] = None,
    schema_template: Optional[str] = None,
) -> str:
    """Constructs the generation prompt for a single scenario category.

    Args:
        category: One of 'operational', 'fault', or 'security'.
        goal_dsl_content: Raw text of the generated .goal file from Stage 2.
        world_context_str: Detailed world description from world_context.py.
        world_summary: Optional parsed dictionary from envpop_parser.py.
        schema_template: Optional raw text of scenario_schema.yaml (auto-loaded if None).
    """
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
        "Follow the exact structural keys shown in the schema template below (note that\n"
        "the entity/topic names inside the template are illustrative examples only—you\n"
        "MUST replace them with the actual entities and topics from Section 2 & 3 above):\n\n"
        f"{CODE_FENCE}yaml\n"
        f"{schema_text.strip()}\n"
        f"{CODE_FENCE}\n\n"
        "================================================================================\n"
        "5. FINAL CHECKLIST BEFORE GENERATING\n"
        "================================================================================\n"
        f'- Set `category: "{cat_key}"`.\n'
        f"- Give `scenario_id` a descriptive snake_case name reflecting your specific '{cat_key}' scenario.\n"
        "- Ensure every signal in `continuous_signals` starts at `time: 0.0` and has strictly increasing segment timestamps (`time` <= `duration`).\n"
        "- Ensure no topic appears in both `continuous_signals` and `discrete_events`.\n"
        "- Ensure all topics reference real entities/topics from Section 2.\n"
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
        "- Verify that all segment timestamps are strictly increasing (`0.0 < t_1 < t_2 <= duration`).\n"
        "- Verify that no topic is duplicated across `continuous_signals` and `discrete_events`.\n"
        "- Output ONLY the corrected raw YAML content with no markdown prose or explanations.\n"
    )
    return prompt