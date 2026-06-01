import logging
from datetime import datetime, time
import httpx

# 1. Configure the logging system at the top of your script
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG to catch everything; change to INFO for production
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TripProcessor")
# Configuration
BASE_URL = "http://track.millitrack.com/api"
USERNAME = "VALSJR"
PASSWORD = "654321"

def get_base_maps(client):
    """Fetches devices and geofences to build lookup maps."""
    # 1. Fetch Devices
    res_devices = client.get(f"{BASE_URL}/devices")
    res_devices.raise_for_status()
    device_id_and_name = {d["id"]: d["name"] for d in res_devices.json()}

    # 2. Fetch Geofences
    res_geofences = client.get(f"{BASE_URL}/geofences")
    res_geofences.raise_for_status()
    geofence_id_and_name = {g["id"]: g["name"] for g in res_geofences.json()}

    return device_id_and_name, geofence_id_and_name


def fetch_events(client, device_ids, target_date_str):
    """
    Fetches events for a list of device IDs for a specific date string (YYYY-MM-DD).
    Logs crucial milestones to assist in debugging payload or API mismatches.
    """
    logger.info(f"Starting event fetch pipeline for date: {target_date_str}")
    logger.debug(f"Targeting {len(device_ids)} device IDs: {device_ids}")

    try:
        # Parse target date and construct 00:00 and 23:59 ISO strings
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        from_dt = datetime.combine(target_date, time.min).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to_dt = datetime.combine(target_date, time.max).strftime("%Y-%m-%dT%H:%M:%S.999Z")
        
        logger.debug(f"Calculated time window -> From: {from_dt} | To: {to_dt}")

        headers = {
            "Accept": "application/json"
        }

        # Build query parameters
        params = [
            ("from", from_dt),
            ("to", to_dt),
            ("mail", "false"),
            ("type", "geofenceExit"),
            ("type", "geofenceEnter")
        ]
        for dev_id in device_ids:
            params.append(("deviceId", str(dev_id)))

        url = f"{BASE_URL}/reports/events"
        
        # Log the outgoing request details (without leaking basic auth headers)
        logger.info(f"Sending GET request to {url}")
        logger.debug(f"Total query parameters compiled: {len(params)}")

        response = client.get(url, params=params,headers=headers)
        
        # Log HTTP Response tracking
        logger.info(f"Received HTTP {response.status_code} from Millitrack API")  # Log first 200 chars of body for quick inspection
        
        # If the API throws a 4xx or 5xx, this triggers the except block below
        response.raise_for_status()
        
        events_data = response.json()
        logger.info(f"Successfully retrieved and parsed {len(events_data)} raw events.")
        
        # Log a snippet of the payload if data came back, great for validating structure changes
        if events_data:
            logger.debug(f"Sample first event element: {events_data[0]}")
        else:
            logger.warning(f"No events were returned by the API for date {target_date_str} across these devices.")

        return events_data

    except httpx.HTTPStatusError as exc:
        logger.error(f"API Error Response: Status {exc.response.status_code} | Details: {exc.response.text}")
        raise  # Re-raise so the calling main function handles it gracefully
    except httpx.RequestError as exc:
        logger.critical(f"Network connectivity or structural request error mapping to {exc.request.url}: {exc}")
        raise
    except ValueError as exc:
        logger.error(f"Date parsing failed! Ensure target_date_str matches 'YYYY-MM-DD'. Error: {exc}")
        raise
def process_trips(events_list):
    """Processes raw event array into structured trip windows per device."""
    # Sort events globally by serverTime chronologically
    events_list.sort(key=lambda x: x["serverTime"])
    
    # Group events by deviceId
    events_by_device = {}
    for event in events_list:
        dev_id = event["deviceId"]
        events_by_device.setdefault(dev_id, []).append(event)
        
    structured_trips = {}

    for dev_id, events in events_by_device.items():
        structured_trips[dev_id] = {}
        trip_counter = 1
        
        current_trip_geofence = None
        valout_time = None
        tmlin_candidates = []
        tmlout_candidates = []

        for event in events:
            g_id = event["geofenceId"]
            e_type = event["type"]
            e_time = event["serverTime"]

            # TRIPS START: Exit from 12616 or 12623
            if e_type == "geofenceExit" and g_id in [12616, 12623] and current_trip_geofence is None:
                current_trip_geofence = g_id
                valout_time = e_time
                tmlin_candidates, tmlout_candidates = [], []
                continue

            # MID-TRIP TRACKING: Activity in Geofence 12617
            if current_trip_geofence is not None and g_id == 12617:
                if e_type == "geofenceEnter":
                    tmlin_candidates.append(e_time)
                elif e_type == "geofenceExit":
                    tmlout_candidates.append(e_time)
                continue

            # TRIP COMPLETION: Return to starting geofence
            if e_type == "geofenceEnter" and g_id == current_trip_geofence:
                valin_time = e_time
                tmlin = tmlin_candidates[0] if tmlin_candidates else ""
                tmlout = tmlout_candidates[-1] if tmlout_candidates else ""

                trip_key = f"tripNumber_{trip_counter}"
                structured_trips[dev_id][trip_key] = {
                    "valin": valin_time,
                    "valout": valout_time,
                    "tmlin": tmlin,
                    "tmlout": tmlout
                }
                
                trip_counter += 1
                current_trip_geofence = None
                valout_time = None
                tmlin_candidates, tmlout_candidates = [], []

    return structured_trips


def main():
    # Use a specific target date string (Change this variable as needed)
    target_date_str = "2026-05-27"
    
    # Using an HTTPX Client to manage authentication reuse cleanly
    auth = httpx.BasicAuth(USERNAME, PASSWORD)
    
    with httpx.Client(auth=auth, timeout=30.0) as client:
        try:
            print("Fetching Device and Geofence maps...")
            deviceIdAndName, geofenceIdAndName = get_base_maps(client)
            
            # Extract the device ID list keys from the map we created
            device_ids = list(deviceIdAndName.keys())
            
            print(f"Fetching events for {len(device_ids)} devices on {target_date_str}...")
            events_list = fetch_events(client, device_ids, target_date_str)
            
            print("Processing trips pipeline...")
            final_trip_data = process_trips(events_list)
            
            # Print the final output
            import json
            print("\nFinal Structured Data Output:")
            print(json.dumps(final_trip_data, indent=4))
            
        except httpx.HTTPStatusError as exc:
            print(f"HTTP Error: {exc.response.status_code} - {exc.response.text}")
        except Exception as exc:
            print(f"An unexpected error occurred: {exc}")

if __name__ == "__main__":
    main()