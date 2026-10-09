import argparse
import sys
from pathlib import Path

# Add root src directory to sys.path for shared imports
sys.path.append(str(Path(__file__).resolve().parents[1]))

from requirement_refiner import RequirementRefiner

from common.config import DATA_DIR, WORLD_MODELS_DIR


def run_interactive_session(world_yaml_path: Path | None = None):
    print("=" * 65)
    print("   STAGE 1: REQUIREMENT REFINEMENT & DIALOGUE AGENT")
    print("=" * 65)

    if world_yaml_path and world_yaml_path.exists():
        print(f"🌍 Grounded Mode: Using world model '{world_yaml_path.name}'")
    else:
        print("🌐 Standalone Mode: No world model provided (pure logical mode)")

    print("Type your operational goal in natural language.")
    print("Type 'exit' or 'quit' to terminate the session at any time.\n" + "-" * 65)

    refiner = RequirementRefiner(yaml_world_file=world_yaml_path, max_turns=15)

    while True:
        try:
            user_input = input("\n👤 Operator: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nSession aborted by user.")
            break

        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit"]:
            print("Exiting refinement session.")
            break

        print("\n🤖 Assistant is analyzing...", end="\r")
        result = refiner.process_turn(user_input)

        print(" " * 40, end="\r")  # Clear waiting indicator
        print(f"🤖 Refiner:\n{result['message']}")

        if result["is_complete"]:
            refined_prompt = result["refined_prompt"]

            # Save the refined prompt to data/prompts/
            output_dir = DATA_DIR / "prompts"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / "refined_prompt.txt"

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(refined_prompt)

            # Also update prompt.txt for verification_loop.py
            stage2_prompt_path = (
                Path(__file__).resolve().parents[1]
                / "2_constraint_generator"
                / "prompt.txt"
            )
            try:
                with open(stage2_prompt_path, "w", encoding="utf-8") as f:
                    f.write(refined_prompt)
            except Exception:
                pass

            print("\n" + "=" * 65)
            print(f"🎉 Requirements finalized in {result['turns_taken']} turns!")
            print(f"📁 Refined prompt saved to: {output_path}")
            print(f"🚀 Synced directly to Stage 2: {stage2_prompt_path.name}")
            print("=" * 65)
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Interactive CLI for CPS Goal-DSL Requirement Refinement."
    )
    parser.add_argument(
        "--world",
        type=str,
        default=None,
        help="Path to world_model.yaml. If omitted, uses default from WORLD_MODELS_DIR if available.",
    )
    parser.add_argument(
        "--no-world",
        action="store_true",
        help="Run without any world model even if one exists.",
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

    run_interactive_session(world_path)