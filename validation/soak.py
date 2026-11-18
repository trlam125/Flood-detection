from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import urlopen


def summarize(samples, duration_sec, requested_duration_sec):
    available_sec = 0.0
    cameras = {}
    for index, sample in enumerate(samples):
        next_time = samples[index + 1]["elapsed_sec"] if index + 1 < len(samples) else duration_sec
        span = max(0, next_time - sample["elapsed_sec"])
        if not sample["available"]:
            continue
        available_sec += span
        for camera in sample["cameras"]:
            key = camera["camera_id"]
            values = cameras.setdefault(key, {"camera_id": key, "observed_sec": 0.0, "connected_sec": 0.0,
                "confirmed_sec": 0.0, "unknown_sec": 0.0, "max_frame_age_sec": 0.0, "max_analysis_age_sec": 0.0,
                "inference_p95_ms_max": None, "reconnect_attempts_max": 0})
            values["observed_sec"] += span
            values["connected_sec"] += span if camera["connection"] == "connected" else 0
            values["confirmed_sec" if camera["severity"] != "UNKNOWN" else "unknown_sec"] += span
            values["max_frame_age_sec"] = max(values["max_frame_age_sec"], camera.get("frame_age_sec") or 0)
            values["max_analysis_age_sec"] = max(values["max_analysis_age_sec"], camera.get("analysis_age_sec") or 0)
            latency = camera.get("inference_ms", {}).get("p95")
            if latency is not None:
                values["inference_p95_ms_max"] = max(values["inference_p95_ms_max"] or 0, latency)
            values["reconnect_attempts_max"] = max(values["reconnect_attempts_max"], camera.get("reconnect_attempts", 0))
    complete = duration_sec >= requested_duration_sec * .99
    for values in cameras.values():
        values["unobserved_sec"] = max(0, duration_sec - values["observed_sec"])
        values["confirmed_fraction_of_run"] = values["confirmed_sec"] / duration_sec if duration_sec else None
    return {"samples": len(samples), "requested_duration_sec": requested_duration_sec, "duration_sec": duration_sec,
        "completed_requested_duration": complete, "server_available_fraction": available_sec / duration_sec if duration_sec else None,
        "cameras": list(cameras.values()), "field_acceptance": "not_established",
        "has_fresh_confirmed_observations": any(camera["confirmed_sec"] > 0 for camera in cameras.values()),
        "limitations": ["Availability is estimated at the polling interval", "No event precision/recall without reviewed labels",
                        "Provider acceptance does not verify delivery to a recipient"],
        "next_action": "Review outages and labelled-event report" if cameras and complete else "Collect a complete run with active cameras"}


def run_soak(url: str, output: Path, duration_sec: float, interval_sec: float, fetch=None):
    if duration_sec <= 0 or interval_sec <= 0:
        raise ValueError("Duration and polling interval must be positive")
    output.mkdir(parents=True, exist_ok=False)
    def request():
        with urlopen(url.rstrip("/") + "/api/operations", timeout=min(10, max(1, interval_sec))) as response:
            return json.loads(response.read(8 * 1024 * 1024))
    fetch = fetch or request
    samples = []
    started = time.monotonic()
    try:
        with (output / "samples.jsonl").open("w", encoding="utf-8") as stream:
            while time.monotonic() - started < duration_sec:
                at = time.monotonic() - started
                try:
                    result = fetch()
                    sample = {"elapsed_sec": at, "at_unix": time.time(), "available": True,
                        "status": result["status"], "cameras": result["cameras"], "storage": result["storage"],
                        "deliveries": result["deliveries"], "issues": result["issues"]}
                except Exception:
                    sample = {"elapsed_sec": at, "at_unix": time.time(), "available": False, "cameras": [], "error": "Cannot read operational state"}
                samples.append(sample)
                stream.write(json.dumps(sample, allow_nan=False) + "\n")
                stream.flush()
                time.sleep(max(0, min(interval_sec - (time.monotonic() - started - at), duration_sec - (time.monotonic() - started))))
    except KeyboardInterrupt:
        pass
    report = summarize(samples, time.monotonic() - started, duration_sec)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report
