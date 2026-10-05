import csv
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import uuid

from dotenv import load_dotenv
from envpop_parser import parse_envpop_model
from envpop_validator import validate_goal_dsl_against_world
import requests

# Import dev configuration switches
from pipeline_config import (
    ENABLE_TIER_1,
    ENABLE_TIER_2,
    ENABLE_TIER_3,
    MAX_RETRIES,
    MODEL,
)
from semantic_judge import evaluate_semantics

load_dotenv()

TOKEN = os.getenv("OPENROUTER_API_TOKEN")
BASE_URL = "https://openrouter.ai/api/v1"


def load_example_code(filename="reference_example.goal"):
    base_path = os.path.dirname(__file__)
    file_path = os.path.join(base_path, filename)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def load_nl_input(filename="prompt.txt"):
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(script_dir, filename)
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"Error: {filename} not found.")
        return None


def _extract_syntax_error_details(syntax_error, dsl_code):
    """Extracts the line number and the exact code line from textX compiler errors."""
    if not syntax_error:
        return "-", "-"

    # Matches patterns like: "filename.goal:14:1: Expected..." or ":14:"
    match = re.search(r":(\d+):(?:\d+)?:", syntax_error)
    if not match:
        match = re.search(r":(\d+):", syntax_error)

    if match:
        line_num = int(match.group(1))
        lines = dsl_code.splitlines()
        if 1 <= line_num <= len(lines):
            return str(line_num), lines[line_num - 1].strip()
        return str(line_num), "-"

    return "-", "-"


def _sanitize_error_text(text):
    """Flattens multiline error logs for clean single-line CSV display."""
    if not text:
        return "-"
    return text.strip().replace("\r\n", " | ").replace("\n", " | ")


def _log_attempt_to_csv(csv_path, record):
    """Appends an attempt row to eval_results.csv."""
    file_exists = os.path.exists(csv_path)
    headers = [
        "Run_ID",
        "Timestamp",
        "Model",
        "Attempt",
        "Max_Retries",
        "Status",
        "Active_Tiers",
        "Failed_Tier",
        "Error_Line_No",
        "Error_Code_Line",
        "Error_Message",
    ]

    with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)


def _query_llm_generator(messages):
    """Handles API request dispatching, error catching, and markdown sanitation."""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "HTTP-Referer": "https://github.com/your-username/thesis",
        "X-Title": "Thesis Verification Loop",
    }
    payload = {"model": MODEL, "messages": messages, "temperature": 0.0}

    try:
        response = requests.post(
            f"{BASE_URL}/chat/completions", headers=headers, json=payload
        )
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return None

    if response.status_code != 200:
        print(f"❌ API Error {response.status_code}: {response.text}")
        return None

    data = response.json()
    if "choices" not in data:
        print(f"❌ Provider returned unexpected payload:\n{data}")
        return None

    generated_code = data["choices"][0]["message"]["content"].strip()

    # Sanitize markdown formatting block wraps
    if generated_code.startswith("```"):
        lines = generated_code.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        generated_code = "\n".join(lines).strip()

    return generated_code


def _run_syntax_check(file_path):
    print("Validating with Goal DSL Parser (Tier 1)...")
    result = subprocess.run(
        ["goaldsl", "validate", file_path], capture_output=True, text=True
    )
    is_valid = result.returncode == 0 and "success" in result.stdout
    error_msg = (
        None if is_valid else (result.stderr if result.stderr else result.stdout)
    )
    return is_valid, error_msg


def generate_and_verify_dsl(nl_prompt, yaml_world_file=None, max_retries=MAX_RETRIES):
    example_code = load_example_code()

    # Create a unique ID for this entire execution run
    run_id = f"RUN-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

    world_model = None
    valid_devices_hint = "No active world model specified."
    if yaml_world_file and os.path.exists(yaml_world_file):
        try:
            world_model = parse_envpop_model(yaml_world_file)
            valid_devices_hint = (
                f"Sensors: {list(world_model['sensors'].keys())}\n"
                f"Actuators: {list(world_model['actuators'].keys())}\n"
                f"Locations: {world_model['locations']}"
            )
        except Exception as e:
            print(f"⚠️ Warning: Could not parse world model YAML: {e}")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a Goal DSL expert. Generate ONLY valid Goal DSL code based on the user's prompt. "
                f"Below are examples of valid Goal DSL files:\n{example_code}"
                " "
                "CRITICAL SYNTAX RULES:"
                "1. Every 'Entity' MUST contain: 'type', 'uri', 'source', and 'attributes'."
                "2. In 'Goal<EntityStateCondition>', use the 'condition:' block (and optionally the timeConstraints:). NEVER include an 'entity:' line in this goal type."
                "3. Entities in conditions MUST be referenced using dot-notation: <EntityName>.<attributeName>."
                "Do not include markdown formatting or explanations.\n"
                "If the prompt omits technical details, automatically infer valid defaults to satisfy the syntax rules.\n"
            ),
        },
        {"role": "user", "content": nl_prompt},
    ]

    script_dir = os.path.dirname(os.path.abspath(__file__))
    file_name = os.path.join(script_dir, "llm_created_file.goal")
    csv_path = os.path.join(script_dir, "eval_results.csv")
    pipeline_success = False

    active_tier_list = [
        name
        for name, enabled in [
            ("Tier 1", ENABLE_TIER_1),
            ("Tier 2", ENABLE_TIER_2),
            ("Tier 3", ENABLE_TIER_3),
        ]
        if enabled
    ]
    active_tiers_str = ", ".join(active_tier_list) if active_tier_list else "None"

    print(f"🚀 Initializing {run_id} | Active Stages: [{active_tiers_str}]")

    for attempt in range(max_retries):
        attempt_num = attempt + 1
        print(f"\n==============================================")
        print(f"   {run_id} | Attempt {attempt_num} of {max_retries}")
        print(f"==============================================")

        generated_code = _query_llm_generator(messages)
        if not generated_code:
            _log_attempt_to_csv(
                csv_path,
                {
                    "Run_ID": run_id,
                    "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "Model": MODEL,
                    "Attempt": attempt_num,
                    "Max_Retries": max_retries,
                    "Status": "FAILED",
                    "Active_Tiers": active_tiers_str,
                    "Failed_Tier": "LLM Generation",
                    "Error_Line_No": "-",
                    "Error_Code_Line": "-",
                    "Error_Message": "No response payload received from OpenRouter.",
                },
            )
            break

        with open(file_name, "w", encoding="utf-8") as f:
            f.write(generated_code)

        # ----------------------------------------------------
        # TIER 1: Grammar & Syntax Check (goaldsl CLI)
        # ----------------------------------------------------
        if ENABLE_TIER_1:
            syntax_passed, syntax_error = _run_syntax_check(file_name)
            if not syntax_passed:
                print(f"❌ Tier 1 Failed. Syntax Error:\n{syntax_error}")
                line_no, code_line = _extract_syntax_error_details(syntax_error, generated_code)

                _log_attempt_to_csv(
                    csv_path,
                    {
                        "Run_ID": run_id,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "Model": MODEL,
                        "Attempt": attempt_num,
                        "Max_Retries": max_retries,
                        "Status": "FAILED",
                        "Active_Tiers": active_tiers_str,
                        "Failed_Tier": "Tier 1 (Syntax)",
                        "Error_Line_No": line_no,
                        "Error_Code_Line": code_line,
                        "Error_Message": _sanitize_error_text(syntax_error),
                    },
                )

                messages.append({"role": "assistant", "content": generated_code})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"The validation failed with this syntax error: {syntax_error}. "
                            "Please fix the syntax constraints."
                        ),
                    }
                )
                continue
            print("✅ Tier 1 Passed: Syntax is structurally valid.")
        else:
            print("⏩ Tier 1 Skipped (Disabled in config).")

        # ----------------------------------------------------
        # TIER 2: EnvPop Ground Truth Validation (Deterministic)
        # ----------------------------------------------------
        if ENABLE_TIER_2:
            print("Validating with EnvPop World Model (Tier 2)...")
            if not world_model:
                print("⚠️ Tier 2 Error: World validation enabled, but no valid YAML file was loaded.")
                _log_attempt_to_csv(
                    csv_path,
                    {
                        "Run_ID": run_id,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "Model": MODEL,
                        "Attempt": attempt_num,
                        "Max_Retries": max_retries,
                        "Status": "FAILED",
                        "Active_Tiers": active_tiers_str,
                        "Failed_Tier": "Tier 2 (Config Error)",
                        "Error_Line_No": "-",
                        "Error_Code_Line": "-",
                        "Error_Message": "World model YAML file missing or invalid.",
                    },
                )
                break

            world_passed, world_errors = validate_goal_dsl_against_world(
                generated_code, world_model
            )

            if not world_passed:
                feedback_str = "\n".join(f"- {err}" for err in world_errors)
                print(f"❌ Tier 2 Failed: World Model Inconsistencies Detected:\n{feedback_str}")

                _log_attempt_to_csv(
                    csv_path,
                    {
                        "Run_ID": run_id,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "Model": MODEL,
                        "Attempt": attempt_num,
                        "Max_Retries": max_retries,
                        "Status": "FAILED",
                        "Active_Tiers": active_tiers_str,
                        "Failed_Tier": "Tier 2 (World Grounding)",
                        "Error_Line_No": "-",
                        "Error_Code_Line": "-",
                        "Error_Message": _sanitize_error_text(feedback_str),
                    },
                )

                messages.append({"role": "assistant", "content": generated_code})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"The syntax is valid, but your code references devices that do not exist or mismatch "
                            f"the 2D world model:\n{feedback_str}\n"
                            f"Please regenerate the code using ONLY real devices:\n{valid_devices_hint}"
                        ),
                    }
                )
                continue
            print("✅ Tier 2 Passed: All entities match the physical world model.")
        else:
            print("⏩ Tier 2 Skipped (Disabled in config).")

        # ----------------------------------------------------
        # TIER 3: Domain & Physical Feasibility (LLM Judge)
        # ----------------------------------------------------
        if ENABLE_TIER_3:
            print("Validating with Semantic Judge (Tier 3)...")
            is_correct, judge_feedback = evaluate_semantics(nl_prompt, generated_code)

            if not is_correct:
                print(f"❌ Tier 3 Failed: Domain / Physics Discrepancy:\n{judge_feedback}")

                _log_attempt_to_csv(
                    csv_path,
                    {
                        "Run_ID": run_id,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "Model": MODEL,
                        "Attempt": attempt_num,
                        "Max_Retries": max_retries,
                        "Status": "FAILED",
                        "Active_Tiers": active_tiers_str,
                        "Failed_Tier": "Tier 3 (Semantic Judge)",
                        "Error_Line_No": "-",
                        "Error_Code_Line": "-",
                        "Error_Message": _sanitize_error_text(judge_feedback),
                    },
                )

                messages.append({"role": "assistant", "content": generated_code})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"The syntax and world entities are correct, but your code has a physical/domain logical error:\n"
                            f"{judge_feedback}\nPlease output the corrected code."
                        ),
                    }
                )
                continue
            print("✅ Tier 3 Passed: Domain semantics confirmed.")
        else:
            print("⏩ Tier 3 Skipped (Disabled in config).")

        # All enabled stages passed
        print(f"\n🎉 Verification Success on Attempt {attempt_num}! Passed: [{active_tiers_str}].")
        pipeline_success = True

        _log_attempt_to_csv(
            csv_path,
            {
                "Run_ID": run_id,
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Model": MODEL,
                "Attempt": attempt_num,
                "Max_Retries": max_retries,
                "Status": "SUCCESS",
                "Active_Tiers": active_tiers_str,
                "Failed_Tier": "-",
                "Error_Line_No": "-",
                "Error_Code_Line": "-",
                "Error_Message": "-",
            },
        )
        return generated_code

    if not pipeline_success and os.path.exists(file_name):
        failed_backup = os.path.join(script_dir, "failed_output.goal")
        os.replace(file_name, failed_backup)
        print(f"\n🚨 Loop terminated. Invalid file saved to: {failed_backup}")

    return None


if __name__ == "__main__":
    script_dir = Path(__file__).parent
    yaml_world_file = script_dir / "mission_6a97199291cd57a8ea506e26.yaml"

    instruction = "Task: Create a complete Goal DSL file that follows all formal constraints. "
    nl_content = load_nl_input("prompt.txt")

    if nl_content:
        full_prompt = instruction + "Here is the explanation in natural language:\n" + nl_content
        generate_and_verify_dsl(full_prompt, yaml_world_file, max_retries=MAX_RETRIES)
    else:
        print("No natural language input provided. Exiting.")