#!/usr/bin/env python

import argparse
import os

from commlib.msg import RPCMessage
from commlib.node import Node
from dotenv import load_dotenv


class ExecuteModelMsg(RPCMessage):
    class Request(RPCMessage.Request):
        model: str

    class Response(RPCMessage.Response):
        status: int = 1
        result: str = ""


def deploy_goal_model(
    model_content: str,
    broker_type: str = "mqtt",
    uid: str | None = None,
    node_name: str = "orchestrator_deployer",
    host: str | None = None,
    port: int | None = None,
) -> ExecuteModelMsg.Response:
    """
    Deploys a raw Goal-DSL model string to goaldsl-executor via RPC.

    Can be imported and called directly by any thesis orchestrator module.
    """
    load_dotenv()

    # 1. Select Transport and Connection Parameters
    if broker_type == "redis":
        from commlib.transports.redis import ConnectionParameters

        conn_params = ConnectionParameters(
            host=host or os.getenv("REDIS_HOST", "localhost"),
            port=port or int(os.getenv("REDIS_PORT", "6379")),
            username=os.getenv("REDIS_USERNAME", ""),
            password=os.getenv("REDIS_PASSWORD", ""),
        )
    elif broker_type == "amqp":
        from commlib.transports.amqp import ConnectionParameters

        conn_params = ConnectionParameters()
    elif broker_type == "mqtt":
        from commlib.transports.mqtt import ConnectionParameters

        conn_params = ConnectionParameters(
            host=host or os.getenv("MQTT_HOST", "localhost"),
            port=port or int(os.getenv("MQTT_PORT", "1883")),
            username=os.getenv("MQTT_USERNAME", ""),
            password=os.getenv("MQTT_PASSWORD", ""),
        )
    else:
        raise ValueError(f"Unsupported broker type: {broker_type}")

    # 2. Setup RPC Client Node
    node = Node(
        node_name=node_name,
        connection_params=conn_params,
        heartbeats=False,
    )

    target_uid = uid or os.getenv("UID", "TEST")
    rpc_name = os.getenv("EXECUTE_MODEL_RPC", "goaldsl.{ID}.deploy_sync").replace(
        "{ID}", target_uid
    )

    rpc = node.create_rpc_client(msg_type=ExecuteModelMsg, rpc_name=rpc_name)
    node.run()

    # 3. Dispatch RPC Request
    try:
        msg = ExecuteModelMsg.Request(model=model_content)
        resp = rpc.call(msg)
        return resp
    finally:
        node.stop()


def deploy_goal_file(
    file_path: str, broker_type: str = "mqtt", **kwargs
) -> ExecuteModelMsg.Response:
    """Helper to read a .goal file from disk and deploy it."""
    with open(file_path, "r", encoding="utf-8") as f:
        model_str = f.read()
    return deploy_goal_model(model_str, broker_type=broker_type, **kwargs)


if __name__ == "__main__":
    # Retains full CLI backward compatibility
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("model_file", help="Path to the model file to send")
    parser.add_argument("broker", nargs="?", default="mqtt")
    args = parser.parse_args()

    response = deploy_goal_file(args.model_file, broker_type=args.broker)
    print(response)
