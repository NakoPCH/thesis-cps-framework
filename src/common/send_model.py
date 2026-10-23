#!/usr/bin/env python

import os
import sys
import time
from pathlib import Path

from commlib.msg import RPCMessage
from commlib.node import Node
from dotenv import load_dotenv


class ExecuteModelMsg(RPCMessage):
    class Request(RPCMessage.Request):
        model: str

    class Response(RPCMessage.Response):
        status: int = 1
        result: str = ""


if __name__ == "__main__":
    # Load the repository .env while allowing shell variables to override it.
    load_dotenv()

    if len(sys.argv) < 2:
        broker = "redis"
    else:
        broker = str(sys.argv[1])
    if broker == "redis":
        from commlib.transports.redis import ConnectionParameters
    elif broker == "amqp":
        from commlib.transports.amqp import ConnectionParameters
    elif broker == "mqtt":
        from commlib.transports.mqtt import ConnectionParameters
    else:
        print("Not a valid broker-type was given!")
        sys.exit(1)

    model_file = Path(__file__).resolve().parents[2] / "data" / "test_broker.goal"

    with open(model_file, "r") as f:
        model = f.read()

    if broker == "redis":
        conn_params = ConnectionParameters(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            username=os.getenv("REDIS_USERNAME", ""),
            password=os.getenv("REDIS_PASSWORD", ""),
        )
    else:
        conn_params = ConnectionParameters()

    node = Node(
        node_name="test_hello",
        connection_params=conn_params,
        heartbeats=False,
    )
    uid = os.getenv("UID", "TEST")
    rpc_name = os.getenv("EXECUTE_MODEL_RPC", "goaldsl.{ID}.deploy_sync").replace(
        "{ID}", uid
    )
    print(f"Calling RPC: {rpc_name}")
    rpc = node.create_rpc_client(msg_type=ExecuteModelMsg, rpc_name=rpc_name)
    node.run()

    msg = ExecuteModelMsg.Request(model=model)
    resp = rpc.call(msg)
    print(resp)