#!/usr/bin/env python
"""scenario_runner.py

Execution engine for CPS test scenarios and deterministic fault sweeps in Stage 3.
Supports two automatic execution modes based on the input YAML file:
  1. Single Scenario Mode: Validates and replays a single scenario YAML file.
  2. Fault Sweep Mode: Detects a fault_sweep_manifest.yaml file, loads its
     referenced nominal_baseline.yaml once, materializes each entity fault case
     in memory without re-running validation, and pauses for manual keyboard
     confirmation between cases so goaldsl-executor can be restarted cleanly.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml
from commlib.node import Node
from dotenv import load_dotenv

# Ensure local stage modules are importable regardless of working directory
current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

from fault_sweep_generator import materialize_fault_scenario

# Optional pre-flight validator integration (used only in Single Scenario Mode)
try:
    from scenario_validator import validate_scenario
except ImportError:
    validate_scenario = None

# ==============================================================================
# DEFAULT RUNTIME CONFIGURATION
# ==============================================================================
DEFAULT_BROKER_TYPE: str = "mqtt"
DEFAULT_HOST: str = "localhost"
DEFAULT_PORT: int = 1883
DEFAULT_FREQUENCY_HZ: float = 1.0  # Telemetry streaming rate (10 Hz = 0.1s interval)
DEFAULT_TIME_SCALE: float = 1.0     # 1.0 = real-time, 2.0 = 2x speedup


def get_connection_parameters(
    broker_type: str = "mqtt",
    host: Optional[str] = None,
    port: Optional[int] = None,
):
    """Builds transport connection parameters matching goaldsl-executor conventions."""
    load_dotenv()
    if broker_type == "redis":
        from commlib.transports.redis import ConnectionParameters
        return ConnectionParameters(
            host=host or os.getenv("REDIS_HOST", "localhost"),
            port=port or int(os.getenv("REDIS_PORT", "6379")),
            username=os.getenv("REDIS_USERNAME", ""),
            password=os.getenv("REDIS_PASSWORD", ""),
        )
    elif broker_type == "amqp":
        from commlib.transports.amqp import ConnectionParameters
        return ConnectionParameters()
    elif broker_type == "mqtt":
        from commlib.transports.mqtt import ConnectionParameters
        if host or port:
            return ConnectionParameters(
                host=host or "localhost",
                port=port or 1883,
            )
        return ConnectionParameters()
    else:
        raise ValueError(f"Unsupported broker type: {broker_type}")


def set_nested_value(target_dict: Dict[str, Any], attr_path: str, value: Any) -> None:
    """Sets a value in a nested dictionary using dot notation (e.g., 'position.x' -> target['position']['x'])."""
    keys = attr_path.split(".")
    curr = target_dict
    for k in keys[:-1]:
        if k not in curr or not isinstance(curr[k], dict):
            curr[k] = {}
        curr = curr[k]
    curr[keys[-1]] = value


def evaluate_signal_value(segments: List[Dict[str, Any]], current_time: float) -> Optional[float]:
    """Calculates signal value at time t using piecewise linear dynamics:

    value(t) = segment.value + segment.rate * (t - segment.time)
    Returns None when a segment models a silent/no-transmission fault (value: null).
    """
    if not segments:
        return None

    # Segments are ordered chronologically [t_0, t_1, ..., t_k]
    active_seg = segments[0]
    for seg in segments:
        if seg["time"] <= current_time:
            active_seg = seg
        else:
            break

    if active_seg.get("value") is None:
        return None

    t_base = float(active_seg["time"])
    v_base = float(active_seg["value"])
    rate = float(active_seg["rate"])
    dt = max(0.0, current_time - t_base)

    return round(v_base + (rate * dt), 4)


class ScenarioRunner:
    def __init__(
        self,
        scenario_file: str | Path,
        broker_type: str = DEFAULT_BROKER_TYPE,
        host: Optional[str] = DEFAULT_HOST,
        port: Optional[int] = DEFAULT_PORT,
        frequency_hz: float = DEFAULT_FREQUENCY_HZ,
        time_scale: float = DEFAULT_TIME_SCALE,
    ):
        self.scenario_file = Path(scenario_file)
        self.broker_type = broker_type.lower()
        self.host = host
        self.port = port
        self.frequency_hz = max(0.1, frequency_hz)
        self.period_sec = 1.0 / self.frequency_hz
        self.time_scale = max(0.01, time_scale)

        self.is_sweep_mode: bool = False
        self.scenario_data: Dict[str, Any] = {}
        self.manifest_data: Dict[str, Any] = {}
        self.baseline_data: Dict[str, Any] = {}
        self.publishers: Dict[str, Any] = {}
        self.node: Optional[Node] = None

        self._load_input_file()
        self._init_commlib_node()

    def _load_input_file(self) -> None:
        """Detects whether the input file is a single scenario or a fault sweep manifest."""
        if not self.scenario_file.exists():
            raise FileNotFoundError(f"Input file not found at: {self.scenario_file}")

        with open(self.scenario_file, "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)

        if not isinstance(raw_data, dict):
            raise ValueError(f"Invalid YAML root in file: {self.scenario_file}")

        if "sweep_id" in raw_data and "cases" in raw_data:
            self.is_sweep_mode = True
            self.manifest_data = raw_data

            baseline_ref = Path(self.manifest_data["baseline_file"])
            if not baseline_ref.exists():
                fallback_baseline = self.scenario_file.parent / baseline_ref.name
                if fallback_baseline.exists():
                    baseline_ref = fallback_baseline
                else:
                    raise FileNotFoundError(
                        f"Baseline file referenced by manifest not found: {baseline_ref}"
                    )

            with open(baseline_ref, "r", encoding="utf-8") as bf:
                self.baseline_data = yaml.safe_load(bf)

            cases = self.manifest_data.get("cases", [])
            print(f"[*] Mode            : FAULT SWEEP MANIFEST")
            print(f"[*] Sweep ID        : '{self.manifest_data.get('sweep_id')}'")
            print(f"[*] Baseline File   : {baseline_ref.name}")
            print(f"[*] Total Cases     : {len(cases)}")
            print(f"[*] Inject Time     : {self.manifest_data.get('inject_time')}s")
        else:
            self.is_sweep_mode = False
            if validate_scenario:
                val_result = validate_scenario(self.scenario_file)
                if not val_result.is_valid:
                    print(f"❌ Pre-flight Validation Failed for {self.scenario_file.name}:")
                    for err in val_result.errors:
                        print(f"   - {err.format_for_llm()}")
                    raise ValueError("Cannot run an invalid scenario file.")

            self.scenario_data = raw_data
            print(f"[*] Mode            : SINGLE SCENARIO")
            print(f"[*] Loaded Scenario : '{self.scenario_data.get('scenario_id')}'")
            print(f"[*] Category        : [{self.scenario_data.get('category', 'unknown').upper()}]")
            print(f"[*] Duration        : {self.scenario_data.get('duration')}s")
            print(f"[*] Signals Count   : {len(self.scenario_data.get('continuous_signals', []))}")
            print(f"[*] Events Count    : {len(self.scenario_data.get('discrete_events', []))}")

    def _init_commlib_node(self) -> None:
        """Initializes commlib Node and establishes publishers for all target topics."""
        print(f"[*] Initializing {self.broker_type.upper()} Node...")
        conn_params = get_connection_parameters(self.broker_type, self.host, self.port)

        run_label = (
            self.manifest_data.get("sweep_id", "sweep")
            if self.is_sweep_mode
            else self.scenario_data.get("scenario_id", "sim")
        )
        source_scenario = self.baseline_data if self.is_sweep_mode else self.scenario_data

        self.node = Node(
            node_name=f"scenario_runner_{run_label}",
            connection_params=conn_params,
            heartbeats=False,
        )

        all_topics: Set[str] = set()
        for sig in source_scenario.get("continuous_signals", []):
            if "topic" in sig:
                all_topics.add(sig["topic"])
        for evt in source_scenario.get("discrete_events", []):
            if "topic" in evt:
                all_topics.add(evt["topic"])

        for topic in sorted(all_topics):
            self.publishers[topic] = self.node.create_publisher(topic=topic)
            print(f"    - Registered topic publisher: {topic}")

        if self.publishers:
            self.node.run()
            print("[*] Node connected and publishers ready.")

    def _execute_single_scenario(self, scenario_data: Dict[str, Any]) -> bool:
        """Executes the playback loop for one scenario dictionary.

        Returns True if playback finished normally, or False if interrupted by user.
        """
        duration = float(scenario_data.get("duration", 0.0))
        continuous_signals = scenario_data.get("continuous_signals", [])
        discrete_events = sorted(
            scenario_data.get("discrete_events", []),
            key=lambda x: x.get("time", 0.0),
        )

        # Group continuous signals by publisher topic
        signals_by_topic: Dict[str, List[Dict[str, Any]]] = {}
        for sig in continuous_signals:
            topic = sig["topic"]
            signals_by_topic.setdefault(topic, []).append(sig)

        dispatched_event_indices: Set[int] = set()

        sim_time = 0.0
        wall_start_time = time.time()
        completed_normally = True

        print("\n" + "=" * 60)
        print(f"▶️ RUNNING SCENARIO: {scenario_data.get('scenario_id')}")
        print(f"   Rate: {self.frequency_hz} Hz (step = {self.period_sec:.3f}s) | Scale: {self.time_scale}x")
        print("=" * 60)

        try:
            while sim_time <= duration:
                step_start_wall = time.time()

                # Dispatch any discrete events scheduled at or before the current timestamp
                for idx, event in enumerate(discrete_events):
                    if idx not in dispatched_event_indices and event.get("time", 0.0) <= sim_time:
                        topic = event["topic"]
                        attr = event["attribute"]
                        val = event["value"]
                        desc = event.get("description", "Discrete Trigger")

                        evt_payload: Dict[str, Any] = {}
                        set_nested_value(evt_payload, attr, val)

                        if topic in self.publishers:
                            self.publishers[topic].publish(evt_payload)
                            print(
                                f"⚡ [t={sim_time:6.2f}s] DISCRETE EVENT -> {topic} | "
                                f"{json.dumps(evt_payload)} ({desc})"
                            )
                        dispatched_event_indices.add(idx)

                # Evaluate and stream continuous telemetry grouped by topic
                # Attributes evaluating to None (no_tx faults) are omitted; if all
                # attributes on a topic are None, publishing is suppressed completely.
                for topic, sig_list in signals_by_topic.items():
                    telemetry_payload: Dict[str, Any] = {}
                    for sig in sig_list:
                        attr = sig["attribute"]
                        val = evaluate_signal_value(sig["segments"], sim_time)
                        if val is not None:
                            set_nested_value(telemetry_payload, attr, val)

                    if telemetry_payload and topic in self.publishers:
                        self.publishers[topic].publish(telemetry_payload)

                # Status ticker every second of simulation time
                if int(sim_time) != int(sim_time - self.period_sec) or sim_time == 0.0:
                    pct = (sim_time / duration) * 100.0 if duration > 0 else 100.0
                    print(f"   ⏳ [t={sim_time:6.2f}s / {duration:.1f}s] ({pct:5.1f}%) Streaming telemetry...")

                # Advance clock
                sim_time += self.period_sec

                # Maintain loop frequency adjusted for time scaling
                target_sleep = (self.period_sec / self.time_scale) - (time.time() - step_start_wall)
                if target_sleep > 0:
                    time.sleep(target_sleep)

        except KeyboardInterrupt:
            completed_normally = False
            print("\n⚠️ Simulation manually interrupted by user.")
        finally:
            elapsed_wall = time.time() - wall_start_time
            print("\n" + "=" * 60)
            print(f"🏁 SCENARIO COMPLETED | Simulated: {min(sim_time, duration):.2f}s | Wall Clock: {elapsed_wall:.2f}s")
            print("=" * 60)

        return completed_normally

    def run(self) -> None:
        """Runs either a single scenario or an interactive batch fault sweep."""
        try:
            if not self.is_sweep_mode:
                self._execute_single_scenario(self.scenario_data)
                return

            cases = self.manifest_data.get("cases", [])
            total_cases = len(cases)
            if total_cases == 0:
                print("⚠️ Fault sweep manifest contains 0 cases. Nothing to run.")
                return

            completed_count = 0
            for idx, case in enumerate(cases):
                case_num = idx + 1
                fault_id = case.get("fault_id", f"case_{case_num}")
                entity_name = case.get("entity_name", "unknown")
                fault_type = case.get("fault_type", "unknown")
                inject_time = case.get("inject_time", 0.0)
                in_goal = case.get("in_goal_spec", False)

                if idx > 0:
                    print("\n" + "-" * 60)
                    print(
                        f"⏭️  UPCOMING CASE [{case_num}/{total_cases}]: {fault_id}\n"
                        f"    Entity: {entity_name} (in_goal_spec={in_goal}) | "
                        f"Fault: {fault_type} @ t={inject_time}s"
                    )
                    print("-" * 60)
                    try:
                        input(
                            "⏸️  Restart goaldsl-executor and load the .goal model, "
                            "then press Enter to start the next case (or Ctrl+C to stop)..."
                        )
                    except KeyboardInterrupt:
                        print("\n⚠️ Fault sweep aborted by user before next case.")
                        break
                else:
                    print(
                        f"\n▶️  STARTING SWEEP CASE [{case_num}/{total_cases}]: {fault_id} "
                        f"(Entity: {entity_name}, Fault: {fault_type} @ t={inject_time}s)"
                    )

                mutated_scenario = materialize_fault_scenario(self.baseline_data, case)
                ok = self._execute_single_scenario(mutated_scenario)
                if not ok:
                    print("🛑 Stopping fault sweep due to manual interruption.")
                    break
                completed_count += 1

            print("\n" + "=" * 60)
            print(f"📊 FAULT SWEEP SUMMARY | Completed {completed_count}/{total_cases} cases.")
            print("=" * 60)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stops the underlying commlib node cleanly."""
        if self.node:
            try:
                self.node.stop()
                print("[*] Commlib node stopped cleanly.")
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replay a Stage 3 scenario or fault sweep manifest against goaldsl-executor."
    )
    parser.add_argument(
        "scenario_file",
        help="Path to a scenario YAML file or fault_sweep_manifest.yaml",
    )
    parser.add_argument(
        "--broker",
        default=DEFAULT_BROKER_TYPE,
        choices=["mqtt", "redis", "amqp"],
        help=f"Broker transport type (default: {DEFAULT_BROKER_TYPE})",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Broker host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Broker port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--freq",
        type=float,
        default=DEFAULT_FREQUENCY_HZ,
        help=f"Publishing frequency in Hz (default: {DEFAULT_FREQUENCY_HZ})",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=DEFAULT_TIME_SCALE,
        help=f"Time multiplier (default: 1.0 real-time; 2.0 = 2x speed)",
    )

    args = parser.parse_args()

    runner = ScenarioRunner(
        scenario_file=args.scenario_file,
        broker_type=args.broker,
        host=args.host,
        port=args.port,
        frequency_hz=args.freq,
        time_scale=args.scale,
    )
    runner.run()