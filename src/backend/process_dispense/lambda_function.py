import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone


SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY")
MACHINE_ID = os.environ.get("MACHINE_ID", "coffeevend_01") 
COFFEE_ITEM_ID = os.environ.get("COFFEE_ITEM_ID")


PURCHASES_TABLE = "Purchases"
EVENTS_TABLE = "DispenseEvents"
INVENTORY_TABLE = "CoffeeInventory"


#this connects with the supabase and resturns the rows as a list
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
      }
  )
  try:
      with urllib.request.urlopen(req, timeout=10) as response:
          raw = response.read().decode()
          return json.loads(raw) if raw else []
  except urllib.error.HTTPError as e:
      print("Supabase said:", e.read().decode())
      raise








# adds one row to dispense events
def create_dispense_event(record):
  rows = supabase_request(EVENTS_TABLE, "POST", record)
  return rows[0] if rows else {}




def catch_errors(handler):
  def wrapper(event, context):
      try:
          return handler(event, context)
      except Exception as e:
          print(f"Internal server error: {e}")
          return {"statusCode": 500, "body": json.dumps({"error": "Internal server error", "error_type": "Server Error"})} 
  return wrapper




def reject(error, error_type, purchase=None, machine_id=None):
  return {"statusCode": 409, "body": json.dumps({"status": "REJECTED", "error": error, "error_type": error_type})} 




def get_dispense_job(event):
  params = event.get("queryStringParameters") or {}
  machine_id = params.get("machine_id") or MACHINE_ID
  component = params.get("component")
  if component not in ("cup", "pod"):
    return {"statusCode": 400, "body": json.dumps({"error": "component must be cup or pod"})}
  col = f"{component}_job_status"
  now = datetime.now(timezone.utc).isoformat()

  jobs = supabase_request(f"{EVENTS_TABLE}?machine_id=eq.{machine_id}&{col}=eq.Authorized&order=id.asc&limit=1&select=*")
  if not jobs:
    return {"statusCode": 200, "body": json.dumps({"job_available": False})}
  job = jobs[0]

  claimed = supabase_request(f"{EVENTS_TABLE}?id=eq.{job['id']}&{col}=eq.Authorized", "PATCH",
    {col: "Dispensing", f"{component}_started_at": now})
  if not claimed:
    return {"statusCode": 200, "body": json.dumps({"job_available": False})}

  if component == "cup":
    supabase_request(f"{EVENTS_TABLE}?id=eq.{job['id']}&dispense_status=eq.Authorized", "PATCH",
      {"dispense_status": "Dispensing", "started_at": now})

  return {"statusCode": 200, "body": json.dumps({"job_available": True, "job_id": job["id"], "purchase_id": job["purchase_id"], "machine_id": machine_id, "component": component})}


@catch_errors
def lambda_handler(event, context):
  if event.get("requestContext", {}).get("http", {}).get("method") == "GET":
      return get_dispense_job(event)
  # read the request
  body = json.loads(event["body"]) if event.get("body") else event
  purchase_id = body.get("purchase_id")
  machine_id = body.get("machine_id") or MACHINE_ID




  # make sure we received a real purchase_id and machine_id
  if purchase_id is None or not str(purchase_id).isdigit():
      return reject("purchase_id is missing or not a number", "Invalid Request")
  if not isinstance(machine_id, str) or not re.match(r"^[A-Za-z0-9_-]{1,64}$", machine_id):
      return reject("machine_id is missing or invalid", "Invalid Request")




  # read the matching row from purchases
  purchases = supabase_request(f"{PURCHASES_TABLE}?id=eq.{purchase_id}&select=*")
  if not purchases:
      return reject("Purchase ID not found", "Invalid Purchase")
  purchase = purchases[0]




  # check the item and status
  if COFFEE_ITEM_ID and str(purchase["item_id"]) != str(COFFEE_ITEM_ID):
      return reject("This purchase is not a coffee", "Not Coffee", purchase, machine_id)
  if purchase["status"] != "Ordered":
      return reject("Purchase already used or cancelled", "Invalid Status", purchase, machine_id)




  done = supabase_request(f"{EVENTS_TABLE}?purchase_id=eq.{purchase_id}&dispense_status=eq.Completed&select=id")
  if done:
      return reject("Purchase already dispensed", "Already Dispensed", purchase, machine_id)
  active = supabase_request(f"{EVENTS_TABLE}?purchase_id=eq.{purchase_id}&dispense_status=in.(Pending,Authorized,Dispensing)&select=id")
  if active:
      return reject("Purchase already has an active job", "Active Job Exists", purchase, machine_id)




  # check CoffeeInventory
  inventory_records = supabase_request(f"{INVENTORY_TABLE}?machine_id=eq.{machine_id}&select=*")
  if not inventory_records:
      return reject("No inventory found for this machine", "Invalid Machine", purchase, machine_id)
  inventory = inventory_records[0]
  print("inventory row:", inventory_records)
  if inventory.get("cups_remaining", 0) <= 0 or inventory.get("pods_remaining", 0) <= 0:
      return reject("Out of cups or pods", "Out of Stock", purchase, machine_id)




  busy = supabase_request(f"{EVENTS_TABLE}?machine_id=eq.{machine_id}&dispense_status=in.(Pending,Authorized,Dispensing)&select=id")
  if busy:
      return reject("Machine is busy with another job", "Machine Busy", purchase, machine_id)




  # everything passed: create the DispenseEvents row
  dispense_event = create_dispense_event({
      "purchase_id": purchase["id"],
      "machine_id": machine_id,                 
      "authorized_at": datetime.now(timezone.utc).isoformat(),
      "dispense_status": "Authorized",
      "cup_job_status": "Authorized",
      "pod_job_status": "Authorized",
      "cup_attempts": 0,
      "pod_attempts": 0
  })




  return {"statusCode": 200, "body": json.dumps({
      "status": "AUTHORIZED",
      "purchase_id": purchase["id"],
      "machine_id": machine_id,
      "job_id": dispense_event.get("id")
  })}







