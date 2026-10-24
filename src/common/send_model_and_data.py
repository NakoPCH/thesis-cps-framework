#!/usr/bin/env python

import argparse
import os
import sys

# Import functions from the separate scripts
from send_data2model import launch_telemetry_ui
from send_model import deploy_goal_file


def run(model_file: str, broker_type: str = "mqtt", **kwargs):
    """
    Deploys the .goal model to the running executor, then opens the UI.
    Reusable directly within orchestrator workflows.
    """
    if not os.path.isfile(model_file):
        print(f"[!] Error: Model file '{model_file}' not found.")
        sys.exit(1)

    # Step 1: Deploy model via RPC using send_model.py
    print(f"[*] Step 1: Deploying '{model_file}' to executor via {broker_type.upper()}...")
    try:
        response = deploy_goal_file(file_path=model_file, broker_type=broker_type, **kwargs)
        print(f"[*] Deploy response: status={response.status}, result='{response.result}'")
        if response.status != 1:
            print(f"[!] Warning: Executor returned failure status ({response.status}). Aborting UI launch.")
            return
    except Exception as ex:
        print(f"[!] Deployment failed: {ex}")
        print("    Make sure goaldsl-executor is actively running in your other terminal.")
        return

    # Step 2: Open the telemetry injector window using send_data2model.py
    print("[*] Step 2: Launching Telemetry Injector UI...")
    launch_telemetry_ui(model_file=model_file, broker_type=broker_type, **kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deploy a .goal model to goaldsl-executor and open the telemetry UI."
    )
    parser.add_argument("model_file", help="Path to the .goal model file")
    parser.add_argument(
        "broker",
        nargs="?",
        default="mqtt",
        help="Broker type: mqtt (default), redis, amqp",
    )
    args = parser.parse_args()

    run(model_file=args.model_file, broker_type=args.broker)