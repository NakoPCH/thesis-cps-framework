import requests
import subprocess
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ARIADNE_API_TOKEN")
BASE_URL = os.getenv("ARIADNE_BASE_URL")
PROVIDER = "gcp"
MODEL = "gemini-2.5-flash"

if not TOKEN:
    print("Error: ARIADNE_API_TOKEN not found in .env file!")
else:
    print("Token loaded successfully!")


def load_example_code(filename="reference_example.goal"):
    """Reads the reference Goal DSL code from an external file."""
    base_path = os.path.dirname(__file__)
    file_path = os.path.join(base_path, filename)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"Warning: {filename} not found. Proceeding without example.")
        return ""


def load_nl_input(filename="prompt.txt"):
    """Reads the user's natural language request from a text file in the same folder as the script."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))

        full_path = os.path.join(script_dir, filename)

        with open(full_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"Error: {filename} not found in the script's directory ({script_dir}).")
        return None


def generate_and_verify_dsl(nl_prompt, max_retries=3):

    example_code = load_example_code()
    # Initialize the conversation history for the /messages endpoint
    messages = [
        {
            "role": "system",
            "content": f"You are a Goal DSL expert. Generate ONLY valid Goal DSL code based on the user's prompt. Do not include markdown formatting or explanations. Below are examples of valid Goal DSL files: {example_code}",
        },
        {"role": "user", "content": nl_prompt},
    ]

    file_name = "llm_created_file.goal"

    for attempt in range(max_retries):
        print(f"\n--- Attempt {attempt + 1} of {max_retries} ---")

        # 1. Call Ariadne API
        response = requests.post(
            f"{BASE_URL}/v1/messages",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"provider": PROVIDER, "model": MODEL, "messages": messages},
        )

        # Extract the generated code
        data = response.json()
        generated_code = data["content"][0]["text"]
        print(
            f"\n💰 Cost: ${data['billing']['creditUsed']:.6f} | Remaining: ${data['billing']['creditRemaining']:.2f}"
        )

        # 2. Save code to a file
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(generated_code)

        # 3. Formal Validation using Goal DSL CLI
        print("Validating with Goal DSL Parser...")
        # Run the validation command: goaldsl validate <filename>
        result = subprocess.run(
            ["goaldsl", "validate", file_name], capture_output=True, text=True
        )

        # 4. Check the result
        if result.returncode == 0 and "success" in result.stdout:
            print("\n✅ Success! The generated code is formally valid.")
            print(generated_code)
            return generated_code
        else:
            # If it fails, capture the error output (stderr or stdout depending on the CLI)
            error_msg = result.stderr if result.stderr else result.stdout
            print(f"❌ Validation Failed. Error:\n{error_msg}")

            # Append the LLM's failed code and the parser's error to the history
            messages.append({"role": "assistant", "content": generated_code})
            messages.append(
                {
                    "role": "user",
                    "content": f"The validation failed with this error: {error_msg}. Please fix the code and return only the corrected Goal DSL code.",
                }
            )

    print("\n🚨 Maximum retries reached. The model could not generate valid code.")
    print("Last generated code:")
    print(generated_code)
    return None


# Example Usage
if __name__ == "__main__":
    # Start with a clear instruction and load the user's natural language input from a file
    instruction = (
        "Task: Create a complete Goal DSL file that follows all formal constraints. "
    )
    nl_content = load_nl_input("prompt.txt")

    if nl_content:
        full_prompt = (
            instruction + "Here is the explanation in natural language:\n" + nl_content
        )
        generate_and_verify_dsl(full_prompt, 3)
    else:
        print("No natural language input provided. Exiting.")
