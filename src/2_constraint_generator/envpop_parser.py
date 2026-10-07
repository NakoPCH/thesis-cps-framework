from pathlib import Path
from pprint import pprint

import yaml


def parse_envpop_model(yaml_file_path: str | Path) -> dict:
    with open(yaml_file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    metric_map = {
        "temperature_sensors": "temperature",
        "thermostats": "temperature",
        "humidity_sensors": "humidity",
        "humidifiers": "humidity",
        "ambient_light_sensor": "luminosity",
        "lights": "luminosity",
        "gas_sensors": "co2",
        "ph_sensors": "ph",
        "alarms_area": "area_alarm",
        "alarms_linear": "linear_alarm",
        "distance_sensors": "distance",
        "speakers": "audio",
        "relays": "switch",
    }

    world_summary = {
        "simulation": data.get("simulation", {}).get("name", "streamsim"),
        "locations": [],
        "sensors": {},
        "actuators": {},
        "actors": {},
        "entities_by_metric": {
            "temperature": {"sensors": [], "actuators": []},
            "humidity": {"sensors": [], "actuators": []},
            "luminosity": {"sensors": [], "actuators": []},
            "co2": {"sensors": [], "actuators": []},
            "ph": {"sensors": [], "actuators": []},
            "security": {"sensors": [], "actuators": []},
            "other": {"sensors": [], "actuators": []},
        },
        "all_valid_entity_names": set(),
        "all_topics": {"publishers": [], "subscribers": []},
    }

    # 1. Parse Locations (POIs and Places)
    for poi in data.get("world", {}).get("pois", []):
        name = poi.get("name")
        if name:
            world_summary["locations"].append(name)
            world_summary["all_valid_entity_names"].add(name)

    for place in data.get("world", {}).get("places", []):
        name = place.get("name") if isinstance(place, dict) else place
        if name:
            world_summary["locations"].append(name)
            world_summary["all_valid_entity_names"].add(name)

    # 2. Devices (Sensors & Actuators)
    for device_type, device_list in data.get("env_devices", {}).items():
        if not isinstance(device_list, list):
            continue

        metric = metric_map.get(
            device_type, "security" if "alarm" in device_type else "other"
        )

        for dev in device_list:
            name = dev.get("name")
            if not name:
                continue

            category = dev.get("category", "")
            transports = dev.get("transports", {})
            pub_topics = [
                p.get("topic")
                for p in transports.get("publishers", [])
                if p.get("topic")
            ]
            sub_topics = [
                s.get("topic")
                for s in transports.get("subscribers", [])
                if s.get("topic")
            ]

            device_info = {
                "name": name,
                "type": device_type,
                "metric": metric,
                "mode": dev.get("mode"),
                "hz": dev.get("hz"),
                "operation": dev.get("operation"),
                "range": dev.get("range"),
                "pose": dev.get("pose", {}),
                "pub_topics": pub_topics,
                "sub_topics": sub_topics,
            }

            world_summary["all_valid_entity_names"].add(name)
            world_summary["all_topics"]["publishers"].extend(pub_topics)
            world_summary["all_topics"]["subscribers"].extend(sub_topics)

            if (
                category == "sensors"
                or "sensor" in device_type
                or "alarm" in device_type
            ):
                world_summary["sensors"][name] = device_info
                world_summary["entities_by_metric"][metric]["sensors"].append(name)
            else:
                world_summary["actuators"][name] = device_info
                world_summary["entities_by_metric"][metric]["actuators"].append(name)

    # 3. Dynamic Actors & Robots
    for actor_type, actor_list in data.get("actors", {}).items():
        if isinstance(actor_list, list):
            for i, actor in enumerate(actor_list):
                name = actor.get("name") or f"{actor_type}_{actor.get('id', i + 1)}"
                world_summary["actors"][name] = {
                    "name": name,
                    "type": actor_type,
                    "position": {
                        "x": actor.get("x", 0),
                        "y": actor.get("y", 0),
                    },
                    "range": actor.get("range"),
                }
                world_summary["all_valid_entity_names"].add(name)

    for robot in data.get("robots", []):
        name = robot.get("name")
        if name:
            world_summary["actors"][name] = {"name": name, "type": "robot"}
            world_summary["all_valid_entity_names"].add(name)

    world_summary["all_valid_entity_names"] = sorted(  # noqa: C414
        list(world_summary["all_valid_entity_names"])
    )
    return world_summary


if __name__ == "__main__":
    current_dir = Path(__file__).parent
    yaml_file = current_dir / "world_model.yaml"

    if not yaml_file.exists():
        print(f"Error: Could not find {yaml_file.name} in {current_dir}")
    else:
        results = parse_envpop_model(yaml_file)
        print("=" * 50)
        print(f"Loaded Simulation: {results['simulation']}")
        print(f"Total Valid Entities: {len(results['all_valid_entity_names'])}")
        print("=" * 50)

        print("\n--- All Valid Entity Names ---")
        pprint(results["all_valid_entity_names"])

        print("\n--- Sensors & Actuators by Metric ---")
        pprint(results["entities_by_metric"])

        print("\n--- Parsed Actors ---")
        pprint(results["actors"])

        print("\n--- Parsed Locations (POIs) ---")
        pprint(results["locations"])
