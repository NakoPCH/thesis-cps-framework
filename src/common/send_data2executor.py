#!/usr/bin/env python

import argparse
import json
import os
import sys
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

import goal_dsl
from commlib.node import Node
from dotenv import load_dotenv


def get_connection_parameters(
    broker_type: str = "mqtt",
    host: Optional[str] = None,
    port: Optional[int] = None,
):
    """Builds transport connection parameters matching send_model.py."""
    load_dotenv()
    if broker_type == "redis":
        from commlib.transports.redis import ConnectionParameters
        return ConnectionParameters(
            host=host or os.getenv("REDIS_HOST", "localhost"),
            port=port or int(os.getenv("REDIS_PORT", "6379")),
            username=os.getenv("REDIS_USERNAME", ""),
            password=os.getenv("REDIS_PASSWORD", ""),
        )
    elif broker_type == "amqp":
        from commlib.transports.amqp import ConnectionParameters
        return ConnectionParameters()
    elif broker_type == "mqtt":
        from commlib.transports.mqtt import ConnectionParameters
        if host or port:
            return ConnectionParameters(
                host=host or "localhost",
                port=port or 1883,
            )
        return ConnectionParameters()
    else:
        raise ValueError(f"Unsupported broker type: {broker_type}")


class TelemetryInjectorApp:
    def __init__(
        self,
        root: tk.Tk,
        model_file: Optional[str] = None,
        model_content: Optional[str] = None,
        broker_type: str = "mqtt",
        host: Optional[str] = None,
        port: Optional[int] = None,
    ):
        self.root = root
        self.broker_type = broker_type
        self.entries_map: Dict[str, Dict[str, tuple[tk.Entry, str]]] = {}
        self.publishers: Dict[str, Any] = {}

        # 1. Parse .goal model via goal_dsl
        print("[*] Parsing Goal-DSL model...")
        mm = goal_dsl.get_metamodel()
        if model_file:
            self.model = mm.model_from_file(model_file)
            self.root.title(f"Goal Telemetry Injector - {os.path.basename(model_file)}")
        elif model_content:
            self.model = mm.model_from_str(model_content)
            self.root.title("Goal Telemetry Injector - In-Memory Model")
        else:
            raise ValueError("Either model_file or model_content must be provided.")

        print(f"[*] Found {len(self.model.entities)} entities in model.")

        # 2. Build UI first
        self._build_ui()
        self._force_focus()

        # 3. Connect commlib Node
        print(f"[*] Initializing {self.broker_type.upper()} Node...")
        conn_params = get_connection_parameters(broker_type, host, port)
        self.node = Node(
            node_name="goal_telemetry_injector",
            connection_params=conn_params,
            heartbeats=False,
        )
        for entity in self.model.entities:
            topic = entity.uri
            if topic not in self.publishers:
                self.publishers[topic] = self.node.create_publisher(topic=topic)

        if self.publishers:
            self.node.run()
            print("[*] Node connected and ready.")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _force_focus(self):
        """Ensures the window appears in front of other windows on Windows."""
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after_idle(self.root.attributes, "-topmost", False)
        self.root.focus_force()

    def _build_ui(self):
        self.root.geometry("540x480")
        self.root.minsize(460, 360)

        main_frame = ttk.Frame(self.root, padding="12")
        main_frame.pack(fill=tk.BOTH, expand=True)

        header_label = ttk.Label(
            main_frame,
            text=f"Broker: {self.broker_type.upper()} | Entities: {len(self.model.entities)}",
            font=("Segoe UI", 9, "bold"),
        )
        header_label.pack(anchor="w", pady=(0, 8))

        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        for entity in self.model.entities:
            tab = ttk.Frame(notebook, padding="10")
            notebook.add(tab, text=entity.name)

            # Topic Info
            topic_frame = ttk.Frame(tab)
            topic_frame.pack(fill=tk.X, pady=(0, 10))

            ttk.Label(topic_frame, text="URI / Topic:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
            topic_val = ttk.Label(topic_frame, text=entity.uri, foreground="blue")
            topic_val.pack(side=tk.LEFT, padx=(6, 0))

            # Attributes form
            form_frame = ttk.LabelFrame(tab, text=" Entity Attributes ", padding="10")
            form_frame.pack(fill=tk.BOTH, expand=True)

            self.entries_map[entity.name] = {}

            for attr in entity.attributes:
                row_frame = ttk.Frame(form_frame)
                row_frame.pack(fill=tk.X, pady=4)

                # Extract data type correctly using dtype
                attr_type = str(getattr(attr.dtype, "name", attr.dtype)).lower()

                lbl_text = f"{attr.name} ({attr_type}):"
                ttk.Label(row_frame, text=lbl_text, width=22, anchor="w").pack(side=tk.LEFT)

                entry = ttk.Entry(row_frame)
                entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

                default_val = self._get_default_value(attr.name, attr_type)
                entry.insert(0, default_val)

                self.entries_map[entity.name][attr.name] = (entry, attr_type)

            # Publish Button per entity
            btn_frame = ttk.Frame(tab)
            btn_frame.pack(fill=tk.X, pady=(10, 0))

            pub_btn = ttk.Button(
                btn_frame,
                text=f"Publish to {entity.name}",
                command=lambda e=entity: self._publish_telemetry(e),
            )
            pub_btn.pack(side=tk.RIGHT)

        # Status footer
        self.status_var = tk.StringVar(value="Ready to send telemetry.")
        status_bar = ttk.Label(
            main_frame,
            textvariable=self.status_var,
            relief=tk.SUNKEN,
            anchor="w",
            padding=(5, 3),
            font=("Segoe UI", 8),
        )
        status_bar.pack(fill=tk.X, pady=(8, 0))

    def _get_default_value(self, name: str, attr_type: str) -> str:
        if "float" in attr_type:
            return "0.0"
        if "int" in attr_type:
            return "0"
        if "bool" in attr_type:
            return "False"
        if "dict" in attr_type:
            return "{}"
        if "list" in attr_type:
            return "[]"
        return ""

    def _publish_telemetry(self, entity):
        payload: Dict[str, Any] = {}
        attr_dict = self.entries_map[entity.name]

        for attr_name, (entry, attr_type) in attr_dict.items():
            raw_val = entry.get().strip()
            try:
                if "float" in attr_type:
                    payload[attr_name] = float(raw_val)
                elif "int" in attr_type:
                    payload[attr_name] = int(raw_val)
                elif "bool" in attr_type:
                    payload[attr_name] = raw_val.lower() in ("true", "1", "yes")
                elif "dict" in attr_type or "list" in attr_type:
                    payload[attr_name] = json.loads(raw_val)
                else:
                    payload[attr_name] = raw_val
            except Exception as ex:
                messagebox.showerror(
                    "Parsing Error",
                    f"Attribute '{attr_name}' expects type '{attr_type}'.\nInvalid value: '{raw_val}'\nError: {ex}",
                )
                return

        topic = entity.uri
        self.publishers[topic].publish(payload)

        timestamp = datetime.now().strftime("%H:%M:%S")
        status_msg = f"[{timestamp}] Sent to {topic}: {json.dumps(payload)}"
        self.status_var.set(status_msg)
        print(f"[*] {status_msg}")

    def _on_close(self):
        try:
            self.node.stop()
        except Exception:
            pass
        self.root.destroy()


def launch_telemetry_ui(
    model_file: Optional[str] = None,
    model_content: Optional[str] = None,
    broker_type: str = "mqtt",
    host: Optional[str] = None,
    port: Optional[int] = None,
):
    """Programmatic entry point for thesis orchestrators."""
    root = tk.Tk()
    TelemetryInjectorApp(
        root=root,
        model_file=model_file,
        model_content=model_content,
        broker_type=broker_type,
        host=host,
        port=port,
    )
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send telemetry to Goal-DSL entities via native GUI")
    parser.add_argument("model_file", help="Path to the .goal model file")
    parser.add_argument("broker", nargs="?", default="mqtt", help="Broker type: mqtt (default), redis, amqp")
    args = parser.parse_args()

    if not os.path.isfile(args.model_file):
        print(f"Error: File '{args.model_file}' not found.")
        sys.exit(1)

    launch_telemetry_ui(model_file=args.model_file, broker_type=args.broker)