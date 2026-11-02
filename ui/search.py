"""Native global search with paginated evidence and record navigation."""
from __future__ import annotations

from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from lib.native.search import SCOPES, Search
from ui.workspace import RecordScreen


class SearchScreen(RecordScreen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page = None
        self.loaded_scope = None
        self.loaded_query = None
        layout = BoxLayout(orientation="vertical", spacing=dp(8))
        self.heading.text = "Global catalogue search"
        layout.add_widget(self.heading)
        bar = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        self.scope = Spinner(text=SCOPES["books"], values=tuple(SCOPES.values()))
        self.query = TextInput(multiline=False, hint_text="Search inside books — exact word tokens from book contents")
        self.query.bind(on_text_validate=lambda *_: self.guard(self.search))
        self.query.bind(text=self.invalidate)
        self.scope.bind(text=self.invalidate)
        self.search_button = self.button("Search", self.search)
        for widget in (self.scope, self.query, self.search_button):
            bar.add_widget(widget)
        layout.add_widget(bar)
        layout.add_widget(Label(text="Search inside indexed book bodies (whole-book embedded text). "
                                     "Use \"Works\" to search titles/authors/metadata only.",
                                size_hint_y=None, height=dp(34)))
        scroll = ScrollView()
        self.results = BoxLayout(orientation="vertical", spacing=dp(6), size_hint_y=None)
        self.results.bind(minimum_height=self.results.setter("height"))
        scroll.add_widget(self.results)
        layout.add_widget(scroll)
        pager = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        self.previous = self.button("Previous", lambda: self.search(self.page.number - 1))
        self.following = self.button("Next", lambda: self.search(self.page.number + 1))
        self.page_label = Label(text="")
        self.cancel_button = self.button("Cancel search", self.cancel)
        for widget in (self.previous, self.page_label, self.following, self.cancel_button):
            pager.add_widget(widget)
        layout.add_widget(pager)
        layout.add_widget(self.message)
        self.add_widget(layout)
        self.controls()

    def invalidate(self, *_args):
        self.page = None
        self.results.clear_widgets()
        self.page_label.text = ""
        self.controls()

    def search(self, number=1):
        scope = next(key for key, label in SCOPES.items() if label == self.scope.text)
        query, service = self.query.text, Search(self.workspace().catalogue)
        self.loaded_scope, self.loaded_query = scope, query
        self.results.clear_widgets()
        self.submit_context(lambda context: service.page(scope, query, number, context),
                            self.show, lambda _error: self.invalidate())

    def show(self, page):
        self.page = page
        category = {"works": "works", "quotes": "notes", "notes": "notes", "documents": "documents",
                    "books": "works"}[self.loaded_scope]
        for row in page.rows:
            title = row[1] or f"{category.title()} #{row[0]}"
            metadata = " | ".join(str(value) for value in row[2:-1] if value is not None)
            button = self.button(f"{title} | ID {row[0]} | {metadata}\n{row[-1] or ''}",
                                 lambda record_id=row[0]: self.open_record(category, record_id))
            button.size_hint_y, button.height = None, dp(100)
            button.halign, button.valign = "left", "middle"
            button.bind(size=lambda widget, size: setattr(widget, "text_size", (size[0] - dp(16), size[1])))
            self.results.add_widget(button)
        self.page_label.text = (f"Page {page.number} of {max(1, (page.total + page.size - 1) // page.size)}"
                                f" | {page.total:,} matches")
        self.message.text = "Click a result to open its saved record." if page.total else "No matching saved records."

    def cancel(self):
        for task in self.supervisor.statuses():
            if task.key == "catalogue-editor" and task.state in {"queued", "running", "cancelling"}:
                self.supervisor.cancel(task.id)

    def reset(self):
        self.query.text = ""
        self.invalidate()

    def controls(self):
        for widget in (self.scope, self.query, self.search_button):
            widget.disabled = self.busy
        self.results.disabled = self.busy
        self.previous.disabled = self.busy or self.page is None or self.page.number <= 1
        self.following.disabled = self.busy or self.page is None or self.page.number * self.page.size >= self.page.total
        self.cancel_button.disabled = not self.busy
