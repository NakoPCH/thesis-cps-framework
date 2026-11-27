"""scenario_pipeline.py

Stage 3 Orchestrator: Scenario & Fault Generation Pipeline.
Autonomously generates, validates, and self-repairs CPS test scenarios across
three categories ('operational', 'fault', 'security') using the active .goal
specification and physical world model.
"""

import csv
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# Resolve cross-stage module imports matching the src/ directory structure
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent
stage1_dir = src_dir / "1_requirement_refinement"
stage2_dir = src_dir / "2_constraint_generator"

for path in (current_dir, src_dir, stage1_dir, stage2_dir):
    if str(path) not in sys.path:
        sys.path.append(str(path))

from envpop_parser import parse_envpop_model
from scenario_prompts import (
    SYSTEM_PROMPT,
    build_scenario_generation_prompt,
    build_scenario_repair_prompt,
    load_scenario_schema,
)
from scenario_validator import validate_scenario
from world_context import extract_detailed_world_context

from common.config import (
    BASE_URL,
    DATA_DIR,
    EXPERIMENTS_DIR,
    MAX_RETRIES,
    MODEL,
    TOKEN,
    WORLD_MODELS_DIR,
)

SCENARIO_CATEGORIES = ["nominal", "operational", "fault", "security"]
SCENARIOS_OUTPUT_DIR = DATA_DIR / "scenarios"
CODE_FENCE = "`" * 3


def load_goal_dsl_file(goal_path: Optional[str | Path] = None) -> Optional[str]:
    """Loads the compiled .goal specification produced by Stage 2."""
    target_path = Path(goal_path) if goal_path else (DATA_DIR / "llm_created_file.goal")
    if not target_path.exists():
        print(f"❌ Error: Goal-DSL file not found at {target_path}")
        return None

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        print(f"❌ Error reading Goal-DSL file {target_path}: {e}")
        return None


def _sanitize_error_text(text: Optional[str]) -> str:
    """Flattens multiline error logs for single-line CSV display."""
    if not text:
        return "-"
    return text.strip().replace("\r\n", " | ").replace("\n", " | ")


def _log_scenario_attempt_to_csv(csv_path: str | Path, record: Dict[str, Any]) -> None:
    """Appends a scenario generation/validation attempt row to eval_scenarios.csv."""
    os.makedirs(Path(csv_path).parent, exist_ok=True)
    file_exists = os.path.exists(csv_path)
    headers = [
        "Run_ID",
        "Timestamp",
        "Model",
        "Category",
        "Attempt",
        "Max_Retries",
        "Status",
        "Error_Count",
        "Primary_Error_Type",
        "Error_Line_No",
        "Error_Code_Line",
        "Error_Message",
    ]

    with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)


def _query_llm_generator(
    messages: List[Dict[str, str]], 
    temperature: float = 0.7,
    max_api_retries: int = 3,
) -> Optional[str]:
    """Handles API dispatching with 429 rate-limit backoff and robust YAML extraction."""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "HTTP-Referer": "https://github.com/your-username/thesis",
        "X-Title": "Thesis Stage 3 Scenario Generator",
    }
    
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
    }

    for api_attempt in range(max_api_retries):
        t0 = time.time()
        print(f"   ⏳ Dispatching prompt to {MODEL} (temp={temperature})...")

        try:
            response = requests.post(
                f"{BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
        except requests.exceptions.Timeout:
            print("   ⏱️ API Error: Request timed out after 120 seconds.")
            return None
        except Exception as e:
            print(f"   ❌ Connection error: {e}")
            return None

        elapsed = time.time() - t0
        print(f"   📥 Response received in {elapsed:.1f}s.")

        # Handle upstream Google 429 rate limits dynamically
        if response.status_code == 429:
            delay = 45.0
            match = re.search(r"retry in ([0-9.]+)s", response.text)
            if not match:
                match = re.search(r'"retryDelay":\s*"(\d+)s?"', response.text)
            if match:
                delay = float(match.group(1)) + 2.0

            print(f"   ⚠️ Rate limited (429). Sleeping {delay:.1f}s before retry ({api_attempt + 1}/{max_api_retries})...")
            time.sleep(delay)
            continue

        if response.status_code != 200:
            print(f"   ❌ API Error {response.status_code}: {response.text}")
            return None

        data = response.json()
        if "choices" not in data or not data["choices"]:
            print(f"   ❌ Provider returned unexpected payload:\n{data}")
            return None

        raw_content = data["choices"][0]["message"].get("content")
        if not raw_content:
            print("   ❌ Provider returned null or empty content.")
            return None

        raw_content = raw_content.strip()

        # Extract clean YAML from markdown code fences, ignoring conversational preamble
        match = re.search(r"```(?:yaml)?\s*\n(.*?)\n```", raw_content, re.DOTALL | re.IGNORECASE)
        if match:
            generated_yaml = match.group(1).strip()
        else:
            scen_match = re.search(r"(scenario_id\s*:.*)", raw_content, re.DOTALL)
            generated_yaml = scen_match.group(1).strip() if scen_match else raw_content

        return generated_yaml

    print("   ❌ Exhausted API retries due to repeated rate limits.")
    return None


def generate_and_verify_category_scenario(
    category: str,
    goal_dsl_content: str,
    world_context_str: str,
    world_summary: Optional[Dict[str, Any]],
    yaml_world_file: Optional[str | Path],
    schema_template: str,
    run_id: str,
    max_retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """Generates and validates a scenario for a single category with self-repair retries."""
    os.makedirs(SCENARIOS_OUTPUT_DIR, exist_ok=True)
    csv_path = EXPERIMENTS_DIR / "eval_scenarios.csv"

    # Extract domain prefix from '<domain>_world_model.yaml' (e.g., 'hospital_')
    world_prefix = ""
    if yaml_world_file:
        stem = Path(yaml_world_file).stem
        if stem != "world_model":
            domain_name = stem.removesuffix("_world_model")
            if domain_name:
                world_prefix = f"{domain_name}_"

    base_filename = (
        "nominal_baseline.yaml" if category == "nominal" else f"scenario_{category}.yaml"
    )
    output_filename = f"{world_prefix}{base_filename}"
    output_file = SCENARIOS_OUTPUT_DIR / output_filename

    initial_user_prompt = build_scenario_generation_prompt(
        category=category,
        goal_dsl_content=goal_dsl_content,
        world_context_str=world_context_str,
        world_summary=world_summary,
        schema_template=schema_template,
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_user_prompt},
    ]

    attempts_history: List[Dict[str, Any]] = []

    print(f"\n----------------------------------------------")
    print(f"🎯 Generating Scenario Category: [{category.upper()}] -> {output_filename}")
    print(f"----------------------------------------------")

    for attempt in range(max_retries):
        attempt_num = attempt + 1
        
        # Fresh creative scenario on attempt 1; deterministic precision on repair attempts
        current_temp = 0.7 if attempt == 0 else 0.0
        
        print(f"\n   {run_id} | [{category}] Attempt {attempt_num} of {max_retries} (temp={current_temp})")
        generated_yaml = _query_llm_generator(messages, temperature=current_temp)

        if not generated_yaml:
            err_msg = "No response payload received from LLM provider."
            attempts_history.append(
                {
                    "attempt": attempt_num,
                    "status": "FAILED",
                    "error_count": 1,
                    "primary_error_type": "LLM API Error",
                    "error_line_no": "-",
                    "error_code_line": "-",
                    "error_message": err_msg,
                    "generated_yaml": None,
                }
            )
            _log_scenario_attempt_to_csv(
                csv_path,
                {
                    "Run_ID": run_id,
                    "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "Model": MODEL,
                    "Category": category,
                    "Attempt": attempt_num,
                    "Max_Retries": max_retries,
                    "Status": "FAILED",
                    "Error_Count": 1,
                    "Primary_Error_Type": "LLM API Error",
                    "Error_Line_No": "-",
                    "Error_Code_Line": "-",
                    "Error_Message": err_msg,
                },
            )
            break

        # Save candidate YAML to disk
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(generated_yaml)

        # Validate candidate scenario against schema, math, time, and world model
        val_result = validate_scenario(
            scenario_input=generated_yaml,
            world_model_path=yaml_world_file,
        )

        # Confirm the LLM kept the requested category tag
        if val_result.is_valid and val_result.scenario_data:
            actual_cat = str(val_result.scenario_data.get("category", "")).strip().lower()
            if actual_cat != category:
                val_result.is_valid = False
                from scenario_validator import ValidationError

                val_result.errors.append(
                    ValidationError(
                        line_number=2,
                        error_type="Category Mismatch Error",
                        source_line=f"category: {actual_cat}",
                        message=f"Expected category '{category}', but YAML declared '{actual_cat}'.",
                        fix_hint=f'Set category: "{category}" at the top of the file.',
                    )
                )

        if not val_result.is_valid:
            first_err = val_result.errors[0] if val_result.errors else None
            err_type = first_err.error_type if first_err else "Validation Error"
            err_line = str(first_err.line_number) if (first_err and first_err.line_number) else "-"
            err_code = first_err.source_line.strip() if (first_err and first_err.source_line) else "-"
            feedback_text = val_result.get_llm_feedback()

            print(f"❌ [{category}] Validation Failed ({len(val_result.errors)} error(s)):")
            print(val_result)

            attempts_history.append(
                {
                    "attempt": attempt_num,
                    "status": "FAILED",
                    "error_count": len(val_result.errors),
                    "primary_error_type": err_type,
                    "error_line_no": err_line,
                    "error_code_line": err_code,
                    "error_message": feedback_text,
                    "generated_yaml": generated_yaml,
                }
            )

            _log_scenario_attempt_to_csv(
                csv_path,
                {
                    "Run_ID": run_id,
                    "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "Model": MODEL,
                    "Category": category,
                    "Attempt": attempt_num,
                    "Max_Retries": max_retries,
                    "Status": "FAILED",
                    "Error_Count": len(val_result.errors),
                    "Primary_Error_Type": err_type,
                    "Error_Line_No": err_line,
                    "Error_Code_Line": _sanitize_error_text(err_code),
                    "Error_Message": _sanitize_error_text(feedback_text),
                },
            )

            # Append self-repair turn to conversation history
            repair_prompt = build_scenario_repair_prompt(
                category=category,
                broken_yaml=generated_yaml,
                validation_feedback=feedback_text,
                goal_dsl_content=goal_dsl_content,
                world_context_str=world_context_str,
                world_summary=world_summary,
            )
            messages.append({"role": "assistant", "content": generated_yaml})
            messages.append({"role": "user", "content": repair_prompt})
            continue

        # Scenario passed all checks
        print(f"✅ [{category.upper()}] Scenario Validated Successfully on Attempt {attempt_num}!")
        if val_result.warnings:
            for w in val_result.warnings:
                print(f"   ⚠️ Warning: {w}")

        attempts_history.append(
            {
                "attempt": attempt_num,
                "status": "SUCCESS",
                "error_count": 0,
                "primary_error_type": None,
                "error_line_no": "-",
                "error_code_line": "-",
                "error_message": None,
                "generated_yaml": generated_yaml,
            }
        )

        _log_scenario_attempt_to_csv(
            csv_path,
            {
                "Run_ID": run_id,
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Model": MODEL,
                "Category": category,
                "Attempt": attempt_num,
                "Max_Retries": max_retries,
                "Status": "SUCCESS",
                "Error_Count": 0,
                "Primary_Error_Type": "-",
                "Error_Line_No": "-",
                "Error_Code_Line": "-",
                "Error_Message": "-",
            },
        )

        return {
            "category": category,
            "status": "SUCCESS",
            "total_attempts": attempt_num,
            "file_path": str(output_file),
            "generated_yaml": generated_yaml,
            "scenario_data": val_result.scenario_data,
            "attempts": attempts_history,
        }

    # If all retries exhausted, move broken output to failed backup
    if output_file.exists():
        failed_backup = SCENARIOS_OUTPUT_DIR / f"{world_prefix}failed_scenario_{category}.yaml"
        os.replace(output_file, failed_backup)

    print(f"🚨 [{category.upper()}] Failed to generate valid scenario after {max_retries} attempts.")
    return {
        "category": category,
        "status": "FAILED",
        "total_attempts": len(attempts_history),
        "file_path": None,
        "generated_yaml": None,
        "scenario_data": None,
        "attempts": attempts_history,
    }

    # If all retries exhausted, move broken output to failed backup
    if output_file.exists():
        failed_backup = SCENARIOS_OUTPUT_DIR / f"failed_scenario_{category}.yaml"
        os.replace(output_file, failed_backup)

    print(f"🚨 [{category.upper()}] Failed to generate valid scenario after {max_retries} attempts.")
    return {
        "category": category,
        "status": "FAILED",
        "total_attempts": len(attempts_history),
        "file_path": None,
        "generated_yaml": None,
        "scenario_data": None,
        "attempts": attempts_history,
    }


def run_scenario_generation_pipeline(
    goal_dsl_input: Optional[str | Path] = None,
    yaml_world_file: Optional[str | Path] = None,
    max_retries: int = MAX_RETRIES,
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Main entry point for Stage 3 (Scenario & Fault Generation).

    Args:
        goal_dsl_input: Raw Goal-DSL code string, path to .goal file, or None
                        (defaults to DATA_DIR / 'llm_created_file.goal').
        yaml_world_file: Path to the EnvPop world_model.yaml file
                         (defaults to WORLD_MODELS_DIR / 'world_model.yaml').
        max_retries: Maximum self-repair attempts per scenario category.
        categories: List of scenario categories to generate
                    (defaults to ['operational', 'fault', 'security']).

    Returns:
        Dictionary containing overall status, run_id, and per-category results.
    """
    run_id = f"SCEN-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    target_categories = categories or SCENARIO_CATEGORIES

    # 1. Resolve Goal-DSL input (accepts raw code string or file path)
    if goal_dsl_input and isinstance(goal_dsl_input, str) and "Goal" in goal_dsl_input:
        goal_dsl_content = goal_dsl_input.strip()
    else:
        goal_dsl_content = load_goal_dsl_file(goal_dsl_input)

    if not goal_dsl_content:
        return {
            "status": "FAILED",
            "run_id": run_id,
            "error": "No valid Goal-DSL (.goal) specification available for Stage 3.",
            "scenarios": {},
        }

    # 2. Resolve World Model Context (Stage 1 & Stage 2 parsers)
    world_path = Path(yaml_world_file) if yaml_world_file else (WORLD_MODELS_DIR / "world_model.yaml")
    world_context_str = extract_detailed_world_context(world_path)

    world_summary = None
    if world_path.exists():
        try:
            world_summary = parse_envpop_model(world_path)
        except Exception as e:
            print(f"⚠️ Warning: Could not parse world summary via envpop_parser: {e}")
    else:
        print(f"⚠️ Warning: World model file not found at {world_path}")

    # 3. Load Schema Template
    schema_template = load_scenario_schema()

    print("=" * 60)
    print(f"🚀 INITIALIZING STAGE 3: SCENARIO & FAULT GENERATOR ({run_id})")
    print(f"   Categories : {target_categories}")
    print(f"   World Model: {world_path.name}")
    print(f"   Max Retries: {max_retries}")
    print("=" * 60)

    scenario_results: Dict[str, Dict[str, Any]] = {}
    all_succeeded = True

    for idx, category in enumerate(target_categories):
        if idx > 0:
            cooldown_sec = 20
            print(f"\n⏳ Inter-category cooldown: waiting {cooldown_sec}s to replenish token quota...")
            time.sleep(cooldown_sec)

        res = generate_and_verify_category_scenario(
            category=category,
            goal_dsl_content=goal_dsl_content,
            world_context_str=world_context_str,
            world_summary=world_summary,
            yaml_world_file=world_path,
            schema_template=schema_template,
            run_id=run_id,
            max_retries=max_retries,
        )
        scenario_results[category] = res
        if res["status"] != "SUCCESS":
            all_succeeded = False

    overall_status = "SUCCESS" if all_succeeded else (
        "PARTIAL_SUCCESS"
        if any(r["status"] == "SUCCESS" for r in scenario_results.values())
        else "FAILED"
    )

    print("\n" + "=" * 60)
    print(f"🏁 STAGE 3 COMPLETE | Status: {overall_status}")
    for cat, res in scenario_results.items():
        status_icon = "✅" if res["status"] == "SUCCESS" else "❌"
        print(
            f"   {status_icon} {cat.upper():<12} | Attempts: {res['total_attempts']} | "
            f"File: {res['file_path'] or 'None'}"
        )
    print("=" * 60)

    return {
        "status": overall_status,
        "run_id": run_id,
        "output_dir": str(SCENARIOS_OUTPUT_DIR),
        "scenarios": scenario_results,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run Stage 3 Scenario & Fault Generation Pipeline."
    )
    parser.add_argument(
        "--world",
        type=str,
        default=str(WORLD_MODELS_DIR / "world_model.yaml"),
        help="Path to the EnvPop world model YAML file.",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=str(DATA_DIR / "llm_created_file.goal"),
        help="Path to the validated .goal file.",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=None,
        help="Optional subset of categories to generate (e.g., nominal operational fault security).",
    )
    args = parser.parse_args()

    pipeline_output = run_scenario_generation_pipeline(
        goal_dsl_input=Path(args.goal),
        yaml_world_file=Path(args.world),
        max_retries=MAX_RETRIES,
        categories=args.categories,
    )