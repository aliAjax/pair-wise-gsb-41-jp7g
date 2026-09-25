"""Catastrophe insurance claim triage and settlement service."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "catastrophe_claims.db"
TERMINAL = {"duplicate", "approved", "rejected", "closed"}
TRANSITIONS = {
    "received": {"triaged"},
    "triaged": {"assigned", "escalated"},
    "assigned": {"survey", "escalated"},
    "survey": {"review", "escalated"},
    "review": {"approved", "rejected", "escalated"},
    "escalated": {"assigned", "review", "rejected"},
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def actor_id(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def coordinate(value: Any, label: str, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc
    if not low <= value <= high:
        raise DomainError("%s超出有效范围" % label)
    return value


CENT = 0.005


def valid_money(value: Any, label: str) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc


def valid_pct(value: Any, label: str) -> float:
    try:
        value = round(float(value), 6)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc
    if not 0 < value <= 1:
        raise DomainError("%s应在 0 到 1 之间（不含0）" % label)
    return value


class CatastropheClaimService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_no TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    region TEXT NOT NULL,
                    peril_type TEXT NOT NULL,
                    policy_no TEXT NOT NULL,
                    claimant_ref TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    estimated_loss REAL NOT NULL,
                    urgent_need INTEGER NOT NULL DEFAULT 0,
                    fraud_score REAL NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'received',
                    assignee TEXT,
                    surveyor TEXT,
                    lodging_required INTEGER NOT NULL DEFAULT 0,
                    remote_review INTEGER NOT NULL DEFAULT 0,
                    emergency_advance REAL NOT NULL DEFAULT 0,
                    final_payout REAL,
                    duplicate_of INTEGER REFERENCES claims(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    submitter TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id,sha256)
                );
                CREATE TABLE IF NOT EXISTS survey_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    surveyor TEXT NOT NULL,
                    damage_ratio REAL NOT NULL,
                    findings TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    approved_by TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER REFERENCES claims(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                CREATE TABLE IF NOT EXISTS reinsurance_treaties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    treaty_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    retention REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reinsurance_layers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES reinsurance_treaties(id),
                    layer_order INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    reinsurer TEXT NOT NULL,
                    cession_pct REAL NOT NULL,
                    payout_cap REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(treaty_id,layer_order)
                );
                CREATE TABLE IF NOT EXISTS cession_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    event_id TEXT NOT NULL,
                    layer_id INTEGER REFERENCES reinsurance_layers(id),
                    bucket TEXT NOT NULL,
                    layer_order INTEGER,
                    reinsurer TEXT,
                    layer_name TEXT,
                    amount REAL NOT NULL,
                    absorbed REAL,
                    cession_pct REAL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cession_claim ON cession_entries(claim_id);
                CREATE INDEX IF NOT EXISTS idx_cession_event ON cession_entries(event_id,bucket);
                CREATE INDEX IF NOT EXISTS idx_cession_layer ON cession_entries(layer_id);
                """
            )

    def _audit(self, conn: sqlite3.Connection, claim_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(claim_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (claim_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _claim(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("理赔案件不存在", 404)
        return row

    def create_claim(self, actor: str, role: str, claim_no: str, event_id: str,
                     region: str, peril_type: str, policy_no: str, claimant_ref: str,
                     latitude: float, longitude: float, estimated_loss: float,
                     urgent_need: bool = False, lodging_required: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "supervisor"}, "创建报案")
        values = [claim_no, event_id, region, peril_type, policy_no, claimant_ref]
        if not all(str(v).strip() for v in values):
            raise DomainError("案件必需字段不能为空")
        lat = coordinate(latitude, "纬度", -90, 90)
        lon = coordinate(longitude, "经度", -180, 180)
        try:
            estimated_loss = float(estimated_loss)
        except (TypeError, ValueError) as exc:
            raise DomainError("预估损失必须是数值") from exc
        if estimated_loss < 0:
            raise DomainError("预估损失不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            duplicate_of = None
            candidates = conn.execute(
                """SELECT * FROM claims WHERE event_id=? AND policy_no=? AND status<>'duplicate'
                   ORDER BY id DESC LIMIT 50""",
                (event_id.strip(), policy_no.strip()),
            ).fetchall()
            for row in candidates:
                within_time = abs((datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])).total_seconds()) <= 172800
                loss_close = abs(row["estimated_loss"] - estimated_loss) <= max(1000.0, row["estimated_loss"] * 0.1)
                if within_time and loss_close and haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 3.0:
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "received"
            try:
                cur = conn.execute(
                    """INSERT INTO claims(claim_no,event_id,region,peril_type,policy_no,claimant_ref,latitude,longitude,
                       estimated_loss,urgent_need,lodging_required,status,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_no.strip(), event_id.strip(), region.strip(), peril_type.strip(), policy_no.strip(),
                     claimant_ref.strip(), lat, lon, estimated_loss, int(bool(urgent_need)), int(bool(lodging_required)),
                     status, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("报案编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "claim.created", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "claim.duplicate_detected", {"new_claim": claim_no.strip()})
            return dict(self._claim(conn, cur.lastrowid))

    def triage_claim(self, actor: str, role: str, claim_id: int, expected_version: int,
                     fraud_score: float = 0.0, remote_review: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "案件分级")
        try:
            fraud_score = float(fraud_score)
        except (TypeError, ValueError) as exc:
            raise DomainError("欺诈评分必须是数值") from exc
        if not 0 <= fraud_score <= 1:
            raise DomainError("欺诈评分应在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "received":
                raise DomainError("只有待分级案件可以分级", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            priority = min(100.0, claim["estimated_loss"] / 100000.0 * 25 + (40 if claim["urgent_need"] else 0) + fraud_score * 20 + (10 if claim["lodging_required"] else 0))
            new_status = "escalated" if fraud_score >= 0.8 else "triaged"
            conn.execute(
                "UPDATE claims SET fraud_score=?,priority_score=?,remote_review=?,status=?,version=version+1,updated_at=? WHERE id=?",
                (fraud_score, priority, int(bool(remote_review)), new_status, utcnow(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.triaged", {"priority": priority, "status": new_status})
            return dict(self._claim(conn, claim_id))

    def assign_claim(self, actor: str, role: str, claim_id: int, assignee: str,
                     expected_version: int, surveyor: str | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "分配案件")
        assignee = assignee.strip()
        if not assignee:
            raise DomainError("查勘负责人不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"triaged", "escalated", "assigned"}:
                raise DomainError("当前状态不能分配", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET assignee=?,surveyor=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (assignee, surveyor.strip() if surveyor else None, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.assigned", {"assignee": assignee, "surveyor": surveyor})
            return dict(self._claim(conn, claim_id))

    def add_evidence(self, actor: str, role: str, claim_id: int, sha256: str,
                     filename: str, source: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "adjuster", "surveyor", "supervisor"}, "添加损失证据")
        sha256 = sha256.strip().lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise DomainError("证据哈希必须是 64 位 SHA-256 十六进制")
        if not filename.strip() or not source.strip():
            raise DomainError("证据文件名和来源不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] in TERMINAL:
                raise DomainError("已结束案件不能添加证据", 409)
            existing = conn.execute("SELECT * FROM evidence WHERE claim_id=? AND sha256=?", (claim_id, sha256)).fetchone()
            if existing:
                return dict(existing)
            cur = conn.execute(
                "INSERT INTO evidence(claim_id,sha256,filename,source,submitter,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, sha256, filename.strip(), source.strip(), actor, utcnow()),
            )
            hash_claims = [r["claim_id"] for r in conn.execute(
                "SELECT DISTINCT claim_id FROM evidence WHERE sha256=?", (sha256,)
            ).fetchall()]
            suspicious = len(hash_claims) >= 3
            if suspicious:
                for cid in hash_claims:
                    conn.execute(
                        "UPDATE claims SET fraud_score=MAX(fraud_score,0.95),status='escalated',version=version+1,updated_at=? WHERE id=? AND status<>'duplicate'",
                        (utcnow(), cid),
                    )
                self._audit(conn, claim_id, actor, "evidence.bulk_reuse_detected", {"sha256": sha256, "claim_ids": hash_claims})
            self._audit(conn, claim_id, actor, "evidence.added", {"evidence_id": cur.lastrowid, "suspicious": suspicious})
            return {"evidence": dict(conn.execute("SELECT * FROM evidence WHERE id=?", (cur.lastrowid,)).fetchone()), "bulk_reuse": suspicious, "affected_claims": hash_claims}

    def record_survey(self, actor: str, role: str, claim_id: int, damage_ratio: float,
                      findings: str, recommendation: str, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "录入查勘结果")
        try:
            damage_ratio = float(damage_ratio)
        except (TypeError, ValueError) as exc:
            raise DomainError("损失比例必须是数值") from exc
        if not 0 <= damage_ratio <= 1 or not findings.strip() or not recommendation.strip():
            raise DomainError("损失比例或查勘内容无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"assigned", "escalated"}:
                raise DomainError("当前状态不能录入查勘", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["assignee"] != actor and claim["surveyor"] != actor:
                raise DomainError("只有被分配的查勘人员可以录入结果", 403)
            if claim["status"] == "escalated" and claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件须先完成复核降险，不能直接提交查勘", 409)
            conn.execute(
                "INSERT INTO survey_notes(claim_id,surveyor,damage_ratio,findings,recommendation,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, actor, damage_ratio, findings.strip(), recommendation.strip(), utcnow()),
            )
            conn.execute("UPDATE claims SET status='survey',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "survey.recorded", {"damage_ratio": damage_ratio, "recommendation": recommendation})
            return dict(self._claim(conn, claim_id))

    def submit_review(self, actor: str, role: str, claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "提交核损")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "survey":
                raise DomainError("只有已查勘案件可以提交核损", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not conn.execute("SELECT 1 FROM survey_notes WHERE claim_id=?", (claim_id,)).fetchone():
                raise DomainError("缺少查勘记录", 409)
            conn.execute("UPDATE claims SET status='review',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "claim.review_submitted", {})
            return dict(self._claim(conn, claim_id))

    def emergency_advance(self, actor: str, role: str, claim_id: int, amount: float,
                          expected_version: int, reference: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "批准紧急预付")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("预付金额必须是数值") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not claim["urgent_need"]:
                raise DomainError("非紧急案件不能预付", 409)
            if claim["status"] in {"duplicate", "approved", "rejected", "closed"}:
                raise DomainError("当前案件状态不能预付", 409)
            if claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件不能预付", 409)
            limit = claim["estimated_loss"] * 0.2
            if amount <= 0 or amount > limit:
                raise DomainError("预付金额必须大于0且不超过预估损失的20%", 409)
            if claim["emergency_advance"] + amount > limit:
                raise DomainError("累计预付超过上限", 409)
            try:
                conn.execute(
                    "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, "emergency_advance", amount, actor, reference.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("付款参考号已存在", 409) from exc
            conn.execute("UPDATE claims SET emergency_advance=emergency_advance+?,version=version+1,updated_at=? WHERE id=?", (amount, utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "payment.emergency_advance", {"amount": amount, "reference": reference})
            return dict(self._claim(conn, claim_id))

    def finalize_claim(self, actor: str, role: str, claim_id: int, decision: str,
                       payout: float, expected_version: int, reason: str = "",
                       accept_uncovered: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor", "finance"}, "最终核定")
        if decision not in {"approve", "reject"}:
            raise DomainError("核定决定无效")
        try:
            payout = float(payout)
        except (TypeError, ValueError) as exc:
            raise DomainError("核定金额必须是数值") from exc
        if payout < 0:
            raise DomainError("核定金额不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "review":
                raise DomainError("只有待复核案件可以最终核定", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["duplicate_of"]:
                raise DomainError("重复报案不能核定赔付", 409)
            if claim["fraud_score"] >= 0.8 and decision == "approve":
                raise DomainError("高风险案件未解除风险标记，不能赔付", 409)
            if decision == "approve" and payout > claim["estimated_loss"]:
                raise DomainError("核定金额不能超过预估损失", 409)
            if decision == "reject" and not reason.strip():
                raise DomainError("拒赔必须填写理由", 409)
            allocation = None
            approved_payout = payout if decision == "approve" else 0
            if decision == "approve":
                allocation = self._allocate_payout(conn, claim["event_id"], approved_payout)
                if allocation["uncovered"] > CENT and not accept_uncovered:
                    raise DomainError(
                        "分保后仍有未覆盖赔付，核定被挡；确认自留缺口后可携带 accept_uncovered 重新提交",
                        409,
                        self._uncovered_payload(claim, allocation),
                    )
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, approved_payout, utcnow(), claim_id, expected_version),
            )
            if allocation is not None:
                self._save_cession(conn, claim, allocation)
            self._audit(conn, claim_id, actor, "claim.finalized", {
                "decision": decision, "payout": approved_payout, "reason": reason.strip(),
                "accept_uncovered": bool(accept_uncovered and allocation and allocation["uncovered"] > CENT),
            })
            result = dict(self._claim(conn, claim_id))
            if allocation is not None:
                result["cession"] = self._allocation_summary(allocation)
            return result

    def _treaty_for_event(self, conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM reinsurance_treaties WHERE event_id=?", (event_id,)).fetchone()

    def _treaty_layers(self, conn: sqlite3.Connection, treaty_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM reinsurance_layers WHERE treaty_id=? ORDER BY layer_order", (treaty_id,)
        ).fetchall()

    def _layer_used(self, conn: sqlite3.Connection, layer_ids: list[int]) -> dict[int, float]:
        used: dict[int, float] = {lid: 0.0 for lid in layer_ids}
        if not layer_ids:
            return used
        marks = ",".join("?" for _ in layer_ids)
        rows = conn.execute(
            "SELECT layer_id, COALESCE(SUM(absorbed),0) AS absorbed "
            "FROM cession_entries WHERE layer_id IN (%s) GROUP BY layer_id" % marks,
            layer_ids,
        ).fetchall()
        for row in rows:
            used[row["layer_id"]] = round(row["absorbed"], 2)
        return used

    def _allocate_payout(self, conn: sqlite3.Connection, event_id: str, payout: float,
                         extra_used: dict[int, float] | None = None,
                         base_used: dict[int, float] | None = None) -> dict[str, Any]:
        """Layered cession waterfall on a single payout.

        Retention is deducted first (company keeps it); each layer absorbs the
        incoming amount up to its remaining aggregate capacity, cedes
        cession_pct of the absorbed loss to the reinsurer, and rolls the
        remainder to the next layer. Anything left after every layer is full
        is marked uncovered.
        """
        payout = round(float(payout), 2)
        treaty = self._treaty_for_event(conn, event_id)
        entries: list[dict[str, Any]] = []
        if treaty is None:
            return {
                "event_id": event_id, "treaty": None, "payout": payout,
                "retention": payout, "uncovered": 0.0, "layers": [], "entries": entries,
            }
        retention = round(min(payout, treaty["retention"]), 2)
        if retention:
            entries.append({"bucket": "retention", "amount": retention})
        layers = self._treaty_layers(conn, treaty["id"])
        if base_used is not None:
            used = dict(base_used)
        else:
            used = self._layer_used(conn, [layer["id"] for layer in layers])
        if extra_used:
            for lid, amount in extra_used.items():
                used[lid] = round(used.get(lid, 0.0) + amount, 2)
        remaining = round(payout - retention, 2)
        layer_results: list[dict[str, Any]] = []
        for layer in layers:
            remaining_cap = round(layer["payout_cap"] - used.get(layer["id"], 0.0), 2)
            absorbed = round(max(0.0, min(remaining, remaining_cap)), 2)
            if absorbed > 0:
                paid = round(absorbed * layer["cession_pct"], 2)
                kept = round(absorbed - paid, 2)
                overflow = round(absorbed - remaining, 2) if remaining < absorbed else 0.0
                entries.append({
                    "bucket": "layer", "layer_id": layer["id"], "layer_order": layer["layer_order"],
                    "reinsurer": layer["reinsurer"], "layer_name": layer["name"],
                    "absorbed": absorbed, "amount": paid, "kept": kept, "cession_pct": layer["cession_pct"],
                })
            else:
                paid = kept = 0.0
                overflow = round(remaining, 2) if remaining > CENT else 0.0
            layer_remaining = round(remaining - absorbed, 2)
            exhausted = remaining_cap <= CENT and remaining > CENT
            result = {
                "layer_id": layer["id"], "layer_order": layer["layer_order"], "name": layer["name"],
                "reinsurer": layer["reinsurer"], "cession_pct": layer["cession_pct"],
                "payout_cap": round(layer["payout_cap"], 2), "used_before": used.get(layer["id"], 0.0),
                "incoming": round(remaining, 2), "absorbed": absorbed, "ceded": paid,
                "company_share": kept, "overflow": overflow, "remaining_capacity": round(max(0.0, remaining_cap - absorbed), 2),
                "exhausted": exhausted,
            }
            layer_results.append(result)
            if exhausted:
                result["overflow_note"] = "本层累计上限已满，差额继续下滚"
            remaining = layer_remaining
        uncovered = round(max(0.0, remaining), 2)
        if uncovered > CENT:
            entries.append({"bucket": "uncovered", "amount": uncovered})
        return {
            "event_id": event_id, "treaty": {"id": treaty["id"], "treaty_no": treaty["treaty_no"], "name": treaty["name"], "retention": round(treaty["retention"], 2)},
            "payout": payout, "retention": retention, "uncovered": uncovered,
            "layers": layer_results, "entries": entries,
        }

    def _allocation_summary(self, allocation: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": allocation["event_id"],
            "treaty_no": allocation["treaty"]["treaty_no"] if allocation["treaty"] else None,
            "retention": allocation["retention"],
            "ceded": round(sum(e["amount"] for e in allocation["entries"] if e["bucket"] == "layer"), 2),
            "uncovered": allocation["uncovered"],
            "layers": allocation["layers"],
            "entries": allocation["entries"],
        }

    def _uncovered_payload(self, claim: sqlite3.Row, allocation: dict[str, Any]) -> dict[str, Any]:
        blockers = [
            {
                "reason": "layer_exhausted", "layer_order": layer["layer_order"],
                "layer_name": layer["name"], "reinsurer": layer["reinsurer"],
                "overflow": layer["overflow"],
            }
            for layer in allocation["layers"]
            if layer["overflow"] > CENT
        ]
        if allocation["uncovered"] > CENT:
            blockers.append({
                "reason": "uncovered",
                "overflow": allocation["uncovered"],
            })
        return {
            "claim_id": claim["id"], "claim_no": claim["claim_no"], "event_id": claim["event_id"],
            "payout": allocation["payout"], "retention": allocation["retention"],
            "uncovered": allocation["uncovered"], "blockers": blockers,
            "layers": [
                {
                    "layer_order": layer["layer_order"], "name": layer["name"], "reinsurer": layer["reinsurer"],
                    "payout_cap": layer["payout_cap"], "used_before": layer["used_before"],
                    "incoming": layer["incoming"], "absorbed": layer["absorbed"],
                    "ceded": layer["ceded"], "overflow": layer["overflow"],
                    "remaining_capacity": layer["remaining_capacity"], "exhausted": layer["exhausted"],
                }
                for layer in allocation["layers"]
            ],
        }

    def _save_cession(self, conn: sqlite3.Connection, claim: sqlite3.Row, allocation: dict[str, Any]) -> None:
        now = utcnow()
        for entry in allocation["entries"]:
            conn.execute(
                """INSERT INTO cession_entries(claim_id,event_id,layer_id,bucket,layer_order,reinsurer,
                   layer_name,amount,absorbed,cession_pct,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    claim["id"], claim["event_id"], entry.get("layer_id"), entry["bucket"],
                    entry.get("layer_order"), entry.get("reinsurer"), entry.get("layer_name"),
                    round(entry["amount"], 2), entry.get("absorbed"), entry.get("cession_pct"), now,
                ),
            )

    def _normalize_layers(self, layers: Any) -> list[dict[str, Any]]:
        if not isinstance(layers, list) or not layers:
            raise DomainError("至少配置一个合约层")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(layers, start=1):
            if not isinstance(raw, dict):
                raise DomainError("第%d层配置必须是对象" % index)
            order = raw.get("layer_order", index)
            try:
                order = int(order)
            except (TypeError, ValueError) as exc:
                raise DomainError("层序号必须是整数") from exc
            reinsurer = str(raw.get("reinsurer", "")).strip()
            if not reinsurer:
                raise DomainError("第%d层必须填写再保人" % order)
            pct = valid_pct(raw.get("cession_pct"), "第%d层分保比例" % order)
            cap = valid_money(raw.get("payout_cap"), "第%d层赔付上限" % order)
            if cap <= 0:
                raise DomainError("第%d层赔付上限必须大于0" % order)
            name = str(raw.get("name", "")).strip() or ("第%d层" % order)
            normalized.append({
                "layer_order": order, "name": name, "reinsurer": reinsurer,
                "cession_pct": pct, "payout_cap": cap,
            })
        normalized.sort(key=lambda item: item["layer_order"])
        orders = [item["layer_order"] for item in normalized]
        if len(set(orders)) != len(orders):
            raise DomainError("合约层序号不能重复")
        return normalized

    def create_treaty(self, actor: str, role: str, event_id: str, treaty_no: str,
                      name: str, retention: float, layers: list[dict[str, Any]]) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor", "finance"}, "配置再保合约")
        event_id = str(event_id or "").strip()
        treaty_no = str(treaty_no or "").strip()
        name = str(name or "").strip()
        if not event_id or not treaty_no or not name:
            raise DomainError("事件编号、合约编号、合约名称不能为空")
        retention_value = valid_money(retention, "自留额")
        if retention_value < 0:
            raise DomainError("自留额不能为负数")
        normalized = self._normalize_layers(layers)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._treaty_for_event(conn, event_id):
                raise DomainError("该灾害事件已存在再保合约", 409)
            now = utcnow()
            try:
                cur = conn.execute(
                    """INSERT INTO reinsurance_treaties(event_id,treaty_no,name,retention,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (event_id, treaty_no, name, retention_value, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("再保合约编号已存在", 409) from exc
            for layer in normalized:
                conn.execute(
                    """INSERT INTO reinsurance_layers(treaty_id,layer_order,name,reinsurer,cession_pct,payout_cap,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (cur.lastrowid, layer["layer_order"], layer["name"], layer["reinsurer"],
                     layer["cession_pct"], layer["payout_cap"], now),
                )
            self._audit(conn, None, actor, "treaty.created", {"event_id": event_id, "treaty_no": treaty_no, "layers": len(normalized)})
            return self._treaty_state(conn, event_id)

    def add_treaty_layer(self, actor: str, role: str, event_id: str, layer_order: int,
                         reinsurer: str, cession_pct: float, payout_cap: float,
                         name: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor", "finance"}, "新增合约层")
        reinsurer = str(reinsurer or "").strip()
        if not reinsurer:
            raise DomainError("再保人不能为空")
        try:
            layer_order = int(layer_order)
        except (TypeError, ValueError) as exc:
            raise DomainError("层序号必须是整数") from exc
        pct = valid_pct(cession_pct, "分保比例")
        cap = valid_money(payout_cap, "赔付上限")
        if cap <= 0:
            raise DomainError("赔付上限必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            treaty = self._treaty_for_event(conn, event_id)
            if not treaty:
                raise DomainError("该事件尚未配置再保合约", 404)
            if conn.execute("SELECT 1 FROM reinsurance_layers WHERE treaty_id=? AND layer_order=?", (treaty["id"], layer_order)).fetchone():
                raise DomainError("第%d层已存在" % layer_order, 409)
            conn.execute(
                "INSERT INTO reinsurance_layers(treaty_id,layer_order,name,reinsurer,cession_pct,payout_cap,created_at) VALUES(?,?,?,?,?,?,?)",
                (treaty["id"], layer_order, name.strip() or ("第%d层" % layer_order), reinsurer, pct, cap, utcnow()),
            )
            conn.execute("UPDATE reinsurance_treaties SET updated_at=? WHERE id=?", (utcnow(), treaty["id"]))
            self._audit(conn, None, actor, "treaty.layer_added", {"event_id": event_id, "layer_order": layer_order})
            return self._treaty_state(conn, event_id)

    def update_treaty_layer(self, actor: str, role: str, event_id: str, layer_order: int,
                            reinsurer: str | None = None, cession_pct: float | None = None,
                            payout_cap: float | None = None, name: str | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor", "finance"}, "修改合约层")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            treaty = self._treaty_for_event(conn, event_id)
            if not treaty:
                raise DomainError("该事件尚未配置再保合约", 404)
            layer = conn.execute(
                "SELECT * FROM reinsurance_layers WHERE treaty_id=? AND layer_order=?",
                (treaty["id"], layer_order),
            ).fetchone()
            if not layer:
                raise DomainError("合约层不存在", 404)
            updates: dict[str, Any] = {}
            if reinsurer is not None:
                reinsurer = str(reinsurer).strip()
                if not reinsurer:
                    raise DomainError("再保人不能为空")
                updates["reinsurer"] = reinsurer
            if cession_pct is not None:
                updates["cession_pct"] = valid_pct(cession_pct, "分保比例")
            if payout_cap is not None:
                cap = valid_money(payout_cap, "赔付上限")
                if cap <= 0:
                    raise DomainError("赔付上限必须大于0")
                used = self._layer_used(conn, [layer["id"]])[layer["id"]]
                if cap < used - CENT:
                    raise DomainError("第%d层已占用赔付%.2f，新上限不能低于已占用金额" % (layer_order, used), 409,
                                      {"layer_order": layer_order, "used": used, "payout_cap": cap})
                updates["payout_cap"] = cap
            if name is not None:
                name = str(name).strip()
                if not name:
                    raise DomainError("层名称不能为空")
                updates["name"] = name
            if not updates:
                raise DomainError("没有需要更新的字段")
            assignments = ",".join("%s=?" % key for key in updates)
            conn.execute(
                "UPDATE reinsurance_layers SET %s WHERE id=?" % assignments,
                (*updates.values(), layer["id"]),
            )
            conn.execute("UPDATE reinsurance_treaties SET updated_at=? WHERE id=?", (utcnow(), treaty["id"]))
            self._audit(conn, None, actor, "treaty.layer_updated", {"event_id": event_id, "layer_order": layer_order, "fields": sorted(updates)})
            return self._treaty_state(conn, event_id)

    def preview_cession(self, role: str, event_id: str, payout: float) -> dict[str, Any]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor", "finance"}:
            raise DomainError("角色无权预览分保", 403)
        payout_value = valid_money(payout, "赔付金额")
        if payout_value < 0:
            raise DomainError("赔付金额不能为负数")
        with self.connect() as conn:
            allocation = self._allocate_payout(conn, str(event_id or "").strip(), payout_value)
            return self._allocation_summary(allocation)

    def recompute_cession(self, actor: str, role: str, event_id: str,
                          accept_uncovered: bool = False, commit: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor", "finance"}, "重算分保台账")
        event_id = str(event_id or "").strip()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            treaty = self._treaty_for_event(conn, event_id)
            if not treaty:
                raise DomainError("该事件尚未配置再保合约", 404)
            claims = conn.execute(
                "SELECT * FROM claims WHERE event_id=? AND status='approved' AND final_payout>0 ORDER BY id",
                (event_id,),
            ).fetchall()
            used: dict[int, float] = {layer["id"]: 0.0 for layer in self._treaty_layers(conn, treaty["id"])}
            results: list[dict[str, Any]] = []
            blockers: list[dict[str, Any]] = []
            total_uncovered = 0.0
            for claim in claims:
                allocation = self._allocate_payout(conn, event_id, claim["final_payout"], base_used=used)
                for entry in allocation["entries"]:
                    if entry["bucket"] == "layer":
                        used[entry["layer_id"]] = round(used.get(entry["layer_id"], 0.0) + entry["absorbed"], 2)
                for layer in allocation["layers"]:
                    if layer["overflow"] > CENT:
                        blockers.append({
                            "reason": "layer_exhausted", "claim_id": claim["id"], "claim_no": claim["claim_no"],
                            "layer_order": layer["layer_order"], "layer_name": layer["name"],
                            "reinsurer": layer["reinsurer"], "overflow": layer["overflow"],
                        })
                if allocation["uncovered"] > CENT:
                    total_uncovered = round(total_uncovered + allocation["uncovered"], 2)
                    blockers.append({
                        "reason": "uncovered", "claim_id": claim["id"], "claim_no": claim["claim_no"],
                        "overflow": allocation["uncovered"],
                    })
                results.append({
                    "claim_id": claim["id"], "claim_no": claim["claim_no"], "payout": allocation["payout"],
                    "retention": allocation["retention"], "uncovered": allocation["uncovered"],
                    "layers": allocation["layers"], "entries": allocation["entries"],
                })
            payload = {
                "event_id": event_id, "claim_count": len(results),
                "total_uncovered": total_uncovered, "blockers": blockers, "results": results,
                "committed": False,
            }
            if total_uncovered > CENT and not accept_uncovered:
                raise DomainError("重算存在未覆盖差额，操作被挡；确认后可携带 accept_uncovered 落账", 409, payload)
            if not commit:
                return payload
            conn.execute("DELETE FROM cession_entries WHERE event_id=?", (event_id,))
            for claim, result in zip(claims, results):
                allocation = next(item for item in results if item["claim_id"] == claim["id"])
                self._save_cession(conn, claim, allocation)
            payload["committed"] = True
            self._audit(conn, None, actor, "cession.recomputed", {
                "event_id": event_id, "claims": len(results),
                "uncovered": total_uncovered, "accept_uncovered": bool(blockers),
            })
            conn.commit()
            return payload

    def _treaty_state(self, conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
        treaty = self._treaty_for_event(conn, event_id)
        if not treaty:
            raise DomainError("该事件尚未配置再保合约", 404)
        layers = self._treaty_layers(conn, treaty["id"])
        used = self._layer_used(conn, [layer["id"] for layer in layers])
        layer_rows = []
        for layer in layers:
            absorbed = used.get(layer["id"], 0.0)
            layer_rows.append({
                "id": layer["id"], "layer_order": layer["layer_order"], "name": layer["name"],
                "reinsurer": layer["reinsurer"], "cession_pct": layer["cession_pct"],
                "payout_cap": round(layer["payout_cap"], 2), "absorbed_used": absorbed,
                "ceded_used": round(absorbed * layer["cession_pct"], 2),
                "remaining_capacity": round(max(0.0, layer["payout_cap"] - absorbed), 2),
            })
        return {"treaty": dict(treaty), "layers": layer_rows}

    def reinsurance_state(self, role: str, event_id: str | None = None) -> dict[str, Any]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor", "finance"}:
            raise DomainError("角色无权查看再保台账", 403)
        with self.connect() as conn:
            treaty_rows = conn.execute("SELECT * FROM reinsurance_treaties ORDER BY id").fetchall()
            treaties = []
            events = []
            wanted = {str(event_id).strip()} if event_id else None
            for treaty in treaty_rows:
                if wanted and treaty["event_id"] not in wanted:
                    continue
                events.append(treaty["event_id"])
                treaties.append(self._treaty_state(conn, treaty["event_id"]))
            if wanted and not events:
                raise DomainError("该事件尚未配置再保合约", 404)
            entries: list[dict[str, Any]] = []
            claims_by_id: dict[int, sqlite3.Row] = {}
            if events:
                marks = ",".join("?" for _ in events)
                rows = conn.execute(
                    "SELECT * FROM cession_entries WHERE event_id IN (%s) ORDER BY claim_id,id" % marks, events
                ).fetchall()
                entries = [dict(row) for row in rows]
                claim_ids = sorted({row["claim_id"] for row in rows})
                if claim_ids:
                    cmarks = ",".join("?" for _ in claim_ids)
                    claims_by_id = {
                        row["id"]: row for row in conn.execute(
                            "SELECT id,claim_no,event_id,final_payout,status FROM claims WHERE id IN (%s)" % cmarks,
                            claim_ids,
                        ).fetchall()
                    }
            ledger = []
            for entry in entries:
                claim = claims_by_id.get(entry["claim_id"])
                ledger.append({**entry, "claim_no": claim["claim_no"] if claim else None})
            event_summary = []
            for state in treaties:
                eid = state["treaty"]["event_id"]
                own_entries = [e for e in entries if e["event_id"] == eid]
                payout_total = round(sum(
                    row["final_payout"] or 0.0 for row in claims_by_id.values() if row["event_id"] == eid
                ), 2)
                event_summary.append({
                    "event_id": eid,
                    "payout_total": payout_total,
                    "retention_total": round(sum(e["amount"] for e in own_entries if e["bucket"] == "retention"), 2),
                    "ceded_total": round(sum(e["amount"] for e in own_entries if e["bucket"] == "layer"), 2),
                    "uncovered_total": round(sum(e["amount"] for e in own_entries if e["bucket"] == "uncovered"), 2),
                    "entry_count": len(own_entries),
                })
            return {"treaties": treaties, "ledger": ledger, "summary": event_summary}

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor", "finance"}:
            raise DomainError("角色无权查看理赔队列", 403)
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE (assignee=? OR surveyor=?) AND status NOT IN ('approved','rejected','duplicate') ORDER BY priority_score DESC,created_at",
                    (actor, actor),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,created_at").fetchall()
        return [dict(r) for r in rows]

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        allowed = role in {"intake", "supervisor", "adjuster", "surveyor", "auditor", "finance"}
        if not allowed:
            return {"claims": [], "evidence": [], "payments": [], "timeline": [], "access_limited": True}
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                claims = [dict(r) for r in conn.execute(
                    "SELECT * FROM claims WHERE assignee=? OR surveyor=? ORDER BY priority_score DESC,id DESC", (actor, actor)
                ).fetchall()]
            else:
                claims = [dict(r) for r in conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,id DESC").fetchall()]
            ids = [c["id"] for c in claims]
            if ids:
                marks = ",".join("?" for _ in ids)
                evidence = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline WHERE claim_id IN (%s) ORDER BY id DESC LIMIT 300" % marks, ids).fetchall()]
            else:
                evidence, payments, timeline = [], [], []
        return {"claims": claims, "evidence": evidence, "payments": payments, "timeline": timeline, "access_limited": False}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM claims").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        c1 = self.create_claim("intake-demo", "intake", "CLM-DEMO-001", "TY2026", "沿海A区", "洪水", "P-1001", "R-01", 30.1, 121.2, 500000, True, True)
        self.create_claim("intake-demo", "intake", "CLM-DEMO-002", "TY2026", "沿海A区", "洪水", "P-1002", "R-02", 30.2, 121.3, 240000, False, False)
        try:
            self.create_treaty(
                "finance-demo", "finance", "TY2026", "TR-TY2026", "台风2026分层超赔合约", 100000,
                [
                    {"layer_order": 1, "name": "第一层", "reinsurer": "中再产险", "cession_pct": 0.8, "payout_cap": 300000},
                    {"layer_order": 2, "name": "第二层", "reinsurer": "瑞再", "cession_pct": 0.9, "payout_cap": 500000},
                ],
            )
        except DomainError:
            pass
        return {"seeded": True, "first_claim_id": c1["id"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: CatastropheClaimService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "catastrophe-claims"})
            elif path == "/api/state":
                self._send(200, self.service.state(*self._headers()))
            elif path == "/api/queue":
                actor, role = self._headers()
                self._send(200, {"queue": self.service.queue(role, actor)})
            elif path == "/api/reinsurance/state":
                actor, role = self._headers()
                query = parse_qs(urlparse(self.path).query)
                event_id = query.get("event_id", [None])[0]
                self._send(200, self.service.reinsurance_state(role, event_id))
            elif path == "/api/reinsurance/preview":
                actor, role = self._headers()
                query = parse_qs(urlparse(self.path).query)
                event_id = query.get("event_id", [""])[0]
                payout = query.get("payout", ["0"])[0]
                self._send(200, self.service.preview_cession(role, event_id, payout))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            payload = {"error": str(exc)}
            if exc.details:
                payload["details"] = exc.details
            self._send(exc.status, payload)

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/claims":
                result = self.service.create_claim(actor, role, **data)
            elif path == "/api/claims/triage":
                result = self.service.triage_claim(actor, role, **data)
            elif path == "/api/claims/assign":
                result = self.service.assign_claim(actor, role, **data)
            elif path == "/api/evidence":
                result = self.service.add_evidence(actor, role, **data)
            elif path == "/api/claims/survey":
                result = self.service.record_survey(actor, role, **data)
            elif path == "/api/claims/submit-review":
                result = self.service.submit_review(actor, role, **data)
            elif path == "/api/claims/emergency-advance":
                result = self.service.emergency_advance(actor, role, **data)
            elif path == "/api/claims/finalize":
                result = self.service.finalize_claim(actor, role, **data)
            elif path == "/api/reinsurance/treaties":
                result = self.service.create_treaty(actor, role, **data)
            elif path == "/api/reinsurance/layers/add":
                result = self.service.add_treaty_layer(actor, role, **data)
            elif path == "/api/reinsurance/layers/update":
                result = self.service.update_treaty_layer(actor, role, **data)
            elif path == "/api/reinsurance/recompute":
                result = self.service.recompute_cession(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            payload = {"error": str(exc)}
            if exc.details:
                payload["details"] = exc.details
            self._send(exc.status, payload)
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: CatastropheClaimService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Catastrophe claim service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="巨灾保险理赔调度服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8207)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = CatastropheClaimService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
