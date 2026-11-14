import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY")
MACHINE_ID = os.environ["MACHINE_ID"]

#formats response from lambda function
def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

#creates function to request information from supabase
def supabase_request(path, method="GET", data=None):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        },
    )
    try:
      with urllib.request.urlopen(req, timeout=5) as response:
          raw = response.read().decode()
          return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        print("Supabase said:", e.code, e.read().decode())
        raise
    #e.code is an HTTP status number, e.read() returns response body as raw bytes [explains error], .decode decodes byte into normal string
    #error checking

def lambda_handler(event, context):
    try:
        #gets params [necessary info] from the event
        params = event.get("queryStringParameters") or {}
        if not params:
            body = event.get('body')
            if isinstance(body, str) and body:
                params = json.loads(body)
            elif isinstance(body, dict):
                params = body
            else:
                params = event

        job_id = params.get('job_id')
        if job_id is None:
            return response(400, {"error": "job_id required"})

        events = supabase_request(f"DispenseEvents?id=eq.{job_id}&select=purchase_id")
        if not events:
            return response(404, {"error": f"unknown job_id {job_id}"})
        purchase_id = events[0]["purchase_id"]

        cup_status = params.get("cup_status")
        pod_status = params.get("pod_status")

        #checks that cup and pod status exists
        if cup_status is None and pod_status is None:
            return response(400, {"error": "cup_status or pod_status required"})

        #checks if cup and pod dispense was successful
        cup_ok = cup_status == "success"
        pod_ok = pod_status == "success"
        both_ok = cup_ok and pod_ok
        now = datetime.now(timezone.utc).isoformat()

        supabase_request(
            f"DispenseEvents?id=eq.{job_id}",
            method="PATCH",
            data={
                "cup_status": cup_status if cup_ok else "failed",
                "pod_status": pod_status if pod_ok else "failed",
                "dispense_status": "Completed" if both_ok else "Failed",
                "completed_at": now,
                "error_type": "partial_dispense" if not both_ok else ""
            },
        )

        supabase_request(
            f"Purchases?id=eq.{purchase_id}",
            method="PATCH",
            data={
                "status": "Delivered" if both_ok else "Failed"
            },
        )

        #makes machine id safe to put in url
        mid = urllib.parse.quote(MACHINE_ID, safe="")

        #goes through inventory and checks that the machine id of the inventory is the same as the machine id we have stored 
        #then selects cups_remaining and pods_remaining columsn instead of the whole row
        rows = supabase_request(
            f"CoffeeInventory?machine_id=eq.{mid}&select=cups_remaining,pods_remaining"
        )
        #if no rows match it means that the machine id doesn't exist and returns error response
        if not rows:
            return response(404, {"error": f"unknown machine_id {MACHINE_ID}"})
        #else take the first row from rows created database
        row = rows[0]

        #edit the first row; make sure cups and pods remaining are above 0 always
        #if cup and pod are dispensed, decrement by 1 else decrement by 0
        supabase_request(
            f"CoffeeInventory?machine_id=eq.{mid}",
            method="PATCH",
            data={
                "cups_remaining": max(0, row["cups_remaining"] - (1 if cup_ok else 0)),
                "pods_remaining": max(0, row["pods_remaining"] - (1 if pod_ok else 0)),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        return response(200, {"recorded": True, "dispense_status": "Completed" if both_ok else "Failed"})
    
    #error handling
    except json.JSONDecodeError:
        return response(400, {"error": "invalid JSON body"})
    except urllib.error.HTTPError as e:
        print(f"Supabase HTTP {e.code}")
        return response(500, {"recorded": False, "dispense_status": "Failed"})
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
        return response(500, {"recorded": False, "dispense_status": "Failed"})