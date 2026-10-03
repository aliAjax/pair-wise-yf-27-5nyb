"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _tx_conn(self):
        """自动提交模式的连接，配合显式 BEGIN IMMEDIATE 使用，保证写事务真正串行化。"""
        conn = self.connect()
        conn.isolation_level = None
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_parties(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    display_name TEXT NOT NULL,
                    share REAL NOT NULL DEFAULT 0,
                    withdrawn INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS share_revisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    revision INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    total REAL NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 0,
                    changed_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id,revision)
                );
                CREATE TABLE IF NOT EXISTS share_distributions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('confirmed','void','pending')),
                    total REAL NOT NULL,
                    snapshot TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    confirmed_by TEXT REFERENCES users(id),
                    confirmed_at TEXT,
                    voided_by TEXT REFERENCES users(id),
                    voided_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            # 轻量迁移：为旧库的 claims 补 share_status 列。
            claim_cols = [r["name"] for r in conn.execute("PRAGMA table_info(claims)").fetchall()]
            if "share_status" not in claim_cols:
                conn.execute("ALTER TABLE claims ADD COLUMN share_status TEXT NOT NULL DEFAULT 'pending'")
            # 旧数据回填：没有份额记录的主张，按登记的主张人一人独占 100% 处理。
            self._backfill_legacy_shares(conn)

    def _backfill_legacy_shares(self, conn):
        legacy = conn.execute(
            """SELECT c.* FROM claims c
               WHERE NOT EXISTS (SELECT 1 FROM claim_parties cp WHERE cp.claim_id=c.id)"""
        ).fetchall()
        for c in legacy:
            ts = c["created_at"]
            conn.execute(
                """INSERT INTO claim_parties(claim_id,claimant_id,display_name,share,withdrawn,created_at,updated_at)
                   VALUES(?,?,?,100.0,0,?,?)""",
                (c["id"], c["claimant_id"], c["claimed_by"], ts, ts),
            )
            snapshot = [{"claimant_id": c["claimant_id"], "display_name": c["claimed_by"], "share": 100.0, "withdrawn": 0}]
            conn.execute(
                """INSERT INTO share_revisions(claim_id,revision,snapshot,total,complete,changed_by,created_at)
                   VALUES(?,1,?,100.0,1,?,?)""",
                (c["id"], json.dumps(snapshot, ensure_ascii=False), c["claimant_id"], ts),
            )
            conn.execute(
                """INSERT INTO share_distributions(claim_id,revision,status,total,snapshot,note,confirmed_at,created_at)
                   VALUES(?,1,'confirmed',100.0,?,?,?,?)""",
                (c["id"], json.dumps(snapshot, ensure_ascii=False), "旧数据回填：一人独占 100%", ts, ts),
            )
            conn.execute("UPDATE claims SET share_status='confirmed' WHERE id=?", (c["id"],))

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("claimant2", "共同主张人乙", "claimant"),
                    ("claimant3", "共同主张人丙", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        claims = [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()]
        for c in claims:
            c["parties"] = [dict(x) for x in conn.execute("SELECT * FROM claim_parties WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]
            c["share_distributions"] = [dict(x) for x in conn.execute("SELECT id,revision,status,total,note,confirmed_by,confirmed_at,voided_at,created_at FROM share_distributions WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": claims,
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            ts = now()
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at,share_status)
                   VALUES(?,?,?,?,?,?, 'confirmed')""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), ts, ts),
            )
            claim_id = cur.lastrowid
            # 新主张默认登记主张人一人独占 100%，与旧数据回填口径一致。
            snapshot = [{"claimant_id": user_id, "display_name": claimed_by.strip(), "share": 100.0, "withdrawn": 0}]
            conn.execute(
                """INSERT INTO claim_parties(claim_id,claimant_id,display_name,share,withdrawn,created_at,updated_at)
                   VALUES(?,?,?,100.0,0,?,?)""",
                (claim_id, user_id, claimed_by.strip(), ts, ts),
            )
            conn.execute(
                """INSERT INTO share_revisions(claim_id,revision,snapshot,total,complete,changed_by,created_at)
                   VALUES(?,1,?,100.0,1,?,?)""",
                (claim_id, json.dumps(snapshot, ensure_ascii=False), user_id, ts),
            )
            conn.execute(
                """INSERT INTO share_distributions(claim_id,revision,status,total,snapshot,note,confirmed_by,confirmed_at,created_at)
                   VALUES(?,1,'confirmed',100.0,?,?,?,?,?)""",
                (claim_id, json.dumps(snapshot, ensure_ascii=False), "新主张默认一人独占 100%", user_id, ts, ts),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": claim_id})
            return {"id": claim_id, "object_id": object_id, "status": "submitted", "share_status": "confirmed"}

    def _share_status(self, conn, claim_id):
        row = conn.execute(
            "SELECT status FROM share_distributions WHERE claim_id=? ORDER BY id DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
        return row["status"] if row else "pending"

    def _active_parties(self, conn, claim_id):
        return [dict(r) for r in conn.execute(
            "SELECT id,claimant_id,display_name,share,withdrawn FROM claim_parties WHERE claim_id=? AND withdrawn=0 ORDER BY id",
            (claim_id,),
        ).fetchall()]

    def _claim_share_view(self, conn, claim_id, include_details):
        parties = self._active_parties(conn, claim_id)
        total = round(sum(p["share"] for p in parties), 6)
        view = {
            "claim_id": claim_id,
            "share_status": self._share_status(conn, claim_id),
            "total": total,
            "parties": parties if include_details else [],
        }
        if include_details:
            view["revisions"] = [dict(r) for r in conn.execute(
                "SELECT id,revision,snapshot,total,complete,changed_by,created_at FROM share_revisions WHERE claim_id=? ORDER BY revision",
                (claim_id,),
            ).fetchall()]
            view["distributions"] = [dict(r) for r in conn.execute(
                "SELECT id,revision,status,total,note,confirmed_by,confirmed_at,voided_by,voided_at,created_at FROM share_distributions WHERE claim_id=? ORDER BY id",
                (claim_id,),
            ).fetchall()]
        return view

    def set_claim_shares(self, user_id, claim_id, parties):
        """登记/替换共同主张人的份额。改动即作废原确认并置为待补齐，随后尝试重新分配。"""
        with self._tx_conn() as conn:
            self._user(conn, user_id, {"reviewer"})
            conn.execute("BEGIN IMMEDIATE")
            try:
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                clean, seen, total = [], set(), 0.0
                for p in parties:
                    cid = str(p.get("claimant_id", "")).strip()
                    name = str(p.get("display_name", "")).strip()
                    try:
                        share = float(p.get("share"))
                    except (TypeError, ValueError):
                        raise BusinessError("份额必须是数字", 422, "invalid_share")
                    if not cid:
                        raise BusinessError("必须填写主张人账号", 422, "invalid_party")
                    u = conn.execute("SELECT * FROM users WHERE id=?", (cid,)).fetchone()
                    if not u:
                        raise BusinessError(f"主张人 {cid} 不存在", 404, "user_not_found")
                    if u["role"] != "claimant":
                        raise BusinessError(f"{cid} 不是主张人身份", 422, "invalid_party_role")
                    if cid in seen:
                        raise BusinessError(f"同一主张人 {cid} 不能重复登记", 422, "duplicate_party")
                    if share <= 0 or share > 100:
                        raise BusinessError("份额必须在 0 到 100 之间", 422, "invalid_share")
                    seen.add(cid)
                    clean.append({"claimant_id": cid, "display_name": name or u["name"], "share": share, "withdrawn": 0})
                    total += share
                total = round(total, 6)
                if total > 100 + 1e-9:
                    raise BusinessError("份额合计不能超过 100%", 422, "shares_exceed")
                complete = abs(total - 100.0) < 1e-9
                ts = now()
                # 替换当前份额（历史已在 share_revisions 中留档）。
                conn.execute("DELETE FROM claim_parties WHERE claim_id=?", (claim_id,))
                for c in clean:
                    conn.execute(
                        """INSERT INTO claim_parties(claim_id,claimant_id,display_name,share,withdrawn,created_at,updated_at)
                           VALUES(?,?,?,?,0,?,?)""",
                        (claim_id, c["claimant_id"], c["display_name"], c["share"], ts, ts),
                    )
                rev_row = conn.execute("SELECT COALESCE(MAX(revision),0)+1 AS r FROM share_revisions WHERE claim_id=?", (claim_id,)).fetchone()
                rev = rev_row["r"]
                conn.execute(
                    """INSERT INTO share_revisions(claim_id,revision,snapshot,total,complete,changed_by,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (claim_id, rev, json.dumps(clean, ensure_ascii=False), total, 1 if complete else 0, user_id, ts),
                )
                # 原确认先作废。
                conn.execute(
                    "UPDATE share_distributions SET status='void', voided_by=?, voided_at=? WHERE claim_id=? AND status='confirmed'",
                    (user_id, ts, claim_id),
                )
                # 重新分配：合计 100% 才确认，否则停在待补齐。
                if complete:
                    conn.execute(
                        """INSERT INTO share_distributions(claim_id,revision,status,total,snapshot,note,confirmed_by,confirmed_at,created_at)
                           VALUES(?,?,'confirmed',?,?,?,?,?,?)""",
                        (claim_id, rev, total, json.dumps(clean, ensure_ascii=False), "份额合计 100%，分配确认", user_id, ts, ts),
                    )
                    new_status = "confirmed"
                else:
                    conn.execute(
                        """INSERT INTO share_distributions(claim_id,revision,status,total,snapshot,note,created_at)
                           VALUES(?,?,'pending',?,?,?,?)""",
                        (claim_id, rev, total, json.dumps(clean, ensure_ascii=False), f"份额合计 {total:g}%，待补齐", ts),
                    )
                    new_status = "pending"
                conn.execute("UPDATE claims SET share_status=?, updated_at=? WHERE id=?", (new_status, ts, claim_id))
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, ts, claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.shares.set",
                            {"claim_id": claim_id, "revision": rev, "total": total, "status": new_status, "parties": len(clean)})
                return {"claim_id": claim_id, "revision": rev, "total": total, "share_status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def redistribute_claim(self, user_id, claim_id):
        """重新分配（失败后可重试）。当前份额合计 100% 才确认，否则保持待补齐。"""
        with self._tx_conn() as conn:
            self._user(conn, user_id, {"reviewer"})
            conn.execute("BEGIN IMMEDIATE")
            try:
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                current = self._share_status(conn, claim_id)
                if current == "confirmed":
                    rev_row = conn.execute(
                        "SELECT revision FROM share_distributions WHERE claim_id=? AND status='confirmed' ORDER BY id DESC LIMIT 1",
                        (claim_id,),
                    ).fetchone()
                    return {"claim_id": claim_id, "share_status": "confirmed",
                            "revision": rev_row["revision"] if rev_row else None,
                            "total": 100.0, "redistributed": False}
                parties = self._active_parties(conn, claim_id)
                total = round(sum(p["share"] for p in parties), 6)
                if abs(total - 100.0) > 1e-9:
                    raise BusinessError(f"份额合计 {total:g}% 不足 100%，待补齐后再重新分配", 409, "shares_incomplete")
                latest_rev = conn.execute("SELECT COALESCE(MAX(revision),0) AS r FROM share_revisions WHERE claim_id=?", (claim_id,)).fetchone()["r"]
                snapshot = [{"claimant_id": p["claimant_id"], "display_name": p["display_name"], "share": p["share"], "withdrawn": 0} for p in parties]
                ts = now()
                conn.execute(
                    "UPDATE share_distributions SET status='void', voided_by=?, voided_at=? WHERE claim_id=? AND status='confirmed'",
                    (user_id, ts, claim_id),
                )
                conn.execute(
                    """INSERT INTO share_distributions(claim_id,revision,status,total,snapshot,note,confirmed_by,confirmed_at,created_at)
                       VALUES(?,?,'confirmed',100.0,?,?,?,?,?)""",
                    (claim_id, latest_rev, json.dumps(snapshot, ensure_ascii=False), "份额补齐，重新分配确认", user_id, ts, ts),
                )
                conn.execute("UPDATE claims SET share_status='confirmed', updated_at=? WHERE id=?", (ts, claim_id))
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, ts, claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.redistribute",
                            {"claim_id": claim_id, "revision": latest_rev, "total": 100.0})
                return {"claim_id": claim_id, "share_status": "confirmed", "revision": latest_rev,
                        "total": 100.0, "redistributed": True, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def withdraw_party(self, user_id, claim_id, party_id):
        """标记某位共同主张人退出，其份额释放回待分配池。"""
        with self._tx_conn() as conn:
            self._user(conn, user_id, {"reviewer"})
            conn.execute("BEGIN IMMEDIATE")
            try:
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                party = conn.execute("SELECT * FROM claim_parties WHERE id=? AND claim_id=?", (party_id, claim_id)).fetchone()
                if not party:
                    raise BusinessError("共同主张人不存在", 404, "party_not_found")
                ts = now()
                conn.execute("UPDATE claim_parties SET withdrawn=1, updated_at=? WHERE id=?", (ts, party_id))
                parties = self._active_parties(conn, claim_id)
                total = round(sum(p["share"] for p in parties), 6)
                rev_row = conn.execute("SELECT COALESCE(MAX(revision),0)+1 AS r FROM share_revisions WHERE claim_id=?", (claim_id,)).fetchone()
                rev = rev_row["r"]
                conn.execute(
                    """INSERT INTO share_revisions(claim_id,revision,snapshot,total,complete,changed_by,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (claim_id, rev, json.dumps(parties, ensure_ascii=False), total, 0, user_id, ts),
                )
                # 退出后原确认作废，份额不足 100% 停在待补齐。
                conn.execute(
                    "UPDATE share_distributions SET status='void', voided_by=?, voided_at=? WHERE claim_id=? AND status='confirmed'",
                    (user_id, ts, claim_id),
                )
                conn.execute(
                    """INSERT INTO share_distributions(claim_id,revision,status,total,snapshot,note,created_at)
                       VALUES(?,?,'pending',?,?,?,?)""",
                    (claim_id, rev, total, json.dumps(parties, ensure_ascii=False),
                     f"{party['display_name']} 退出，剩余份额合计 {total:g}%，待补齐", ts),
                )
                conn.execute("UPDATE claims SET share_status='pending', updated_at=? WHERE id=?", (ts, claim_id))
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, ts, claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.party.withdraw",
                            {"claim_id": claim_id, "party_id": party_id, "total": total})
                return {"claim_id": claim_id, "party_id": party_id, "share_status": "pending", "total": total}
            except Exception:
                conn.rollback()
                raise

    def get_claim_shares(self, user_id, claim_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if not claim:
                raise BusinessError("权利主张不存在", 404, "not_found")
            if user["role"] == "public":
                raise BusinessError("公众无权查看份额明细", 403, "forbidden")
            if user["role"] == "claimant":
                # 主张人只能查看自己参与的主张的份额。
                is_party = conn.execute(
                    "SELECT 1 FROM claim_parties WHERE claim_id=? AND claimant_id=? AND withdrawn=0",
                    (claim_id, user_id),
                ).fetchone()
                if not is_party and claim["claimant_id"] != user_id:
                    raise BusinessError("无权查看该主张", 403, "forbidden")
            return self._claim_share_view(conn, claim_id, include_details=True)

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self._tx_conn() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                allowed = CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                if new_status == "resolved_return" and self._share_status(conn, claim_id) != "confirmed":
                    raise BusinessError("份额尚未确认分配（待补齐），不能完成返还", 409, "shares_not_confirmed")
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                new_version = claim["object_id"]
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [
                    dict(c)
                    | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                    | {"share_status": self._share_status(conn, c["id"])}
                    | {"parties": self._active_parties(conn, c["id"])}
                    for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
                ],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
                    # 主张人是共同当事人之一，可查看本主张的份额结构。
                    c["share_status"] = self._share_status(conn, c["id"])
                    c["parties"] = self._active_parties(conn, c["id"])
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "shares" and method == "POST":
            d = self._body(); return self._send(200, store.set_claim_shares(user, int(parts[2]), d.get("parties", [])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "shares" and method == "GET":
            return self._send(200, store.get_claim_shares(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "redistribute" and method == "POST":
            return self._send(200, store.redistribute_claim(user, int(parts[2])))
        if len(parts) == 6 and parts[:2] == ["api", "claims"] and parts[3] == "parties" and parts[5] == "withdraw" and method == "POST":
            return self._send(200, store.withdraw_party(user, int(parts[2]), int(parts[4])))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
