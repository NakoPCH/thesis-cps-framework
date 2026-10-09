import os
from pathlib import Path
from typing import Optional

import yaml


def extract_detailed_world_context(yaml_file_path: Optional[Path | str]) -> str:
    """Parses devices, payloads, attributes, and POI coordinates from the world model

    into a structured text block for LLM system prompts.
    """
    if not yaml_file_path or not os.path.exists(yaml_file_path):
        return "No physical world model provided. Infer plausible cyber-physical entities."

    try:
        with open(yaml_file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        lines = ["ACTIVE 2D PHYSICAL WORLD CONSTRAINTS & CAPABILITIES:"]

        # 1. Parse Robots / Actuators with their payload attributes
        lines.append("- Robots & Actuators:")
        for robot in data.get("robots", []):
            r_name = robot.get("name")
            topics = [
                p.get("topic")
                for p in robot.get("transports", {}).get("publishers", [])
            ]
            payload_keys = []
            for p in robot.get("transports", {}).get("publishers", []):
                payload_keys.extend(list(p.get("payload", {}).keys()))
            payload_str = ", ".join(set(payload_keys)) if payload_keys else "position"
            uri_str = topics[0] if topics else f"robot.{r_name}"
            lines.append(
                f"  * Entity: {r_name} | Type: hybrid | URI: '{uri_str}' | Attributes: {payload_str}"
            )

        # 2. Parse Environmental Sensors with their attributes
        lines.append("- Environmental Sensors:")
        for dev_type, dev_list in data.get("env_devices", {}).items():
            if isinstance(dev_list, list):
                for dev in dev_list:
                    d_name = dev.get("name")
                    topics = [
                        p.get("topic")
                        for p in dev.get("transports", {}).get("publishers", [])
                    ]
                    payload_keys = []
                    for p in dev.get("transports", {}).get("publishers", []):
                        payload_keys.extend(list(p.get("payload", {}).keys()))
                    payload_str = ", ".join(set(payload_keys)) if payload_keys else dev_type
                    uri_str = topics[0] if topics else f"sensors.{d_name}"
                    lines.append(
                        f"  * Entity: {d_name} | Type: sensor | URI: '{uri_str}' | Attributes: {payload_str}"
                    )

        # 3. Parse POIs with exact coordinates
        lines.append("- Points of Interest (POIs):")
        for poi in data.get("world", {}).get("pois", []):
            p_name = poi.get("name")
            pose = poi.get("pose", {})
            x = pose.get("x", 0.0)
            y = pose.get("y", 0.0)
            lines.append(f"  * POI: {p_name} | Coordinates: Point2D({x}, {y})")

        lines.append(
            "CRITICAL: The system ONLY contains these physical devices and locations. "
            "Map user requirements directly to these entity names, URIs, and attributes."
        )
        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ Error extracting detailed world model: {e}"


if __name__ == "__main__":
    # Test script standalone
    test_yaml = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "world_models"
        / "world_model.yaml"
    )
    print(extract_detailed_world_context(test_yaml))