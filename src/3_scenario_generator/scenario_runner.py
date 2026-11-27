#!/usr/bin/env python
"""scenario_runner.py

Execution engine for CPS test scenarios and deterministic fault sweeps in Stage 3.
Supports two automatic execution modes based on the input YAML file:
  1. Single Scenario Mode: Validates and replays a single scenario YAML file.
  2. Fault Sweep Mode: Detects a fault_sweep_manifest.yaml file, loads its
     referenced nominal_baseline.yaml once, and materializes each entity fault
     case in memory without re-running validation.

When --goal-file is provided, the runner automatically deploys (or re-deploys)
the Goal-DSL specification to goaldsl-executor via RPC before every run in
both Single Scenario Mode and Fault Sweep Mode.

Runtime Monitoring & CSV Logging:
  Subscribes automatically to 'goaldsl.*.event' (matching any random executor
  U_ID via MQTT wildcard) to capture RTMonitor goal state transitions and
  final scenario verdicts, writing results row-by-row to a timestamped CSV
  file in a 'results/' directory next to the input file.
"""

import argparse
import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml
from commlib.msg import PubSubMessage, RPCMessage
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
DEFAULT_FREQUENCY_HZ: float = 1.0  # Telemetry streaming rate (1.0 Hz = 1.0s interval)
DEFAULT_TIME_SCALE: float = 1.0  # 1.0 = real-time, 2.0 = 2x speedup
DEFAULT_DEPLOY_WAIT: float = (
    1.0  # Settle delay (s) after RPC deploy before t=0.0s telemetry
)
DEFAULT_POST_VERDICT_WAIT: float = (
    8  # Grace wait (s) after playback for final RTMonitor verdict
)
DEFAULT_START_SYNC_WAIT: float = (
    5.0  # Max wait (s) for RTMonitor scenario_started after deploy
)
RTMONITOR_WILDCARD_TOPIC: str = "goaldsl.*.event"

CSV_FIELDNAMES: List[str] = [
    "timestamp_utc",
    "mode",
    "scenario_id",
    "fault_id",
    "entity_name",
    "in_goal_spec",
    "fault_type",
    "inject_time_s",
    "verdict",
    "final_score",
    "failed_goal",
    "detection_latency_s",
    "goalee_elapsed_s",
    "wall_elapsed_s",
    "goal_results",
    "warning_count",
    "warning_latency_s",
    "runtime_warnings",
]


class ExecuteModelMsg(RPCMessage):
    class Request(RPCMessage.Request):
        model: str

    class Response(RPCMessage.Response):
        status: int = 1
        result: str = ""


class EventMsg(PubSubMessage):
    type: str
    data: Dict[str, Any]


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

        return ConnectionParameters(
            host=host or os.getenv("MQTT_HOST", "localhost"),
            port=port or int(os.getenv("MQTT_PORT", "1883")),
            username=os.getenv("MQTT_USERNAME", ""),
            password=os.getenv("MQTT_PASSWORD", ""),
        )
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


def evaluate_signal_value(
    segments: List[Dict[str, Any]], current_time: float
) -> Optional[float]:
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
        goal_file: Optional[str | Path] = None,
        broker_type: str = DEFAULT_BROKER_TYPE,
        host: Optional[str] = DEFAULT_HOST,
        port: Optional[int] = DEFAULT_PORT,
        frequency_hz: float = DEFAULT_FREQUENCY_HZ,
        time_scale: float = DEFAULT_TIME_SCALE,
        uid: Optional[str] = None,
        deploy_wait: float = DEFAULT_DEPLOY_WAIT,
    ):
        load_dotenv()
        self.scenario_file = Path(scenario_file)
        self.goal_file = Path(goal_file) if goal_file else None
        self.broker_type = broker_type.lower()
        self.host = host
        self.port = port
        self.frequency_hz = max(0.1, frequency_hz)
        self.period_sec = 1.0 / self.frequency_hz
        self.time_scale = max(0.01, time_scale)
        self.target_uid = uid or os.getenv("UID", "TEST")
        self.deploy_wait = max(0.0, deploy_wait)

        self.is_sweep_mode: bool = False
        self.goal_model_content: Optional[str] = None
        self.scenario_data: Dict[str, Any] = {}
        self.manifest_data: Dict[str, Any] = {}
        self.baseline_data: Dict[str, Any] = {}
        self.publishers: Dict[str, Any] = {}
        self.deploy_rpc: Optional[Any] = None
        self.event_sub: Optional[Any] = None
        self.node: Optional[Node] = None

        # Runtime monitor synchronization and per-case verdict state
        self._event_lock = threading.Lock()
        self._started_event = threading.Event()
        self._finished_event = threading.Event()
        self._current_sim_time: float = 0.0
        self._scenario_finished_data: Optional[Dict[str, Any]] = None
        self._first_failed_goal: Optional[str] = None
        self._first_failed_sim_time: Optional[float] = None
        self._entity_warnings: List[Dict[str, Any]] = []
        self._first_warning_sim_time: Optional[float] = None

        # Automatic CSV output state
        self.csv_path: Optional[Path] = None
        self._csv_file: Optional[Any] = None
        self._csv_writer: Optional[csv.DictWriter] = None

        self._load_input_file()
        self._init_csv_logger()
        self._init_commlib_node()

    def _load_input_file(self) -> None:
        """Loads the optional .goal model and detects single scenario vs. fault sweep manifest."""
        if self.goal_file is not None:
            if not self.goal_file.exists():
                raise FileNotFoundError(
                    f"Goal specification file not found at: {self.goal_file}"
                )
            with open(self.goal_file, "r", encoding="utf-8") as gf:
                self.goal_model_content = gf.read()
            if not self.goal_model_content.strip():
                raise ValueError(f"Goal specification file is empty: {self.goal_file}")

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
            print(
                f"[*] Goal File       : {self.goal_file.name if self.goal_file else 'None (manual deploy)'}"
            )
            print(f"[*] Total Cases     : {len(cases)}")
            print(f"[*] Inject Time     : {self.manifest_data.get('inject_time')}s")
        else:
            self.is_sweep_mode = False
            if validate_scenario:
                val_result = validate_scenario(self.scenario_file)
                if not val_result.is_valid:
                    print(
                        f"❌ Pre-flight Validation Failed for {self.scenario_file.name}:"
                    )
                    for err in val_result.errors:
                        print(f"   - {err.format_for_llm()}")
                    raise ValueError("Cannot run an invalid scenario file.")

            self.scenario_data = raw_data
            print(f"[*] Mode            : SINGLE SCENARIO")
            print(f"[*] Loaded Scenario : '{self.scenario_data.get('scenario_id')}'")
            print(
                f"[*] Category        : [{self.scenario_data.get('category', 'unknown').upper()}]"
            )
            print(
                f"[*] Goal File       : {self.goal_file.name if self.goal_file else 'None (replay only)'}"
            )
            print(f"[*] Duration        : {self.scenario_data.get('duration')}s")
            print(
                f"[*] Signals Count   : {len(self.scenario_data.get('continuous_signals', []))}"
            )
            print(
                f"[*] Events Count    : {len(self.scenario_data.get('discrete_events', []))}"
            )

    def _init_csv_logger(self) -> None:
        """Creates a timestamped CSV results file in a 'results/' subfolder next to the input file."""
        results_dir = self.scenario_file.resolve().parent / "results"
        results_dir.mkdir(parents=True, exist_ok=True)

        run_tag = (
            self.manifest_data.get("sweep_id", "sweep")
            if self.is_sweep_mode
            else self.scenario_data.get("scenario_id", "scenario")
        )
        safe_tag = "".join(
            c if (c.isalnum() or c in ("-", "_")) else "_" for c in str(run_tag)
        )
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = results_dir / f"{safe_tag}_{ts_str}.csv"

        self._csv_file = open(self.csv_path, mode="w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_FIELDNAMES)
        self._csv_writer.writeheader()
        self._csv_file.flush()
        print(f"[*] Results CSV     : {self.csv_path}")

    def _reset_run_telemetry(self) -> None:
        """Clears captured RTMonitor events before starting a new scenario execution."""
        with self._event_lock:
            self._started_event.clear()
            self._finished_event.clear()
            self._current_sim_time = 0.0
            self._scenario_finished_data = None
            self._first_failed_goal = None
            self._first_failed_sim_time = None
            self._entity_warnings = []
            self._first_warning_sim_time = None

    def _on_rtmonitor_event(self, msg: Any, topic: Optional[str] = None) -> None:
        """Callback invoked when goalee's RTMonitor publishes an EventMsg on goaldsl.*.event."""

        if isinstance(msg, dict):
            evt_type = msg.get("type", "")
            evt_data = msg.get("data") or {}
        else:
            evt_type = getattr(msg, "type", "")
            evt_data = getattr(msg, "data", None) or {}

        with self._event_lock:
            if evt_type == "scenario_started":
                self._started_event.set()

            elif evt_type == "goal_state":
                state = str(evt_data.get("state", "")).upper()
                goal_name = str(evt_data.get("goal_name", ""))
                if state == "FAILED" and self._first_failed_goal is None:
                    self._first_failed_goal = goal_name
                    self._first_failed_sim_time = round(self._current_sim_time, 3)

            elif evt_type == "entity_warning":
                sim_t = round(self._current_sim_time, 3)
                if self._first_warning_sim_time is None:
                    self._first_warning_sim_time = sim_t
                warning_record = dict(evt_data)
                warning_record["sim_time_s"] = sim_t
                self._entity_warnings.append(warning_record)
                ent = warning_record.get("entity_name", "unknown")
                w_type = warning_record.get("warning_type", "unknown")
                print(
                    f"   ⚠️ [t={sim_t:6.2f}s] RTMonitor Entity Warning: {ent} -> {w_type}"
                )

            elif evt_type == "scenario_finished":
                self._scenario_finished_data = evt_data
                self._finished_event.set()

    def _init_commlib_node(self) -> None:
        """Initializes commlib Node, telemetry publishers, RTMonitor subscriber, and deploy RPC client."""
        print(f"[*] Initializing {self.broker_type.upper()} Node...")
        conn_params = get_connection_parameters(self.broker_type, self.host, self.port)

        run_label = (
            self.manifest_data.get("sweep_id", "sweep")
            if self.is_sweep_mode
            else self.scenario_data.get("scenario_id", "sim")
        )
        source_scenario = (
            self.baseline_data if self.is_sweep_mode else self.scenario_data
        )

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

        self.event_sub = self.node.create_psubscriber(
            topic=RTMONITOR_WILDCARD_TOPIC,
            msg_type=EventMsg,
            on_message=self._on_rtmonitor_event,
        )
        print(
            f"    - Registered RTMonitor pattern listener: {RTMONITOR_WILDCARD_TOPIC}"
        )

        if self.goal_model_content is not None:
            rpc_name = os.getenv(
                "EXECUTE_MODEL_RPC", "goaldsl.{ID}.deploy_sync"
            ).replace("{ID}", self.target_uid)
            self.deploy_rpc = self.node.create_rpc_client(
                msg_type=ExecuteModelMsg,
                rpc_name=rpc_name,
            )
            print(f"    - Registered deploy RPC client: {rpc_name}")

        self.node.run()
        print("[*] Node connected and ready.")

    def _deploy_goal_model(self) -> bool:
        """Deploys the loaded Goal-DSL specification to goaldsl-executor via RPC."""
        if self.deploy_rpc is None or self.goal_model_content is None:
            return True

        goal_label = self.goal_file.name if self.goal_file else "goal_model"
        print(f"[*] Deploying '{goal_label}' to goaldsl-executor via RPC...")
        try:
            msg = ExecuteModelMsg.Request(model=self.goal_model_content)
            resp = self.deploy_rpc.call(msg)
        except Exception as exc:
            print(f"❌ RPC error while deploying '{goal_label}': {exc}")
            return False

        if resp is None or getattr(resp, "status", 0) != 1:
            err_detail = getattr(resp, "result", "No response from executor")
            print(f"❌ Executor rejected model deployment: {err_detail}")
            return False

        print(
            f"[*] Model deployed successfully ({resp.result}). Waiting for goalee scenario_started..."
        )
        if self._started_event.wait(timeout=DEFAULT_START_SYNC_WAIT):
            print(
                "[*] Received RTMonitor scenario_started; starting telemetry at t=0.0s."
            )
        elif self.deploy_wait > 0:
            time.sleep(self.deploy_wait)
        return True

    def _record_case_result(
        self,
        scenario_id: str,
        elapsed_wall: float,
        completed_normally: bool,
        case_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Evaluates captured RTMonitor events, prints the verdict, and appends a row to the CSV."""
        with self._event_lock:
            fin = self._scenario_finished_data
            failed_goal = self._first_failed_goal
            failed_sim_t = self._first_failed_sim_time
            warnings = list(self._entity_warnings)
            first_warn_sim_t = self._first_warning_sim_time

        inject_time = case_meta.get("inject_time") if case_meta else None

        if not completed_normally:
            verdict = "INTERRUPTED"
            final_score: Any = ""
            goalee_elapsed_s: Any = ""
            goal_results_str = ""
        elif fin is None:
            verdict = "NO_RTMONITOR"
            final_score = ""
            goalee_elapsed_s = ""
            goal_results_str = ""
        else:
            raw_score = fin.get("score")
            final_score = round(float(raw_score), 4) if raw_score is not None else ""
            results = fin.get("results") or []
            all_passed = bool(results) and all(
                bool(item[1]) for item in results if len(item) >= 2
            )
            verdict = "PASS" if all_passed else "FAIL"

            if failed_goal is None and not all_passed:
                for item in results:
                    if len(item) >= 2 and not bool(item[1]):
                        failed_goal = str(item[0])
                        break

            elapsed_ms = fin.get("elapsed_time")
            goalee_elapsed_s = (
                round(float(elapsed_ms) / 1000.0, 3) if elapsed_ms is not None else ""
            )
            goal_results_str = "; ".join(
                f"{item[0]}={'PASS' if item[1] else 'FAIL'}"
                for item in results
                if len(item) >= 2
            )

        detection_latency_s: Any = ""
        if failed_sim_t is not None and inject_time is not None:
            detection_latency_s = round(max(0.0, failed_sim_t - float(inject_time)), 3)

        warning_count = len(warnings)
        warning_latency_s: Any = ""
        if first_warn_sim_t is not None and inject_time is not None:
            warning_latency_s = round(
                max(0.0, first_warn_sim_t - float(inject_time)), 3
            )
        runtime_warnings_str = "; ".join(
            f"{w.get('entity_name', 'unknown')}:{w.get('warning_type', 'unknown')}@{w.get('sim_time_s', '?')}s"
            for w in warnings
        )

        row: Dict[str, Any] = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "sweep" if case_meta is not None else "single",
            "scenario_id": scenario_id,
            "fault_id": case_meta.get("fault_id", "nominal")
            if case_meta
            else "nominal",
            "entity_name": case_meta.get("entity_name", "N/A") if case_meta else "N/A",
            "in_goal_spec": case_meta.get("in_goal_spec", "N/A")
            if case_meta
            else "N/A",
            "fault_type": case_meta.get("fault_type", "nominal")
            if case_meta
            else "nominal",
            "inject_time_s": inject_time if inject_time is not None else "",
            "verdict": verdict,
            "final_score": final_score,
            "failed_goal": failed_goal or "",
            "detection_latency_s": detection_latency_s,
            "goalee_elapsed_s": goalee_elapsed_s,
            "wall_elapsed_s": round(elapsed_wall, 3),
            "goal_results": goal_results_str,
            "warning_count": warning_count,
            "warning_latency_s": warning_latency_s,
            "runtime_warnings": runtime_warnings_str,
        }

        if self._csv_writer is not None and self._csv_file is not None:
            self._csv_writer.writerow(row)
            self._csv_file.flush()

        score_disp = f"{final_score:.2f}" if isinstance(final_score, float) else "N/A"
        fail_disp = f" | Failed Goal: {failed_goal}" if failed_goal else ""
        lat_disp = (
            f" | Latency: {detection_latency_s}s" if detection_latency_s != "" else ""
        )
        warn_disp = f" | Warnings: {warning_count}" if warning_count > 0 else ""
        print(
            f"📋 VERDICT: {verdict} | Score: {score_disp}{fail_disp}{lat_disp}{warn_disp}"
        )
        if verdict == "NO_RTMONITOR":
            print(
                "   ⚠️ No RTMonitor event received (verify RTMonitor block is present in .goal file)."
            )

        return row

    def _execute_single_scenario(
        self,
        scenario_data: Dict[str, Any],
        case_meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Executes the playback loop for one scenario dictionary and logs its verdict.

        Returns the logged CSV result dictionary if playback finished normally, or None if interrupted.
        """
        scenario_id = str(scenario_data.get("scenario_id", "unknown"))
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
        print(f"▶️ RUNNING SCENARIO: {scenario_id}")
        print(
            f"   Rate: {self.frequency_hz} Hz (step = {self.period_sec:.3f}s) | Scale: {self.time_scale}x"
        )
        print("=" * 60)

        try:
            while sim_time <= duration:
                with self._event_lock:
                    self._current_sim_time = sim_time

                if self._finished_event.is_set():
                    print(
                        f"⚡ [t={sim_time:6.2f}s] RTMonitor reported scenario_finished early; "
                        f"stopping telemetry stream."
                    )
                    break

                step_start_wall = time.time()

                # Dispatch any discrete events scheduled at or before the current timestamp
                for idx, event in enumerate(discrete_events):
                    if (
                        idx not in dispatched_event_indices
                        and event.get("time", 0.0) <= sim_time
                    ):
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

                # Evaluate and stream continuous telemetry grouped by topic.
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
                    print(
                        f"   ⏳ [t={sim_time:6.2f}s / {duration:.1f}s] ({pct:5.1f}%) Streaming telemetry..."
                    )

                # Advance clock
                sim_time += self.period_sec

                # Maintain loop frequency adjusted for time scaling
                target_sleep = (self.period_sec / self.time_scale) - (
                    time.time() - step_start_wall
                )
                if target_sleep > 0:
                    time.sleep(target_sleep)

            # Wait briefly for goalee's final verdict if the stream reached duration end first
            if not self._finished_event.is_set() and DEFAULT_POST_VERDICT_WAIT > 0:
                print(
                    f"   ⏳ Waiting up to {DEFAULT_POST_VERDICT_WAIT:.1f}s for final RTMonitor verdict..."
                )
                self._finished_event.wait(timeout=DEFAULT_POST_VERDICT_WAIT)

        except KeyboardInterrupt:
            completed_normally = False
            print("\n⚠️ Simulation manually interrupted by user.")

        elapsed_wall = time.time() - wall_start_time
        print("\n" + "=" * 60)
        print(
            f"🏁 SCENARIO COMPLETED | Simulated: {min(sim_time, duration):.2f}s | Wall Clock: {elapsed_wall:.2f}s"
        )
        row = self._record_case_result(
            scenario_id=scenario_id,
            elapsed_wall=elapsed_wall,
            completed_normally=completed_normally,
            case_meta=case_meta,
        )
        print("=" * 60)

        return row if completed_normally else None

    def run(self) -> None:
        """Runs either a single scenario or a batch fault sweep."""
        try:
            if not self.is_sweep_mode:
                self._reset_run_telemetry()
                if not self._deploy_goal_model():
                    print(
                        "🛑 Aborting scenario execution due to model deployment failure."
                    )
                    return
                self._execute_single_scenario(self.scenario_data)
                if self.csv_path:
                    print(f"[*] Result saved to: {self.csv_path}")
                return

            cases = self.manifest_data.get("cases", [])
            total_cases = len(cases)
            if total_cases == 0:
                print("⚠️ Fault sweep manifest contains 0 cases. Nothing to run.")
                return

            completed_count = 0
            pass_count = 0
            fail_count = 0
            other_count = 0
            auto_deploy = self.deploy_rpc is not None

            for idx, case in enumerate(cases):
                case_num = idx + 1
                fault_id = case.get("fault_id", f"case_{case_num}")
                entity_name = case.get("entity_name", "unknown")
                fault_type = case.get("fault_type", "unknown")
                inject_time = case.get("inject_time", 0.0)
                in_goal = case.get("in_goal_spec", False)

                if not auto_deploy and idx > 0:
                    print("\n" + "-" * 60)
                    print(
                        f"⏭️  UPCOMING CASE [{case_num}/{total_cases}]: {fault_id}\n"
                        f"    Entity: {entity_name} (in_goal_spec={in_goal}) | "
                        f"Fault: {fault_type} @ t={inject_time}s"
                    )
                    print("-" * 60)
                    try:
                        input(
                            "⏸️  Deploy the .goal model to goaldsl-executor, "
                            "then press Enter to start the next case (or Ctrl+C to stop)..."
                        )
                    except KeyboardInterrupt:
                        print("\n⚠️ Fault sweep aborted by user before next case.")
                        break
                else:
                    print("\n" + "-" * 60)
                    print(
                        f"▶️  STARTING SWEEP CASE [{case_num}/{total_cases}]: {fault_id}\n"
                        f"    Entity: {entity_name} (in_goal_spec={in_goal}) | "
                        f"Fault: {fault_type} @ t={inject_time}s"
                    )
                    print("-" * 60)

                self._reset_run_telemetry()

                if auto_deploy and not self._deploy_goal_model():
                    print(
                        f"🛑 Stopping fault sweep at case [{case_num}/{total_cases}] due to deployment failure."
                    )
                    break

                mutated_scenario = materialize_fault_scenario(self.baseline_data, case)
                result_row = self._execute_single_scenario(
                    mutated_scenario, case_meta=case
                )
                if result_row is None:
                    print("🛑 Stopping fault sweep due to manual interruption.")
                    break

                completed_count += 1
                verdict = result_row.get("verdict")
                if verdict == "PASS":
                    pass_count += 1
                elif verdict == "FAIL":
                    fail_count += 1
                else:
                    other_count += 1

            print("\n" + "=" * 60)
            print(
                f"📊 FAULT SWEEP SUMMARY | Completed: {completed_count}/{total_cases} | "
                f"PASS: {pass_count} | FAIL: {fail_count} | OTHER: {other_count}"
            )
            if self.csv_path:
                print(f"💾 Results CSV saved to: {self.csv_path}")
            print("=" * 60)
        finally:
            self.stop()

    def stop(self) -> None:
        """Closes the CSV file and stops the underlying commlib node cleanly."""
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None

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
        "--goal-file",
        default=None,
        help="Optional path to a .goal model file to automatically deploy before each run",
    )
    parser.add_argument(
        "--uid",
        default=None,
        help="Target executor UID (overrides UID env var; default: TEST)",
    )
    parser.add_argument(
        "--deploy-wait",
        type=float,
        default=DEFAULT_DEPLOY_WAIT,
        help=f"Seconds to wait after RPC model deployment before streaming telemetry (default: {DEFAULT_DEPLOY_WAIT})",
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
        help="Time multiplier (default: 1.0 real-time; 2.0 = 2x speed)",
    )

    args = parser.parse_args()

    runner = ScenarioRunner(
        scenario_file=args.scenario_file,
        goal_file=args.goal_file,
        broker_type=args.broker,
        host=args.host,
        port=args.port,
        frequency_hz=args.freq,
        time_scale=args.scale,
        uid=args.uid,
        deploy_wait=args.deploy_wait,
    )
    runner.run()
