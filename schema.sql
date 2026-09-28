-- Bibliotheca schema. SQLite 3.50+, FTS5 available.
-- Apply by: python -c "import lib.db as d; d.init_db(d.get_conn(d.load_config()))"
-- Build order: core -> authorities -> search -> governance. Sentinels anchor each chunk.
PRAGMA foreign_keys = ON;

-- =====================================================================
-- CORE CATALOGUE
-- =====================================================================
CREATE TABLE files (
    id            INTEGER PRIMARY KEY,
    library_root  TEXT NOT NULL,
    path          TEXT NOT NULL UNIQUE,        -- absolute path (\\?\ prefix where long)
    rel_path      TEXT NOT NULL,               -- relative to library_root
    filename      TEXT NOT NULL,
    lower_name    TEXT NOT NULL,
    ext           TEXT NOT NULL,               -- lowercased '.pdf'
    size_bytes    INTEGER,
    mtime         REAL,
    kind          TEXT,                        -- book|journal|magazine|audio|image|archive|other
    state         TEXT NOT NULL DEFAULT 'active', -- active|ignored|missing
    quick_hash    TEXT,                        -- size+head+tail hash
    content_hash  TEXT,                        -- full sha1, populated on demand/collisions
    is_duplicate_of INTEGER REFERENCES files(id),
    ingest_run    TEXT,
    created       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (library_root, rel_path)
);
CREATE INDEX idx_files_ext      ON files(ext);
CREATE INDEX idx_files_state    ON files(state);
CREATE INDEX idx_files_dup      ON files(is_duplicate_of);
CREATE INDEX idx_files_size     ON files(size_bytes);
CREATE INDEX idx_files_name     ON files(filename);
CREATE INDEX idx_files_root_rel ON files(library_root, rel_path);

-- Physical shelf = folder mirror (the library tree as hierarchical shelves).
CREATE TABLE collections (
    id          INTEGER PRIMARY KEY,
    library_root TEXT NOT NULL,
    rel_path    TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES collections(id),
    depth       INTEGER NOT NULL DEFAULT 0,
    book_count  INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    UNIQUE (library_root, rel_path)
);
CREATE INDEX idx_coll_parent ON collections(parent_id);
CREATE INDEX idx_coll_root   ON collections(library_root, rel_path);

CREATE TABLE works (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    subtitle      TEXT,
    sort_title    TEXT,
    work_type     TEXT NOT NULL DEFAULT 'book', -- book|chapter|journal|magazine|course|thesis|report|audiovisual
    edition       TEXT,
    volume        TEXT,
    series_id     INTEGER REFERENCES series(id),
    series_number TEXT,
    publisher_id  INTEGER REFERENCES publishers(id),
    place         TEXT,
    year          INTEGER,
    pub_date      TEXT,                          -- ISO date string if known
    language      TEXT,
    isbn10        TEXT,
    isbn13        TEXT,
    doi           TEXT,
    pages         INTEGER,
    description   TEXT,
    cover_file    TEXT,
    source        TEXT NOT NULL DEFAULT 'filename', -- filename|embedded|lookup|manual
    confidence    REAL NOT NULL DEFAULT 0.5,    -- 0..1 reliability of auto parse
    created       TEXT NOT NULL DEFAULT (datetime('now')),
    updated       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_works_title   ON works(sort_title COLLATE NOCASE);
CREATE INDEX idx_works_year    ON works(year);
CREATE INDEX idx_works_pub     ON works(publisher_id);
CREATE INDEX idx_works_series  ON works(series_id);
CREATE INDEX idx_works_isbn13  ON works(isbn13);
CREATE INDEX idx_works_doi     ON works(doi);
CREATE INDEX idx_works_source  ON works(source);
CREATE INDEX idx_works_conf    ON works(confidence);

CREATE TABLE work_files(
    work_id  INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    edition_of INTEGER,
    PRIMARY KEY (work_id, file_id)
);
CREATE INDEX idx_wf_file ON work_files(file_id);

-- =====================================================================
-- AUTHORITIES: people, publishers, series, subjects
-- =====================================================================
CREATE TABLE authors (
    id           INTEGER PRIMARY KEY,
    sort_name    TEXT NOT NULL,            -- "Watts, Alan W." (surnames, given)
    display_name TEXT NOT NULL,            -- "Alan W. Watts"
    given        TEXT,
    family       TEXT,
    dates        TEXT,
    bio          TEXT,
    wikidata_id  TEXT,
    UNIQUE (sort_name)
);
CREATE INDEX idx_authors_name ON authors(display_name COLLATE NOCASE);
CREATE TABLE author_alias(
    id        INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    UNIQUE (author_id, alias)
);
CREATE TABLE work_authors(
    work_id  INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    role     TEXT NOT NULL DEFAULT 'author', -- author|editor|translator|illustrator
    seq      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (work_id, author_id, role)
);
CREATE INDEX idx_wa_author ON work_authors(author_id);

CREATE TABLE publishers(
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    place TEXT,
    UNIQUE (name)
);
CREATE INDEX idx_pub_name ON publishers(name COLLATE NOCASE);

CREATE TABLE series(
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    publisher_id INTEGER REFERENCES publishers(id),
    issn         TEXT,
    UNIQUE (name)
);
CREATE INDEX idx_series_name ON series(name COLLATE NOCASE);

CREATE TABLE subjects(
    id          INTEGER PRIMARY KEY,
    term        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'subject', -- subject|keyword|person|place|movement|event|deity
    parent_id   INTEGER REFERENCES subjects(id),
    description TEXT,                            -- encyclopedia prose (markdown)
    source      TEXT NOT NULL DEFAULT 'taxonomy',
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    updated     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (slug),
    UNIQUE (term, kind)
);
CREATE INDEX idx_subjects_parent ON subjects(parent_id);
CREATE INDEX idx_subjects_kind   ON subjects(kind);
CREATE INDEX idx_subjects_term   ON subjects(term COLLATE NOCASE);
CREATE TABLE work_subjects(
    work_id    INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    weight     REAL NOT NULL DEFAULT 1.0,
    source     TEXT NOT NULL DEFAULT 'taxonomy',
    PRIMARY KEY (work_id, subject_id)
);
CREATE INDEX idx_ws_subject ON work_subjects(subject_id);

-- =====================================================================
-- CROSS-REFERENCES (encyclopedia 'see also' graph)
-- =====================================================================
CREATE TABLE crossrefs(
    from_id   INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    to_id     INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    relation  TEXT NOT NULL,    -- see_also|broader|narrower|related|disputed_with|cited_by|same_as
    note      TEXT,
    PRIMARY KEY (from_id, to_id, relation)
);
CREATE INDEX idx_cross_from ON crossrefs(from_id);

-- =====================================================================
-- RESEARCH CONTENT: notes, reading lists, citations
-- =====================================================================
CREATE TABLE notes(
    id         INTEGER PRIMARY KEY,
    work_id    INTEGER REFERENCES works(id) ON DELETE CASCADE,
    subject_id INTEGER REFERENCES subjects(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'note',     -- note|quote|summary|todo
    page_ref   TEXT,
    body       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',   -- manual|extracted
    created    TEXT NOT NULL DEFAULT (datetime('now')),
    updated    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_notes_work ON notes(work_id);
CREATE INDEX idx_notes_subj ON notes(subject_id);
CREATE INDEX idx_notes_kind ON notes(kind);
CREATE TABLE note_tags(
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);
CREATE TABLE reading_lists(
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    created     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE reading_list_items(
    list_id INTEGER NOT NULL REFERENCES reading_lists(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    added   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (list_id, work_id)
);
CREATE TABLE citations(
    id            INTEGER PRIMARY KEY,
    work_id       INTEGER REFERENCES works(id),
    list_id       INTEGER REFERENCES reading_lists(id),
    style         TEXT NOT NULL DEFAULT 'harvard',
    fields        TEXT NOT NULL,            -- json
    rendered_list TEXT NOT NULL,            -- reference-list entry
    rendered_in   TEXT NOT NULL,            -- in-text "(Author, Year)"
    created       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_cit_work ON citations(work_id);

-- FTS5 search tables (kept in sync via triggers in the next block).
-- NOTE: these are standalone fts5 tables, NOT content='works' external
-- content tables. The indexes deliberately carry derived values (author
-- display names, publisher name) that do not exist as columns on `works`,
-- and FTS5 external-content mode resolves its 'delete' command by re-reading
-- the content table - so a content='works' table declaring `authors` makes
-- every UPDATE on `works` fail with "database disk image is malformed".
-- The triggers below therefore use plain DELETE ... WHERE rowid = OLD.id.
CREATE VIRTUAL TABLE doc_fts USING fts5(
    title, subtitle, authors, publisher, description,
    tokenize='unicode61'
);
CREATE VIRTUAL TABLE quote_fts USING fts5(
    body, page_ref,
    content='notes', content_rowid='id', tokenize='unicode61'
);
-- Triggers that keep doc_fts / quote_fts in sync with works / notes.
CREATE TRIGGER trg_works_ai AFTER INSERT ON works BEGIN
    INSERT INTO doc_fts(rowid, title, subtitle, authors, publisher, description)
    VALUES (NEW.id, NEW.title, NEW.subtitle,
            (SELECT group_concat(a.display_name, '; ') FROM work_authors wa
              JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=NEW.id
              AND wa.role IN ('author','editor')),
            (SELECT name FROM publishers p WHERE p.id=NEW.publisher_id),
            NEW.description);
END;
CREATE TRIGGER trg_works_au AFTER UPDATE ON works BEGIN
    DELETE FROM doc_fts WHERE rowid = OLD.id;
    INSERT INTO doc_fts(rowid, title, subtitle, authors, publisher, description)
    VALUES (NEW.id, NEW.title, NEW.subtitle,
            (SELECT group_concat(a.display_name, '; ') FROM work_authors wa
              JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=NEW.id
               AND wa.role IN ('author','editor')),
            (SELECT name FROM publishers p WHERE p.id=NEW.publisher_id),
            NEW.description);
END;
CREATE TRIGGER trg_works_ad AFTER DELETE ON works BEGIN
    DELETE FROM doc_fts WHERE rowid = OLD.id;
END;
CREATE TRIGGER trg_notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO quote_fts(rowid, body, page_ref) VALUES (NEW.id, NEW.body, NEW.page_ref);
END;
CREATE TRIGGER trg_notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO quote_fts(quote_fts, rowid) VALUES('delete', OLD.id);
END;

-- =====================================================================
-- ORGANISATION / GOVERNANCE
-- =====================================================================
CREATE TABLE review_queue(
    id          INTEGER PRIMARY KEY,
    work_id     INTEGER REFERENCES works(id),
    file_id     INTEGER REFERENCES files(id),
    field       TEXT NOT NULL,            -- title|author|year|publisher|subject
    current     TEXT,
    suggested   TEXT,
    confidence  REAL NOT NULL DEFAULT 0.5,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'open', -- open|done|rejected
    created     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE move_plan(
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id),
    from_path   TEXT NOT NULL,
    to_path     TEXT NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed',
    applied_at  TEXT,
    undo_token  TEXT NOT NULL,
    created     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_move_file   ON move_plan(file_id);
CREATE INDEX idx_move_status ON move_plan(status);
CREATE TABLE undo_log(
    token       TEXT PRIMARY KEY,
    steps       TEXT NOT NULL,            -- json list of {file_id, from_path, to_path}
    applied     INTEGER NOT NULL DEFAULT 0,
    applied_at  TEXT
);
CREATE TABLE lookup_cache(
    key      TEXT PRIMARY KEY,            -- isbn13 or doi
    value    TEXT NOT NULL,               -- json from upstream API
    source   TEXT,
    fetched  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE ingest_log(
    run_id      TEXT PRIMARY KEY,
    started     TEXT NOT NULL,
    finished    TEXT,
    root        TEXT NOT NULL,
    files_seen  INTEGER,
    files_added INTEGER,
    files_changed INTEGER,
    works_added INTEGER,
    errors      INTEGER,
    summary     TEXT
);
CREATE TABLE edit_audit(
    id          INTEGER PRIMARY KEY,
    table_name  TEXT NOT NULL,
    row_id      INTEGER NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    user_note   TEXT,
    changed     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE meta(
    k TEXT PRIMARY KEY,
    v TEXT
);
INSERT OR IGNORE INTO meta(k,v) VALUES ('schema_version','1');

-- Phase 2 embedded-metadata cache (one row per leader file).
-- Populated by lib/extract.py; read by lib/normalise.py. file_meta is a
-- cache, not the catalogue: the authoritative record lives in works/files.
CREATE TABLE IF NOT EXISTS file_meta(
    file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    title         TEXT,
    creator       TEXT,
    publisher     TEXT,
    pub_date      TEXT,
    language      TEXT,
    isbn          TEXT,
    pages         INTEGER,
    has_text      INTEGER,
    snippet       TEXT,
    identifiers   TEXT,             -- json list of {scheme, value}
    description   TEXT,
    archive_count INTEGER,
    archive_members TEXT,           -- json list of member paths (capped)
    raw_json      TEXT,             -- full extracted metadata as JSON
    error         TEXT,
    extracted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fm_isbn ON file_meta(isbn);
CREATE INDEX IF NOT EXISTS idx_fm_title ON file_meta(title);
-- EXTRACT_SCHEMA_END
-- CHUNK1_END sentinel



