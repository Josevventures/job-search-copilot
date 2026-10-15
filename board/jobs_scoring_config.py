"""Jobs ranking config (design: docs/ARCHITECTURE.md, "Ranking engine"; private spec §6).

<data_dir>/scoring_config.json is written by this module only (the board's Ranking settings drawer). Until the
first save, the defaults below apply. Stdlib + fastapi only. Formulas live in web/jobs_scoring.js; this module
only validates and normalizes numbers.

Every default here is an invented sample, not anyone's real preferences: set your own floor, weights, bands and
home metros, from the drawer or by POSTing the config."""
import copy
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

FACTORS = ("role_fit", "domain", "odds", "comp", "location", "freshness")
LEVELS = ("entry", "mid", "senior", "manager", "director_plus")
BODY_KEYS = {"expected_version", "active_preset", "active_weights", "compensation_settings", "market_bands",
             "market_bands_note", "bands", "home_metros"}
# The metro slugs triage writes (skills/job-apply-fleet/lib/checkpoint.py METROS) that can be a home metro.
HOME_METROS = ("new_york", "north_jersey", "san_francisco", "boston", "austin", "seattle", "los_angeles",
               "washington_dc", "chicago", "other_us")
BAND_KEYS = ("apply_now_min", "worth_a_look_min")
RETRIES, RETRY_SLEEP_S = 10, 0.05
# Invented sample values (see the module docstring). jobs_scoring.js DEFAULT_CONFIG must stay identical.
_BAL = {"role_fit": 0.30, "domain": 0.20, "odds": 0.15, "comp": 0.15, "location": 0.10, "freshness": 0.10}
_DEFAULTS = {
    "schema_version": 1, "version": 0, "updated_at": None, "active_preset": "Balanced",
    "active_weights": dict(_BAL),
    "compensation_settings": {"annual_base_floor": 100000, "hard_gate_clamp_ceiling": 40},
    "bands": {"apply_now_min": 75, "worth_a_look_min": 55},
    "presets": {
        "Balanced": dict(_BAL),
        "High Leverage": {"role_fit": 0.20, "domain": 0.10, "odds": 0.40, "comp": 0.10, "location": 0.10, "freshness": 0.10},
        "Mission First": {"role_fit": 0.20, "domain": 0.40, "odds": 0.10, "comp": 0.10, "location": 0.10, "freshness": 0.10},
    },
    "market_bands": {"entry": [70000, 95000], "mid": [90000, 125000], "senior": [120000, 160000],
                     "manager": [130000, 175000], "director_plus": [165000, 230000]},
    "market_bands_note": "sample band",
    "home_metros": ["boston"],
}


def defaults() -> dict:
    return copy.deepcopy(_DEFAULTS)


class ConfigError(Exception):
    def __init__(self, status: int, problems):
        super().__init__(str(problems))
        self.status, self.problems = status, problems


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def normalize(weights: dict) -> dict:
    """w / sum(w) at full float precision: no rounding and no residual step, so it equals jobs_scoring.js
    normalizeWeights() to ~1e-15 (spec §6.4). Fixtures store 6-decimal values and are compared at 1e-6."""
    vals = {k: float(weights[k]) for k in FACTORS}
    total = sum(vals.values())
    return {k: vals[k] / total for k in FACTORS}


def _check_weights(w, where, probs):
    if not isinstance(w, dict) or set(w) != set(FACTORS):
        probs.append(f"{where}: need exactly {', '.join(FACTORS)}")
        return
    for k in FACTORS:
        if not (_num(w[k]) and 0 <= w[k] <= 1000):
            probs.append(f"{where}.{k}: a number 0-1000")
    if not probs and sum(w.values()) <= 0:
        probs.append(f"{where}: weights must sum to more than 0")


def _check_bands(b, probs):
    if not isinstance(b, dict) or set(b) != set(LEVELS):
        probs.append(f"market_bands: need exactly {', '.join(LEVELS)}")
        return
    for k in LEVELS:
        v = b[k]
        if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool)
                                                              and 10_000 <= x <= 2_000_000 for x in v) and v[0] <= v[1]):
            probs.append(f"market_bands.{k}: [min, max] integers 10000-2000000, min <= max")


def _check_home(h, probs):
    if not (isinstance(h, list) and 0 < len(h) <= len(HOME_METROS) and all(isinstance(m, str) for m in h)
            and set(h) <= set(HOME_METROS) and len(set(h)) == len(h)):
        probs.append(f"home_metros: a non-empty list of distinct metros from {', '.join(HOME_METROS)}")


def _apply(body: dict, current: dict) -> dict:
    if not isinstance(body, dict):
        raise ConfigError(422, ["body must be a JSON object"])
    probs = [f"unknown key {k!r}" for k in sorted(set(body) - BODY_KEYS)]
    ev = body.get("expected_version")
    if not (isinstance(ev, int) and not isinstance(ev, bool)):
        probs.append("expected_version: integer required")
    preset = body.get("active_preset")
    if preset is not None and (not isinstance(preset, str) or preset not in current["presets"]):
        probs.append(f"active_preset: one of {', '.join(current['presets'])} or null")
    _check_weights(body.get("active_weights"), "active_weights", probs)
    cs = body.get("compensation_settings")
    if not isinstance(cs, dict) or set(cs) - {"annual_base_floor", "hard_gate_clamp_ceiling"}:
        probs.append("compensation_settings: {annual_base_floor, hard_gate_clamp_ceiling?}")
        cs = {}
    floor = cs.get("annual_base_floor")
    if not (isinstance(floor, int) and not isinstance(floor, bool) and 10_000 <= floor <= 1_000_000):
        probs.append("compensation_settings.annual_base_floor: integer 10000-1000000")
    ceil = cs.get("hard_gate_clamp_ceiling", current["compensation_settings"]["hard_gate_clamp_ceiling"])
    if not (isinstance(ceil, int) and not isinstance(ceil, bool) and 1 <= ceil <= 100):
        probs.append("compensation_settings.hard_gate_clamp_ceiling: integer 1-100")
    if "market_bands" in body:
        _check_bands(body["market_bands"], probs)
    if "home_metros" in body:
        _check_home(body["home_metros"], probs)
    bands = body.get("bands", current["bands"])
    if not (isinstance(bands, dict) and set(bands) == set(BAND_KEYS)
            and all(isinstance(bands[k], int) and not isinstance(bands[k], bool) and 1 <= bands[k] <= 100
                    for k in BAND_KEYS)):
        probs.append("bands: {apply_now_min, worth_a_look_min}, integers 1-100")
    elif not (bands["apply_now_min"] > bands["worth_a_look_min"]
              > (ceil if isinstance(ceil, int) and not isinstance(ceil, bool) else 40)):
        probs.append(f"bands: need apply_now_min > worth_a_look_min > hard_gate_clamp_ceiling ({ceil}), "
                     "so every capped lead stays in Low")
    note = body.get("market_bands_note")
    if note is not None and not (isinstance(note, str) and 0 < len(note) <= 60):
        probs.append("market_bands_note: 1-60 characters")
    if probs:
        raise ConfigError(422, probs)
    if ev != current["version"]:
        raise ConfigError(409, {"message": "settings changed on another device", "config": current})
    doc = copy.deepcopy(current)
    doc["active_preset"] = preset
    doc["active_weights"] = dict(current["presets"][preset]) if preset else normalize(body["active_weights"])
    doc["compensation_settings"] = {"annual_base_floor": floor, "hard_gate_clamp_ceiling": ceil}
    doc["bands"] = {k: bands[k] for k in BAND_KEYS}
    if "market_bands" in body:
        doc["market_bands"] = body["market_bands"]
    if note is not None:
        doc["market_bands_note"] = note
    if "home_metros" in body:
        doc["home_metros"] = list(body["home_metros"])
    doc["version"] = current["version"] + 1
    return doc


def _valid_doc(d) -> bool:
    try:
        probs = []
        _check_weights(d["active_weights"], "active_weights", probs)
        for p in d["presets"].values():
            _check_weights(p, "preset", probs)
        _check_bands(d["market_bands"], probs)
        if "home_metros" in d:
            _check_home(d["home_metros"], probs)
        ceil = d["compensation_settings"]["hard_gate_clamp_ceiling"]
        floor = d["compensation_settings"]["annual_base_floor"]
        b = d.get("bands", _DEFAULTS["bands"])
        bands_ok = (isinstance(ceil, int) and 1 <= ceil <= 100 and isinstance(b, dict)
                    and all(isinstance(b.get(k), int) and not isinstance(b.get(k), bool) and 1 <= b[k] <= 100
                            for k in BAND_KEYS)
                    and b["apply_now_min"] > b["worth_a_look_min"] > ceil)   # same rule as the POST
        floor_ok = isinstance(floor, int) and not isinstance(floor, bool) and floor > 0  # scorer divides by it
        return not probs and bands_ok and floor_ok and isinstance(d["version"], int)
    except (KeyError, TypeError, AttributeError):
        return False


class ConfigStore:
    def __init__(self, path: Path, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()

    def load(self) -> tuple[dict, str | None]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return defaults(), None
        except (OSError, ValueError) as e:
            print(f"[jobs-config] {self.path.name} unreadable: {e!r}", flush=True)
            return defaults(), f"config unreadable: {e.__class__.__name__}; showing defaults until you save"
        if not _valid_doc(d):
            return defaults(), "config unreadable: invalid shape; showing defaults until you save"
        return {**defaults(), **d}, None

    def save(self, body: dict) -> dict:
        with self.lock:
            current, _ = self.load()
            doc = _apply(body, current)
            doc["updated_at"] = self.clock().isoformat()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(RETRIES):
                try:
                    os.replace(tmp, self.path)
                    return doc
                except PermissionError:  # Windows: target held open by a reader/indexer/AV
                    if attempt == RETRIES - 1:
                        raise
                    time.sleep(RETRY_SLEEP_S)


def make_router(store: ConfigStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/scoring-config")
    def get_config():
        doc, warning = store.load()
        return {"config": doc, "factors": list(FACTORS), "warning": warning}

    @router.post("/api/jobs/scoring-config")
    def post_config(body: dict = Body(...)):
        try:
            return {"config": store.save(body), "factors": list(FACTORS), "warning": None}
        except ConfigError as e:
            raise HTTPException(status_code=e.status, detail=e.problems)

    return router
