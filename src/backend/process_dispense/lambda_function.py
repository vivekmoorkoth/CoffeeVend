import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

#environmental variables that are set in AWS for security
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY")
MACHINE_ID = os.environ.get("MACHINE_ID", "coffeevend_01") 
COFFEE_ITEM_ID = os.environ.get("COFFEE_ITEM_ID")

#table names
PURCHASES_TABLE = "Purchases"
EVENTS_TABLE = "DispenseEvents"
INVENTORY_TABLE = "CoffeeInventory"


#This connects with the supabase for the request and returns the rows as a list
#Turns the python into JSON thats readable 
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
      #This part sends the actual request and reads the reply. json.loads turns it back into python 
      #timeout=10 sets a 10 second timer befiore giving up
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



#catch errors so that crashes get displayed as 500 errors
def catch_errors(handler):
  def wrapper(event, context):
      try:
          return handler(event, context)
      except Exception as e:
          print(f"Internal server error: {e}")
          return {"statusCode": 500, "body": json.dumps({"error": "Internal server error", "error_type": "Server Error"})} 
  return wrapper



#tells the user the information if status is ever rejected like what error type it is 
def reject(error, error_type, purchase=None, machine_id=None):
  return {"statusCode": 409, "body": json.dumps({"status": "REJECTED", "error": error, "error_type": error_type})} 



#read machine id and the component. 
def get_dispense_job(event):
    params = event.get("queryStringParameters") or {}

    machine_id = params.get("machine_id") or MACHINE_ID
    component = params.get("component")
    #picks the column for the cups and pods
    if component == "cup":
        status_column = "cup_job_status"
        started_column = "cup_started_at"
    elif component == "pod":
        status_column = "pod_job_status"
        started_column = "pod_started_at"
    else:
        return {
            #instead of crashing send a 400 status code if not pod or cup
            "statusCode": 400,
            "body": json.dumps({
                "error": "component must be cup or pod"
            })
        }

    now = datetime.now(timezone.utc).isoformat()

    # Find oldest job this component has not claimed yet
    jobs = supabase_request(
        f"{EVENTS_TABLE}"
        f"?machine_id=eq.{machine_id}"
        f"&{status_column}=eq.Authorized"
        f"&order=id.asc"
        f"&limit=1"
        f"&select=*"
    )

    if not jobs:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "job_available": False
            })
        }

    job = jobs[0]

    # Claim only this component's part of the job
    #updates the job to dispensisng status and starts the time if authorized


    claimed = supabase_request(
        f"{EVENTS_TABLE}"
        f"?id=eq.{job['id']}"
        f"&{status_column}=eq.Authorized",
        "PATCH",
        {
            status_column: "Dispensing",
            started_column: now,
            "dispense_status": "Dispensing",
            "started_at": now
        }
    )

    if not claimed:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "job_available": False
            })
        }
    #JSON for job, purchase and machine id, and component
    return {
        "statusCode": 200,
        "body": json.dumps({
            "job_available": True,
            "job_id": job["id"],
            "purchase_id": job["purchase_id"],
            "machine_id": machine_id,
            "component": component
        })
    }

def get_display_state(event):
    params = event.get("queryStringParameters") or {}
    machine_id = params.get("machine_id", MACHINE_ID)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", machine_id):
        return {"statusCode": 400, "body": json.dumps({"error": "bad machine_id"})}

    rows = supabase_request(
        f"{EVENTS_TABLE}?machine_id=eq.{machine_id}&order=id.desc&limit=1"
        f"&select=id,dispense_status,error_type"
    )
    if not rows:
        state, job_id, error_type = "idle", None, None
    else:
        job = rows[0]
        job_id, error_type = job["id"], job.get("error_type")
        status = job["dispense_status"]
        if status == "Completed":
            state = "success"
        elif status == "Failed":
            state = "failed"
        else:
            state = "dispensing"

    return {"statusCode": 200, "body": json.dumps(
        {"state": state, "job_id": job_id, "error_type": error_type})}

@catch_errors
def lambda_handler(event, context):
  if event.get("rawPath", "").endswith("/display-state"):
      return get_display_state(event)
  if event.get("requestContext", {}).get("http", {}).get("method") == "GET":
      return get_dispense_job(event)
  # read the request
  body = json.loads(event["body"]) if event.get("body") else event
  purchase_id = body.get("purchase_id")
  machine_id = body.get("machine_id") or MACHINE_ID




  # make sure we received a real and valid purchase_id and machine_id
  if purchase_id is None or not str(purchase_id).isdigit():
      return reject("purchase_id is missing or not a number", "Invalid Request")
  if not isinstance(machine_id, str) or not re.match(r"^[A-Za-z0-9_-]{1,64}$", machine_id):
      return reject("machine_id is missing or invalid", "Invalid Request")




  # read the matching row from purchases
  #confirms the member bought something, id not it gets rejected
  purchases = supabase_request(f"{PURCHASES_TABLE}?id=eq.{purchase_id}&select=*")
  if not purchases:
      return reject("Purchase ID not found", "Invalid Purchase")
  purchase = purchases[0]




  # check that the item and status is valid before continuing
  #this first if checks if the purchase is a coffee
  if COFFEE_ITEM_ID and str(purchase["item_id"]) != str(COFFEE_ITEM_ID):
      return reject("This purchase is not a coffee", "Not Coffee", purchase, machine_id)
  #makes sure the purchase isnt already being ordered
  if purchase["status"] != "Ordered":
      return reject("Purchase already used or cancelled", "Invalid Status", purchase, machine_id)



#checks if the purchase has already been completed and stores it into done as a url
  done = supabase_request(f"{EVENTS_TABLE}?purchase_id=eq.{purchase_id}&dispense_status=eq.Completed&select=id") 
  if done:
      return reject("Purchase already dispensed", "Already Dispensed", purchase, machine_id)
  active = supabase_request(f"{EVENTS_TABLE}?purchase_id=eq.{purchase_id}&dispense_status=in.(Pending,Authorized,Dispensing)&select=id")
  if active:
      return reject("Purchase already has an active job", "Active Job Exists", purchase, machine_id)




  # check CoffeeInventory to confirm if the item is in inventory or if it is out of stock
  inventory_records = supabase_request(f"{INVENTORY_TABLE}?machine_id=eq.{machine_id}&select=*")
  if not inventory_records:
      return reject("No inventory found for this machine", "Invalid Machine", purchase, machine_id)
  inventory = inventory_records[0]
  print("inventory row:", inventory_records)
  if inventory.get("cups_remaining", 0) <= 0 or inventory.get("pods_remaining", 0) <= 0:
      return reject("Out of cups or pods", "Out of Stock", purchase, machine_id)


#check to make sure the machine isnt already in use with another job
  busy = supabase_request(f"{EVENTS_TABLE}?machine_id=eq.{machine_id}&dispense_status=in.(Pending,Authorized,Dispensing)&select=id")
  if busy:
      return reject("Machine is busy with another job", "Machine Busy", purchase, machine_id)



  # After all checks are done this assumes everything passed and creates the DispenseEvents row
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



#status code 200 means success and the API sends this responce back to the website
  return {"statusCode": 200, "body": json.dumps({
      "status": "AUTHORIZED",
      "purchase_id": purchase["id"],
      "machine_id": machine_id,
      "job_id": dispense_event.get("id")
  })}







