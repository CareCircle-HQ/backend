import asyncio
import os

from dotenv import load_dotenv
from highlevel import HighLevel

load_dotenv()

CLIENT_ID = os.getenv("GHL_CLIENT_ID")
CLIENT_SECRET = os.getenv("GHL_CLIENT_SECRET")
LOCATION_ID = os.getenv("GHL_LOCATION_ID")
PRIVATE_TOKEN = os.getenv("GHL_PRIVATE_TOKEN")


def make_client():
    # Preferred for automated/batch jobs: Private Integration Token (no OAuth flow).
    if PRIVATE_TOKEN:
        return HighLevel(private_integration_token=PRIVATE_TOKEN)
    # Fallback: OAuth app credentials (requires a stored access token from the OAuth flow).
    return HighLevel(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )


async def test_connection():
    """Smoke test: confirm we can authenticate and reach the contacts API."""
    if not PRIVATE_TOKEN and not (CLIENT_ID and CLIENT_SECRET):
        print("[!] Missing auth. Set GHL_PRIVATE_TOKEN (recommended) in .env,")
        print("    or complete the OAuth flow with GHL_CLIENT_ID / GHL_CLIENT_SECRET.")
        return

    if not PRIVATE_TOKEN:
        print("[!] No GHL_PRIVATE_TOKEN found. OAuth app credentials alone cannot")
        print("    authenticate API calls without a stored access token.")
        print("    Generate a Private Integration Token in GHL:")
        print("    Settings > Private Integrations > scopes: contacts.readonly, contacts.write")
        return

    print(f"[*] Connecting to HighLevel location {LOCATION_ID} ...")
    client = make_client()
    try:
        response = await client.contacts.search_contacts_advanced({
            "locationId": LOCATION_ID,
            "pageLimit": 5,
        })
        print("[+] Connection OK. Sample response:")
        print(response)
    except Exception as e:
        print(f"[!] API call failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test_connection())