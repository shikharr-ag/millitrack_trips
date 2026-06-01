from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, time
import httpx
import logging

# Logger Config
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TripServer")

app = FastAPI(title="Millitrack Web Dashboard")
templates = Jinja2Templates(directory="templates")

BASE_URL = "http://track.millitrack.com/api"
USERNAME = "VALSJR"
PASSWORD = "654321"


def process_trips(events_list):
    """Event processing state machine logic compiled earlier."""
    events_list.sort(key=lambda x: x["serverTime"])
    events_by_device = {}
    for event in events_list:
        dev_id = event["deviceId"]
        events_by_device.setdefault(dev_id, []).append(event)
        
    structured_trips = {}
    for dev_id, events in events_by_device.items():
        structured_trips[dev_id] = {}
        trip_counter = 1
        current_trip_geofence, valout_time = None, None
        tmlin_candidates, tmlout_candidates = [], []

        for event in events:
            g_id = event["geofenceId"]
            e_type = event["type"]
            e_time = event["serverTime"]

            if e_type == "geofenceExit" and g_id in [12616, 12623] and current_trip_geofence is None:
                current_trip_geofence = g_id
                valout_time = e_time
                tmlin_candidates, tmlout_candidates = [], []
                continue

            if current_trip_geofence is not None and g_id == 12617:
                if e_type == "geofenceEnter":
                    tmlin_candidates.append(e_time)
                elif e_type == "geofenceExit":
                    tmlout_candidates.append(e_time)
                continue

            if e_type == "geofenceEnter" and g_id == current_trip_geofence:
                trip_key = f"tripNumber_{trip_counter}"
                structured_trips[dev_id][trip_key] = {
                    "valin": e_time,
                    "valout": valout_time,
                    "tmlin": tmlin_candidates[0] if tmlin_candidates else "",
                    "tmlout": tmlout_candidates[-1] if tmlout_candidates else ""
                }
                trip_counter += 1
                current_trip_geofence, valout_time = None, None
                tmlin_candidates, tmlout_candidates = [], []

    return structured_trips


# ROUTE 1: Serves the Web Dashboard Interface
@app.get("/", response_class=HTMLResponse)
def serve_dashboard(request: Request):
    return templates.TemplateResponse(name="index.html",request= {"request": request})


# ROUTE 2: Dynamic JSON Endpoint that fetches from Millitrack, filters, and processes data
@app.get("/api/processed-trips")
async def get_processed_trips_api(date: str = Query(..., description="Date format YYYY-MM-DD")):
    auth = httpx.AsyncClient(auth=httpx.BasicAuth(USERNAME, PASSWORD))
    
    async with httpx.AsyncClient(auth=httpx.BasicAuth(USERNAME, PASSWORD), timeout=45.0) as client:
        try:
            # 1. Fetch live Device Map to extract active target IDs
            res_devices = await client.get(f"{BASE_URL}/devices")
            res_devices.raise_for_status()
            device_ids = [d["id"] for d in res_devices.json()]

            # 2. Structure Time Parameters
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
            from_dt = datetime.combine(target_date, time.min).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            to_dt = datetime.combine(target_date, time.max).strftime("%Y-%m-%dT%H:%M:%S.999Z")

            params = [
                ("from", from_dt), ("to", to_dt), ("mail", "false"),
                ("type", "geofenceExit"), ("type", "geofenceEnter")
            ]
            for d_id in device_ids:
                params.append(("deviceId", str(d_id)))

            # 3. Request Remote Events Stream
            logger.info(f"Querying events stream for date {date}")
            headers = {"Accept": "application/json"}
            res_events = await client.get(f"{BASE_URL}/reports/events", params=params, headers=headers)
            res_events.raise_for_status()

            # 4. Pipeline execution
            processed_data = process_trips(res_events.json())
            return processed_data

        except httpx.HTTPStatusError as e:
            logger.error(f"External API Error: {e.response.text}")
            raise HTTPException(status_code=502, detail="Upstream telemetry platform failed.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Date parameters supplied.")