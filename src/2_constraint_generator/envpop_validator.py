from pathlib import Path
from pprint import pprint
import re
from envpop_parser import parse_envpop_model


def extract_goal_dsl_entities(dsl_code: str) -> list[dict]:
    """Parses declared Entity blocks and their metadata from Goal-DSL code."""
    entities = []
    # Match Entity <name> ... end blocks
    entity_blocks = re.findall(
        r"Entity\s+([A-Za-z0-9_]+)(.*?)(?:end|(?=Entity|\Z))",
        dsl_code,
        re.DOTALL,
    )

    for name, body in entity_blocks:
        # Extract entity type (sensor, actuator, hybrid)
        type_match = re.search(r"type:\s*(\w+)", body)
        entity_type = type_match.group(1).lower() if type_match else "unknown"

        # Extract URI
        uri_match = re.search(r"uri:\s*['\"](.*?)['\"]", body)
        uri = uri_match.group(1) if uri_match else ""

        # Extract declared attributes
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
    """Extracts all (entity_name, attribute_name) pairs referenced inside conditions.

    Example: 'ClimateSensor.humidity' -> ('ClimateSensor', 'humidity')
    """
    # Matches patterns like EntityName.attributeName
    matches = re.findall(
        r"\b([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b",
        dsl_code,
    )
    # Filter out common noise (e.g., standard libraries or sub-keys if any)
    return set(matches)


def match_dsl_entity_to_world(
    dsl_entity: dict, world_model: dict
) -> str | None:
    """Finds the corresponding physical entity in the world model by name or URI matching."""
    dsl_name_clean = dsl_entity["name"].lower()
    dsl_uri_clean = dsl_entity["uri"].lower()

    for valid_name in world_model["all_valid_entity_names"]:
        vn_clean = valid_name.lower()
        # 1. Exact name match
        if vn_clean == dsl_name_clean:
            return valid_name
        # 2. Substring match (e.g. 'upper_temperature_sensor' -> 'upper_temperature')
        if vn_clean in dsl_name_clean or dsl_name_clean in vn_clean:
            return valid_name
        # 3. URI match (e.g. URI contains 'upper_temperature')
        if vn_clean in dsl_uri_clean:
            return valid_name

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

    # Map DSL entity name to matched world entity name
    dsl_to_world_map = {}

    for entity in declared_entities:
        e_name = entity["name"]
        e_type = entity["type"]
        matched_world_entity = match_dsl_entity_to_world(entity, world_model)

        # 1. Entity Existence Check (Hallucination Barrier)
        if not matched_world_entity:
            errors.append(
                f"Grounding Error: Entity '{e_name}' (URI: '{entity['uri']}') does not exist "
                f"in the 2D world model. Available entities are: {world_model['all_valid_entity_names']}"
            )
            continue

        dsl_to_world_map[e_name] = matched_world_entity

        # 2. Category & Role Check (Sensor vs. Actuator)
        is_world_sensor = matched_world_entity in world_model["sensors"]
        is_world_actuator = matched_world_entity in world_model["actuators"]

        if e_type == "sensor" and not is_world_sensor and is_world_actuator:
            errors.append(
                f"Role Mismatch: Entity '{e_name}' is declared as 'type: sensor', but in the "
                f"physical world '{matched_world_entity}' is an actuator (effector)."
            )
        elif (
            e_type == "actuator" and not is_world_actuator and is_world_sensor
        ):
            errors.append(
                f"Role Mismatch: Entity '{e_name}' is declared as 'type: actuator', but in the "
                f"physical world '{matched_world_entity}' is a sensor."
            )

        # 3. Physical Metric Consistency Check
        if is_world_sensor:
            device_info = world_model["sensors"][matched_world_entity]
            actual_metric = device_info["metric"]
            for attr in entity["attributes"].keys():
                attr_lower = attr.lower()
                if (
                    "temp" in attr_lower
                    and actual_metric != "temperature"
                    or "humid" in attr_lower
                    and actual_metric != "humidity"
                ):
                    errors.append(
                        f"Metric Inconsistency: Entity '{e_name}' measures physical attribute '{actual_metric}', "
                        f"but the DSL code declares attribute '{attr}'."
                    )

    # 4. Check references inside Condition blocks
    condition_refs = extract_condition_attribute_usages(dsl_code)
    for ref_entity, ref_attr in condition_refs:
        # Ignore references to Goal objects or non-entity keywords
        if ref_entity in [e["name"] for e in declared_entities]:
            if ref_entity not in dsl_to_world_map:
                errors.append(
                    f"Invalid Reference: Condition accesses entity '{ref_entity}', which has no valid counterpart in the physical world."
                )

    is_valid = len(errors) == 0
    return is_valid, errors


if __name__ == "__main__":
    current_dir = Path(__file__).parent
    yaml_path = current_dir / "greenhouse_model.yaml"
    dsl_path = current_dir / "llm_created_file.goal"

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