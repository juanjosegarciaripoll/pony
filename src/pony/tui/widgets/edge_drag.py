"""Drag a pane's own border to resize it.

A dedicated splitter widget would cost a column (or a row) of screen
space that the panes need more than the handle does.  Instead the drag
handle *is* the border the pane already draws, so the feature is free:
nothing moves, nothing shrinks, and the layout is unchanged when the
mouse is not in use.

Textual's built-in widgets bind their mouse behaviour to the private
``_on_mouse_down`` / ``_on_mouse_move`` / ``_on_mouse_up`` handlers, and
Textual dispatches the private handler *and* a public one of the same
name.  A subclass can therefore add the public handlers here without
displacing the scrolling and row-selection the base widget already does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from textual import events
from textual.message import Message

if TYPE_CHECKING:
    from textual.geometry import Region

Edge = Literal["right", "bottom"]


class PaneDragged(Message):
    """The user moved a pane border with the mouse.

    ``size`` is the pane's new outer size in cells along the dragged
    axis.  ``final`` marks the release, which is when the new layout is
    worth persisting — writing on every intermediate move would hit the
    disk dozens of times per drag.
    """

    def __init__(self, edge: Edge, size: int, *, final: bool) -> None:
        super().__init__()
        self.edge: Edge = edge
        self.size = size
        self.final = final


class DraggableEdgeMixin:
    """Mix into a bordered widget to make one border a resize handle.

    Set :attr:`DRAG_EDGE` to the border to make draggable.  The widget
    posts :class:`PaneDragged` as the pointer moves; the screen decides
    what the new size means and whether to accept it.
    """

    DRAG_EDGE: ClassVar[Edge | None] = None

    _edge_drag_active: bool = False

    if TYPE_CHECKING:
        # Supplied by the Widget this mixes into; declared so the mixin
        # type-checks on its own rather than through a pile of ignores.
        # ``region`` must be a property here, matching Widget, or it
        # reads as an incompatible attribute override.
        @property
        def region(self) -> Region: ...
        def capture_mouse(self, capture: bool = True) -> None: ...
        def release_mouse(self) -> None: ...
        def post_message(self, message: Message) -> bool: ...

    def _is_on_drag_edge(self, event: events.MouseEvent) -> bool:
        """True when *event* landed on the draggable border cell."""
        region = self.region
        if self.DRAG_EDGE == "right":
            return event.x == region.width - 1
        if self.DRAG_EDGE == "bottom":
            return event.y == region.height - 1
        return False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self.DRAG_EDGE is None or not self._is_on_drag_edge(event):
            return
        self._edge_drag_active = True
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._edge_drag_active:
            return
        self.post_message(self._drag_message(event, final=False))
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._edge_drag_active:
            return
        self._edge_drag_active = False
        self.release_mouse()
        self.post_message(self._drag_message(event, final=True))
        event.stop()

    def _drag_message(self, event: events.MouseEvent, *, final: bool) -> PaneDragged:
        region = self.region
        assert self.DRAG_EDGE is not None
        if self.DRAG_EDGE == "right":
            size = event.screen_x - region.x + 1
        else:
            size = event.screen_y - region.y + 1
        return PaneDragged(self.DRAG_EDGE, max(1, size), final=final)
