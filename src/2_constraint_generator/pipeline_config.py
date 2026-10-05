# ==============================================================================
# Pipeline Verification Tiers (Set True to enable, False to bypass)
# ==============================================================================
ENABLE_TIER_1 = True   # Grammar & Syntax validation via goaldsl CLI
ENABLE_TIER_2 = False  # World Grounding validation via EnvPop validator
ENABLE_TIER_3 = True  # Domain Semantics validation via Semantic Judge LLM

# ==============================================================================
# Model & Execution Settings
# ==============================================================================
MODEL = "liquid/lfm-2.5-2.6b:free"
MAX_RETRIES = 3