import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ARIADNE_API_TOKEN")
BASE_URL = os.getenv("ARIADNE_BASE_URL")

if not TOKEN:
    print("Error: Δεν βρέθηκε το ARIADNE_API_TOKEN στο .env αρχείο!")
else:
    print("Το Token φορτώθηκε επιτυχώς!")


# Get available models
response = requests.get(
    f"{BASE_URL}/v1/models",
    headers={"Authorization": f"Bearer {TOKEN}"}
)
models = response.json()["data"]
# Use the first available model
if models:
    first_model = models[0]
    provider = first_model["provider"]
    model_id = first_model["id"]
    # Now use it in a chat request
    chat_response = requests.post(
        f"{BASE_URL}/v1/chat",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "provider": provider,
            "model": model_id,
            "message": "hi"
        }
    )

    data = chat_response.json()
    print(f"\n💬 {data['content'][0]['text']}")
    print(f"\n💰 Cost: ${data['billing']['creditUsed']:.6f} | Remaining: ${data['billing']['creditRemaining']:.2f}")