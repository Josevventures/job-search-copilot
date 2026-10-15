"""One JSON file per job lead. Each field group has exactly one writer; update() enforces it."""
import json
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

from lib import paths
from lib.canonical import lead_id as make_lead_id

GROUPS = {
    "intake": frozenset({"sources", "post_text", "preview", "jd_links", "screen", "created_at"}),
    "triage": frozenset({
        "triage_status", "triage_error", "batch_id", "triaged_at", "company", "role_title", "location",
        "pay", "pay_type", "jd_url", "jd_read", "jd_note", "posting_status", "category", "relevance",
        "why", "fit_score", "fit_rationale", "network", "engage", "airtable_match", "eligibility_note",
        "alternates", "draft_answers", "bucket", "bucket_reason", "fetch_method",
        "route", "checkpoint_version", "job_summary", "scores", "main_gap", "level", "locations", "modality",
        "relocation_assistance", "pay_bands", "pay_source", "bonus", "equity", "posted_date",
        "posted_date_source", "reposted", "applicant_count", "inbound", "recruiter_signal"}),
    "decision": frozenset({"decision", "history", "dispatch", "airtable_record_id",
                           "pending", "job_status", "prep_file", "band_override", "still_open"}),
}
DECISION_TO_BUCKET = {"skipped": "skip", "moved_tier2": "tier2", "moved_tier1": "tier1"}
SKIPS_FILE = "_skips.jsonl"  # skip log (Jobs tab); not a lead file: list() globs *.json only
LOCK_TIMEOUT_S = 5.0
LOCK_STALE_S = 30.0
RETRIES = 10
RETRY_SLEEP_S = 0.05


class LeadStoreError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LeadStore:
    def __init__(self, base: Path | None = None):
        self.base = Path(base) if base else paths.leads_dir()
        self.base.mkdir(parents=True, exist_ok=True)

    # ---- low level -------------------------------------------------------
    def _path(self, lid: str) -> Path:
        return self.base / f"{lid}.json"

    def _lock(self, lid: str):
        lock = self.base / f"{lid}.json.lock"
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return lock
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise LeadStoreError(f"lock timeout on {lid}")
                time.sleep(0.05)

    def _write(self, lid: str, data: dict) -> None:
        tmp = self.base / f"{lid}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(RETRIES):
            try:
                os.replace(tmp, self._path(lid))
                return
            except PermissionError as e:  # Windows: target held open by a reader/indexer/AV
                if attempt == RETRIES - 1:
                    raise LeadStoreError(f"cannot replace {lid}.json after {RETRIES} tries: {e}")
                time.sleep(RETRY_SLEEP_S)

    # ---- public ----------------------------------------------------------
    def read(self, lid: str) -> dict:
        p = self._path(lid)
        for attempt in range(RETRIES):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise LeadStoreError(f"no lead file {p.name}")
            except json.JSONDecodeError as e:
                raise LeadStoreError(f"invalid JSON in {p.name}: {e}")
            except PermissionError as e:
                if attempt == RETRIES - 1:
                    raise LeadStoreError(f"cannot read {p.name} after {RETRIES} tries: {e}")
                time.sleep(RETRY_SLEEP_S)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.base.glob("*.json")):
            try:
                out.append(self.read(p.stem))
            except LeadStoreError as e:
                warnings.warn(str(e), UserWarning)
        return out

    def create_or_merge(self, canonical_url: str, source: dict, **intake_fields) -> tuple[str, bool]:
        bad = set(intake_fields) - GROUPS["intake"]
        if bad:
            raise LeadStoreError(f"non-intake fields passed to create_or_merge: {sorted(bad)}")
        lid = make_lead_id(canonical_url)
        lock = self._lock(lid)
        try:
            src = {**source, "added_at": source.get("added_at") or _now()}
            if self._path(lid).exists():
                data = self.read(lid)
                intake = data.setdefault("intake", {})
                if src["url"] not in {s["url"] for s in intake.get("sources", [])}:
                    intake.setdefault("sources", []).append(src)
                for k, v in intake_fields.items():
                    if v not in (None, "", []) and intake.get(k) in (None, "", []):
                        intake[k] = v
                self._write(lid, data)
                return lid, False
            data = {"schema_version": 1, "lead_id": lid, "canonical_url": canonical_url,
                    "intake": {"sources": [src], "created_at": _now(), **intake_fields},
                    "triage": {"triage_status": "pending"}, "decision": {"decision": None, "history": []}}
            self._write(lid, data)
            return lid, True
        finally:
            lock.unlink(missing_ok=True)

    def append_source(self, lid: str, source: dict) -> bool:
        lock = self._lock(lid)
        try:
            data = self.read(lid)
            sources = data.setdefault("intake", {}).setdefault("sources", [])
            if source["url"] in {s["url"] for s in sources}:
                return False
            sources.append({**source, "added_at": source.get("added_at") or _now()})
            self._write(lid, data)
            return True
        finally:
            lock.unlink(missing_ok=True)

    def update(self, lid: str, group: str, fields: dict, schema_version: int | None = None) -> dict:
        """schema_version: only the triage writer may bump the lead's top-level version (ranking-engine §3.2)."""
        if group not in GROUPS:
            raise LeadStoreError(f"unknown group {group!r}")
        bad = set(fields) - GROUPS[group]
        if bad:
            raise LeadStoreError(f"fields {sorted(bad)} are not in group {group!r}")
        if schema_version is not None and group != "triage":
            raise LeadStoreError("schema_version can only be set with a triage update")
        lock = self._lock(lid)
        try:
            data = self.read(lid)
            data.setdefault(group, {}).update(fields)
            if schema_version is not None:
                data["schema_version"] = schema_version
            self._write(lid, data)
            return data
        finally:
            lock.unlink(missing_ok=True)

    def delete(self, lid: str) -> bool:
        """Remove a lead file. Idempotent: False if it was already gone."""
        lock = self._lock(lid)
        try:
            p = self._path(lid)
            for attempt in range(RETRIES):
                try:
                    p.unlink()
                    return True
                except FileNotFoundError:
                    return False
                except PermissionError as e:  # Windows: held open by a reader/indexer/AV
                    if attempt == RETRIES - 1:
                        raise LeadStoreError(f"cannot delete {p.name} after {RETRIES} tries: {e}")
                    time.sleep(RETRY_SLEEP_S)
        finally:
            lock.unlink(missing_ok=True)

    @property
    def skips_path(self) -> Path:
        return self.base / SKIPS_FILE

    def append_skip(self, record: dict) -> None:
        with open(self.skips_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def skipped_ids(self) -> set[str]:
        try:
            text = self.skips_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        ids = set()
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                ids.add(json.loads(line)["lead_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                warnings.warn(f"{SKIPS_FILE} line {n} ignored: {e!r}", UserWarning)
        return ids

    @staticmethod
    def effective_bucket(lead: dict) -> str | None:
        if lead.get("schema_version", 1) >= 2:
            return None                                  # v2 leads are scored, never bucketed
        d = (lead.get("decision") or {}).get("decision")
        if d in DECISION_TO_BUCKET:
            return DECISION_TO_BUCKET[d]
        return (lead.get("triage") or {}).get("bucket")
