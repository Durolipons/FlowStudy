"""Library embedded-text workflow, deliberately separate from manuscript OCR."""
from __future__ import annotations

import json

from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from lib.native.book_analysis import BookAnalysis, MODES
from lib.native.library_index import read_status
from lib.native.library import Library
from lib.native.workspace import integer
from ui.export import export_dialog
from ui.workspace import RecordScreen


class BookAnalysisScreen(RecordScreen):
    def __init__(self, *args, library_root, **kwargs):
        super().__init__(*args, **kwargs)
        self.library_root = library_root
        self.proposal = self.confirmation = None
        layout = BoxLayout(orientation="vertical", spacing=dp(6))
        layout.add_widget(self.heading)
        layout.add_widget(Label(text="Library books: embedded text only. Unreadable PDFs are flagged.\n"
                                     "No OCR, Tesseract, manuscript translation, model or network fallback.",
                                size_hint_y=None, height=dp(48)))
        bar = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(6))
        self.mode = Spinner(text=MODES["pending"], values=tuple(MODES.values()))
        self.limit = Spinner(text="5", values=("1", "5", "10", "25"), size_hint_x=.25)
        self.after = TextInput(text="0", multiline=False, hint_text="After work ID", size_hint_x=.4)
        for widget in (self.mode, Label(text="Books/batch", size_hint_x=.4), self.limit,
                       Label(text="After work ID", size_hint_x=.5), self.after):
            bar.add_widget(widget)
        layout.add_widget(bar)
        actions = GridLayout(cols=3, size_hint_y=None, height=dp(88), spacing=dp(6))
        self.coverage_button = self.button("Coverage / flagged books", lambda: self.confirm_discard(self.coverage))
        self.select_button = self.button("Preview book selection", lambda: self.confirm_discard(self.select))
        self.analyse_button = self.button("Analyse selected books (no writes)", self.analyse)
        self.repair_button = self.button("Preview saved-index repair",
                                        lambda: self.confirm_discard(lambda: self.derived("repair")))
        self.notes_button = self.button("Preview description summaries / body quotes",
                                       lambda: self.confirm_discard(lambda: self.derived("notes")))
        self.next_button = self.button("Next batch after last selected ID", self.next_batch)
        self.actions = (self.coverage_button, self.select_button, self.analyse_button,
                        self.repair_button, self.notes_button, self.next_button)
        for button in self.actions:
            actions.add_widget(button)
        layout.add_widget(actions)
        bulk = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(6))
        self.bulk_status_button = self.button("Running library index: show progress", self.bulk_status)
        self.bulk_stop_button = self.button("Stop bulk indexing after current book", self.stop_bulk)
        bulk.add_widget(self.bulk_status_button)
        bulk.add_widget(self.bulk_stop_button)
        layout.add_widget(bulk)
        evidence = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(6))
        self.work_id = TextInput(multiline=False, hint_text="Work ID for saved metadata / TOC evidence")
        self.evidence_button = self.button("Show saved book evidence", self.evidence)
        self.open_button = self.button("Open work record", lambda: self.open_record(
            "works", integer(self.work_id.text, "Work ID")))
        for widget in (self.work_id, self.evidence_button, self.open_button):
            evidence.add_widget(widget)
        layout.add_widget(evidence)
        self.report = TextInput(readonly=True)
        layout.add_widget(self.report)
        self.flagged_container = BoxLayout(orientation="vertical", spacing=dp(4),
                                           size_hint_y=None)
        layout.add_widget(self.flagged_container)
        bar = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        self.apply_button = self.button("Review / publish this preview", self.confirm_apply)
        self.export_button = self.button("Export preview JSON", self.export)
        self.cancel_button = self.button("Cancel active task", self.cancel)
        for widget in (self.apply_button, self.export_button, self.cancel_button):
            bar.add_widget(widget)
        layout.add_widget(bar)
        layout.add_widget(self.message)
        self.add_widget(layout)
        self.reset()

    def service(self):
        root = self.library_root()
        if root is None:
            raise ValueError("Choose a physical library folder first")
        return BookAnalysis(self.workspace(), Library(self.workspace().catalogue, root))

    def bulk_status(self):
        path = self.workspace().catalogue.database.parent / "whole-book-index-status.json"
        if not path.is_file():
            raise FileNotFoundError("No bulk-index status exists for this catalogue")
        self.confirm_discard(lambda: self.show_report(read_status(self.workspace().catalogue.database)))

    def stop_bulk(self):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        content.add_widget(Label(text="Stop the bulk library index after its current book?\n"
                                     "Committed books stay indexed; a later run resumes pending books."))
        popup = Popup(title="Stop library indexing", content=content, size_hint=(.7, .35))
        def stop():
            path = self.workspace().catalogue.database.parent / "whole-book-index.stop"
            path.touch(exist_ok=True)
            popup.dismiss()
            self.message.text = "Stop requested. The indexer will finish the current book, then stop."
        content.add_widget(self.button("Confirm stop bulk index", stop))
        content.add_widget(self.button("Keep indexing", popup.dismiss))
        popup.open()

    def reset(self):
        if self.confirmation:
            self.confirmation.dismiss()
        self.proposal = None
        self.last_id = None
        self.dirty = False
        self.report.text = ""
        self.after.text = "0"
        self.work_id.text = ""
        self.heading.text = "Whole-book analysis / embedded text"
        self.controls()

    def coverage(self):
        service = self.service()
        self.submit_context(service.coverage, lambda value: self.show_report(value))

    def show_report(self, value):
        self.proposal = None
        self.dirty = False
        # Clear any previous flagged-book buttons
        self.flagged_container.clear_widgets()

        if isinstance(value, dict) and "counts" in value and "latest_flags" in value:
            # Coverage report — render readable summary + clickable flagged books
            counts = value["counts"]
            flags = value["latest_flags"] or []
            lines = []
            lines.append(
                f"Indexed: {counts.get('indexed', 0)}  "
                f"Flagged: {counts.get('flagged', 0)}  "
                f"Pending: {counts.get('pending', 0)}")
            if value.get("summary"):
                lines.append(value["summary"])
            lines.append("")
            if flags:
                lines.append("Flagged books — click to open the work record:")
                for work_id, title, reason, status in flags:
                    lines.append(f"  #{work_id}: {title or '(untitled)'} — {reason}")
                self.report.text = "\n".join(lines)
                btn_box = BoxLayout(orientation="vertical", spacing=dp(4),
                                    size_hint_y=None)
                btn_box.height = dp(44) * len(flags)
                for work_id, title, reason, status in flags:
                    label = f"#{work_id}: {title or '(untitled)'}"
                    btn = self.button(
                        label,
                        lambda wid=work_id: self.open_record("works", wid))
                    btn_box.add_widget(btn)
                self.flagged_container.add_widget(btn_box)
                self.message.text = (
                    "Flagged books have no usable embedded text (likely scanned PDFs). "
                    "Open a work record to inspect, convert with Acrobat, then retry.")
            else:
                self.report.text = "\n".join(lines) + "\nNo flagged books."
                self.message.text = "Read-only report. No flagged books."
        else:
            self.report.text = json.dumps(value, indent=2, ensure_ascii=False)
            self.message.text = "Read-only report. No library files or catalogue records changed."

    def select(self):
        mode = next(key for key, label in MODES.items() if label == self.mode.text)
        limit, after, service = int(self.limit.text), integer(self.after.text, "After work ID"), self.service()
        self.submit_context(lambda context: service.select(mode, limit, context, after), self.show)

    def show(self, proposal):
        self.proposal = proposal
        selected = proposal.values["selected"]
        self.last_id = max((row["work_id"] for row in selected), default=None)
        self.dirty = True
        self.report.text = json.dumps(BookAnalysis.display(proposal), indent=2, ensure_ascii=False)
        self.message.text = ("Selection only. Analyse explicitly, inspect results, then confirm publication."
                             if proposal.category == "whole_book_selection" else
                             "Preview only. Database writes require a verified backup and separate confirmation.")

    def analyse(self):
        service, proposal = self.service(), self.proposal
        self.submit_context(lambda context: service.analyse(proposal, context), self.show)

    def derived(self, operation):
        service = self.service()
        limit, after = int(self.limit.text), integer(self.after.text, "After work ID")
        self.submit_context(lambda context: service.derived(operation, limit, context, after), self.show)

    def next_batch(self):
        if self.last_id is None:
            raise ValueError("Preview a non-empty batch first")
        def move():
            self.after.text = str(self.last_id)
            self.select()
        self.confirm_discard(move)

    def evidence(self):
        service, work_id = self.service(), integer(self.work_id.text, "Work ID")
        self.confirm_discard(lambda: self.submit(lambda: service.evidence(work_id), self.show_report))

    def confirm_apply(self):
        if not self.workspace().editable:
            raise PermissionError("Enable catalogue editing and create a verified backup before publication")
        proposal, service = self.proposal, self.service()
        if proposal is None or proposal.category == "whole_book_selection":
            raise ValueError("Analyse or preview saved-data changes first")
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        content.add_widget(TextInput(readonly=True, text=self.report.text))
        content.add_widget(Label(text="Publish this exact preview? Existing notes/manual metadata stay intact.\n"
                                     "Unreadable PDFs are flagged, never passed to OCR. Cancellation rolls back publication.",
                                size_hint_y=None, height=dp(54)))
        popup = Popup(title="Confirm whole-book analysis", content=content, size_hint=(.9, .9), auto_dismiss=False)
        self.confirmation = popup
        def apply():
            popup.dismiss()
            self.submit_context(lambda context: service.apply(proposal, context), self.applied)
        bar = BoxLayout(size_hint_y=None, height=dp(40))
        bar.add_widget(self.button("Confirm whole-book publication", apply))
        bar.add_widget(self.button("Cancel", popup.dismiss))
        content.add_widget(bar)
        popup.bind(on_dismiss=lambda *_: setattr(self, "confirmation", None))
        popup.open()

    def applied(self, result):
        self.proposal = None
        self.dirty = False
        self.report.text += "\n\nPublished: " + json.dumps(result)
        self.message.text = "Publication complete. Pending mode resumes after saved successes/flags; no OCR was run."

    def export(self):
        service, proposal = self.service(), self.proposal
        if proposal is None:
            raise ValueError("Preview books before exporting")
        export_dialog(self, "Export whole-book evidence", "book-analysis", ("json",),
                      lambda path, _format, protected: service.export(proposal, path, protected))

    def cancel(self):
        for task in self.supervisor.statuses():
            if task.key == "catalogue-editor" and task.state in {"queued", "running", "cancelling"}:
                self.supervisor.cancel(task.id)

    def controls(self):
        for widget in (*self.actions, self.evidence_button, self.open_button, self.mode, self.limit,
                       self.after, self.work_id, self.bulk_status_button, self.bulk_stop_button):
            widget.disabled = self.busy
        selection = self.proposal is not None and self.proposal.category == "whole_book_selection"
        self.analyse_button.disabled = self.busy or not selection or not self.proposal.values["selected"]
        self.next_button.disabled = self.busy or self.last_id is None
        self.apply_button.disabled = self.busy or self.proposal is None or selection
        self.export_button.disabled = self.busy or self.proposal is None
        self.cancel_button.disabled = not self.busy
