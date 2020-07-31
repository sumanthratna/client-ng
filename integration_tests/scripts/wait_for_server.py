import requests
import time
import sys
import os

start_time = time.time()

TIMEOUT_SECONDS = 100
RETRY_RATE = 0.8
# Checks if the script should timeout
def is_timeout():
    return (time.time() - start_time) > TIMEOUT_SECONDS

while not is_timeout():
    try:
        res = requests.get("http://localhost:9000/ready", timeout=20)
        if res.status_code == 200:
            print("Server responded with 200. Success!")
            sys.exit(os.EX_OK)
    except Exception as e:
        print("Pinging server failed. Retrying")
    time.sleep(RETRY_RATE)

print("Server failed to respond with a 200.")
sys.exit(os.EX_OK)
