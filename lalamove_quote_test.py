#!/usr/bin/env python3
"""
Simple Lalamove quotation tester that mirrors route.ts logic.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://rest.lalamove.com"
MARKET = "SG"
QUOTATION_PATH = "/v3/quotations"


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


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


def process_lalamove_response(response_data: dict[str, Any]) -> dict[str, Any]:
    if "data" not in response_data:
        return {"error": "No data found in response"}

    data = response_data["data"]
    valid_quotes: list[dict[str, Any]] = []

    if data.get("priceBreakdown"):
        price_breakdown = data["priceBreakdown"]
        distance_info = data.get("distance", {})

        valid_quote = {
            "service_type": data.get("serviceType", "CAR"),
            "total_price": float(price_breakdown.get("total", 0)),
            "base_price": float(price_breakdown.get("base", 0)),
            "extra_mileage": float(price_breakdown.get("extraMileage", 0)),
            "currency": price_breakdown.get("currency", "SGD"),
            "distance": float(distance_info.get("value", 0)) / 1000,
            "quotation_id": data.get("quotationId", ""),
            "expires_at": data.get("expiresAt", ""),
            "breakdown": price_breakdown,
        }
        valid_quotes.append(valid_quote)

    return {
        "valid_quotes": valid_quotes,
        "total_valid_quotes": len(valid_quotes),
    }


def request_quote(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get("LALAMOVE_APIKEY")
    secret_key = os.environ.get("LALAMOVE_SECRETKEY")

    if not api_key or not secret_key:
        raise RuntimeError("Missing LALAMOVE_APIKEY or LALAMOVE_SECRETKEY in environment.")

    payload_data: dict[str, Any] = {
        "serviceType": args.vehicle_type,
        "specialRequests": [],
        "language": "en_SG",
        "stops": [
            {
                "coordinates": {"lat": str(args.pickup_lat), "lng": str(args.pickup_lng)},
                "address": args.pickup_address,
            },
            {
                "coordinates": {"lat": str(args.dropoff_lat), "lng": str(args.dropoff_lng)},
                "address": args.dropoff_address,
            },
        ],
        "isRouteOptimized": True,
    }

    if args.schedule_at:
        payload_data["scheduleAt"] = args.schedule_at

    payload = {"data": payload_data}
    body = json.dumps(payload, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    auth_header = generate_hmac_auth(api_key, secret_key, timestamp, "POST", QUOTATION_PATH, body)

    headers = {
        "Content-Type": "application/json",
        "Authorization": auth_header,
        "market": MARKET,
    }

    req = Request(
        url=f"{API_BASE}{QUOTATION_PATH}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)
            return {"status": response.status, "data": data}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        return {"status": exc.code, "data": data}
    except URLError as exc:
        raise RuntimeError(f"Network error while requesting Lalamove quote: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Request a Lalamove quote using HMAC auth.")
    parser.add_argument("--vehicle-type", default="CAR")
    parser.add_argument("--pickup-lat", type=float, default=1.3521)
    parser.add_argument("--pickup-lng", type=float, default=103.8198)
    parser.add_argument("--pickup-address", default="Singapore")
    parser.add_argument("--dropoff-lat", type=float, default=1.3003)
    parser.add_argument("--dropoff-lng", type=float, default=103.8419)
    parser.add_argument("--dropoff-address", default="Marina Bay Sands, Singapore")
    parser.add_argument("--schedule-at", default="")
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parents[1] / ".env"),
        help="Path to .env file containing LALAMOVE_APIKEY and LALAMOVE_SECRETKEY",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))

    result = request_quote(args)
    status = result["status"]
    response_data = result["data"]

    print(f"HTTP status: {status}")
    print(json.dumps(response_data, indent=2, ensure_ascii=True))

    if status in (200, 201) and isinstance(response_data, dict):
        processed = process_lalamove_response(response_data)
        print("\nProcessed quote output:")
        print(json.dumps(processed, indent=2, ensure_ascii=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
