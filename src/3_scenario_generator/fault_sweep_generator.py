"""fault_sweep_generator.py

Deterministic Entity-Level Fault Sweep Generator for Stage 3.
Takes a validated Golden Nominal baseline (nominal_baseline.yaml) and an EnvPop
physical world model, and systematically generates an exhaustive single-entity
fault sweep manifest across every entity in the environment without LLM calls.

Supported entity-level fault modes:
  - freeze : Locks all entity continuous attributes at their exact baseline
             values at inject_time (rate = 0.0).
  - zero   : Drops all entity continuous attributes to 0.0 at inject_time
             (rate = 0.0).
  - no_tx  : Sets all entity continuous attributes to None (YAML null) at
             inject_time, instructing scenario_runner.py to suppress telemetry
             transmission for that entity.
"""

import argparse
import copy
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Resolve cross-stage module imports matching the src/ directory structure
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

try:
    from common.config import DATA_DIR, WORLD_MODELS_DIR
except ImportError:
    DATA_DIR = src_dir.parent / "data"
    WORLD_MODELS_DIR = DATA_DIR / "world_models"


DEFAULT_BASELINE_PATH = DATA_DIR / "scenarios" / "nominal_baseline.yaml"
DEFAULT_WORLD_PATH = WORLD_MODELS_DIR / "world_model.yaml"
DEFAULT_MANIFEST_PATH = DATA_DIR / "scenarios" / "fault_sweep_manifest.yaml"
DEFAULT_GOAL_PATH = DATA_DIR / "llm_created_file.goal"

SUPPORTED_FAULT_MODES: Tuple[str, ...] = ("freeze", "zero", "no_tx")


def _topic_matches_pattern(topic: str, declared_pattern: str) -> bool:
    """Checks whether a concrete scenario topic matches a declared world topic or template."""
    if topic == declared_pattern:
        return True
    pattern = re.sub(r"\{UID\}", r"[^.]+", re.escape(declared_pattern))
    pattern = pattern.replace(r"\*", r".*")
    return bool(re.fullmatch(pattern, topic))


def _extract_topics_from_node(node: Any) -> List[str]:
    """Recursively extracts broker topic strings from an entity definition node."""
    found: List[str] = []
    if isinstance(node, dict):
        for key, val in node.items():
            key_lower = str(key).lower()
            if key_lower in {"topic", "uri", "channel"} and isinstance(val, str) and val.strip():
                found.append(val.strip())
            elif key_lower in {"publishers", "publishes", "topics", "telemetry", "sensors", "actuators", "interfaces"}:
                found.extend(_extract_topics_from_node(val))
            elif isinstance(val, (dict, list)):
                found.extend(_extract_topics_from_node(val))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str) and "." in item:
                found.append(item.strip())
            else:
                found.extend(_extract_topics_from_node(item))
    return found


def resolve_entity_topic_map(
    world_model_path: Path,
    baseline_data: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Maps every entity declared in the EnvPop world model to its publisher topic(s).

    Inspects 'robots' (list) as well as grouped dictionaries under 'env_devices'
    and 'actors' in world_model.yaml, extracting strictly from 'transports.publishers'
    so subscriber-only topics are not mistaken for telemetry streams.
    """
    if not world_model_path.exists():
        raise FileNotFoundError(f"World model file not found at: {world_model_path}")

    with open(world_model_path, "r", encoding="utf-8") as f:
        raw_world = yaml.safe_load(f) or {}

    world_summary: Dict[str, Any] = {}
    if parse_envpop_model:
        try:
            world_summary = parse_envpop_model(world_model_path) or {}
        except Exception as exc:
            print(f"⚠️ Warning: envpop_parser fallback triggered ({exc})")

    baseline_topics = {
        sig.get("topic")
        for sig in baseline_data.get("continuous_signals", [])
        if isinstance(sig, dict) and sig.get("topic")
    }

    entity_raw_topics: Dict[str, List[str]] = {}

    def _register_entity_list(items: Any) -> None:
        if not isinstance(items, list):
            return
        for ent_item in items:
            if not isinstance(ent_item, dict):
                continue
            ent_name = ent_item.get("name") or ent_item.get("id")
            if not ent_name:
                continue
            transports = ent_item.get("transports") or {}
            pub_list = transports.get("publishers") if isinstance(transports, dict) else []
            topics = _extract_topics_from_node(pub_list)
            existing = entity_raw_topics.setdefault(str(ent_name), [])
            for t in topics:
                if t not in existing:
                    existing.append(t)

    # 1. Extract from top-level 'robots' list
    _register_entity_list(raw_world.get("robots"))

    # 2. Extract from categorized groups under 'env_devices' and 'actors'
    for group_key in ("env_devices", "actors"):
        group_dict = raw_world.get(group_key)
        if isinstance(group_dict, dict):
            for device_list in group_dict.values():
                _register_entity_list(device_list)

    # 3. Preserve static POIs/places from envpop_parser so unmapped items are reported
    for ent_name in world_summary.get("all_valid_entity_names", []):
        entity_raw_topics.setdefault(str(ent_name), [])

    # 4. Resolve declared topic templates to concrete topics present in nominal_baseline.yaml
    resolved_map: Dict[str, List[str]] = {}
    for ent_name, raw_topics in entity_raw_topics.items():
        matched: List[str] = []
        for rt in raw_topics:
            for bt in sorted(baseline_topics):
                if _topic_matches_pattern(bt, rt) and bt not in matched:
                    matched.append(bt)
        resolved_map[ent_name] = matched

    return resolved_map


def _detect_goal_referenced_entities(
    entity_names: List[str],
    goal_dsl_path: Optional[Path],
) -> set[str]:
    """Identifies which world entities are explicitly referenced in the .goal specification."""
    if not goal_dsl_path or not goal_dsl_path.exists():
        return set()

    try:
        goal_text = goal_dsl_path.read_text(encoding="utf-8")
    except Exception:
        return set()

    referenced: set[str] = set()
    for name in entity_names:
        if re.search(rf"\b{re.escape(name)}\b", goal_text):
            referenced.add(name)
    return referenced


def evaluate_signal_value_at_time(
    segments: List[Dict[str, Any]],
    target_time: float,
) -> Optional[float]:
    """Evaluates the exact piecewise linear signal value at target_time.

    Uses forward-interval semantics:
        value(t) = active_segment.value + active_segment.rate * (t - active_segment.time)
    """
    if not segments:
        return None

    sorted_segs = sorted(segments, key=lambda s: float(s["time"]))
    active_seg = sorted_segs[0]

    for seg in sorted_segs:
        if float(seg["time"]) <= target_time:
            active_seg = seg
        else:
            break

    base_val = active_seg.get("value")
    if base_val is None:
        return None

    rate = float(active_seg.get("rate", 0.0))
    dt = max(0.0, target_time - float(active_seg["time"]))
    return float(base_val) + rate * dt


def materialize_fault_scenario(
    baseline_data: Dict[str, Any],
    fault_case: Dict[str, Any],
) -> Dict[str, Any]:
    """Applies a single entity-level fault mutation onto a copy of nominal_baseline.yaml.

    Can be imported and called directly by scenario_runner.py in memory so the
    sweep can execute dozens of entity mutations without generating individual
    YAML files on disk.
    """
    fault_id = fault_case["fault_id"]
    entity_name = fault_case["entity_name"]
    target_topics = set(fault_case.get("topics", []))
    fault_type = fault_case["fault_type"]
    inject_time = round(float(fault_case["inject_time"]), 6)

    if fault_type not in SUPPORTED_FAULT_MODES:
        raise ValueError(
            f"Unsupported fault_type '{fault_type}'. Expected one of {SUPPORTED_FAULT_MODES}."
        )

    mutated = copy.deepcopy(baseline_data)
    mutated["scenario_id"] = fault_id
    mutated["category"] = "fault"
    mutated["description"] = (
        f"Deterministic fault sweep case: entity '{entity_name}' experiences "
        f"'{fault_type}' failure across topics {sorted(list(target_topics))} "
        f"at t={inject_time}s."
    )

    for signal in mutated.get("continuous_signals", []):
        if signal.get("topic") not in target_topics:
            continue

        orig_segments = sorted(
            signal.get("segments", []),
            key=lambda s: float(s["time"]),
        )
        prior_segments = [
            seg for seg in orig_segments if float(seg["time"]) < inject_time
        ]

        if fault_type == "freeze":
            frozen_val = evaluate_signal_value_at_time(orig_segments, inject_time)
            if frozen_val is None:
                frozen_val = 0.0
            fault_segment = {
                "time": inject_time,
                "value": round(float(frozen_val), 6),
                "rate": 0.0,
            }
        elif fault_type == "zero":
            fault_segment = {
                "time": inject_time,
                "value": 0.0,
                "rate": 0.0,
            }
        else:  # fault_type == "no_tx"
            fault_segment = {
                "time": inject_time,
                "value": None,
                "rate": 0.0,
            }

        signal["segments"] = prior_segments + [fault_segment]

    # Suppress any post-fault discrete events emitted by the broken entity
    filtered_events = []
    for evt in mutated.get("discrete_events", []):
        if evt.get("topic") in target_topics and float(evt.get("time", 0.0)) >= inject_time:
            continue
        filtered_events.append(evt)
    mutated["discrete_events"] = filtered_events

    return mutated


def generate_fault_sweep_manifest(
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    world_model_path: str | Path = DEFAULT_WORLD_PATH,
    manifest_output_path: str | Path = DEFAULT_MANIFEST_PATH,
    goal_dsl_path: Optional[str | Path] = DEFAULT_GOAL_PATH,
    inject_ratio: float = 0.30,
    inject_time: Optional[float] = None,
    fault_modes: Tuple[str, ...] = SUPPORTED_FAULT_MODES,
    export_individual_dir: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Generates the entity-level fault sweep manifest from nominal_baseline.yaml.

    Args:
        baseline_path: Path to the validated Golden Nominal baseline YAML.
        world_model_path: Path to the EnvPop physical world model YAML.
        manifest_output_path: Output path for fault_sweep_manifest.yaml.
        goal_dsl_path: Optional path to the active .goal file (used only to tag
                       whether each entity is referenced in the specification).
        inject_ratio: Fraction of total scenario duration at which the fault is
                      injected when inject_time is not explicitly set (default 0.30).
        inject_time: Optional explicit injection timestamp in seconds. Overrides
                     inject_ratio when provided.
        fault_modes: Tuple of fault types to sweep per entity.
        export_individual_dir: Optional directory path to also write standalone
                               scenario YAML files for debugging individual cases.

    Returns:
        The generated fault sweep manifest dictionary.
    """
    baseline_file = Path(baseline_path)
    world_file = Path(world_model_path)
    manifest_file = Path(manifest_output_path)
    goal_file = Path(goal_dsl_path) if goal_dsl_path else None

    if not baseline_file.exists():
        raise FileNotFoundError(
            f"Nominal baseline file not found at {baseline_file}. "
            "Run Stage 3 scenario generation with category 'nominal' first."
        )

    with open(baseline_file, "r", encoding="utf-8") as f:
        baseline_data = yaml.safe_load(f)

    if not isinstance(baseline_data, dict):
        raise ValueError(f"Invalid YAML root in baseline file: {baseline_file}")

    duration = float(baseline_data.get("duration", 0.0))
    if duration <= 0.0:
        raise ValueError(f"Baseline scenario duration must be > 0, got {duration}.")

    if inject_time is not None:
        resolved_inject_time = round(float(inject_time), 6)
        if not (0.0 <= resolved_inject_time < duration):
            raise ValueError(
                f"Explicit inject_time ({resolved_inject_time}s) must satisfy "
                f"0.0 <= inject_time < duration ({duration}s)."
            )
        resolved_ratio = round(resolved_inject_time / duration, 6)
    else:
        if not (0.0 <= inject_ratio < 1.0):
            raise ValueError(
                f"inject_ratio ({inject_ratio}) must satisfy 0.0 <= inject_ratio < 1.0."
            )
        resolved_ratio = round(float(inject_ratio), 6)
        resolved_inject_time = round(duration * resolved_ratio, 6)

    for mode in fault_modes:
        if mode not in SUPPORTED_FAULT_MODES:
            raise ValueError(
                f"Unsupported fault mode '{mode}'. Allowed: {SUPPORTED_FAULT_MODES}"
            )

    entity_topic_map = resolve_entity_topic_map(world_file, baseline_data)
    if not entity_topic_map:
        raise RuntimeError(
            f"No entities could be resolved from world model: {world_file}"
        )

    # Index all baseline attributes per topic for clear manifest visibility
    topic_attributes: Dict[str, List[str]] = {}
    for sig in baseline_data.get("continuous_signals", []):
        top = sig.get("topic")
        attr = sig.get("attribute")
        if top and attr:
            topic_attributes.setdefault(top, [])
            if attr not in topic_attributes[top]:
                topic_attributes[top].append(attr)

    goal_entities = _detect_goal_referenced_entities(
        list(entity_topic_map.keys()), goal_file
    )

    sweep_id = f"SWEEP-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    cases: List[Dict[str, Any]] = []
    unmapped_entities: List[str] = []

    for entity_name, topics in entity_topic_map.items():
        if not topics:
            unmapped_entities.append(entity_name)
            continue

        affected_attrs: List[str] = []
        for top in topics:
            for attr in topic_attributes.get(top, []):
                qualified = f"{top}:{attr}" if len(topics) > 1 else attr
                affected_attrs.append(qualified)

        safe_entity_slug = re.sub(r"[^a-zA-Z0-9_]+", "_", entity_name).strip("_").lower()
        in_goal = entity_name in goal_entities

        for mode in fault_modes:
            fault_id = f"fault_{safe_entity_slug}_{mode}"
            case_entry = {
                "fault_id": fault_id,
                "entity_name": entity_name,
                "in_goal_spec": in_goal,
                "topics": topics,
                "affected_attributes": affected_attrs,
                "fault_type": mode,
                "inject_time": resolved_inject_time,
            }
            cases.append(case_entry)

            if export_individual_dir:
                export_dir_path = Path(export_individual_dir)
                export_dir_path.mkdir(parents=True, exist_ok=True)
                materialized = materialize_fault_scenario(baseline_data, case_entry)
                out_yaml = export_dir_path / f"{fault_id}.yaml"
                with open(out_yaml, "w", encoding="utf-8") as ef:
                    yaml.safe_dump(materialized, ef, sort_keys=False, default_flow_style=False)

    manifest: Dict[str, Any] = {
        "sweep_id": sweep_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_file": str(baseline_file),
        "world_model_file": str(world_file),
        "duration": duration,
        "inject_ratio": resolved_ratio,
        "inject_time": resolved_inject_time,
        "fault_modes": list(fault_modes),
        "total_entities_in_world": len(entity_topic_map),
        "active_entities_swept": len(entity_topic_map) - len(unmapped_entities),
        "unmapped_entities": unmapped_entities,
        "total_cases": len(cases),
        "cases": cases,
    }

    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_file, "w", encoding="utf-8") as mf:
        yaml.safe_dump(manifest, mf, sort_keys=False, default_flow_style=False)

    print("=" * 60)
    print(f"⚡ FAULT SWEEP MANIFEST GENERATED ({sweep_id})")
    print(f"   Baseline File   : {baseline_file.name}")
    print(f"   World Model     : {world_file.name}")
    print(f"   Duration        : {duration}s")
    print(f"   Injection Time  : {resolved_inject_time}s ({resolved_ratio * 100:.1f}%)")
    print(f"   Fault Modes     : {list(fault_modes)}")
    print(f"   Entities Swept  : {manifest['active_entities_swept']} / {manifest['total_entities_in_world']}")
    if unmapped_entities:
        print(f"   ⚠️ Unmapped     : {unmapped_entities} (no publisher signals in baseline)")
    print(f"   Total Test Cases: {len(cases)}")
    print(f"   Manifest Saved  : {manifest_file}")
    print("=" * 60)

    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate deterministic entity-level fault sweep manifest from nominal_baseline.yaml."
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=str(DEFAULT_BASELINE_PATH),
        help="Path to nominal_baseline.yaml",
    )
    parser.add_argument(
        "--world",
        type=str,
        default=str(DEFAULT_WORLD_PATH),
        help="Path to EnvPop world_model.yaml",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_MANIFEST_PATH),
        help="Output path for fault_sweep_manifest.yaml",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=str(DEFAULT_GOAL_PATH),
        help="Optional path to .goal file to tag goal-referenced vs peripheral entities",
    )
    parser.add_argument(
        "--inject-ratio",
        type=float,
        default=0.30,
        help="Relative mission time (0.0 to <1.0) to inject faults (default: 0.30)",
    )
    parser.add_argument(
        "--inject-time",
        type=float,
        default=None,
        help="Explicit fault injection timestamp in seconds (overrides --inject-ratio)",
    )
    parser.add_argument(
        "--export-dir",
        type=str,
        default=None,
        help="Optional directory to also export individual materialized fault YAML files",
    )

    args = parser.parse_args()

    generate_fault_sweep_manifest(
        baseline_path=args.baseline,
        world_model_path=args.world,
        manifest_output_path=args.output,
        goal_dsl_path=args.goal,
        inject_ratio=args.inject_ratio,
        inject_time=args.inject_time,
        export_individual_dir=args.export_dir,
    )