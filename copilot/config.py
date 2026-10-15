"""One config file for every stage: config.yaml in the repo root, else config.example.yaml.

`JSC_CONFIG` points at another file (tests, a second profile). Every module's switch lives under `modules:`;
everything is off by default except the core. Values in config.yaml are merged over the example file, so a
partial config.yaml only needs the keys you change."""
import copy
import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"
MODULES = ("slack_intake", "ai_triage", "form_extractor", "job_apply", "airtable")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def config_path() -> Path:
    env = os.environ.get("JSC_CONFIG")
    if env:
        return Path(env)
    own = ROOT / "config.yaml"
    return own if own.exists() else EXAMPLE


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> dict:
    base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}
    p = Path(path)
    if p.resolve() == EXAMPLE.resolve():
        return base
    return _merge(base, yaml.safe_load(p.read_text(encoding="utf-8")) or {})


def load() -> dict:
    p = config_path()
    return copy.deepcopy(_load(str(p), p.stat().st_mtime))


def resolve(rel) -> Path:
    """A config path: absolute as given, otherwise relative to the repo root."""
    p = Path(str(rel))
    return p if p.is_absolute() else ROOT / p


def data_dir(cfg: dict | None = None) -> Path:
    env = os.environ.get("JSC_DATA_DIR")
    return resolve(env or (cfg or load()).get("data_dir") or "data")


def module(name: str, cfg: dict | None = None) -> dict:
    if name not in MODULES:
        raise KeyError(f"unknown module {name!r}; one of {', '.join(MODULES)}")
    return dict(((cfg or load()).get("modules") or {}).get(name) or {})


def enabled(name: str, cfg: dict | None = None) -> bool:
    return bool(module(name, cfg).get("enabled"))


# ---- the data layout (one place, so every stage agrees) -------------------------------------------------
def leads_dir(cfg=None) -> Path:
    return data_dir(cfg) / "leads"


def prep_dir(cfg=None) -> Path:
    return data_dir(cfg) / "prep"


def forms_dir(cfg=None) -> Path:
    return data_dir(cfg) / "prep" / "_forms"


def state_dir(cfg=None) -> Path:
    """Run state that is never shared: dispatch job list, lockdown folders, Slack run dirs."""
    return data_dir(cfg) / ".state"


def scoring_config_path(cfg=None) -> Path:
    return data_dir(cfg) / "scoring_config.json"


def summaries_dir(cfg=None) -> Path:
    return data_dir(cfg) / "reports"
