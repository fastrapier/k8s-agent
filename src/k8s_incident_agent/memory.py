"""Эпизодическая память: прошлые инциденты (F8, S5, docs/MEMORY.md §3).

SQLite-файл `data/memory.sqlite`: таблица `incidents` по схеме MEMORY §3.1 и полнотекстовый индекс
FTS5 над ней. Поиск — сначала фильтры (namespace, актуальность, исход), потом BM25. Эмбеддингов нет:
это упрощение v1, обоснование и путь апгрейда — в docs/DEFENSE.md, раздел М6.

Сюда попадают только структурированные факты, а не сырые логи: каждое текстовое поле проходит
маскирование и ограничено по длине (MEMORY §3.2, «что запрещено сохранять»).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from k8s_incident_agent.masking import mask_text
from k8s_incident_agent.schemas import IncidentRecord

FIELD_LIMIT = 600            # символов на текстовое поле: факт, а не простыня лога
STALE_DAYS = 180             # MEMORY §3.3: старый фикс мог устареть вместе с приложением
STALE_PENALTY = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    cluster       TEXT NOT NULL,
    namespace     TEXT,
    kinds         TEXT,
    objects       TEXT,
    symptoms      TEXT NOT NULL,
    root_cause    TEXT NOT NULL,
    fix           TEXT NOT NULL,
    fix_applied   INTEGER NOT NULL,
    outcome       TEXT,
    evidence_ids  TEXT,
    trace_id      TEXT,
    schema_ver    INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT REFERENCES incidents(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS incidents_fts USING fts5(
    symptoms, root_cause, fix, objects,
    content='incidents', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_WORD = re.compile(r"\w{3,}", re.UNICODE)


@dataclass(frozen=True)
class PastIncident:
    """Найденный прошлый инцидент — то, что видит аналитик и попадает в отчёт."""

    id: str
    created_at: str
    namespace: str | None
    objects: list[str]
    symptoms: str
    root_cause: str
    fix: str
    outcome: str
    score: float

    def brief(self) -> str:
        return (f"{self.id} ({self.created_at[:10]}, {self.namespace}, {', '.join(self.objects)})\n"
                f"  симптомы: {self.symptoms}\n  причина: {self.root_cause}\n  фикс: {self.fix}")


def _clean(text: str) -> str:
    text = mask_text(" ".join(text.split()))
    return text if len(text) <= FIELD_LIMIT else text[:FIELD_LIMIT - 1] + "…"


def fts_query(text: str) -> str:
    """Слова запроса как префиксы через OR, у длинных слов срезаны два последних символа:
    «падает» → «пада*» найдёт и «падал», и «падает». Дешёвая замена стеммингу для смешанного
    русско-английского текста. Кавычки снимают у FTS5 спецсинтаксис (AND, NEAR, двоеточия)."""
    words = dict.fromkeys(w.lower() for w in _WORD.findall(text))
    return " OR ".join(f'"{w[:-2] if len(w) > 5 else w}"*' for w in words)


class MemoryStore:
    def __init__(self, path: Path | str = ":memory:", cluster: str = "kind-k8s-incident-agent"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.cluster = cluster

    def _next_id(self, day: str) -> str:
        prefix = f"inc-{day}-"
        count = self.db.execute("SELECT count(*) FROM incidents WHERE id LIKE ?",
                                (prefix + "%",)).fetchone()[0]
        return f"{prefix}{count + 1:03d}"

    def write(self, record: IncidentRecord, *, trace_id: str | None = None,
              created_at: str | None = None, incident_id: str | None = None) -> str:
        """Сохраняет запись; дубль (тот же namespace, объекты и фикс) замещает старую через
        `superseded_by`, а не плодит одинаковые строки в выдаче поиска."""
        created_at = created_at or datetime.now(UTC).isoformat(timespec="seconds")
        incident_id = incident_id or self._next_id(created_at[:10])
        objects = json.dumps(sorted(record.objects), ensure_ascii=False)
        fix = _clean(record.fix)
        row = (incident_id, created_at, self.cluster, record.namespace,
               json.dumps(sorted(record.kinds)), objects, _clean(record.symptoms),
               _clean(record.root_cause), fix, int(record.fix_applied), record.outcome,
               json.dumps(record.evidence_ids), trace_id)
        with self.db:
            old = self.db.execute("SELECT rowid, * FROM incidents WHERE id = ?",
                                  (incident_id,)).fetchone()
            if old:  # повторная загрузка фикстуры: перезаписать, индекс обновить
                self._unindex(old)
                self.db.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
            cur = self.db.execute(
                "INSERT INTO incidents (id, created_at, cluster, namespace, kinds, objects,"
                " symptoms, root_cause, fix, fix_applied, outcome, evidence_ids, trace_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self.db.execute(
                "INSERT INTO incidents_fts (rowid, symptoms, root_cause, fix, objects)"
                " VALUES (?,?,?,?,?)", (cur.lastrowid, row[6], row[7], fix, objects))
            if record.outcome != "unresolved":
                self.db.execute(
                    "UPDATE incidents SET superseded_by = ? WHERE id != ? AND superseded_by IS NULL"
                    " AND namespace IS ? AND objects = ? AND fix = ? AND outcome != 'unresolved'",
                    (incident_id, incident_id, record.namespace, objects, fix))
        return incident_id

    def _unindex(self, row: sqlite3.Row) -> None:
        self.db.execute(
            "INSERT INTO incidents_fts (incidents_fts, rowid, symptoms, root_cause, fix, objects)"
            " VALUES ('delete', ?, ?, ?, ?, ?)",
            (row["rowid"], row["symptoms"], row["root_cause"], row["fix"], row["objects"]))

    def search(self, query: str, namespace: str | None = None, limit: int = 3,
               now: datetime | None = None) -> list[PastIncident]:
        """Фильтры до ранжирования (MEMORY §3.3): namespace — условие, а не предпочтение.
        Замещённые и `unresolved` записи в выдачу не попадают."""
        match = fts_query(query)
        if not match:
            return []
        sql = ("SELECT i.*, bm25(incidents_fts) AS rank FROM incidents_fts"
               " JOIN incidents i ON i.rowid = incidents_fts.rowid"
               " WHERE incidents_fts MATCH ? AND i.superseded_by IS NULL"
               " AND i.outcome != 'unresolved'")
        args: list = [match]
        if namespace:
            sql += " AND i.namespace = ?"
            args.append(namespace)
        now = now or datetime.now(UTC)
        found = []
        for row in self.db.execute(sql, args):
            age = (now - datetime.fromisoformat(row["created_at"])).days
            score = -row["rank"] * (STALE_PENALTY if age > STALE_DAYS else 1.0)
            found.append(_past(row, score))
        found.sort(key=lambda p: p.score, reverse=True)
        return found[:limit]

    def get(self, incident_id: str) -> PastIncident | None:
        row = self.db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return _past(row, 0.0) if row else None

    def all(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM incidents ORDER BY created_at DESC").fetchall()

    def seed(self, path: Path) -> list[str]:
        """Фикстура прошлых инцидентов (S5). Повторная загрузка безопасна: id фиксированы."""
        items = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        ids = []
        for item in items:
            meta = {k: item.pop(k) for k in ("id", "created_at") if k in item}
            ids.append(self.write(IncidentRecord.model_validate(item),
                                  incident_id=meta.get("id"), created_at=meta.get("created_at")))
        return ids


def _past(row: sqlite3.Row, score: float) -> PastIncident:
    return PastIncident(
        id=row["id"], created_at=row["created_at"], namespace=row["namespace"],
        objects=json.loads(row["objects"] or "[]"), symptoms=row["symptoms"],
        root_cause=row["root_cause"], fix=row["fix"], outcome=row["outcome"] or "", score=score)
