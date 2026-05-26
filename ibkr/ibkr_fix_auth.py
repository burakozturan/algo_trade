import requests
import json

requests.packages.urllib3.disable_warnings()
BASE_URL = "https://localhost:5000/v1/api"

# Step 1: Check auth status
print("--- Auth Status ---")
auth = requests.get(f"{BASE_URL}/iserver/auth/status", verify=False)
print(json.dumps(auth.json(), indent=2))

# Step 2: Try to re-authenticate
print("\n--- Re-authenticate ---")
reauth = requests.post(f"{BASE_URL}/iserver/reauthenticate", verify=False)
print(json.dumps(reauth.json(), indent=2))

# Step 3: Tickle the session
print("\n--- Tickle ---")
tickle = requests.post(f"{BASE_URL}/tickle", verify=False)
print(json.dumps(tickle.json(), indent=2))
