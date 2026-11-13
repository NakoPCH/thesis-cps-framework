"""scenario_validator.py

Validates LLM-generated CPS test scenarios with line-level tracking.
Produces structured diagnostic messages (row number, source row content,
error classification, and suggested fixes) tailored for LLM self-repair loops.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# Resolve local and cross-stage import paths
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent
stage2_dir = src_dir / "2_constraint_generator"

for path in (current_dir, src_dir, stage2_dir):
    if str(path) not in sys.path:
        sys.path.append(str(path))

try:
    from envpop_parser import parse_envpop_model
except ImportError:
    parse_envpop_model = None


VALID_CATEGORIES = {"nominal", "operational", "fault", "security"}


class LineTrackingSafeLoader(yaml.SafeLoader):
    """Custom YAML loader that embeds 1-indexed '__line__' into every parsed mapping node."""

    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep=deep)
        mapping["__line__"] = node.start_mark.line + 1
        return mapping


@dataclass
class ValidationError:
    """Detailed error object tailored for LLM self-repair prompts."""

    line_number: Optional[int]
    error_type: str
    source_line: Optional[str]
    message: str
    fix_hint: str

    def format_for_llm(self) -> str:
        line_str = f"Line {self.line_number}" if self.line_number else "Line Unknown"
        lines = [
            f"❌ [{line_str}] {self.error_type}:",
            f"   Issue: {self.message}",
        ]
        if self.source_line:
            lines.insert(1, f"   Row Content: '{self.source_line.strip()}'")
        if self.fix_hint:
            lines.append(f"   Suggested Fix: {self.fix_hint}")
        return "\n".join(lines)


@dataclass
class ValidationResult:
    """Container holding validation state, formatted errors, warnings, and sanitized data."""

    is_valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scenario_data: Optional[dict[str, Any]] = None

    def get_llm_feedback(self) -> str:
        """Returns the complete error block ready to inject into LLM reflection prompts."""
        if self.is_valid:
            return "Validation PASSED with no errors."

        feedback_lines = [
            f"Validation FAILED with {len(self.errors)} error(s). Please review and correct each error below:"
        ]
        for idx, err in enumerate(self.errors, start=1):
            feedback_lines.append(f"\n[{idx}] {err.format_for_llm()}")
        return "\n".join(feedback_lines)

    def __str__(self) -> str:
        status = "PASSED" if self.is_valid else "FAILED"
        lines = [f"--- Scenario Validation Result: {status} ---"]
        if self.errors:
            lines.append("Errors for Self-Repair:")
            for err in self.errors:
                lines.append(err.format_for_llm())
        if self.warnings:
            lines.append("Warnings:")
            for warn in self.warnings:
                lines.append(f"  ⚠️ {warn}")
        return "\n".join(lines)


def _strip_line_metadata(data: Any) -> Any:
    """Recursively removes internal '__line__' tracking keys from the scenario data."""
    if isinstance(data, dict):
        return {k: _strip_line_metadata(v) for k, v in data.items() if k != "__line__"}
    if isinstance(data, list):
        return [_strip_line_metadata(item) for item in data]
    return data


def _get_key_line_and_text(
    start_line: Optional[int], key_name: str, source_lines: list[str]
) -> tuple[Optional[int], Optional[str]]:
    """Locates the exact line number and text for a specific key within an entity block."""
    if not start_line or not source_lines:
        return None, None

    for idx in range(start_line - 1, min(len(source_lines), start_line + 15)):
        line_str = source_lines[idx]
        if re.search(rf"^\s*-?\s*{re.escape(key_name)}\s*:", line_str):
            return idx + 1, line_str

    if 0 <= start_line - 1 < len(source_lines):
        return start_line, source_lines[start_line - 1]
    return start_line, None


def _topic_matches_world(topic: str, declared_topics: set[str]) -> bool:
    """Strictly checks whether a topic exists in declared publisher/subscriber topics,

    supporting template wildcards like {UID} and *.
    """
    if topic in declared_topics:
        return True

    for declared in declared_topics:
        pattern = re.sub(r"\{UID\}", r"[^.]+", re.escape(declared))
        pattern = pattern.replace(r"\*", r".*")
        if re.fullmatch(pattern, topic):
            return True

    return False


def validate_scenario(
    scenario_input: str | Path | dict[str, Any],
    world_model_path: Optional[str | Path] = None,
) -> ValidationResult:
    """Validates an LLM-generated scenario against schema, math, time, and world constraints."""
    errors: list[ValidationError] = []
    warnings: list[str] = []
    source_lines: list[str] = []

    # 1. Parsing & Ingestion with Line Tracking
    if isinstance(scenario_input, (str, Path)):
        input_path = Path(scenario_input)
        if input_path.is_file():
            try:
                with open(input_path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
            except Exception as e:
                return ValidationResult(
                    is_valid=False,
                    errors=[
                        ValidationError(
                            line_number=None,
                            error_type="File Read Error",
                            source_line=None,
                            message=f"Could not read scenario file '{input_path.name}': {e}",
                            fix_hint="Ensure the file exists and has valid UTF-8 encoding.",
                        )
                    ],
                )
        else:
            raw_text = str(scenario_input)

        source_lines = raw_text.splitlines()

        try:
            data = yaml.load(raw_text, Loader=LineTrackingSafeLoader)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            line_no = (mark.line + 1) if mark else None
            snippet = (
                source_lines[mark.line]
                if mark and 0 <= mark.line < len(source_lines)
                else None
            )
            return ValidationResult(
                is_valid=False,
                errors=[
                    ValidationError(
                        line_number=line_no,
                        error_type="YAML Syntax Error",
                        source_line=snippet,
                        message=str(e),
                        fix_hint="Fix indentation, unescaped characters, or malformed YAML keys.",
                    )
                ],
            )
    elif isinstance(scenario_input, dict):
        data = scenario_input
    else:
        return ValidationResult(
            is_valid=False,
            errors=[
                ValidationError(
                    line_number=None,
                    error_type="Input Type Error",
                    source_line=None,
                    message=f"Expected YAML string, Path, or dict, received {type(scenario_input).__name__}.",
                    fix_hint="Pass valid YAML text or a loaded dictionary.",
                )
            ],
        )

    if not isinstance(data, dict):
        return ValidationResult(
            is_valid=False,
            errors=[
                ValidationError(
                    line_number=1 if source_lines else None,
                    error_type="Schema Root Error",
                    source_line=source_lines[0] if source_lines else None,
                    message="Top-level scenario YAML must be a key-value mapping/dictionary, not a list or scalar.",
                    fix_hint="Ensure your output begins with root keys such as 'scenario_id:' and 'duration:'.",
                )
            ],
        )

    # 2. Mandatory Top-Level Fields
    scenario_id = data.get("scenario_id")
    if not scenario_id or not isinstance(scenario_id, str):
        line_no, line_text = _get_key_line_and_text(
            data.get("__line__", 1), "scenario_id", source_lines
        )
        errors.append(
            ValidationError(
                line_number=line_no,
                error_type="Schema Error",
                source_line=line_text,
                message=f"'scenario_id' is required and must be a non-empty string. Got {scenario_id}.",
                fix_hint="Add 'scenario_id: \"unique_scenario_name\"' at the root level.",
            )
        )

    category = data.get("category")
    if category not in VALID_CATEGORIES:
        line_no, line_text = _get_key_line_and_text(
            data.get("__line__", 1), "category", source_lines
        )
        errors.append(
            ValidationError(
                line_number=line_no,
                error_type="Category Error",
                source_line=line_text,
                message=f"'category' must be one of {sorted(list(VALID_CATEGORIES))}, got '{category}'.",
                fix_hint="Set category to 'nominal', 'operational', 'fault', or 'security'.",
            )
        )

    duration = data.get("duration")
    if duration is None or not isinstance(duration, (int, float)) or duration <= 0:
        line_no, line_text = _get_key_line_and_text(
            data.get("__line__", 1), "duration", source_lines
        )
        errors.append(
            ValidationError(
                line_number=line_no,
                error_type="Duration Error",
                source_line=line_text,
                message=f"'duration' must be a strictly positive number (> 0). Got {duration}.",
                fix_hint="Specify total simulation runtime in seconds, e.g., 'duration: 120.0'.",
            )
        )
        duration = 0.0

    continuous_signals = data.get("continuous_signals", [])
    discrete_events = data.get("discrete_events", [])

    if not isinstance(continuous_signals, list):
        line_no, line_text = _get_key_line_and_text(
            data.get("__line__", 1), "continuous_signals", source_lines
        )
        errors.append(
            ValidationError(
                line_number=line_no,
                error_type="Schema Error",
                source_line=line_text,
                message="'continuous_signals' must be a list of signal specifications.",
                fix_hint="Declare 'continuous_signals:' as a YAML list of signal blocks.",
            )
        )
        continuous_signals = []

    if not isinstance(discrete_events, list):
        line_no, line_text = _get_key_line_and_text(
            data.get("__line__", 1), "discrete_events", source_lines
        )
        errors.append(
            ValidationError(
                line_number=line_no,
                error_type="Schema Error",
                source_line=line_text,
                message="'discrete_events' must be a list of event specifications.",
                fix_hint="Declare 'discrete_events:' as a YAML list of event blocks (or 'discrete_events: []').",
            )
        )
        discrete_events = []

    if len(continuous_signals) == 0 and len(discrete_events) == 0:
        errors.append(
            ValidationError(
                line_number=data.get("__line__", 1),
                error_type="Empty Scenario Error",
                source_line=source_lines[0] if source_lines else None,
                message="The scenario contains neither continuous signals nor discrete events.",
                fix_hint="Populate at least one signal under 'continuous_signals' or event under 'discrete_events'.",
            )
        )

    # 3. Continuous Signals Math & Temporal Monotonicity
    continuous_topics: set[str] = set()
    seen_signal_attributes: set[tuple[str, str]] = set()

    for s_idx, signal in enumerate(continuous_signals):
        if not isinstance(signal, dict):
            errors.append(
                ValidationError(
                    line_number=None,
                    error_type="Schema Error",
                    source_line=None,
                    message=f"continuous_signals[{s_idx}] must be a mapping dictionary.",
                    fix_hint="Format each continuous signal as a dictionary containing 'topic:', 'attribute:', and 'segments:'.",
                )
            )
            continue

        sig_line = signal.get("__line__")
        topic = signal.get("topic")
        attribute = signal.get("attribute")

        if not topic or not isinstance(topic, str):
            line_no, line_text = _get_key_line_and_text(sig_line, "topic", source_lines)
            errors.append(
                ValidationError(
                    line_number=line_no or sig_line,
                    error_type="Topic Error",
                    source_line=line_text,
                    message=f"continuous_signals[{s_idx}] is missing a valid string 'topic'.",
                    fix_hint="Specify a valid MQTT topic, e.g., 'topic: \"warehouse.robot.forklift\"'.",
                )
            )
            continue

        if not attribute or not isinstance(attribute, str):
            line_no, line_text = _get_key_line_and_text(
                sig_line, "attribute", source_lines
            )
            errors.append(
                ValidationError(
                    line_number=line_no or sig_line,
                    error_type="Attribute Error",
                    source_line=line_text,
                    message=f"continuous_signals[{s_idx}] is missing a valid string 'attribute'.",
                    fix_hint="Specify the payload attribute field, e.g., 'attribute: \"battery\"' or 'attribute: \"position.x\"'.",
                )
            )
            continue

        sig_key = (topic, attribute)
        if sig_key in seen_signal_attributes:
            line_no, line_text = _get_key_line_and_text(
                sig_line, "attribute", source_lines
            )
            errors.append(
                ValidationError(
                    line_number=line_no or sig_line,
                    error_type="Duplicate Signal Error",
                    source_line=line_text,
                    message=f"Signal for topic '{topic}' and attribute '{attribute}' is declared more than once.",
                    fix_hint="Combine piecewise segments for the same attribute into a single signal block.",
                )
            )
        seen_signal_attributes.add(sig_key)
        continuous_topics.add(topic)

        segments = signal.get("segments")

        if not segments or not isinstance(segments, list) or len(segments) == 0:
            line_no, line_text = _get_key_line_and_text(
                sig_line, "segments", source_lines
            )
            errors.append(
                ValidationError(
                    line_number=line_no or sig_line,
                    error_type="Segments Error",
                    source_line=line_text,
                    message=f"Signal '{topic}.{attribute}' must declare a non-empty list of 'segments'.",
                    fix_hint="Add at least one initial segment starting at time 0.0 under 'segments:'.",
                )
            )
            continue

        last_time = -1.0
        for seg_idx, seg in enumerate(segments):
            if not isinstance(seg, dict):
                errors.append(
                    ValidationError(
                        line_number=sig_line,
                        error_type="Segment Structure Error",
                        source_line=None,
                        message=f"Signal '{topic}.{attribute}' -> segments[{seg_idx}] must be a dictionary.",
                        fix_hint="Format each segment with 'time:', 'value:', and 'rate:'.",
                    )
                )
                continue

            seg_line = seg.get("__line__") or sig_line
            t = seg.get("time")
            val = seg.get("value")
            rate = seg.get("rate")

            if t is None or not isinstance(t, (int, float)):
                line_no, line_text = _get_key_line_and_text(
                    seg_line, "time", source_lines
                )
                errors.append(
                    ValidationError(
                        line_number=line_no or seg_line,
                        error_type="Type Error",
                        source_line=line_text,
                        message=f"Segment[{seg_idx}] of '{topic}.{attribute}' has invalid 'time': {t}. Must be numeric.",
                        fix_hint="Provide a numerical timestamp (e.g., 'time: 0.0').",
                    )
                )
            else:
                if t < 0.0:
                    line_no, line_text = _get_key_line_and_text(
                        seg_line, "time", source_lines
                    )
                    errors.append(
                        ValidationError(
                            line_number=line_no or seg_line,
                            error_type="Temporal Error",
                            source_line=line_text,
                            message=f"Segment[{seg_idx}] of '{topic}.{attribute}' has negative timestamp: {t}s.",
                            fix_hint="Timestamps must be non-negative (>= 0.0).",
                        )
                    )
                if duration > 0 and t > duration:
                    line_no, line_text = _get_key_line_and_text(
                        seg_line, "time", source_lines
                    )
                    errors.append(
                        ValidationError(
                            line_number=line_no or seg_line,
                            error_type="Temporal Boundary Error",
                            source_line=line_text,
                            message=f"Segment[{seg_idx}] timestamp ({t}s) exceeds scenario duration ({duration}s).",
                            fix_hint=f"Ensure segment time does not exceed duration ({duration}s).",
                        )
                    )
                if t <= last_time:
                    line_no, line_text = _get_key_line_and_text(
                        seg_line, "time", source_lines
                    )
                    errors.append(
                        ValidationError(
                            line_number=line_no or seg_line,
                            error_type="Monotonicity Error",
                            source_line=line_text,
                            message=(
                                f"Segment[{seg_idx}] timestamp ({t}s) is not strictly greater than "
                                f"prior segment ({last_time}s)."
                            ),
                            fix_hint="Order segments in strictly increasing chronological order.",
                        )
                    )
                last_time = t

            if val is None or not isinstance(val, (int, float)):
                line_no, line_text = _get_key_line_and_text(
                    seg_line, "value", source_lines
                )
                errors.append(
                    ValidationError(
                        line_number=line_no or seg_line,
                        error_type="Type Error",
                        source_line=line_text,
                        message=f"Segment[{seg_idx}] of '{topic}.{attribute}' has non-numeric 'value': {val}.",
                        fix_hint="Provide a numerical starting value (e.g., 'value: 100.0').",
                    )
                )

            if rate is None or not isinstance(rate, (int, float)):
                line_no, line_text = _get_key_line_and_text(
                    seg_line, "rate", source_lines
                )
                errors.append(
                    ValidationError(
                        line_number=line_no or seg_line,
                        error_type="Type Error",
                        source_line=line_text,
                        message=f"Segment[{seg_idx}] of '{topic}.{attribute}' has non-numeric 'rate': {rate}.",
                        fix_hint="Specify a numerical rate of change (e.g., 'rate: 0.0' for constant, or '-0.5').",
                    )
                )

        if segments and isinstance(segments[0], dict):
            first_t = segments[0].get("time")
            if isinstance(first_t, (int, float)) and first_t > 0.0:
                warnings.append(
                    f"Signal '{topic}.{attribute}' starts at t={first_t}s instead of 0.0s. "
                    "Initial behavior before this timestamp is undefined."
                )

    # 4. Discrete Events Checks (Standardized: topic, attribute, value)
    discrete_topics: set[str] = set()

    for e_idx, event in enumerate(discrete_events):
        if not isinstance(event, dict):
            errors.append(
                ValidationError(
                    line_number=None,
                    error_type="Schema Error",
                    source_line=None,
                    message=f"discrete_events[{e_idx}] must be a mapping dictionary.",
                    fix_hint="Format each discrete event with 'time:', 'topic:', 'attribute:', and 'value:'.",
                )
            )
            continue

        evt_line = event.get("__line__")
        t = event.get("time")
        topic = event.get("topic")
        attribute = event.get("attribute")
        val = event.get("value")

        if t is None or not isinstance(t, (int, float)):
            line_no, line_text = _get_key_line_and_text(evt_line, "time", source_lines)
            errors.append(
                ValidationError(
                    line_number=line_no or evt_line,
                    error_type="Type Error",
                    source_line=line_text,
                    message=f"discrete_events[{e_idx}] has non-numeric 'time': {t}.",
                    fix_hint="Provide a numerical timestamp offset, e.g., 'time: 45.0'.",
                )
            )
        else:
            if t < 0.0 or (duration > 0 and t > duration):
                line_no, line_text = _get_key_line_and_text(
                    evt_line, "time", source_lines
                )
                errors.append(
                    ValidationError(
                        line_number=line_no or evt_line,
                        error_type="Temporal Boundary Error",
                        source_line=line_text,
                        message=f"discrete_events[{e_idx}] timestamp ({t}s) is outside [0.0, {duration}s].",
                        fix_hint=f"Set 'time' between 0.0 and the total duration ({duration}s).",
                    )
                )

        if not topic or not isinstance(topic, str):
            line_no, line_text = _get_key_line_and_text(evt_line, "topic", source_lines)
            errors.append(
                ValidationError(
                    line_number=line_no or evt_line,
                    error_type="Topic Error",
                    source_line=line_text,
                    message=f"discrete_events[{e_idx}] is missing a valid string 'topic'.",
                    fix_hint="Specify a valid target topic, e.g., 'topic: \"warehouse.actuators.conveyor\"'.",
                )
            )
        else:
            discrete_topics.add(topic)

        if not attribute or not isinstance(attribute, str):
            line_no, line_text = _get_key_line_and_text(
                evt_line, "attribute", source_lines
            )
            errors.append(
                ValidationError(
                    line_number=line_no or evt_line,
                    error_type="Attribute Error",
                    source_line=line_text,
                    message=f"discrete_events[{e_idx}] is missing a valid string 'attribute'.",
                    fix_hint="Specify the target attribute, e.g., 'attribute: \"state\"'.",
                )
            )

        if val is None:
            line_no, line_text = _get_key_line_and_text(evt_line, "value", source_lines)
            errors.append(
                ValidationError(
                    line_number=line_no or evt_line,
                    error_type="Value Error",
                    source_line=line_text,
                    message=f"discrete_events[{e_idx}] to '{topic}' is missing 'value'.",
                    fix_hint="Provide an explicit state or value, e.g., 'value: 1' or 'value: true'.",
                )
            )

    # 5. Topic Conflict Check (Functional Isolation Rule)
    overlapping_topics = continuous_topics.intersection(discrete_topics)
    if overlapping_topics:
        for overlap in overlapping_topics:
            errors.append(
                ValidationError(
                    line_number=None,
                    error_type="Topic Conflict Error",
                    source_line=None,
                    message=(
                        f"Topic '{overlap}' is declared in both 'continuous_signals' and 'discrete_events'. "
                        "Continuous telemetry streams and discrete action commands must be isolated."
                    ),
                    fix_hint="If you intended a sudden step change, specify a new segment under 'continuous_signals'.",
                )
            )

    # 6. Physical World Model Grounding Check (Strict Topic Verification)
    if world_model_path and Path(world_model_path).exists() and parse_envpop_model:
        try:
            world_summary = parse_envpop_model(world_model_path)
            declared_topics = set(
                world_summary.get("all_topics", {}).get("publishers", [])
                + world_summary.get("all_topics", {}).get("subscribers", [])
            )

            for sig in continuous_signals:
                top = sig.get("topic")
                if top and not _topic_matches_world(top, declared_topics):
                    line_no, line_text = _get_key_line_and_text(
                        sig.get("__line__"), "topic", source_lines
                    )
                    errors.append(
                        ValidationError(
                            line_number=line_no or sig.get("__line__"),
                            error_type="Grounding Error",
                            source_line=line_text,
                            message=(
                                f"Topic '{top}' is not a valid broker topic in '{Path(world_model_path).name}'. "
                                f"Available topics: {sorted(list(declared_topics))}"
                            ),
                            fix_hint=f"Use one of the declared broker topics: {sorted(list(declared_topics))}",
                        )
                    )

            for evt in discrete_events:
                top = evt.get("topic")
                if top and not _topic_matches_world(top, declared_topics):
                    line_no, line_text = _get_key_line_and_text(
                        evt.get("__line__"), "topic", source_lines
                    )
                    errors.append(
                        ValidationError(
                            line_number=line_no or evt.get("__line__"),
                            error_type="Grounding Error",
                            source_line=line_text,
                            message=(
                                f"Topic '{top}' is not a valid broker topic in '{Path(world_model_path).name}'. "
                                f"Available topics: {sorted(list(declared_topics))}"
                            ),
                            fix_hint=f"Use one of the declared broker topics: {sorted(list(declared_topics))}",
                        )
                    )

            # For nominal baselines, ensure every concrete publisher topic in the world model is represented
            if category == "nominal":
                declared_pub_topics = {
                    t
                    for t in world_summary.get("all_topics", {}).get("publishers", [])
                    if "{UID}" not in t and "*" not in t
                }
                missing_pub_topics = sorted(
                    list(declared_pub_topics - continuous_topics)
                )
                if missing_pub_topics:
                    errors.append(
                        ValidationError(
                            line_number=data.get("__line__", 1),
                            error_type="Incomplete Nominal Coverage",
                            source_line=None,
                            message=(
                                "Nominal baseline must include continuous signals for all publisher topics "
                                f"in '{Path(world_model_path).name}'. Missing topics: {missing_pub_topics}"
                            ),
                            fix_hint=(
                                "Add continuous_signals (starting at time: 0.0) with healthy nominal values "
                                f"for the missing topics: {missing_pub_topics}"
                            ),
                        )
                    )

        except Exception as e:
            warnings.append(
                f"World Grounding Check Skipped: Could not parse world model: {e}"
            )

    clean_data = _strip_line_metadata(data) if len(errors) == 0 else None

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        scenario_data=clean_data,
    )
