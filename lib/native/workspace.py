"""Explicit, backed-up native catalogue editing without Flask or file side effects."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
import uuid

from lib import editing, normalise, studio_records, wikilinks, writing
from lib.native.catalogue import Catalogue, Page
from lib.bookstudio import taxonomy

TABLES = {"works": "works", "notes": "notes", "projects": "projects", "documents": "documents"}
STUDIOS = {"the_book": "The Book", "renaissance_print": "Renaissance Print",
           "philosophical_secrets": "Pholosophical Secrets"}


@dataclass(frozen=True)
class Record:
    category: str
    values: dict
    fingerprint: str
    related: dict


def fingerprint(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def integer(value: str, label: str, *, optional: bool = False, signed: bool = False) -> int | None:
    if optional and not value.strip():
        return None
    try:
        number = int(value.strip() or "0")
    except ValueError as error:
        raise ValueError(f"{label} must be a whole number") from error
    if number < 0 and not signed:
        raise ValueError(f"{label} cannot be negative")
    return number


class Workspace:
    def __init__(self, database: Path):
        self.catalogue = Catalogue(database)
        self.lock = threading.RLock()
        self.backup: Path | None = None
        self.identity: tuple[int, int] | None = None

    @property
    def editable(self) -> bool:
        # Published after verification; UI reads must never wait for the writer lock.
        return self.backup is not None

    def enable_editing(self) -> Path:
        with self.lock:
            if self.backup is not None:
                return self.backup
            path = self.catalogue.database.resolve(strict=True)
            original = path.stat()
            with self.catalogue.connection() as source:
                version = source.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
                if version is None or version[0] != "5":
                    raise ValueError("Native editing requires schema 5; no automatic migration was performed")
                backup_directory = path.parent / "native-backups"
                backup_directory.mkdir(exist_ok=True)
                backup = backup_directory / f"{path.stem}-{uuid.uuid4().hex}.db"
                destination = sqlite3.connect(backup)
                try:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Catalogue backup integrity verification failed; editing is still disabled")
                    for table in (*TABLES.values(), "ocr_books", "ocr_pages", "source_details", "note_details",
                                  "note_sources", "studio_decisions"):
                        if (source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] !=
                                destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]):
                            raise RuntimeError("The catalogue changed during backup; enable editing again")
                finally:
                    destination.close()
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != (original.st_dev, original.st_ino):
                raise RuntimeError("The catalogue was replaced during backup; reopen it before editing")
            self.identity = stat.st_dev, stat.st_ino
            self.backup = backup
            return backup

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            if not self.editable:
                raise PermissionError("Enable catalogue editing and verify its backup before saving")
            path = self.catalogue.database.resolve(strict=True)
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != self.identity:
                raise RuntimeError("The catalogue file was replaced; reopen it and create a new backup")
            conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _values(conn: sqlite3.Connection, category: str, record_id: int) -> dict:
        if category not in TABLES:
            raise ValueError("Unknown editable record category")
        row = conn.execute(f"SELECT * FROM {TABLES[category]} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise LookupError("This record no longer exists; refresh the catalogue")
        values = dict(row)
        if category == "notes":
            values["tags"] = [row[0] for row in conn.execute(
                "SELECT tag FROM note_tags WHERE note_id=? ORDER BY tag", (record_id,))]
            details = conn.execute("SELECT * FROM note_details WHERE note_id=?", (record_id,)).fetchone()
            values["studio"] = dict(details) if details else None
            values["source_associations"] = [dict(row) for row in conn.execute(
                "SELECT work_id,source_id FROM note_sources WHERE note_id=? ORDER BY work_id,source_id", (record_id,))]
        return values

    def record(self, category: str, record_id: int) -> Record:
        with self.catalogue.connection() as conn:
            conn.execute("BEGIN")
            values = self._values(conn, category, record_id)
            related = {}
            if category == "notes":
                related = {"links": wikilinks.outlinks(conn, record_id),
                           "backlinks": wikilinks.backlinks(conn, "note", record_id)}
                if values["studio"]:
                    related["sources"] = [dict(row) for row in conn.execute(
                        "SELECT s.source_id,json_extract(s.metadata,'$.title') AS title FROM source_details s"
                        " JOIN note_sources r ON r.work_id=s.work_id AND r.source_id=s.source_id"
                        " WHERE r.note_id=? AND s.origin=? ORDER BY s.source_id",
                        (record_id, values["studio"]["origin"]))]
            elif category == "projects":
                related = {"stats": writing.project_stats(conn, record_id),
                           "streak": writing.streak(conn, record_id),
                           "today": writing.words_today(conn, record_id),
                           "recent": writing.recent_days(conn, 14, record_id)}
            elif category == "documents":
                related = {"versions": [dict(row) for row in conn.execute(
                    "SELECT id,created,word_count,note FROM document_versions WHERE document_id=?"
                    " ORDER BY id DESC LIMIT 100", (record_id,))]}
        return Record(category, values, fingerprint(values), related)

    def work_details(self, work_id: int) -> dict:
        """Read-only display details for a work: authors, chapters, body excerpt, indexing status."""
        import zlib
        with self.catalogue.connection() as conn:
            conn.execute("BEGIN")
            authors = [dict(row) for row in conn.execute(
                "SELECT a.display_name, a.sort_name, w.role, w.seq "
                "FROM work_authors w JOIN authors a ON a.id=w.author_id "
                "WHERE w.work_id=? ORDER BY w.role, w.seq, a.display_name",
                (work_id,))]
            analysis = conn.execute(
                "SELECT evidence FROM native_book_analysis WHERE work_id=?", (work_id,)).fetchone()
            evidence = json.loads(analysis["evidence"]) if analysis else {}
            text_row = conn.execute(
                "SELECT body_zlib, chars, truncated FROM work_text WHERE work_id=?", (work_id,)).fetchone()
        body_excerpt = ""
        chars = 0
        truncated = False
        if text_row:
            chars = text_row["chars"]
            truncated = bool(text_row["truncated"])
            try:
                body = zlib.decompress(text_row["body_zlib"]).decode("utf-8", "replace")
                body_excerpt = body[:500]
            except Exception:
                body_excerpt = ""
        return {
            "authors": authors,
            "chapters": evidence.get("contents", []),
            "status": evidence.get("status", ""),
            "reason": evidence.get("reason", ""),
            "characters": chars,
            "truncated": truncated,
            "body_excerpt": body_excerpt,
            "metadata": evidence.get("metadata", {}),
        }

    def documents(self, project_id: int, number: int = 1) -> Page:
        if number < 1:
            raise ValueError("Page must be positive")
        with self.catalogue.connection() as conn:
            conn.execute("BEGIN")
            self._values(conn, "projects", project_id)
            total = conn.execute("SELECT COUNT(*) FROM documents WHERE project_id=?", (project_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT id,title,parent_id,status,word_count,target_words FROM documents"
                " WHERE project_id=? ORDER BY parent_id,ord,id LIMIT 50 OFFSET ?",
                (project_id, (number - 1) * 50)).fetchall()
        return Page(("id", "title", "parent_id", "status", "word_count", "target_words"),
                    tuple(tuple(row) for row in rows), total, number, 50)

    def studio_notes(self, origin: str, query: str = "", number: int = 1) -> Page:
        if origin not in STUDIOS or number < 1:
            raise ValueError("Choose a known studio and a positive page")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = "d.origin=? AND (n.title LIKE ? ESCAPE '\\' OR n.body LIKE ? ESCAPE '\\')"
        params = (origin, f"%{escaped}%", f"%{escaped}%")
        with self.catalogue.connection() as conn:
            conn.execute("BEGIN")
            total = conn.execute(
                f"SELECT COUNT(*) FROM notes n JOIN note_details d ON d.note_id=n.id WHERE {where}",
                params).fetchone()[0]
            rows = conn.execute(
                "SELECT n.id,n.title,d.source_id,n.note_type,n.status FROM notes n"
                f" JOIN note_details d ON d.note_id=n.id WHERE {where}"
                " ORDER BY d.source_id LIMIT 50 OFFSET ?", (*params, (number - 1) * 50)).fetchall()
        return Page(("id", "title", "source_id", "note_type", "status"),
                    tuple(tuple(row) for row in rows), total, number, 50)

    def studio_assets(self, origin: str, query: str = "", number: int = 1) -> Page:
        if origin not in STUDIOS or number < 1:
            raise ValueError("Choose a known studio and a positive page")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = (origin, f"%{escaped}%")
        with self.catalogue.connection() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM research_assets"
                                 " WHERE origin=? AND rel_path LIKE ? ESCAPE '\\'", params).fetchone()[0]
            rows = conn.execute("SELECT id,origin,rel_path,size_bytes FROM research_assets"
                                " WHERE origin=? AND rel_path LIKE ? ESCAPE '\\'"
                                " ORDER BY rel_path LIMIT 50 OFFSET ?", (*params, (number - 1) * 50)).fetchall()
        return Page(("id", "origin", "rel_path", "size_bytes"),
                    tuple(tuple(row) for row in rows), total, number, 50)

    def asset(self, record_id: int) -> dict:
        with self.catalogue.connection() as conn:
            row = conn.execute("SELECT * FROM research_assets WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise LookupError("This imported asset no longer exists")
            return dict(row)

    def save_studio_note(self, record: Record, body: str, text: str) -> Record:
        if record.category != "notes" or not record.values.get("studio"):
            raise ValueError("Open an imported studio note before saving structured metadata")
        metadata = self.note_metadata(text)
        details = record.values["studio"]
        if details["origin"] not in STUDIOS:
            raise ValueError("Unknown studio origin")
        if metadata.get("id", details["source_id"]) != details["source_id"]:
            raise ValueError("The source note ID cannot be changed")
        previous = json.loads(details["metadata"])
        if metadata.get("chapter", previous.get("chapter")) != previous.get("chapter"):
            raise ValueError("Change chapter assignments through the outline, not note metadata")
        with self.transaction() as conn:
            self._check(conn, record)
            self.validate_note_refs(conn, dict(previous, **metadata), details["origin"])
            extra_sources = [tuple(row) for row in conn.execute(
                "SELECT work_id,source_id FROM note_sources WHERE note_id=?", (record.values["id"],))
                if row["source_id"] not in (previous.get("source_refs") or [])]
            canonical = {"title": record.values["title"], "type": record.values["note_type"],
                         "status": record.values["status"], "tags": record.values["tags"]}
            canonical.update(metadata)
            studio_records.save_note_record(conn, details["source_id"], body, canonical, details["origin"])
            conn.executemany("INSERT OR IGNORE INTO note_sources(note_id,work_id,source_id) VALUES(?,?,?)",
                             [(record.values["id"], *row) for row in extra_sources])
        return self.record("notes", record.values["id"])

    @staticmethod
    def note_metadata(text: str) -> dict:
        metadata = json.loads(text)
        if not isinstance(metadata, dict):
            raise ValueError("Studio metadata must be a JSON object")
        for field in ("concepts", "philosophers", "traditions", "tags", "related", "source_refs"):
            if field in metadata and (not isinstance(metadata[field], list) or
                                      any(not isinstance(item, str) or not item.strip() for item in metadata[field])):
                raise ValueError(f"{field} must be a list of nonempty strings")
        for field in ("id", "title", "type", "status"):
            if field in metadata and not isinstance(metadata[field], str):
                raise ValueError(f"{field} must be text")
        return metadata

    @staticmethod
    def validate_note_refs(conn: sqlite3.Connection, metadata: dict, origin: str) -> None:
        for field, table in (("source_refs", "source_details"), ("related", "note_details")):
            for key in metadata.get(field) or []:
                if not conn.execute(f"SELECT 1 FROM {table} WHERE origin=? AND source_id=?", (origin, key)).fetchone():
                    raise ValueError(f"Unknown {field} ID: {key}")

    def create_studio_note(self, origin: str, source_id: str, body: str, text: str) -> Record:
        with self.transaction() as conn:
            note_id = self.insert_studio_note(conn, origin, source_id, body, text)
        return self.record("notes", note_id)

    @classmethod
    def insert_studio_note(cls, conn: sqlite3.Connection, origin: str, source_id: str, body: str, text: str) -> int:
        metadata = cls.note_metadata(text)
        if origin not in STUDIOS:
            raise ValueError("Choose a known studio")
        if not source_id.strip() or source_id != source_id.strip():
            raise ValueError("A nonempty note ID without surrounding spaces is required")
        if metadata.get("id", source_id) != source_id:
            raise ValueError("The metadata ID must match the new note ID")
        if not metadata.get("title", "").strip() and not body.strip():
            raise ValueError("A new studio note needs a title or body")
        if metadata.get("type", "paraphrase") not in taxonomy.TYPES or metadata.get("status", "draft") not in taxonomy.STATUSES:
            raise ValueError("Choose a supported studio note type and status")
        if metadata.get("chapter") is not None:
            raise ValueError("Assign new notes to chapters through the outline")
        metadata["id"] = source_id
        if conn.execute("SELECT 1 FROM note_details WHERE origin=? AND source_id=?", (origin, source_id)).fetchone():
            raise ValueError("This studio note ID already exists; open it instead")
        cls.validate_note_refs(conn, metadata, origin)
        note_id = studio_records.save_note_record(conn, source_id, body, metadata, origin)
        conn.execute("UPDATE notes SET source='manual' WHERE id=?", (note_id,))
        return note_id

    def _check(self, conn: sqlite3.Connection, record: Record) -> dict:
        current = self._values(conn, record.category, record.values["id"])
        if fingerprint(current) != record.fingerprint:
            raise RuntimeError("This record changed outside the editor; reopen it before saving")
        return current

    @staticmethod
    def _required_title(values: dict[str, str]) -> str:
        title = values.get("title", "").strip()
        if not title:
            raise ValueError("A title is required")
        return title

    def save(self, category: str, form: dict[str, str], record: Record | None = None,
             *, project_id: int | None = None) -> Record:
        if category not in TABLES:
            raise ValueError("Unknown editable record category")
        if record is not None and record.category != category:
            raise ValueError("The form does not match its original record")
        with self.transaction() as conn:
            current = self._check(conn, record) if record else None
            record_id = current["id"] if current else None
            if category == "works":
                if current is None:
                    raise ValueError("New library works must be added by the catalogue importer")
                values = {field: form.get(field, "").strip() for field in editing.WORK_FIELDS}
                values["year"] = integer(values["year"], "Year", optional=True, signed=True)
                values["pages"] = integer(values["pages"], "Pages", optional=True)
                values["volume"] = values["volume"] or None
                editing.edit_work(conn, record_id, values)
            elif category == "notes":
                if current and current["studio"]:
                    raise PermissionError("Imported typed notes need their studio editor; native viewing is available")
                title, body = form.get("title", "").strip(), form.get("body", "").strip()
                if not title and not body:
                    raise ValueError("A note needs a title or some text")
                note_type, status = form.get("note_type", "permanent"), form.get("status", "open")
                if ((note_type not in editing.NOTE_TYPES and (not current or note_type != current["note_type"])) or
                        (status not in editing.NOTE_STATUSES and (not current or status != current["status"]))):
                    raise ValueError("Choose a supported note type and status")
                slug = normalise.slugify(title) if title else None
                page_ref = form.get("page_ref", "").strip() or None
                if current:
                    conn.execute(
                        "UPDATE notes SET title=?,slug=?,body=?,note_type=?,status=?,page_ref=?,"
                        "updated=datetime('now'),source=CASE WHEN source='extracted' THEN source"
                        " ELSE 'manual' END WHERE id=?",
                        (title or None, slug, body, note_type, status, page_ref, record_id))
                else:
                    record_id = conn.execute(
                        "INSERT INTO notes(title,slug,body,note_type,status,page_ref,source)"
                        " VALUES(?,?,?,?,?,?,'manual')",
                        (title or None, slug, body, note_type, status, page_ref)).lastrowid
                editing.set_note_tags(conn, record_id, form.get("tags", ""))
                wikilinks.sync_links(conn, record_id, body)
            elif category == "projects":
                title = self._required_title(form)
                status = form.get("status", "active")
                if status not in writing.PROJ_STATUSES:
                    raise ValueError("Choose a supported project status")
                deadline = form.get("deadline", "").strip() or None
                if deadline:
                    try:
                        date.fromisoformat(deadline)
                    except ValueError as error:
                        raise ValueError("Deadline must be a valid YYYY-MM-DD date") from error
                fields = (title, form.get("subtitle", "").strip() or None,
                          form.get("genre", "").strip() or None, status,
                          integer(form.get("goal_words", ""), "Word goal"), deadline)
                if current:
                    conn.execute(
                        "UPDATE projects SET title=?,subtitle=?,genre=?,status=?,goal_words=?,deadline=?,"
                        "updated=datetime('now') WHERE id=?", (*fields, record_id))
                else:
                    record_id = conn.execute(
                        "INSERT INTO projects(title,subtitle,genre,status,goal_words,deadline) VALUES(?,?,?,?,?,?)",
                        fields).lastrowid
            else:
                title = self._required_title(form)
                status = form.get("status", "outline")
                if status not in writing.DOC_STATUSES:
                    raise ValueError("Choose a supported document status")
                target = integer(form.get("target_words", ""), "Target words", optional=True)
                if current is None:
                    if project_id is None:
                        raise ValueError("Choose a project before creating a document")
                    self._values(conn, "projects", project_id)
                    parent = integer(form.get("parent_id", ""), "Parent document ID", optional=True)
                    if parent is not None:
                        parent_row = self._values(conn, "documents", parent)
                        if parent_row["project_id"] != project_id:
                            raise ValueError("The parent document must belong to the same project")
                    record_id = conn.execute(
                        "INSERT INTO documents(project_id,parent_id,title,ord,status,target_words)"
                        " VALUES(?,?,?,?,?,?)",
                        (project_id, parent, title, writing.next_ord(conn, parent, project_id), status, target)).lastrowid
                conn.execute("UPDATE documents SET title=?,status=?,target_words=? WHERE id=?",
                             (title, status, target, record_id))
                content = form.get("content", "")
                if current is None or content != current["content"]:
                    editing.save_document_content(conn, record_id, content)
                else:
                    conn.execute("UPDATE documents SET updated=datetime('now') WHERE id=?", (record_id,))
        return self.record(category, record_id)

    def snapshot(self, record: Record, note: str = "") -> Record:
        if record.category != "documents":
            raise ValueError("Only writing documents have snapshots")
        with self.transaction() as conn:
            self._check(conn, record)
            editing.snapshot_document(conn, record.values["id"], note.strip() or None)
        return self.record(record.category, record.values["id"])

    def restore(self, record: Record, version_id: int) -> Record:
        if record.category != "documents":
            raise ValueError("Only writing documents have snapshots")
        with self.transaction() as conn:
            current = self._check(conn, record)
            version = conn.execute(
                "SELECT content FROM document_versions WHERE id=? AND document_id=?",
                (version_id, current["id"])).fetchone()
            if version is None:
                raise LookupError("That snapshot does not belong to this document")
            editing.snapshot_document(conn, current["id"], "Before native snapshot restore")
            editing.save_document_content(conn, current["id"], version["content"])
        return self.record(record.category, record.values["id"])

    def move(self, record: Record, direction: str) -> Record:
        if record.category != "documents" or direction not in {"up", "down"}:
            raise ValueError("Choose a writing document and a valid move direction")
        with self.transaction() as conn:
            self._check(conn, record)
            if not writing.reorder(conn, record.values["id"], direction):
                raise ValueError("The document cannot move further among its siblings")
        return self.record(record.category, record.values["id"])
