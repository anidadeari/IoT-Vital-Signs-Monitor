"""Compare the latest sensor reading with the bundled reference datasets."""

from __future__ import annotations

import csv
import math
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("VITALS_DB_PATH", str(BASE_DIR / "vitals.db")))
BIDMC_PATH = BASE_DIR / "datasets" / "bidmc_01_Numerics.csv"
ALAMEDA_PATH = BASE_DIR / "datasets" / "ALAMEDA_PD_tremor_dataset.csv"


def _as_float(value: Any) -> float | None:
    """Return a finite float, or None for missing/invalid CSV values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_values(values: Iterable[float | None]) -> list[float]:
    valid = [value for value in values if value is not None]
    if not valid:
        raise ValueError("The requested dataset column contains no numeric values.")
    return valid


def _average(values: Iterable[float | None]) -> float:
    valid = _valid_values(values)
    return sum(valid) / len(valid)


def _find_column(fieldnames: list[str] | None, *candidates: str) -> str:
    """Find a CSV column while tolerating whitespace and letter case."""
    if not fieldnames:
        raise ValueError("Dataset has no header row.")

    normalized = {name.strip().lower(): name for name in fieldnames}
    for candidate in candidates:
        match = normalized.get(candidate.strip().lower())
        if match is not None:
            return match

    raise ValueError(
        f"Missing dataset column. Expected one of: {', '.join(candidates)}"
    )


@lru_cache(maxsize=1)
def _load_bidmc_reference() -> dict[str, float]:
    if not BIDMC_PATH.is_file():
        raise FileNotFoundError(f"BIDMC dataset not found: {BIDMC_PATH}")

    with BIDMC_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        hr_column = _find_column(reader.fieldnames, "HR", "heart_rate")
        spo2_column = _find_column(reader.fieldnames, "SpO2", "spo2")

        heart_rates: list[float | None] = []
        spo2_values: list[float | None] = []
        for row in reader:
            heart_rates.append(_as_float(row.get(hr_column)))
            spo2_values.append(_as_float(row.get(spo2_column)))

    return {
        "hr_average": round(_average(heart_rates), 2),
        "spo2_average": round(_average(spo2_values), 2),
        "record_count": len([value for value in heart_rates if value is not None]),
    }


@lru_cache(maxsize=1)
def _load_alameda_reference() -> dict[str, float]:
    if not ALAMEDA_PATH.is_file():
        raise FileNotFoundError(f"ALAMEDA dataset not found: {ALAMEDA_PATH}")

    with ALAMEDA_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        magnitude_column = _find_column(
            reader.fieldnames,
            "Magnitude_mean",
            "magnitude_mean",
        )
        magnitudes = _valid_values(
            _as_float(row.get(magnitude_column)) for row in reader
        )

    return {
        "tremor_magnitude_average": round(_average(magnitudes), 4),
        "tremor_magnitude_min": round(min(magnitudes), 4),
        "tremor_magnitude_max": round(max(magnitudes), 4),
        "record_count": len(magnitudes),
    }


def _latest_sensor_reading() -> dict[str, Any] | None:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Vitals database not found: {DB_PATH}")

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _similarity(sensor_value: Any, reference_value: float) -> float:
    sensor = _as_float(sensor_value)
    if sensor is None or sensor <= 0 or reference_value <= 0:
        return 0.0

    difference_ratio = abs(sensor - reference_value) / reference_value
    return round(max(0.0, 100.0 * (1.0 - difference_ratio)), 2)


def _quality(similarity: float) -> str:
    if similarity >= 90:
        return "Close to average"
    if similarity >= 75:
        return "Moderately close"
    if similarity >= 50:
        return "Different"
    if similarity > 0:
        return "Far from average"
    return "Unavailable"


def _comparison(
    sensor_value: Any,
    reference_value: float,
    unit: str,
    dataset_name: str,
) -> dict[str, Any]:
    sensor = _as_float(sensor_value)
    similarity = _similarity(sensor, reference_value)

    if sensor is None or sensor <= 0:
        return {
            "sensor_value": sensor_value,
            "reference_average": reference_value,
            "difference": None,
            "difference_percent": None,
            "relative_to_average_percent": None,
            "similarity": 0.0,
            "quality": "Unavailable",
            "direction": "unavailable",
            "interpretation": "No valid sensor value is available for comparison.",
            "dataset": dataset_name,
        }

    difference = sensor - reference_value
    difference_percent = (difference / reference_value) * 100
    relative_percent = (sensor / reference_value) * 100
    tolerance = max(abs(reference_value) * 0.02, 0.0001)
    direction = (
        "close to"
        if abs(difference) <= tolerance
        else "above"
        if difference > 0
        else "below"
    )

    if direction == "close to":
        sentence = (
            f"The sensor value is close to the {dataset_name} dataset average."
        )
    else:
        sentence = (
            f"The sensor value is {abs(difference):.2f} {unit} "
            f"({abs(difference_percent):.1f}%) {direction} the "
            f"{dataset_name} dataset average."
        )

    return {
        "sensor_value": sensor,
        "reference_average": reference_value,
        "difference": round(difference, 4),
        "difference_percent": round(difference_percent, 2),
        "relative_to_average_percent": round(relative_percent, 2),
        "similarity": similarity,
        "quality": _quality(similarity),
        "direction": direction,
        "interpretation": sentence,
        "dataset": dataset_name,
    }


def _temperature_comparison(sensor_value: Any) -> dict[str, Any]:
    temperature = _as_float(sensor_value)
    if temperature is None or temperature <= 0:
        status = "Unavailable"
        interpretation = "No valid temperature value is available."
    elif temperature < 32:
        status = "Verify sensor"
        interpretation = (
            f"The {temperature:.1f} °C reading is implausible as body temperature "
            "and likely reflects room temperature or insufficient sensor contact."
        )
    elif temperature < 36.1:
        status = "Below reference"
        interpretation = (
            f"The reading is {36.1 - temperature:.1f} °C below the "
            "36.1–37.2 °C project reference range."
        )
    elif temperature > 37.2:
        status = "Above reference"
        interpretation = (
            f"The reading is {temperature - 37.2:.1f} °C above the "
            "36.1–37.2 °C project reference range."
        )
    else:
        status = "Within reference"
        interpretation = "The reading is inside the 36.1–37.2 °C reference range."

    return {
        "sensor_value": temperature,
        "reference_range": "36.1–37.2",
        "quality": status,
        "interpretation": interpretation,
    }


def validate_latest_reading() -> dict[str, Any]:
    """Return the dataset-comparison payload consumed by the API/dashboard."""
    try:
        sensor_data = _latest_sensor_reading()
        if sensor_data is None:
            return {
                "status": "no_data",
                "message": "No sensor readings are available for comparison.",
            }

        bidmc = _load_bidmc_reference()
        alameda = _load_alameda_reference()

        heart_rate = _comparison(
            sensor_data.get("heart_rate"),
            bidmc["hr_average"],
            "bpm",
            "BIDMC",
        )
        spo2 = _comparison(
            sensor_data.get("spo2"),
            bidmc["spo2_average"],
            "percentage points",
            "BIDMC",
        )
        tremor = _comparison(
            sensor_data.get("tremor_amplitude"),
            alameda["tremor_magnitude_average"],
            "amplitude units",
            "ALAMEDA",
        )
        temperature = _temperature_comparison(sensor_data.get("temperature"))

        return {
            "status": "ok",
            "sensor_data": sensor_data,
            "reference_datasets": {
                "bidmc": bidmc,
                "alameda": alameda,
            },
            "validation_results": {
                "heart_rate_similarity": heart_rate["similarity"],
                "heart_rate_quality": heart_rate["quality"],
                "spo2_similarity": spo2["similarity"],
                "spo2_quality": spo2["quality"],
                "tremor_similarity": tremor["similarity"],
                "tremor_quality": tremor["quality"],
            },
            "comparison": {
                "heart_rate": heart_rate,
                "spo2": spo2,
                "tremor": tremor,
                "temperature": temperature,
            },
            "summary": (
                f"Heart rate is {heart_rate['direction']} the BIDMC average; "
                f"SpO2 is {spo2['direction']} the BIDMC average; "
                f"tremor amplitude is {tremor['direction']} the ALAMEDA average. "
                f"Temperature status: {temperature['quality']}."
            ),
            "method_note": (
                "These are mathematical comparisons with dataset averages, not "
                "diagnoses or clinical normality tests. Tremor comparison is valid "
                "only when the sensor and ALAMEDA values use matching units and "
                "signal-processing methods."
            ),
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Dataset comparison failed: {exc}",
        }


if __name__ == "__main__":
    import json

    print(json.dumps(validate_latest_reading(), indent=2))
