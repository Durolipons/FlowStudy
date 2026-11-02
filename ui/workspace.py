"""Native catalogue record forms and a manuscript editor."""
from __future__ import annotations

from concurrent.futures import CancelledError
import logging
import json
from typing import Callable

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from lib import editing, writing
from lib.bookstudio import taxonomy
from lib.native.workspace import Record, Workspace
from lib.native.tasks import TaskSupervisor
from lib.native.drafts import Drafts

LOG = logging.getLogger(__name__)
FIELDS = {
    "works": editing.WORK_FIELDS,
    "notes": ("title", "note_type", "status", "tags", "page_ref"),
    "projects": ("title", "subtitle", "genre", "status", "goal_words", "deadline"),
    "documents": ("title", "status", "target_words", "parent_id"),
}
STUDIO_LIST_FIELDS = ("tags", "concepts", "philosophers", "traditions", "source_refs", "related")


class RecordScreen(Screen):
    def __init__(self, supervisor: TaskSupervisor, workspace: Callable[[], Workspace],
                 open_record: Callable, open_documents: Callable, report_error: Callable[[str], None], **kwargs):
        super().__init__(**kwargs)
        self.supervisor = supervisor
        self.workspace = workspace
        self.open_record = open_record
        self.open_documents = open_documents
        self.report_error = report_error
        self.record: Record | None = None
        self.category = "notes"
        self.project_id: int | None = None
        self.dirty = False
        self.busy = False
        self.populating = False
        self.fields: dict[str, TextInput | Spinner] = {}
        self.studio: dict | None = None
        self.stopped = False
        self.heading = Label(text="Choose a catalogue record", size_hint_y=None, height=dp(36))
        self.message = Label(text="", size_hint_y=None, height=dp(45))

    def button(self, text: str, action: Callable) -> Button:
        button = Button(text=text)
        button.bind(on_release=lambda *_: self.guard(action))
        return button

    def guard(self, action: Callable) -> None:
        try:
            action()
        except Exception as error:
            LOG.exception("Native workspace action failed")
            self.message.text = str(error)
            self.report_error(str(error))

    def changed(self, *_args) -> None:
        if not self.populating:
            self.dirty = True
            self.heading.text = self.heading.text.rstrip(" *") + " *"

    def confirm_discard(self, action: Callable) -> None:
        if self.busy:
            raise RuntimeError("Wait for the current record operation before changing its editor")
        if not self.dirty:
            action()
            return
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        content.add_widget(Label(text=getattr(self, "discard_message", "Discard unsaved record changes?")))
        popup = Popup(title="Unsaved changes", content=content, size_hint=(.65, .3))
        def discard():
            popup.dismiss()
            self.dirty = False
            self.guard(action)
        content.add_widget(self.button("Discard changes", discard))
        content.add_widget(self.button("Keep editing", popup.dismiss))
        popup.open()

    def submit(self, operation: Callable, apply_result: Callable,
               on_error: Callable[[str], None] | None = None) -> None:
        self.submit_context(lambda _context: operation(), apply_result, on_error)

    def submit_context(self, operation: Callable, apply_result: Callable,
                       on_error: Callable[[str], None] | None = None) -> None:
        if self.busy:
            raise RuntimeError("A record operation is already running")
        def run(context):
            context.check_cancelled()
            return operation(context)
        task = self.supervisor.start("catalogue-editor", run)
        self.busy = True
        self.controls()
        self.message.text = "Working..."
        def complete(_dt):
            if self.stopped:
                return
            self.busy = False
            try:
                result = task.future.result()
                apply_result(result)
            except CancelledError:
                self.message.text = "Record operation cancelled"
                self.report_error(self.message.text)
                if on_error:
                    on_error(self.message.text)
            except Exception as error:
                LOG.exception("Native workspace operation failed")
                self.message.text = str(error)
                self.report_error(str(error))
                if on_error:
                    on_error(str(error))
            finally:
                self.controls()
        task.future.add_done_callback(lambda _future: Clock.schedule_once(complete, 0))

    def controls(self) -> None:
        raise NotImplementedError


class WorkspaceScreen(RecordScreen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.draft_key = None
        self.draft_event = None
        self.draft_store = Drafts(self.workspace().catalogue.database)
        self.discard_message = ("Leave without saving to the catalogue?\n"
                                "Your recovery draft is kept under Recover unsaved writing.")
        layout = BoxLayout(orientation="vertical", spacing=dp(8))
        layout.add_widget(self.heading)
        self.details = Label(text="", size_hint_y=None, height=dp(28))
        layout.add_widget(self.details)
        self.scroll = ScrollView(size_hint_y=.45)
        self.form = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.form.bind(minimum_height=self.form.setter("height"))
        self.scroll.add_widget(self.form)
        layout.add_widget(self.scroll)
        self.body = TextInput(multiline=True, font_size=dp(16))
        self.body.bind(text=self.changed)
        layout.add_widget(self.body)
        self.metadata = TextInput(multiline=True, font_size=dp(14), size_hint_y=None, height=0,
                                  hint_text="Studio metadata (JSON)")
        self.metadata.bind(text=self.changed)
        layout.add_widget(self.metadata)
        toolbar = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
        self.save_button = self.button("Save", self.save)
        self.snapshot_button = self.button("Snapshot", self.snapshot)
        self.documents_button = self.button("Open documents", self.browse_documents)
        self.up_button = self.button("Move up", lambda: self.move("up"))
        self.down_button = self.button("Move down", lambda: self.move("down"))
        for button in (self.save_button, self.snapshot_button, self.documents_button,
                       self.up_button, self.down_button):
            toolbar.add_widget(button)
        layout.add_widget(toolbar)
        self.versions = Spinner(text="Choose snapshot", values=())
        versionbar = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
        versionbar.add_widget(self.versions)
        self.restore_button = self.button("Restore selected snapshot", self.restore)
        versionbar.add_widget(self.restore_button)
        layout.add_widget(versionbar)
        layout.add_widget(self.message)
        self.add_widget(layout)

    def changed(self, *_args) -> None:
        super().changed(*_args)
        if (not self.populating and self.category in {"notes", "documents"}
                and self.draft_event is None):
            self.draft_event = Clock.schedule_once(lambda _dt: self.guard(self.flush_draft), .5)

    def confirm_discard(self, action: Callable) -> None:
        self.flush_draft()
        super().confirm_discard(action)

    def flush_draft(self) -> None:
        if self.draft_event is not None:
            self.draft_event.cancel()
            self.draft_event = None
        if not self.dirty or self.category not in {"notes", "documents"}:
            return
        # Retain the original fingerprint: recovery must not bypass stale-save
        # protection if another session has since changed the saved record.
        original = ({"category": self.record.category, "values": self.record.values,
                     "fingerprint": self.record.fingerprint} if self.record else None)
        payload = {"category": self.category, "record": original, "project_id": self.project_id,
                   "fields": {name: widget.text for name, widget in self.fields.items()},
                   "body": self.body.text, "metadata": self.metadata.text,
                   "studio": self.studio}
        self.draft_key = self.draft_store.save(payload, self.draft_key)

    def recovery(self) -> None:
        self.flush_draft()
        store = Drafts(self.workspace().catalogue.database)
        entries = store.entries()
        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        scroll = ScrollView()
        rows = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        rows.bind(minimum_height=rows.setter("height"))
        scroll.add_widget(rows)
        content.add_widget(Label(text="Recover a draft for review. Saved notes/manuscripts are not overwritten.",
                                size_hint_y=None, height=dp(40)))
        content.add_widget(scroll)
        popup = Popup(title="Recover unsaved writing", content=content, size_hint=(.85, .8))
        for entry in entries:
            title = entry["fields"].get("title") or "Untitled"
            row = self.button(f"{entry['updated']} | {entry['category']} | {title}",
                              lambda entry=entry: self.confirm_discard(
                                  lambda: (popup.dismiss(), self.restore_draft(entry))))
            row.size_hint_y, row.height = None, dp(40)
            rows.add_widget(row)
        if not entries:
            rows.add_widget(Label(text="No recovery drafts.", size_hint_y=None, height=dp(40)))
        content.add_widget(self.button("Close", popup.dismiss))
        popup.open()

    def restore_draft(self, entry: dict) -> None:
        original = entry["record"]
        record = (Record(original["category"], original["values"], original["fingerprint"], {})
                  if original else Record(entry["category"], {
                      "studio": entry["studio"], "title": "", "note_type": "permanent",
                      "status": "open", "tags": []}, "", {}))
        self.show(record)
        self.project_id = entry["project_id"]
        self.populating = True
        try:
            for name, value in entry["fields"].items():
                if name in self.fields:
                    self.fields[name].text = value
            self.body.text, self.metadata.text = entry["body"], entry["metadata"]
        finally:
            self.populating = False
        self.draft_key = entry["id"]
        self.changed()
        self.message.text = "Recovered draft. Review and Save explicitly; newer saved edits remain protected."

    def load(self, category: str, record_id: int) -> None:
        workspace = self.workspace()
        self.submit(lambda: workspace.record(category, record_id), self.show)

    def new(self, category: str, project_id: int | None = None) -> None:
        if category not in {"notes", "projects", "documents"}:
            raise ValueError("Choose a note, project or writing document")
        self.project_id = project_id
        defaults = {"notes": {"note_type": "permanent", "status": "open", "tags": []},
                    "projects": {"status": "active", "goal_words": 0},
                    "documents": {"status": "outline", "content": ""}}
        self.show(Record(category, defaults[category], "", {}))
        self.record = None

    def new_studio(self, origin: str) -> None:
        self.show(Record("notes", {"title": "", "body": "", "note_type": "paraphrase",
                                  "status": "draft", "tags": [], "source": "manual",
                                  "studio": {"origin": origin, "source_id": "", "metadata": "{}"}}, "", {}))

    def show(self, record: Record) -> None:
        self.flush_draft()
        self.draft_store = Drafts(self.workspace().catalogue.database)
        self.draft_key = None
        self.record = record if "id" in record.values else None
        self.category = record.category
        if record.category == "documents" and self.record:
            self.project_id = record.values["project_id"]
        self.populating = True
        try:
            self.form.clear_widgets()
            self.fields.clear()
            studio = record.values.get("studio")
            self.studio = studio
            meta = json.loads(studio["metadata"]) if studio else {}
            self.heading.text = (f"{record.category.title()} | {record.values.get('title') or 'Untitled'}"
                                 + (f" | ID {record.values['id']}" if self.record else " | New"))
            if record.category == "documents":
                self.details.text = (f"{record.values.get('word_count', 0)} saved words"
                                     f" | target: {record.values.get('target_words') or 'none'}"
                                     " | Ctrl+S: save | Ctrl+Shift+S: snapshot")
            elif record.category == "notes":
                self.details.text = (f"Source: {record.values.get('source', 'manual')}"
                                     f" | Work: {record.values.get('work_id') or 'standalone'}")
            else:
                self.details.text = "Manual changes stay in the catalogue; source files are untouched."
            fields = (("source_id",) if not self.record else ()) + (
                "title", "note_type", "status", *STUDIO_LIST_FIELDS) if studio else FIELDS[record.category]
            for field in fields:
                list_field = bool(studio) and field in STUDIO_LIST_FIELDS
                row = BoxLayout(size_hint_y=None, height=dp(72) if list_field else dp(38), spacing=dp(8))
                row.add_widget(Label(text=field.replace("_", " ").title() + ("\n(one per line)" if list_field else ""),
                                     size_hint_x=None, width=dp(155)))
                value = record.values.get(field)
                if list_field:
                    value = "\n".join(record.values["tags"] if field == "tags" else meta.get(field) or [])
                elif field == "tags":
                    value = " ".join(value or [])
                choices = None
                if field == "note_type":
                    choices = tuple(dict.fromkeys((*taxonomy.TYPES, value))) if studio else editing.NOTE_TYPES
                elif field == "status":
                    choices = (tuple(dict.fromkeys((*taxonomy.STATUSES, value))) if studio else
                               {"notes": editing.NOTE_STATUSES, "projects": writing.PROJ_STATUSES,
                                "documents": writing.DOC_STATUSES}.get(record.category))
                widget = (Spinner(values=choices, text=str(value or choices[0])) if choices else
                          TextInput(text="" if value is None else str(value), multiline=list_field))
                widget.bind(text=self.changed)
                self.fields[field] = widget
                row.add_widget(widget)
                self.form.add_widget(row)
            self.body.text = record.values.get("body", record.values.get("content", ""))
            self.metadata.height = dp(160) if studio else 0
            self.metadata.opacity = 1 if studio else 0
            if studio:
                meta.update(title=record.values["title"], type=record.values["note_type"],
                            status=record.values["status"], tags=record.values["tags"])
                self.metadata.text = json.dumps(meta, ensure_ascii=False, indent=2)
                self.details.text = f"{studio['origin']} | Source ID: {studio['source_id']} | Advanced metadata below"
            else:
                self.metadata.text = ""
            self.body.size_hint_y = .4 if record.category in {"notes", "documents"} else None
            self.body.height = dp(0) if record.category in {"works", "projects"} else dp(200)
            self.body.opacity = 0 if record.category in {"works", "projects"} else 1
            self.versions.values = tuple(
                f"{version['id']} | {version['created']} | {version['word_count']} words | {version['note'] or ''}"
                for version in record.related.get("versions", []))
            self.versions.text = "Choose snapshot"
            self.message.text = ("Loaded. Save publishes to the catalogue; unsaved writing has automatic recovery drafts."
                                 if record.category in {"notes", "documents"} else
                                 "Loaded. Changes are saved only when you press Save.")
            if record.category == "works" and self.record:
                self._show_work_details()
            if record.category == "projects":
                stats = record.related.get("stats")
                if stats:
                    self.message.text = (f"{stats['documents']} documents | {stats['total_words']} words"
                                         f" | {record.related['today']} today | streak {record.related['streak']}")
            if record.category == "notes":
                for name in ("links", "backlinks"):
                    links = record.related.get(name, [])
                    label = Label(text=f"{name.title()}: {len(links)}", size_hint_y=None, height=dp(30))
                    self.form.add_widget(label)
                    for link in links[:100]:
                        text = link.get("label") or link.get("title") or f"Note {link['id']}"
                        if name == "backlinks" or link.get("to_kind") in {"note", "work"}:
                            category = "notes" if name == "backlinks" or link["to_kind"] == "note" else "works"
                            target = link["id"] if name == "backlinks" else link["to_id"]
                            button = self.button(text, lambda category=category, target=target:
                                                 self.open_record(category, target))
                            button.size_hint_y, button.height = None, dp(35)
                            self.form.add_widget(button)
                        else:
                            self.form.add_widget(Label(text=str(text), size_hint_y=None, height=dp(30)))
                    if len(links) > 100:
                        self.form.add_widget(Label(text="Showing the first 100 links.",
                                                  size_hint_y=None, height=dp(30)))
                if record.values.get("studio"):
                    from kivy.app import App
                    origin = record.values["studio"]["origin"]
                    for source in record.related.get("sources", []):
                        button = self.button(f"Source: {source['title'] or source['source_id']}",
                                             lambda source=source, origin=origin:
                                             App.get_running_app().open_source(source["source_id"], origin))
                        button.size_hint_y, button.height = None, dp(35)
                        self.form.add_widget(button)
                    self.message.text = "Edit studio fields/body. List fields use one value per line. Unknown metadata is preserved."
            self.dirty = False
        finally:
            self.populating = False
        self.controls()

    def _show_work_details(self) -> None:
        work_id = self.record.values["id"]
        def load():
            return self.workspace().work_details(work_id)
        def show(details: dict) -> None:
            self._render_work_details(details)
        self.submit(load, show)

    def _render_work_details(self, details: dict) -> None:
        authors = details.get("authors", [])
        chapters = details.get("chapters", [])
        status = details.get("status", "")
        reason = details.get("reason", "")
        characters = details.get("characters", 0)
        truncated = details.get("truncated", False)
        excerpt = details.get("body_excerpt", "")
        metadata = details.get("metadata", {})

        lines = []
        if authors:
            author_names = ", ".join(a["display_name"] for a in authors if a.get("display_name"))
            lines.append(f"Author(s): {author_names}")
        if status:
            lines.append(f"Indexing status: {status}" + (f" — {reason}" if reason else ""))
        if characters:
            lines.append(f"Full text: {characters:,} characters indexed" + (" (truncated)" if truncated else ""))
        isbn10 = metadata.get("isbn10") or metadata.get("/ISBN") or ""
        isbn13 = metadata.get("isbn13") or ""
        if isbn10 or isbn13:
            lines.append(f"ISBN: {isbn13 or isbn10}")
        if chapters:
            chapter_list = " | ".join(
                f"{c.get('title', 'Chapter')} (p. {c.get('page', '?')})"
                for c in chapters[:12] if c.get("title"))
            lines.append(f"Chapters: {chapter_list}" + (" …" if len(chapters) > 12 else ""))
        if excerpt:
            lines.append("")
            lines.append("Text preview:")
            lines.append(excerpt)

        if not lines:
            return

        # Clear any previous work-details widgets, then add a read-only info block.
        for child in list(self.form.children):
            if getattr(child, "_work_details", False):
                self.form.remove_widget(child)

        block = BoxLayout(orientation="vertical", spacing=dp(4), size_hint_y=None)
        block._work_details = True
        height = dp(26) * len(lines) + dp(10)
        block.height = height
        for line in lines:
            label = Label(text=line, size_hint_y=None, height=dp(26),
                          halign="left", valign="top", shorten=False,
                          text_size=(self.form.width - dp(40), None))
            label.bind(size=lambda w, s: setattr(w, "text_size", (self.form.width - dp(40), s[1])))
            block.add_widget(label)
        self.form.add_widget(block)

    def controls(self) -> None:
        imported = bool(self.studio)
        enabled = self.workspace().editable and not self.busy
        writable = enabled
        for field, widget in self.fields.items():
            if isinstance(widget, TextInput):
                widget.readonly = not writable or (field == "parent_id" and self.record is not None)
                widget.disabled = self.busy
            else:
                widget.disabled = not writable
        self.body.disabled = self.busy or self.category not in {"notes", "documents"}
        self.body.readonly = not enabled
        self.metadata.readonly = not enabled or not imported
        self.metadata.disabled = self.busy or not imported
        self.save_button.disabled = not enabled
        documents = self.category == "documents" and self.record is not None
        self.snapshot_button.disabled = not writable or not documents
        self.restore_button.disabled = not writable or not documents
        self.versions.disabled = self.busy or not documents
        self.up_button.disabled = not writable or not documents
        self.down_button.disabled = not writable or not documents
        self.documents_button.disabled = self.busy or self.category != "projects" or self.record is None

    def save(self) -> None:
        if not self.workspace().editable:
            raise PermissionError("Enable catalogue editing first")
        form = {name: widget.text for name, widget in self.fields.items()}
        if self.category == "notes":
            form["body"] = self.body.text
        elif self.category == "documents":
            form["content"] = self.body.text
        self.flush_draft()
        draft_key, draft_store = self.draft_key, self.draft_store
        workspace, category, record, project = self.workspace(), self.category, self.record, self.project_id
        def saved(result):
            self.show(result)
            if draft_key:
                draft_store.remove(draft_key)
            self.message.text = "Saved to the selected catalogue."
        if self.studio:
            body = self.body.text
            metadata = json.loads(self.metadata.text)
            if not isinstance(metadata, dict):
                raise ValueError("Advanced studio metadata must be a JSON object")
            metadata.update(title=self.fields["title"].text, type=self.fields["note_type"].text,
                            status=self.fields["status"].text)
            for field in STUDIO_LIST_FIELDS:
                metadata[field] = [line.strip() for line in self.fields[field].text.splitlines() if line.strip()]
            text = json.dumps(metadata, ensure_ascii=False)
            if record:
                self.submit(lambda: workspace.save_studio_note(record, body, text), saved)
            else:
                origin, source_id = self.studio["origin"], self.fields["source_id"].text
                self.submit(lambda: workspace.create_studio_note(origin, source_id, body, text), saved)
        else:
            self.submit(lambda: workspace.save(category, form, record, project_id=project), saved)

    def require_saved(self) -> Record:
        if self.dirty:
            raise ValueError("Save your edits before taking a snapshot, restoring or moving")
        if self.record is None:
            raise ValueError("Save the new document first")
        return self.record

    def snapshot(self) -> None:
        record = self.require_saved()
        workspace = self.workspace()
        self.submit(lambda: workspace.snapshot(record, "Native editor snapshot"), self.show)

    def restore(self) -> None:
        record = self.require_saved()
        if self.versions.text not in self.versions.values:
            raise ValueError("Choose a snapshot to restore")
        version_id = int(self.versions.text.split("|")[0])
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        content.add_widget(Label(text="Restore selected content? Current content will be snapshotted first."))
        popup = Popup(title="Restore writing snapshot", content=content, size_hint=(.75, .3))
        def restore():
            popup.dismiss()
            workspace = self.workspace()
            self.submit(lambda: workspace.restore(record, version_id), self.show)
        content.add_widget(self.button("Restore", restore))
        content.add_widget(self.button("Cancel", popup.dismiss))
        popup.open()

    def move(self, direction: str) -> None:
        record = self.require_saved()
        workspace = self.workspace()
        self.submit(lambda: workspace.move(record, direction), self.show)

    def browse_documents(self) -> None:
        if self.record is None or self.category != "projects":
            raise ValueError("Open a saved writing project")
        self.open_documents(self.record.values["id"])
