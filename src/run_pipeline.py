import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Add src subdirectories to Python path for clean cross-stage imports
SRC_DIR = Path(__file__).resolve().parent
sys.path.append(str(SRC_DIR / "1_requirement_refinement"))
sys.path.append(str(SRC_DIR / "2_constraint_generator"))

from requirement_refiner import RequirementRefiner
from verification_loop import generate_and_verify_dsl

from common.config import DATA_DIR, EXPERIMENTS_DIR, MAX_RETRIES, WORLD_MODELS_DIR


def run_pipeline(world_yaml_path: Path | None = None):
    session_start_time = time.time()
    session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"

    print("=" * 70)
    print("   END-TO-END CPS SPECIFICATION & VERIFICATION PIPELINE")
    print("=" * 70)

    if world_yaml_path and world_yaml_path.exists():
        print(f"🌍 Grounded Mode: World Model '{world_yaml_path.name}'")
    else:
        print("🌐 Standalone Mode: No world model loaded")

    print(f"🆔 Session ID: {session_id}\n" + "-" * 70)

    # Initialize Stage 1 Dialogue Manager
    refiner = RequirementRefiner(yaml_world_file=world_yaml_path, max_turns=15)
    stage1_completed = False
    refined_prompt = None

    # -------------------------------------------------------------------------
    # STAGE 1: Interactive Requirement Refinement Dialogue
    # -------------------------------------------------------------------------
    print("\n[STAGE 1] Interactive Requirement Refinement Started.")
    print("Type your operational goal in natural language (or 'exit'/'quit' to cancel).\n")

    while True:
        try:
            user_input = input("\n👤 Operator: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n🚨 Pipeline aborted by user during Stage 1.")
            break

        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit"]:
            print("Session terminated by user.")
            break

        print("\n🤖 Refiner is processing...", end="\r")
        result = refiner.process_turn(user_input)

        print(" " * 40, end="\r")  # Clear waiting indicator
        print(f"🤖 Refiner:\n{result['message']}")

        if result["is_complete"]:
            stage1_completed = True
            refined_prompt = result["refined_prompt"]
            print("\n" + "=" * 70)
            print(f"🎉 Stage 1 Complete in {result['turns_taken']} turns!")
            print("=" * 70)
            break

    stage1_telemetry = refiner.export_session_data()

    # If Stage 1 was aborted without generating a prompt
    if not stage1_completed or not refined_prompt:
        _save_pipeline_json_log(
            session_id=session_id,
            world_yaml_path=world_yaml_path,
            overall_status="ABORTED_IN_STAGE1",
            duration_sec=round(time.time() - session_start_time, 2),
            stage1_data=stage1_telemetry,
            stage2_data=None,
        )
        print("\n❌ Pipeline ended prior to Stage 2 verification.")
        return

    # Save refined prompt files for reference
    prompts_dir = DATA_DIR / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    with open(prompts_dir / "refined_prompt.txt", "w", encoding="utf-8") as f:
        f.write(refined_prompt)

    # -------------------------------------------------------------------------
    # STAGE 2: Code Generation & Tiered Formal Verification
    # -------------------------------------------------------------------------
    print("\n🚀 Transitioning to Stage 2: Code Generation & Verification Loop...")

    full_stage2_prompt = (
        "Task: Create a complete Goal DSL file that follows all formal constraints. "
        "Here is the explanation in natural language:\n" + refined_prompt
    )

    stage2_telemetry = generate_and_verify_dsl(
        nl_prompt=full_stage2_prompt,
        yaml_world_file=world_yaml_path,
        max_retries=MAX_RETRIES,
    )

    overall_status = (
        "SUCCESS" if stage2_telemetry.get("status") == "SUCCESS" else "STAGE2_FAILED"
    )
    total_duration = round(time.time() - session_start_time, 2)

    # -------------------------------------------------------------------------
    # Logging Telemetry Artifact
    # -------------------------------------------------------------------------
    log_file_path = _save_pipeline_json_log(
        session_id=session_id,
        world_yaml_path=world_yaml_path,
        overall_status=overall_status,
        duration_sec=total_duration,
        stage1_data=stage1_telemetry,
        stage2_data=stage2_telemetry,
    )

    print("\n" + "=" * 70)
    print(f"🏁 PIPELINE EXECUTION FINISHED: [{overall_status}]")
    print(f"⏱️ Total Latency: {total_duration}s")
    print(f"📄 Full Session JSON Log Saved To:\n   {log_file_path}")
    print("=" * 70)


def _save_pipeline_json_log(
    session_id: str,
    world_yaml_path: Path | None,
    overall_status: str,
    duration_sec: float,
    stage1_data: dict,
    stage2_data: dict | None,
) -> Path:
    logs_dir = EXPERIMENTS_DIR / "sessions"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = logs_dir / f"{session_id}.json"

    session_artifact = {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        "overall_status": overall_status,
        "total_latency_seconds": duration_sec,
        "world_model_file": str(world_yaml_path) if world_yaml_path else None,
        "stage1_dialogue": stage1_data,
        "stage2_verification": stage2_data,
    }

    with open(log_file_path, "w", encoding="utf-8") as f:
        json.dump(session_artifact, f, indent=2, ensure_ascii=False)

    return log_file_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run End-to-End CPS Pipeline (Stage 1 Dialogue + Stage 2 Formal Verification)."
    )
    parser.add_argument(
        "--world",
        type=str,
        default=None,
        help="Path to world_model.yaml. Uses default from WORLD_MODELS_DIR if omitted.",
    )
    parser.add_argument(
        "--no-world",
        action="store_true",
        help="Run pipeline without world model grounding.",
    )

    args = parser.parse_args()

    world_path = None
    if not args.no_world:
        if args.world:
            world_path = Path(args.world)
        else:
            default_world = WORLD_MODELS_DIR / "world_model.yaml"
            if default_world.exists():
                world_path = default_world

    run_pipeline(world_path)