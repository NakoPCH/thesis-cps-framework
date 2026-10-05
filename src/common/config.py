import os
from pathlib import Path
from dotenv import load_dotenv

# Base Directories
ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
DATA_DIR = ROOT_DIR / "data"
EXPERIMENTS_DIR = ROOT_DIR / "experiments"

# Data Subdirectories
REFERENCE_DIR = DATA_DIR / "references"
WORLD_MODELS_DIR = DATA_DIR / "world_models"
PROMPTS_DIR = DATA_DIR / "prompts"

# Ensure critical directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
WORLD_MODELS_DIR.mkdir(parents=True, exist_ok=True)

# Environment & API Setup
load_dotenv(ROOT_DIR / ".env")
TOKEN = os.getenv("OPENROUTER_API_TOKEN")
BASE_URL = "https://openrouter.ai/api/v1"

# Model Selection
MODEL = "liquid/lfm-2.5-2.6b:free"
JUDGE_MODEL = MODEL

# Verification Loop Configuration
ENABLE_TIER_1 = True
ENABLE_TIER_2 = False
ENABLE_TIER_3 = True
MAX_RETRIES = 3