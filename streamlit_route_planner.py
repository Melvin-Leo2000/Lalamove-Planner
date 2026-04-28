#!/usr/bin/env python3
"""Streamlit route planner for Lalamove quote optimization."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import folium
import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium

API_BASE = "https://rest.lalamove.com"
MARKET = "SG"
QUOTATION_PATH = "/v3/quotations"
FIXED_END_POSTAL_CODE = "629906"

VEHICLE_CONFIG: dict[str, dict[str, str]] = {
    "MOTORCYCLE": {
        "label": "Courier",
        "weight_limit": "8 kg",
        "size_limit": "40 x 30 x 30 cm",
    },
    "CAR": {
        "label": "Car",
        "weight_limit": "20 kg",
        "size_limit": "70 x 50 x 50 cm",
    },
    "MPV": {
        "label": "MPV",
        "weight_limit": "50 kg (max 1 item is 25 kg)",
        "size_limit": "110 x 80 x 50 cm",
    },
    "MINIVAN": {
    "label": "1.7m Van",
    "weight_limit": "400 kg",
    "size_limit": "160 x 120 x 100 cm",
    },
    "VAN": {
        "label": "2.4m Van",
        "weight_limit": "800 kg",
        "size_limit": "230 x 120 x 120 cm",
    },
    "TRUCK330": {
        "label": "10 ft Lorry",
        "weight_limit": "1200 kg",
        "size_limit": "290 x 140 x 170 cm",
    },
    "TRUCK550": {
        "label": "14 ft Lorry",
        "weight_limit": "2000 kg",
        "size_limit": "420 x 170 x 190 cm",
    },
    "LORRY_24FT": {
        "label": "24 ft Lorry",
        "weight_limit": "5000 kg",
        "size_limit": "750 x 230 x 230 cm",
    },
}


@dataclass
class Location:
    name: str
    postal_code: str
    lat: float
    lng: float
    address: str


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip()


def load_runtime_secrets() -> None:
    for key in ("LALAMOVE_APIKEY", "LALAMOVE_SECRETKEY"):
        secret_value = st.secrets.get(key)
        if secret_value and key not in os.environ:
            os.environ[key] = str(secret_value)


def generate_hmac_auth(
    api_key: str,
    secret_key: str,
    timestamp: str,
    method: str = "POST",
    path: str = QUOTATION_PATH,
    body: str = "",
) -> str:
    string_to_sign = f"{timestamp}\r\n{method}\r\n{path}\r\n\r\n{body}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac {api_key}:{timestamp}:{signature}"


def geocode_postal_code(postal_code: str) -> Location | None:
    query = urlencode(
        {
            "searchVal": postal_code,
            "returnGeom": "Y",
            "getAddrDetails": "Y",
            "pageNum": "1",
        }
    )
    url = f"https://www.onemap.gov.sg/api/common/elastic/search?{query}"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results", [])
    if not results:
        return None

    # Prioritize exact postal matches when available.
    picked = None
    for result in results:
        if result.get("POSTAL") == postal_code:
            picked = result
            break
    if picked is None:
        picked = results[0]

    return Location(
        name=f"Stop {postal_code}",
        postal_code=postal_code,
        lat=float(picked["LATITUDE"]),
        lng=float(picked["LONGITUDE"]),
        address=picked.get("ADDRESS", postal_code),
    )


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def route_distance_km(start: Location, stops: list[Location], order: list[int]) -> float:
    total = 0.0
    current = start
    for idx in order:
        nxt = stops[idx]
        total += haversine_km(current.lat, current.lng, nxt.lat, nxt.lng)
        current = nxt
    return total


def nearest_neighbor_order(start: Location, stops: list[Location], first_idx: int) -> list[int]:
    remaining = set(range(len(stops)))
    order = [first_idx]
    remaining.remove(first_idx)
    current = stops[first_idx]

    while remaining:
        next_idx = min(
            remaining,
            key=lambda idx: haversine_km(current.lat, current.lng, stops[idx].lat, stops[idx].lng),
        )
        order.append(next_idx)
        remaining.remove(next_idx)
        current = stops[next_idx]
    return order


def two_opt_path(start: Location, stops: list[Location], order: list[int]) -> list[int]:
    best = order[:]
    improved = True
    while improved:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                if route_distance_km(start, stops, candidate) + 1e-9 < route_distance_km(start, stops, best):
                    best = candidate
                    improved = True
    return best


def build_candidate_orders(start: Location, stops: list[Location], max_candidates: int = 12) -> list[list[int]]:
    if not stops:
        return [[]]

    candidates: list[list[int]] = []
    for first_idx in range(len(stops)):
        order = nearest_neighbor_order(start, stops, first_idx)
        improved = two_opt_path(start, stops, order)
        candidates.append(improved)

    unique: list[list[int]] = []
    seen = set()
    for order in sorted(candidates, key=lambda item: route_distance_km(start, stops, item)):
        key = tuple(order)
        if key in seen:
            continue
        seen.add(key)
        unique.append(order)
        if len(unique) >= max_candidates:
            break
    return unique


def request_lalamove_quote(vehicle_type: str, route_stops: list[Location]) -> tuple[int, Any]:
    api_key = os.environ.get("LALAMOVE_APIKEY")
    secret_key = os.environ.get("LALAMOVE_SECRETKEY")
    if not api_key or not secret_key:
        raise RuntimeError("Missing LALAMOVE_APIKEY or LALAMOVE_SECRETKEY.")

    payload = {
        "data": {
            "serviceType": vehicle_type,
            "specialRequests": [],
            "language": "en_SG",
            "stops": [
                {
                    "coordinates": {"lat": str(stop.lat), "lng": str(stop.lng)},
                    "address": stop.address,
                }
                for stop in route_stops
            ],
            "isRouteOptimized": False,
        }
    }
    body = json.dumps(payload, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    auth_header = generate_hmac_auth(api_key, secret_key, timestamp, "POST", QUOTATION_PATH, body)

    headers = {
        "Content-Type": "application/json",
        "Authorization": auth_header,
        "market": MARKET,
    }
    response = requests.post(
        f"{API_BASE}{QUOTATION_PATH}",
        headers=headers,
        data=body,
        timeout=30,
    )
    try:
        data = response.json()
    except ValueError:
        data = response.text
    return response.status_code, data


def extract_total_price(response_payload: dict[str, Any]) -> float | None:
    data = response_payload.get("data", {})
    breakdown = data.get("priceBreakdown", {})
    total = breakdown.get("total")
    if total is None:
        return None
    try:
        return float(total)
    except (TypeError, ValueError):
        return None


def draw_route_map(start: Location, middle_stops: list[Location], order: list[int], end: Location) -> folium.Map:
    m = folium.Map(location=[1.3521, 103.8198], zoom_start=12, tiles="OpenStreetMap")

    folium.Marker(
        [start.lat, start.lng],
        popup=f"Start: {start.address}",
        icon=folium.Icon(color="green", icon="play"),
    ).add_to(m)

    path_points = [(start.lat, start.lng)]
    for seq, idx in enumerate(order, start=2):
        stop = middle_stops[idx]
        path_points.append((stop.lat, stop.lng))
        folium.Marker(
            [stop.lat, stop.lng],
            popup=f"{seq}. {stop.address} ({stop.postal_code})",
            tooltip=f"{seq}",
            icon=folium.DivIcon(html=f"<div style='font-size: 14px; color: #b30000;'><b>{seq}</b></div>"),
        ).add_to(m)

    final_seq = len(order) + 2
    path_points.append((end.lat, end.lng))
    folium.Marker(
        [end.lat, end.lng],
        popup=f"{final_seq}. {end.address} ({end.postal_code}) [Final Storage]",
        tooltip=f"{final_seq}",
        icon=folium.Icon(color="red", icon="stop"),
    ).add_to(m)

    folium.PolyLine(path_points, color="blue", weight=4, opacity=0.8).add_to(m)
    return m


def render_copy_button(label: str, text_to_copy: str) -> None:
    # Client-side clipboard copy so copied text is available on the user's machine.
    button_id = f"copy_{abs(hash(text_to_copy)) % 10_000_000}"
    safe_text = json.dumps(text_to_copy)
    safe_label = label.replace("<", "&lt;").replace(">", "&gt;")
    html = f"""
    <div style="margin: 0.25rem 0 0.5rem 0;">
      <button id="{button_id}" style="padding: 0.4rem 0.8rem; border: 1px solid #999; border-radius: 0.4rem; background: #f7f7f7; cursor: pointer;">
        {safe_label}
      </button>
      <span id="{button_id}_msg" style="margin-left: 0.5rem; color: #16a34a; font-size: 0.9rem;"></span>
    </div>
    <script>
      const btn = document.getElementById("{button_id}");
      const msg = document.getElementById("{button_id}_msg");
      btn.onclick = async () => {{
        try {{
          await navigator.clipboard.writeText({safe_text});
          msg.textContent = "Copied";
          setTimeout(() => msg.textContent = "", 1500);
        }} catch (err) {{
          msg.textContent = "Copy failed";
        }}
      }};
    </script>
    """
    components.html(html, height=52)


def main() -> None:
    st.set_page_config(page_title="Lalamove Route Planner", layout="wide")
    st.title("Lalamove One-Day Route Planner")
    st.caption(
        f"Enter your job locations only. The app auto-selects the best start point, "
        f"optimizes the route, and keeps final storage fixed at {FIXED_END_POSTAL_CODE}."
    )

    load_runtime_secrets()
    default_env = Path(__file__).resolve().parent / ".env"
    load_env_file(default_env)

    if "location_postal_inputs" not in st.session_state:
        st.session_state.location_postal_inputs = [""] * 2
    if "last_plan" not in st.session_state:
        st.session_state.last_plan = None
    if "last_error" not in st.session_state:
        st.session_state.last_error = ""

    col_a, col_b = st.columns([2, 1])
    with col_a:
        vehicle_options = list(VEHICLE_CONFIG.keys())
        vehicle = st.selectbox(
            "Vehicle type",
            options=vehicle_options,
            index=1,
            format_func=lambda key: f"{VEHICLE_CONFIG[key]['label']} ({key})",
        )
        selected_specs = VEHICLE_CONFIG[vehicle]
        spec_col_1, spec_col_2 = st.columns(2)
        with spec_col_1:
            st.caption(f"Weight limit: {selected_specs['weight_limit']}")
        with spec_col_2:
            st.caption(f"Size limit (L x W x H): {selected_specs['size_limit']}")
    with col_b:
        location_count = st.number_input(
            "Number of locations to optimize",
            min_value=1,
            max_value=9,
            value=2,
            step=1,
            format="%d",
        )

    st.text_input("Final storage postal code", value=FIXED_END_POSTAL_CODE, disabled=True)

    if len(st.session_state.location_postal_inputs) != location_count:
        st.session_state.location_postal_inputs = (st.session_state.location_postal_inputs + [""] * 10)[:location_count]

    st.subheader("Location postal codes")
    for i in range(location_count):
        st.session_state.location_postal_inputs[i] = st.text_input(
            f"Location {i + 1} postal code",
            value=st.session_state.location_postal_inputs[i],
            key=f"location_postal_{i}",
        )

    if st.button("Find Lowest-Price Route", type="primary"):
        st.session_state.last_plan = None
        st.session_state.last_error = ""

        input_postal_codes = [value.strip() for value in st.session_state.location_postal_inputs if value.strip()]
        if len(input_postal_codes) != location_count:
            st.session_state.last_error = "Please fill in all location postal code fields."
            return

        try:
            with st.spinner("Geocoding postal codes..."):
                final_location = geocode_postal_code(FIXED_END_POSTAL_CODE)
                if final_location is None:
                    st.session_state.last_error = f"Could not geocode final storage postal code: {FIXED_END_POSTAL_CODE}"
                    return

                input_locations: list[Location] = []
                for code in input_postal_codes:
                    loc = geocode_postal_code(code)
                    if loc is None:
                        st.session_state.last_error = f"Could not geocode postal code: {code}"
                        return
                    input_locations.append(loc)

                all_postals = [loc.postal_code for loc in input_locations] + [final_location.postal_code]
                if len(set(all_postals)) != len(all_postals):
                    st.session_state.last_error = "Duplicate postal codes detected across input locations and final storage."
                    return

            with st.spinner("Optimizing all start-point candidates and requesting Lalamove quotes..."):
                results = []
                for start_idx, start_location in enumerate(input_locations):
                    remaining_locations = [loc for idx, loc in enumerate(input_locations) if idx != start_idx]
                    candidate_orders = build_candidate_orders(start_location, remaining_locations)

                    for order in candidate_orders:
                        route_stops = [start_location] + [remaining_locations[idx] for idx in order] + [final_location]
                        status, payload = request_lalamove_quote(vehicle, route_stops)
                        total_price = extract_total_price(payload) if isinstance(payload, dict) else None
                        results.append(
                            {
                                "start_location": start_location,
                                "remaining_locations": remaining_locations,
                                "order": order,
                                "status": status,
                                "payload": payload,
                                "price": total_price,
                            }
                        )

            valid = [item for item in results if item["status"] in (200, 201) and item["price"] is not None]
            if not valid:
                st.session_state.last_error = "No successful quotes returned. Check vehicle type, addresses, or API response."
                st.session_state.last_plan = {"api_responses": results}
                return

            best = min(valid, key=lambda item: item["price"])
            best_start_location = best["start_location"]
            best_remaining_locations = best["remaining_locations"]
            best_order = best["order"]
            comparison_rows = []
            for item in sorted(valid, key=lambda row: row["price"]):
                route_codes = (
                    [item["start_location"].postal_code]
                    + [item["remaining_locations"][idx].postal_code for idx in item["order"]]
                    + [final_location.postal_code]
                )
                order_text = " -> ".join(route_codes)
                comparison_rows.append(
                    {
                        "price_sgd": item["price"],
                        "route_order": order_text,
                    }
                )
            st.session_state.last_plan = {
                "selected_vehicle": vehicle,
                "start_location": best_start_location,
                "remaining_locations": best_remaining_locations,
                "final_location": final_location,
                "best": best,
                "best_order": best_order,
                "comparison_rows": comparison_rows,
            }

        except requests.RequestException as exc:
            st.session_state.last_error = f"Network/API request failed: {exc}"
        except RuntimeError as exc:
            st.session_state.last_error = str(exc)
        except Exception as exc:  # fallback guard for easier troubleshooting
            st.session_state.last_error = f"Unexpected error: {exc}"

    if st.session_state.last_error:
        st.error(st.session_state.last_error)
        if st.session_state.last_plan and "api_responses" in st.session_state.last_plan:
            with st.expander("API responses"):
                st.json(st.session_state.last_plan["api_responses"])

    if st.session_state.last_plan and "best" in st.session_state.last_plan:
        plan = st.session_state.last_plan
        required_keys = {
            "selected_vehicle",
            "start_location",
            "remaining_locations",
            "final_location",
            "best",
            "best_order",
            "comparison_rows",
        }
        if not required_keys.issubset(set(plan.keys())):
            st.session_state.last_plan = None
            st.warning("Cached plan was from an older version and has been reset. Please run again.")
            return

        selected_vehicle = plan["selected_vehicle"]
        start_location = plan["start_location"]
        remaining_locations = plan["remaining_locations"]
        final_location = plan["final_location"]
        best = plan["best"]
        best_order = plan["best_order"]

        st.success("Lowest-price route found.")
        st.metric("Best total price", f"{best['price']:.2f} SGD")
        st.write("Recommended stop order:")
        st.write(f"1. {start_location.address} ({start_location.postal_code}) [Start]")

        route_lines = [
            f"Recommended Route Order ({VEHICLE_CONFIG[selected_vehicle]['label']} / {selected_vehicle})",
            f"1. {start_location.address} ({start_location.postal_code}) [Start]",
        ]
        for rank, idx in enumerate(best_order, start=2):
            stop = remaining_locations[idx]
            st.write(f"{rank}. {stop.address} ({stop.postal_code})")
            route_lines.append(f"{rank}. {stop.address} ({stop.postal_code})")
        st.write(
            f"{len(best_order) + 2}. {final_location.address} ({final_location.postal_code}) [Final Storage]"
        )
        route_lines.append(
            f"{len(best_order) + 2}. {final_location.address} ({final_location.postal_code}) [Final Storage]"
        )
        route_lines.append(f"Best Total Price: {best['price']:.2f} SGD")

        formatted_route_text = "\n".join(route_lines)
        st.subheader("Copy-friendly route text")
        render_copy_button("Copy recommended stop order", formatted_route_text)
        st.code(formatted_route_text, language="text")

        st.subheader("Route map")
        route_map = draw_route_map(start_location, remaining_locations, best_order, final_location)
        st_folium(route_map, use_container_width=True, height=550)

        st.subheader("Candidate comparison")
        st.dataframe(plan["comparison_rows"], use_container_width=True)


if __name__ == "__main__":
    main()
