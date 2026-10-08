import re
import sys
from pathlib import Path
from pprint import pprint

sys.path.append(str(Path(__file__).resolve().parents[1]))
from envpop_parser import parse_envpop_model

from common.config import DATA_DIR, WORLD_MODELS_DIR


def extract_goal_dsl_entities(dsl_code: str) -> list[dict]:
    """Parses declared Entity blocks and their metadata from Goal-DSL code."""
    entities = []
    entity_blocks = re.findall(
        r"Entity\s+([A-Za-z0-9_]+)(.*?)(?:end|(?=Entity|\Z))",
        dsl_code,
        re.DOTALL,
    )

    for name, body in entity_blocks:
        type_match = re.search(r"type:\s*(\w+)", body)
        entity_type = type_match.group(1).lower() if type_match else "unknown"

        uri_match = re.search(r"uri:\s*['\"](.*?)['\"]", body)
        uri = uri_match.group(1) if uri_match else ""

        attrs = re.findall(r"-\s*([A-Za-z0-9_]+)\s*:\s*(\w+)", body)
        attributes = {attr_name: attr_type for attr_name, attr_type in attrs}

        entities.append(
            {
                "name": name,
                "type": entity_type,
                "uri": uri,
                "attributes": attributes,
            }
        )

    return entities


def extract_condition_attribute_usages(dsl_code: str) -> set[tuple[str, str]]:
    """Extracts all (entity_name, attribute_name) pairs referenced inside conditions."""
    matches = re.findall(
        r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b",
        dsl_code,
    )
    return set(matches)


def _normalize(name: str) -> str:
    """Removes underscores, hyphens, and casing differences for clean identifier comparison."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def match_dsl_entity_to_world(
    dsl_entity: dict, world_model: dict
) -> str | None:
    """Matches a DSL Entity declaration to a real physical device or robot in the world model.

    Locations (POIs/places) are excluded.
    """
    dsl_norm = _normalize(dsl_entity["name"])
    valid_devices = world_model.get("all_valid_device_names", [])

    # 1. Exact normalized name match (e.g., 'WaterBot' matches 'water_bot')
    for dev_name in valid_devices:
        if _normalize(dev_name) == dsl_norm:
            return dev_name

    # 2. Topic/URI Endpoint Match: check if the declared URI matches the device's actual topic
    dsl_uri = dsl_entity.get("uri", "").strip()
    if dsl_uri:
        for dev_name in valid_devices:
            dev_info = world_model["sensors"].get(dev_name) or world_model["actuators"].get(dev_name)
            if dev_info:
                topics = dev_info.get("pub_topics", []) + dev_info.get("sub_topics", [])
                if dsl_uri in topics and _normalize(dev_name) in dsl_norm:
                    return dev_name

    return None


def validate_goal_dsl_against_world(
    dsl_code: str, world_model: dict
) -> tuple[bool, list[str]]:
    """Deterministically verifies if the generated Goal-DSL is compatible with the EnvPop world model."""
    errors = []
    declared_entities = extract_goal_dsl_entities(dsl_code)

    if not declared_entities:
        return (
            False,
            ["No 'Entity' declarations found in the generated Goal-DSL."],
        )

    valid_devices = world_model.get("all_valid_device_names", [])
    dsl_to_world_map = {}

    for entity in declared_entities:
        e_name = entity["name"]
        e_type = entity["type"]
        matched_world_entity = match_dsl_entity_to_world(entity, world_model)

        # 1. Device Existence Check (Catches hallucinated names)
        if not matched_world_entity:
            errors.append(
                f"Grounding Error: Entity '{e_name}' (URI: '{entity['uri']}') does not exist "
                f"in the physical world model. Valid available devices are: {valid_devices}"
            )
            continue

        dsl_to_world_map[e_name] = matched_world_entity

        # 2. Category & Role Check
        is_world_sensor = matched_world_entity in world_model["sensors"]
        is_world_actuator = matched_world_entity in world_model["actuators"]
        is_world_actor = matched_world_entity in world_model["actors"]

        if e_type == "sensor" and not is_world_sensor:
            errors.append(
                f"Role Mismatch: Entity '{e_name}' is declared as 'type: sensor', but in the "
                f"physical world '{matched_world_entity}' is not registered as a sensor."
            )
        elif e_type == "actuator" and not is_world_actuator:
            errors.append(
                f"Role Mismatch: Entity '{e_name}' is declared as 'type: actuator', but in the "
                f"physical world '{matched_world_entity}' is not an actuator."
            )
        elif e_type == "hybrid" and not (is_world_actor or (is_world_sensor and is_world_actuator)):
            errors.append(
                f"Role Mismatch: Entity '{e_name}' is declared as 'type: hybrid', but in the "
                f"physical world '{matched_world_entity}' is not a hybrid/actor device."
            )

        # 3. Physical Metric Consistency Check
        if is_world_sensor:
            device_info = world_model["sensors"][matched_world_entity]
            actual_metric = device_info.get("metric")
            for attr in entity["attributes"].keys():
                attr_lower = attr.lower()
                if (
                    "temp" in attr_lower
                    and actual_metric != "temperature"
                    or "humid" in attr_lower
                    and actual_metric != "humidity"
                ):
                    errors.append(
                        f"Metric Inconsistency: Entity '{e_name}' measures '{actual_metric}', "
                        f"but the DSL code declares attribute '{attr}'."
                    )

    # 4. Check references inside Condition blocks
    condition_refs = extract_condition_attribute_usages(dsl_code)
    for ref_entity, ref_attr in condition_refs:
        if ref_entity in [e["name"] for e in declared_entities]:
            if ref_entity not in dsl_to_world_map:
                errors.append(
                    f"Invalid Reference: Condition accesses entity '{ref_entity}', which has no valid counterpart in the physical world."
                )

    is_valid = len(errors) == 0
    return is_valid, errors


if __name__ == "__main__":
    current_dir = Path(__file__).parent
    yaml_path = WORLD_MODELS_DIR / "world_model.yaml"
    dsl_path = DATA_DIR / "llm_created_file.goal"

    if not yaml_path.exists():
        print(f"Error: {yaml_path.name} not found.")
    elif not dsl_path.exists():
        print(f"Error: {dsl_path.name} not found.")
    else:
        world = parse_envpop_model(yaml_path)
        with open(dsl_path, "r", encoding="utf-8") as f:
            sample_dsl = f.read()

        valid, report = validate_goal_dsl_against_world(sample_dsl, world)
        print("=" * 60)
        print(f"Goal-DSL World Compatibility: {'PASSED' if valid else 'FAILED'}")
        print("=" * 60)
        if not valid:
            print("Violations Caught:")
            for err in report:
                print(f" - {err}")