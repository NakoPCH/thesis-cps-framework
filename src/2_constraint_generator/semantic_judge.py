import sys
from pathlib import Path

import requests

# Ensure src is in Python path for clean cross-module imports
sys.path.append(str(Path(__file__).resolve().parents[1]))

from common.config import BASE_URL, JUDGE_MODEL, TOKEN


def evaluate_semantics(original_prompt, generated_code):
    """Compares the generated Goal DSL file against the original user prompt

    to ensure no logic constraints were missed, changed, or hallucinated.
    """
    print("🧠 Consulting LLM Semantic Judge...")

    system_instruction = (
        "You are an independent quality assurance auditor for a cyber-physical system framework.\n"
        "Your task is to verify if the generated Goal DSL code matches the user's original intentions "
        "and constraints semantically. Check thresholds, entity URIs, broker specifications, and strategies.\n\n"
        "CRITICAL CONSTRAINTS:\n"
        "- Do NOT call or invoke any tools or functions.\n"
        "- Do NOT write, propose, or generate corrected DSL code or syntax snippets in any format (no code blocks, no pseudo-code, no YAML). Your role is strictly evaluative: explain what requirement was missed, violated, or logically inconsistent in plain conceptual language so the generator can correct itself using its own grammar references.\n"
        "- NEVER generate tool-calling tokens or syntax like <|tool_call_start|>, [verify(...)], [analyze_code(...)], or <|tool_call_end|>.\n"
        "- Output your entire evaluation strictly in plain text using the verdict layout below.\n\n"
        "You must output your final decision using this exact layout prefix:\n"
        "VERDICT: PASSED (Use this if all constraints and variables match perfectly)\n"
        "or\n"
        "VERDICT: FAILED\n"
        "REASON: <Provide a concise, clear explanation of what logic element or value is incorrect or missing>"
    )

    user_payload = (
        f"--- ORIGINAL USER PROMPT ---\n{original_prompt}\n\n"
        f"--- GENERATED GOAL DSL CODE ---\n{generated_code}\n\n"
        "Analyze the code against the prompt. Does the code accurately implement the requested logic?"
    )

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "HTTP-Referer": "https://github.com/NakoPCH/my-thesis-goal-dsl",
        "X-Title": "Thesis Semantic Judge Stage",
    }

    payload = {
        "model": JUDGE_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0.1,  #  determinism for auditing tasks
    }

    try:
        response = requests.post(
            f"{BASE_URL}/chat/completions", headers=headers, json=payload
        )
        if response.status_code == 200:
            choice = response.json().get("choices", [{}])[0]
            message = choice.get("message", {})
            result_text = (message.get("content") or "").strip()

            # Parse the strict verdict layout
            if "VERDICT: PASSED" in result_text:
                return True, "Passed semantic check."
            elif "VERDICT: FAILED" in result_text:
                # Extract the reason block to feed back into the repair loop
                reason = (
                    result_text.split("VERDICT: FAILED")[-1]
                    .replace("REASON:", "")
                    .strip()
                )
                return False, reason if reason else "Logic mismatch detected."
            else:
                return (
                    False,
                    f"Judge formatting error (expected VERDICT prefix): {result_text}",
                )
        else:
            return (
                False,
                f"Judge API communication error: Status {response.status_code}",
            )
    except Exception as e:
        return False, f"Judge exception occurred: {str(e)}"