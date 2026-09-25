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
STAFF_ROLES = {"intake", "adjuster", "surveyor", "supervisor", "auditor", "finance"}
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
    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CENT_EPS = 0.005


def money(value: float) -> float:
    """金额按分取整。"""
    return round(float(value) + 1e-9, 2)


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
                CREATE TABLE IF NOT EXISTS reins_treaties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    retention REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reins_layers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES reins_treaties(id),
                    layer_order INTEGER NOT NULL,
                    reinsurer TEXT NOT NULL,
                    cede_ratio REAL NOT NULL,
                    payout_limit REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(treaty_id,layer_order)
                );
                CREATE TABLE IF NOT EXISTS cession_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES reins_treaties(id),
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    bucket TEXT NOT NULL,
                    layer_id INTEGER REFERENCES reins_layers(id),
                    gross_amount REAL NOT NULL,
                    cede_ratio REAL NOT NULL,
                    ceded_amount REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(treaty_id,claim_id,bucket,layer_id)
                );
                CREATE TABLE IF NOT EXISTS cession_overflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES reins_treaties(id),
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    layer_id INTEGER REFERENCES reins_layers(id),
                    bucket TEXT NOT NULL,
                    amount REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                CREATE INDEX IF NOT EXISTS idx_cession_claim ON cession_entries(claim_id);
                CREATE INDEX IF NOT EXISTS idx_cession_treaty ON cession_entries(treaty_id, claim_id);
                CREATE INDEX IF NOT EXISTS idx_overflow_treaty ON cession_overflows(treaty_id);
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
                       payout: float, expected_version: int, reason: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "最终核定")
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
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, payout if decision == "approve" else 0, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.finalized", {"decision": decision, "payout": payout, "reason": reason.strip()})
            if decision == "approve" and money(payout) > 0:
                self._recompute_event_ledger(conn, claim["event_id"], actor)
            return dict(self._claim(conn, claim_id))

    # ----- 再保分保台账 -----

    def _trait_row(self, conn: sqlite3.Connection, event_id: str, lock: bool = False) -> sqlite3.Row | None:
        sql = "SELECT * FROM reins_treaties WHERE event_id=?"
        if lock:
            sql += " ORDER BY id LIMIT 1"
        return conn.execute(sql, (event_id,)).fetchone()

    def _require_treaty(self, conn: sqlite3.Connection, treaty_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM reins_treaties WHERE id=?", (treaty_id,)).fetchone()
        if not row:
            raise DomainError("再保合约不存在", 404)
        return row

    def _trait_layers(self, conn: sqlite3.Connection, treaty_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM reins_layers WHERE treaty_id=? ORDER BY layer_order", (treaty_id,)
        ).fetchall()

    def create_treaty(self, actor: str, role: str, event_id: str, retention: float,
                      layers: list[dict[str, Any]]) -> dict[str, Any]:
        """按灾害事件建立分层分保合约：自留额先扣，其后按层顺序摊赔。"""
        actor = actor_id(actor)
        require_role(role, {"finance", "supervisor"}, "维护再保合约")
        event_id = (event_id or "").strip()
        if not event_id:
            raise DomainError("灾害事件编号不能为空")
        try:
            retention = money(retention)
        except (TypeError, ValueError) as exc:
            raise DomainError("自留额必须是数值") from exc
        if retention < 0:
            raise DomainError("自留额不能为负数")
        if not isinstance(layers, list) or not layers:
            raise DomainError("至少配置一个分保层")
        normalized: list[tuple[int, str, float, float]] = []
        orders: list[int] = []
        for index, layer in enumerate(layers):
            if not isinstance(layer, dict):
                raise DomainError("分保层配置格式无效")
            order = layer.get("layer_order", index + 1)
            if not isinstance(order, int) or order < 1:
                raise DomainError("分保层顺序必须是正整数")
            reinsurer = (layer.get("reinsurer") or "").strip()
            if not reinsurer:
                raise DomainError("分保层再保人不能为空")
            try:
                ratio = float(layer["cede_ratio"])
                limit = money(layer["payout_limit"])
            except (TypeError, ValueError) as exc:
                raise DomainError("分保比例或赔付上限必须是数值") from exc
            if not 0 < ratio <= 1:
                raise DomainError("分保比例必须在 0 到 1 之间（不含0）")
            if limit <= 0:
                raise DomainError("分保层赔付上限必须大于0")
            if order in orders:
                raise DomainError("分保层顺序不能重复：%d" % order)
            orders.append(order)
            normalized.append((order, reinsurer, ratio, limit))
        normalized.sort(key=lambda item: item[0])
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._trait_row(conn, event_id):
                raise DomainError("该灾害事件已存在分保合约", 409)
            now = utcnow()
            cur = conn.execute(
                """INSERT INTO reins_treaties(event_id,retention,status,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (event_id, retention, "pending", actor, now, now),
            )
            treaty_id = cur.lastrowid
            for order, reinsurer, ratio, limit in normalized:
                conn.execute(
                    """INSERT INTO reins_layers(treaty_id,layer_order,reinsurer,cede_ratio,payout_limit,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (treaty_id, order, reinsurer, ratio, limit, now),
                )
            self._audit(conn, None, actor, "reinsurance.treaty_created", {
                "treaty_id": treaty_id, "event_id": event_id, "retention": retention,
                "layers": [{"order": o, "reinsurer": r, "cede_ratio": x, "payout_limit": y}
                           for o, r, x, y in normalized],
            })
            recompute = self._recompute_event_ledger(conn, event_id, actor)
            return self._ledger_view(conn, treaty_id, recompute=recompute)

    def _recompute_event_ledger(self, conn: sqlite3.Connection, event_id: str, actor: str = "system") -> dict[str, Any] | None:
        """对同一灾害事件的全部已核案件重算分保瀑布，结果覆写到台账。

        规则：自留额先扣；每层按“分保比例 + 事件累计赔付上限”吸收毛赔款，
        摊回=毛额×比例；本层占满后差额滚入下一层；全部层占满仍剩余则记未覆盖。
        层上限是事件内全部已核案件共享的，案件按核定先后累计占用。
        """
        treaty = self._trait_row(conn, event_id, lock=True)
        if not treaty:
            return None
        treaty_id = treaty["id"]
        layers = self._trait_layers(conn, treaty_id)
        claims = conn.execute(
            """SELECT * FROM claims WHERE event_id=? AND status='approved' AND final_payout>0
               ORDER BY updated_at,id""",
            (event_id,),
        ).fetchall()
        conn.execute("DELETE FROM cession_entries WHERE treaty_id=?", (treaty_id,))
        conn.execute("DELETE FROM cession_overflows WHERE treaty_id=?", (treaty_id,))
        now = utcnow()
        remaining_limit = [money(layer["payout_limit"]) for layer in layers]
        retention_left = money(treaty["retention"])
        totals = {"retention": 0.0, "ceded": 0.0, "uncovered": 0.0, "gross": 0.0}
        overflow_rows: list[dict[str, Any]] = []

        def add_entry(claim_id: int, bucket: str, layer_id: int | None, gross: float, ratio: float, ceded: float) -> None:
            if gross <= CENT_EPS:
                return
            conn.execute(
                """INSERT INTO cession_entries(treaty_id,claim_id,bucket,layer_id,gross_amount,cede_ratio,ceded_amount,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (treaty_id, claim_id, bucket, layer_id, money(gross), ratio, money(ceded), now),
            )

        def add_overflow(claim_id: int, layer_id: int | None, bucket: str, amount: float) -> None:
            if amount <= CENT_EPS:
                return
            conn.execute(
                "INSERT INTO cession_overflows(treaty_id,claim_id,layer_id,bucket,amount,created_at) VALUES(?,?,?,?,?,?)",
                (treaty_id, claim_id, layer_id, bucket, money(amount), now),
            )
            overflow_rows.append({
                "claim_id": claim_id, "layer_id": layer_id, "bucket": bucket, "amount": money(amount),
            })

        for claim in claims:
            payout = money(claim["final_payout"])
            totals["gross"] = money(totals["gross"] + payout)
            # 自留额先扣
            to_retention = money(min(payout, retention_left))
            add_entry(claim["id"], "retention", None, to_retention, 0.0, 0.0)
            retention_left = money(retention_left - to_retention)
            totals["retention"] = money(totals["retention"] + to_retention)
            residual = money(payout - to_retention)
            # 逐层分保，层差额滚到下一层
            for idx, layer in enumerate(layers):
                if residual <= CENT_EPS:
                    break
                if remaining_limit[idx] <= CENT_EPS:
                    continue
                gross_fit = money(remaining_limit[idx] / float(layer["cede_ratio"]))
                absorbed = money(min(residual, gross_fit))
                ceded = money(absorbed * float(layer["cede_ratio"]))
                add_entry(claim["id"], "layer", layer["id"], absorbed, float(layer["cede_ratio"]), ceded)
                remaining_limit[idx] = money(remaining_limit[idx] - ceded)
                totals["ceded"] = money(totals["ceded"] + ceded)
                spill = money(residual - absorbed)
                if spill > CENT_EPS:
                    add_overflow(claim["id"], layer["id"], "layer", spill)
                residual = spill
            if residual > CENT_EPS:
                add_entry(claim["id"], "uncovered", None, residual, 0.0, 0.0)
                add_overflow(claim["id"], None, "uncovered", residual)
                totals["uncovered"] = money(totals["uncovered"] + residual)

        new_status = "pending"
        if treaty["status"] == "confirmed" and totals["gross"] > CENT_EPS:
            # 后续核定推高层占用，已确认台账退回待确认
            new_status = "pending"
            conn.execute(
                "UPDATE reins_treaties SET status='pending',version=version+1,updated_at=? WHERE id=?",
                (now, treaty_id),
            )
            self._audit(conn, None, actor, "reinsurance.ledger_reopened", {
                "event_id": event_id, "uncovered": totals["uncovered"],
            })
        return {
            "event_id": event_id,
            "gross_total": totals["gross"],
            "retention_total": totals["retention"],
            "ceded_total": totals["ceded"],
            "uncovered_total": totals["uncovered"],
            "remaining_limits": remaining_limit,
            "overflows": overflow_rows,
            "status": new_status,
        }

    def _ledger_view(self, conn: sqlite3.Connection, treaty_id: int,
                     recompute: dict[str, Any] | None = None) -> dict[str, Any]:
        treaty = self._require_treaty(conn, treaty_id)
        layers = [dict(r) for r in self._trait_layers(conn, treaty_id)]
        layer_map = {layer["id"]: layer for layer in layers}
        entries = [dict(r) for r in conn.execute(
            "SELECT * FROM cession_entries WHERE treaty_id=? ORDER BY claim_id,id", (treaty_id,)
        ).fetchall()]
        overflows = [dict(r) for r in conn.execute(
            "SELECT * FROM cession_overflows WHERE treaty_id=? ORDER BY claim_id,id", (treaty_id,)
        ).fetchall()]
        claims = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM claims WHERE event_id=? ORDER BY id", (treaty["event_id"],)
        ).fetchall()}

        occupied = {layer["id"]: 0.0 for layer in layers}
        for entry in entries:
            if entry["bucket"] == "layer" and entry["layer_id"] in occupied:
                occupied[entry["layer_id"]] = money(occupied[entry["layer_id"]] + entry["ceded_amount"])
        for layer in layers:
            layer["occupied"] = occupied[layer["id"]]
            layer["remaining"] = money(layer["payout_limit"] - occupied[layer["id"]])
            layer["full"] = layer["remaining"] <= CENT_EPS

        retention_total = money(sum(e["gross_amount"] for e in entries if e["bucket"] == "retention"))
        ceded_total = money(sum(e["ceded_amount"] for e in entries if e["bucket"] == "layer"))
        uncovered_total = money(sum(e["gross_amount"] for e in entries if e["bucket"] == "uncovered"))
        gross_total = money(sum(e["gross_amount"] for e in entries))

        for overflow in overflows:
            if overflow["layer_id"] in layer_map:
                overflow["layer_order"] = layer_map[overflow["layer_id"]]["layer_order"]
                overflow["reinsurer"] = layer_map[overflow["layer_id"]]["reinsurer"]
            claim = claims.get(overflow["claim_id"])
            overflow["claim_no"] = claim["claim_no"] if claim else None

        by_claim: dict[int, list[dict[str, Any]]] = {}
        for entry in entries:
            row = dict(entry)
            if entry["layer_id"] in layer_map:
                row["layer_order"] = layer_map[entry["layer_id"]]["layer_order"]
                row["reinsurer"] = layer_map[entry["layer_id"]]["reinsurer"]
            by_claim.setdefault(entry["claim_id"], []).append(row)

        claim_rows = []
        for claim_id, rows in by_claim.items():
            claim = claims.get(claim_id, {})
            claim_rows.append({
                "claim_id": claim_id,
                "claim_no": claim.get("claim_no"),
                "final_payout": money(claim.get("final_payout") or 0),
                "retention": money(sum(r["gross_amount"] for r in rows if r["bucket"] == "retention")),
                "ceded": money(sum(r["ceded_amount"] for r in rows if r["bucket"] == "layer")),
                "uncovered": money(sum(r["gross_amount"] for r in rows if r["bucket"] == "uncovered")),
                "breakdown": rows,
            })

        return {
            "treaty": dict(treaty),
            "retention": money(treaty["retention"]),
            "retention_occupied": retention_total,
            "retention_remaining": money(treaty["retention"] - retention_total),
            "layers": layers,
            "totals": {
                "gross": gross_total,
                "retention": retention_total,
                "ceded": ceded_total,
                "uncovered": uncovered_total,
            },
            "claims": claim_rows,
            "overflows": overflows,
            "recompute": recompute,
        }

    def list_treaties(self, role: str, actor: str = "") -> list[dict[str, Any]]:
        require_role(role, STAFF_ROLES, "查看再保合约")
        with self.connect() as conn:
            treaties = conn.execute("SELECT * FROM reins_treaties ORDER BY id").fetchall()
            result = []
            for treaty in treaties:
                view = self._ledger_view(conn, treaty["id"])
                result.append({
                    "treaty": view["treaty"],
                    "totals": view["totals"],
                    "layer_count": len(view["layers"]),
                })
            return result

    def get_ledger(self, role: str, actor: str, event_id: str | None = None, treaty_id: int | None = None) -> dict[str, Any]:
        require_role(role, STAFF_ROLES, "查看再保分保台账")
        with self.connect() as conn:
            if treaty_id is not None:
                tid = int(treaty_id)
            else:
                row = self._trait_row(conn, (event_id or "").strip())
                if not row:
                    raise DomainError("该灾害事件没有分保合约", 404)
                tid = row["id"]
            return self._ledger_view(conn, tid)

    def confirm_ledger(self, actor: str, role: str, treaty_id: int, expected_version: int) -> dict[str, Any]:
        """财务保存确认分保结果；尚有案件超出层上限（未覆盖）时整体挡下并列出明细。"""
        actor = actor_id(actor)
        require_role(role, {"finance", "supervisor"}, "确认再保分保")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            treaty = self._require_treaty(conn, treaty_id)
            if treaty["version"] != int(expected_version):
                raise DomainError("合约已变化，请刷新后重试", 409)
            event_id = treaty["event_id"]
            recompute = self._recompute_event_ledger(conn, event_id, actor)
            view = self._ledger_view(conn, treaty_id, recompute=recompute)
            uncovered = view["totals"]["uncovered"]
            if uncovered > CENT_EPS:
                details = {
                    "treaty_id": treaty_id,
                    "event_id": event_id,
                    "uncovered_total": uncovered,
                    "blocked_claims": [
                        {
                            "claim_id": row["claim_id"],
                            "claim_no": row["claim_no"],
                            "final_payout": row["final_payout"],
                            "uncovered_amount": row["uncovered"],
                            "overflow_layers": [
                                {
                                    "layer_id": overflow["layer_id"],
                                    "layer_order": overflow.get("layer_order"),
                                    "reinsurer": overflow.get("reinsurer"),
                                    "bucket": overflow["bucket"],
                                    "amount": overflow["amount"],
                                }
                                for overflow in view["overflows"]
                                if overflow["claim_id"] == row["claim_id"] and overflow["amount"] > CENT_EPS
                            ],
                        }
                        for row in view["claims"] if row["uncovered"] > CENT_EPS
                    ],
                    "layers": [
                        {"layer_id": layer["id"], "layer_order": layer["layer_order"],
                         "reinsurer": layer["reinsurer"], "payout_limit": layer["payout_limit"],
                         "occupied": layer["occupied"], "remaining": layer["remaining"]}
                        for layer in view["layers"]
                    ],
                }
                raise DomainError(
                    "分保层上限不足，存在 %s 元未覆盖赔付，请扩容或调整合约后再确认" % format(uncovered, ".2f"),
                    409, details,
                )
            now = utcnow()
            conn.execute(
                "UPDATE reins_treaties SET status='confirmed',confirmed_by=?,confirmed_at=?,version=version+1,updated_at=? WHERE id=?",
                (actor, now, now, treaty_id),
            )
            self._audit(conn, None, actor, "reinsurance.ledger_confirmed", {
                "treaty_id": treaty_id, "event_id": event_id,
                "retention": view["totals"]["retention"], "ceded": view["totals"]["ceded"],
            })
            return self._ledger_view(conn, treaty_id)

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in STAFF_ROLES:
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
        allowed = role in STAFF_ROLES
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
        self.create_treaty("fin-demo", "finance", "TY2026", 100000, [
            {"layer_order": 1, "reinsurer": "中再集团", "cede_ratio": 0.8, "payout_limit": 200000},
            {"layer_order": 2, "reinsurer": "慕尼黑再", "cede_ratio": 0.9, "payout_limit": 300000},
        ])
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
            elif path == "/api/reinsurance/treaties":
                actor, role = self._headers()
                self._send(200, {"treaties": self.service.list_treaties(role, actor)})
            elif path == "/api/reinsurance/ledger":
                actor, role = self._headers()
                query = parse_qs(urlparse(self.path).query)
                event_id = query.get("event_id", [""])[0]
                treaty_id = query.get("treaty_id", [""])[0]
                try:
                    tid = int(treaty_id) if treaty_id else None
                except ValueError as exc:
                    raise DomainError("treaty_id 必须是整数") from exc
                self._send(200, self.service.get_ledger(role, actor, event_id=event_id, treaty_id=tid))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            payload: dict[str, Any] = {"error": str(exc)}
            if exc.details is not None:
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
            elif path == "/api/reinsurance/confirm":
                result = self.service.confirm_ledger(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            payload = {"error": str(exc)}
            if exc.details is not None:
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
