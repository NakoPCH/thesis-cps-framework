import os
import sys

import requests

# Use the /validate JSON endpoint instead of /validate/file to avoid the unpacking bug
GOAL_API_URL = "http://localhost:8082/validate"
API_KEY = os.getenv("GOAL_DSL_API_KEY", "API_KEY")


def validate_goal_file(file_path: str) -> dict:
    """Sends a .goal file content to the GoalDSL REST API for syntax validation."""
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # JSON structure expected by /validate (ValidationModel)
        payload = {"name": os.path.basename(file_path), "model": content}

        response = requests.post(GOAL_API_URL, headers=headers, json=payload)

        if response.status_code == 200:
            res_data = response.json()
            return {"valid": True, "details": res_data}
        else:
            return {
                "valid": False,
                "status_code": response.status_code,
                "error": response.text,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.common.api_goal_validator <path_to_goal_file>")
        sys.exit(1)

    target_file = sys.argv[1]
    result = validate_goal_file(target_file)

    if result["valid"]:
        print(f"✅ SUCCESS: '{target_file}' is valid!")
        print("Details:", result["details"])
    else:
        print(f"❌ VALIDATION FAILED for '{target_file}':")
        print(f"Status Code: {result.get('status_code', 'N/A')}")
        print(f"Error: {result.get('error')}")
