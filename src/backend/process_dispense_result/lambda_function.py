import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY")
MACHINE_ID = os.environ.get("MACHINE_ID", "coffeevend_01")

#functions for response and requesting from supabase
def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def supabase_request(path, method="GET", data=None):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read().decode()
            return json.loads(raw) if raw else []

    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print("Supabase said:", e.code, error_body)
        raise


def lambda_handler(event, context):
    try:
        #parsing json input and getting necessary parameters

        body = event.get("body")

        if isinstance(body, str) and body:
            params = json.loads(body)
        elif isinstance(body, dict):
            params = body
        else:
            params = event

        print("RESULT BODY:", params)

        job_id = params.get("job_id")
        component = params.get("component")
        status = params.get("status")
        attempts = params.get("attempts")
        reported_error = params.get("error_type")

        #checking that all variables are valid
        if job_id is None:
            return response(400, {"error": "job_id required"})

        if component not in ("cup", "pod"):
            return response(400, {"error": "component must be 'cup' or 'pod'"})

        if status not in ("success", "failed"):
            return response(400, {"error": "status must be 'success' or 'failed'"})

        if not isinstance(attempts, int) or attempts < 1 or attempts > 2:
            return response(400, {"error": f"{component} attempts must be 1 or 2"})

        if status == "success" and reported_error is not None:
            return response(
                400,
                {"error": "error_type must be null when status is success"}
            )

        if status == "failed" and reported_error not in (
            "dispense_failed",
            "sensor_failure",
            "motor_failure",
            "timeout"
        ):
            return response(
                400,
                {"error": "invalid error_type for failed dispense"}
            )

        #getting DispenseEvents table and all necessary columns
        events = supabase_request(
            f"DispenseEvents"
            f"?id=eq.{job_id}"
            f"&select=id,purchase_id,machine_id,dispense_status,"
            f"cup_status,pod_status,"
            f"cup_job_status,pod_job_status,"
            f"cup_attempts,pod_attempts"
        )

        #checking events is valid
        if not events:
            return response(
                404,
                {"error": f"unknown job_id {job_id}"}
            )

        #row is first row in DispenseEvents that has an id that matches job_id
        row = events[0]

        #getting purchase and machine id; making sure machine_id is whatever's in the supabase or the env variable 
        purchase_id = row["purchase_id"]
        machine_id = row.get("machine_id") or MACHINE_ID

        component_job_column = f"{component}_job_status"

        print("CURRENT EVENT:", row)
        print("COMPONENT JOB STATE:", row.get(component_job_column))

        #preventing duplicate purchase
        current_component_job_status = row.get(component_job_column)

        #making sure that if current job status is either completed or failed, it just immediately sends a failed response with current dispense status
        if current_component_job_status in ("Completed", "Failed"):
            return response(
                200,
                {
                    "recorded": False,
                    "dispense_status": row.get("dispense_status"),
                },
            )

        #if status is not currently dispensing, makes sure that it returns an error because it should be dispensing while both components are dispensing
        if current_component_job_status != "Dispensing":
            return response(
                409,
                {
                    "recorded": False,
                    "error": f"{component} component is not currently dispensing",
                    "component_job_status": current_component_job_status,
                },
            )

        now = datetime.now(timezone.utc).isoformat()

        # -----------------------------------
        # 5. Finalize THIS component
        # -----------------------------------

        component_final_job_status = (
            "Completed" if status == "success" else "Failed"
        )

        update = {
            f"{component}_status": status,
            f"{component}_attempts": attempts,
            f"{component}_job_status": component_final_job_status,
            "updated_at": now,
        }

        if status == "failed":
            update["error_type"] = reported_error

        print("COMPONENT UPDATE:", update)

        supabase_request(
            f"DispenseEvents?id=eq.{job_id}",
            method="PATCH",
            data=update,
        )

        # -----------------------------------
        # 6. Update inventory if physical dispense succeeded
        # -----------------------------------

        if status == "success":
            encoded_machine_id = urllib.parse.quote(machine_id, safe="")

            if component == "cup":
                inventory_column = "cups_remaining"
            else:
                inventory_column = "pods_remaining"

            inventory_rows = supabase_request(
                f"CoffeeInventory"
                f"?machine_id=eq.{encoded_machine_id}"
                f"&select={inventory_column}"
            )

            if not inventory_rows:
                return response(
                    404,
                    {"error": f"unknown machine_id {machine_id}"}
                )

            current_count = inventory_rows[0][inventory_column]

            print(
                "INVENTORY:",
                inventory_column,
                current_count,
                "->",
                max(0, current_count - 1),
            )

            supabase_request(
                f"CoffeeInventory?machine_id=eq.{encoded_machine_id}",
                method="PATCH",
                data={
                    inventory_column: max(0, current_count - 1),
                    "updated_at": now,
                },
            )

        # -----------------------------------
        # 7. Re-read event after component update
        # -----------------------------------

        updated_events = supabase_request(
            f"DispenseEvents"
            f"?id=eq.{job_id}"
            f"&select=cup_status,pod_status,"
            f"cup_job_status,pod_job_status,dispense_status"
        )

        updated = updated_events[0]

        cup_job_status = updated.get("cup_job_status")
        pod_job_status = updated.get("pod_job_status")

        cup_status = updated.get("cup_status")
        pod_status = updated.get("pod_status")

        print("AFTER COMPONENT RESULT:", updated)

        # -----------------------------------
        # 8. If one component is still working, overall remains Dispensing
        # -----------------------------------

        cup_finished = cup_job_status in ("Completed", "Failed")
        pod_finished = pod_job_status in ("Completed", "Failed")

        if not (cup_finished and pod_finished):
            return response(
                200,
                {
                    "recorded": True,
                    "dispense_status": "Dispensing",
                },
            )

        # -----------------------------------
        # 9. Both components have reported
        # -----------------------------------

        both_ok = (
            cup_job_status == "Completed"
            and pod_job_status == "Completed"
            and cup_status == "success"
            and pod_status == "success"
        )

        both_failed = (
            cup_job_status == "Failed"
            and pod_job_status == "Failed"
        )

        if both_ok:
            overall_status = "Completed"
            error_type = None

        elif both_failed:
            overall_status = "Failed"
            error_type = "both_failed"

        else:
            overall_status = "Failed"
            error_type = "partial_dispense"

        # -----------------------------------
        # 10. Finalize overall DispenseEvents row
        # -----------------------------------

        supabase_request(
            f"DispenseEvents?id=eq.{job_id}",
            method="PATCH",
            data={
                "dispense_status": overall_status,
                "error_type": error_type,
                "completed_at": now,
                "updated_at": now,
            },
        )

        # -----------------------------------
        # 11. Mark purchase Delivered only if BOTH succeeded
        # -----------------------------------

        if both_ok:
            supabase_request(
                f"Purchases?id=eq.{purchase_id}",
                method="PATCH",
                data={
                    "status": "Delivered"
                },
            )

        return response(
            200,
            {
                "recorded": True,
                "dispense_status": overall_status,
            },
        )

    except json.JSONDecodeError:
        return response(400, {"error": "invalid JSON body"})

    except urllib.error.HTTPError as e:
        print(f"Supabase HTTP error: {e.code}")
        return response(
            500,
            {
                "recorded": False,
                "error": "Supabase request failed",
            },
        )

    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
        return response(
            500,
            {
                "recorded": False,
                "error": "Internal server error",
            },
        )