"""Text inputs shared by inline editors."""

from textual.binding import Binding
from textual.widgets import TextArea


class MultilineInput(TextArea):
    """Keep a portable newline shortcut when Enter confirms the editor."""

    BINDINGS = [
        Binding("ctrl+j,shift+enter", "insert_newline", "Newline", show=False, priority=True),
    ]

    def action_insert_newline(self) -> None:
        if not self.read_only:
            self.replace("\n", *self.selection, maintain_selection_offset=False)
