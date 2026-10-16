# Cyber-Physical Systems Verification Framework

An end-to-end research framework for translating natural language requirements into formally verified operational constraints, synthesizing test scenarios, and conducting runtime monitoring for Cyber-Physical Systems (CPS) and smart environments.

---

## 1. Overview & Research Scope

In safety-critical Cyber-Physical Systems, ensuring that runtime behaviors conform to formal operational constraints is vital. However, manually engineering formal specifications using Domain-Specific Languages (DSLs) requires deep domain expertise and is prone to human error.

This framework implements an automated, multi-stage LLM pipeline that:

1. **Refines raw, ambiguous natural language requirements** into structured operational intents via an interactive dialogue agent (Stage 1).
2. **Synthesizes declarative Goal-DSL models** using Large Language Models coupled with a deterministic, **multi-tier self-repair verification loop** (syntax checking, 2D physical world topology validation, and domain semantic checking) (Stage 2).
3. **Orchestrates end-to-end pipeline execution** with automated data handoffs, structured JSON telemetry logging, and visual web dashboard inspection.
4. **Synthesizes operational scenarios and fault injection cases** to stress-test system dynamics.
5. **Interprets faults and generates automated mitigations/responses** when constraints are violated.
6. **Performs real-time execution monitoring** against formalized constraints over message brokers (e.g., Redis, MQTT).

---

## 2. End-to-End Architecture

![Architecture diagram](data/images/architecture_diagram.png)

### Verification Loop Breakdown (Stage 2)

* **Tier 1 (Grammar & Syntax):** Deterministically compiles the synthesized model against the formal `goaldsl` metamodel using the textX compiler (`goaldsl validate`). Any parsing or grammar errors return exact line and token diagnostics.
* **Tier 2 (Physical Grounding):** Cross-references declared entities, sensors, actuators, coordinate POIs, and properties against an `EnvPop` 2D simulation world model (`world_model.yaml`). Hallucinated or misnamed hardware components are rejected deterministically without invoking additional LLMs.
* **Tier 3 (Semantic Feasibility):** Evaluates whether the generated logic physically and logically satisfies the original prompt requirements (e.g., sensible threshold directions, correct spatial coordinates, domain-valid timing) via an LLM Semantic Judge.

---

## 3. Repository Structure

```text
thesis-cps-framework/
├── data/                          # Data assets and generated files
│   ├── prompts/                   # Natural-language input prompts and refined outputs
│   ├── references/                # Canonical Goal-DSL reference files
│   ├── scenarios/                 # Generated scenario traces and SmAuto files
│   └── world_models/              # EnvPop 2D world models and floorplans
├── experiments/                   # Evaluation results and benchmarking
│   ├── artifacts/                 # Serialized experiment runs and logs
│   ├── sessions/                  # End-to-end JSON session telemetry logs
│   └── eval_results.csv           # Verification/evaluation attempt logs
├── external/                      # External dependencies
│   └── goal-dsl/                  # Goal-DSL metamodel, grammar and compiler
├── src/                           # Source code
│   ├── 1_requirement_refinement/  # Stage 1: Interactive NL requirement refinement
│   ├── 2_constraint_generator/    # Stage 2: Goal-DSL generation and verification loop
│   ├── 3_scenario_generator/      # Stage 3: Scenario generation and fault injection
│   ├── 4_response_generation/     # Stage 4: Fault interpretation and response generation
│   ├── 5_runtime_monitoring/      # Stage 5: Runtime monitoring
│   ├── common/                    # Shared infrastructure and configuration
│   ├── run_pipeline.py            # End-to-end orchestrator script (Stage 1 + Stage 2)
│   └── view_logs.py               # Streamlit web dashboard for visual session inspection
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 4. Technology Stack & Key Dependencies

| Technology | Role |
| --- | --- |
| **Python 3.11+ / 3.14** | Core execution runtime. |
| **Goal-DSL (`goaldsl`)** | External Domain-Specific Language developed in textX for runtime behavior verification of CPS and IoT systems. Supports sensors, actuators, hybrid entities, condition goals, area goals, and composite goals. |
| **EnvPop** | 2D world modeling and simulation specification language defining spatial geometries, rooms, POIs, sensors, effectors, and broker transports. |
| **Streamlit** | Web-based visual dashboard framework for inspecting experiment session logs, dialogue transcripts, and compiler diagnostics. |
| **Pandas** | Data processing library for evaluating research metrics and benchmark CSV datasets. |
| **OpenRouter API** | Unified gateway for LLM inference (generation and semantic evaluation). |
| **python-dotenv** | Zero-leak environment variable loading for API secrets. |

---

## 5. Installation & Setup

### Prerequisites

* Python 3.11 or newer (Python 3.14 supported).
* Git.

### 1. Clone the Repository and Submodules

```bash
git clone https://github.com/NakoPCH/thesis-cps-framework.git
cd thesis-cps-framework
```

If `external/goal-dsl` is configured as a Git submodule:

```bash
git submodule update --init --recursive
```

### 2. Set Up Virtual Environment

```bash
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Ensure `goaldsl` is installed in editable mode from the external submodule:

```bash
pip install -e external/goal-dsl
```

Verify the installation by running:

```bash
goaldsl --help
```

### 4. Configure Environment Variables

Create a `.env` file in the repository root:

```env
OPENROUTER_API_KEY=your_openrouter_api_key_here
```

---

## 6. Execution & Verification

### A. Running the End-to-End Pipeline (Stage 1 + Stage 2)

The main orchestrator (`run_pipeline.py`) runs the interactive Stage 1 dialogue session, automatically passes the synthesized narrative prompt to Stage 2 upon operator approval, executes the verification loop, and writes full structured telemetry JSON logs.

```bash
# Run with default world model (data/world_models/world_model.yaml)
python src/run_pipeline.py

# Run with a specific world model
python src/run_pipeline.py --world data/world_models/smart_home.yaml

# Run in standalone mode without world grounding
python src/run_pipeline.py --no-world
```

---

### B. Visualizing Session Logs & Experiments

Launch the interactive Streamlit web dashboard to inspect full session transcripts, dialogue turns, generated Goal-DSL code, attempt retries, and compilation diagnostics in your browser:

```bash
streamlit run src/view_logs.py
```

---

### C. Running Stage 2 Independently (Standalone Verification Loop)

You can also run the constraint verification loop as a standalone module using a static text prompt:

```bash
python src/2_constraint_generator/verification_loop.py
```

---

### D. Experiment Output & Benchmark Logs

* **Session JSON Telemetry Logs:** Every pipeline execution creates a complete structured record stored in `experiments/sessions/session_<timestamp>_<uuid>.json` containing dialogue turns, refined prompts, and Stage 2 attempt histories.
* **Generated Goal DSL:** Successful models are written to `data/llm_created_file.goal`. Models failing all retry attempts are preserved in `data/failed_output.goal`.
* **Execution Audit CSV:** Every verification attempt across all runs is appended to `experiments/eval_results.csv` with the schema:  
  `Run_ID`, `Timestamp`, `Model`, `Attempt`, `Max_Retries`, `Status`, `Active_Tiers`, `Failed_Tier`, `Error_Line_No`, `Error_Code_Line`, `Error_Message`.

---

## 7. Configuration (`src/common/config.py`)

All global directories, model selections, and operational flags are centralized in `src/common/config.py`:

```python
# Global Model Configurations
MODEL = "liquid/lfm-2.5-2.6b:free"   # Generator model
JUDGE_MODEL = MODEL                  # Tier 3 Evaluator model

# Stage 2 Tier Execution Toggles
ENABLE_TIER_1 = True   # textX syntax compiler check
ENABLE_TIER_2 = True   # EnvPop deterministic physical grounding check
ENABLE_TIER_3 = True   # LLM semantic feasibility check
MAX_RETRIES = 3        # Maximum self-repair feedback iterations
```