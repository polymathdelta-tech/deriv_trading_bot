import os
from dotenv import load_dotenv

# Load the environment variables from the .env file
load_dotenv()

# Fetch variables
APP_ID = os.getenv("DERIV_APP_ID")
API_TOKEN = os.getenv("DERIV_API_TOKEN")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Fail-fast validation
if not all([APP_ID, API_TOKEN, TG_TOKEN, TG_CHAT_ID]):
    raise ValueError(
        "Missing critical environment variables! "
        "Check your .env file to ensure APP_ID, API_TOKEN, TG_TOKEN, and TG_CHAT_ID are set."
    )