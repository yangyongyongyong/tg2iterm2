"""记事本模块。"""

from __future__ import annotations

from notebook.models import Note
from notebook.manager import NoteManager

__all__ = ["Note", "NoteManager"]
