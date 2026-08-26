"""Dependency-free pixel icons used by the 16-bit workstation interface."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygon

_INK = "#17233B"
_ICON_SIZES = (16, 20, 24, 32, 48, 64)


def _polygon(*points: tuple[int, int]) -> QPolygon:
    return QPolygon([QPoint(x, y) for x, y in points])


def _draw_new(painter: QPainter) -> None:
    painter.drawPolygon(_polygon((3, 1), (10, 1), (14, 5), (14, 15), (3, 15)))
    painter.drawLine(10, 1, 10, 5)
    painter.drawLine(10, 5, 14, 5)
    painter.drawLine(6, 10, 11, 10)
    painter.drawLine(8, 8, 8, 13)


def _draw_open(painter: QPainter) -> None:
    painter.drawPolyline(_polygon((2, 5), (2, 3), (7, 3), (9, 5), (14, 5)))
    painter.drawPolygon(_polygon((1, 6), (15, 6), (12, 14), (3, 14)))


def _draw_save(painter: QPainter, *, save_as: bool = False) -> None:
    painter.drawRect(QRect(2, 1, 12, 14))
    painter.drawRect(QRect(5, 1, 6, 5))
    painter.drawRect(QRect(5, 10, 6, 5))
    if save_as:
        painter.fillRect(QRect(11, 7, 5, 2), painter.pen().color())
        painter.fillRect(QRect(13, 5, 2, 6), painter.pen().color())


def _draw_record(painter: QPainter) -> None:
    color = painter.pen().color()
    painter.fillRect(QRect(5, 3, 6, 10), color)
    painter.fillRect(QRect(3, 5, 10, 6), color)
    painter.fillRect(QRect(4, 4, 8, 8), color)
    painter.drawRect(QRect(1, 1, 14, 14))


def _draw_stop(painter: QPainter) -> None:
    painter.fillRect(QRect(3, 3, 10, 10), painter.pen().color())
    painter.drawRect(QRect(1, 1, 14, 14))


def _draw_play(painter: QPainter) -> None:
    painter.setBrush(painter.pen().color())
    painter.drawPolygon(_polygon((3, 1), (14, 8), (3, 15)))


def _draw_pause(painter: QPainter) -> None:
    color = painter.pen().color()
    painter.fillRect(QRect(3, 2, 4, 12), color)
    painter.fillRect(QRect(10, 2, 4, 12), color)


def _draw_click(painter: QPainter) -> None:
    painter.setBrush(painter.pen().color())
    painter.drawPolygon(
        _polygon((3, 1), (13, 9), (9, 10), (13, 15), (10, 16), (7, 11), (4, 14))
    )
    painter.drawLine(1, 2, 3, 4)
    painter.drawLine(7, 0, 7, 3)
    painter.drawLine(0, 7, 3, 7)


def _draw_emergency(painter: QPainter) -> None:
    painter.drawPolygon(
        _polygon((5, 0), (11, 0), (16, 5), (16, 11), (11, 16), (5, 16), (0, 11), (0, 5))
    )
    color = painter.pen().color()
    painter.fillRect(QRect(7, 4, 2, 7), color)
    painter.fillRect(QRect(7, 13, 2, 2), color)


def _draw_privacy(painter: QPainter) -> None:
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolygon(_polygon((8, 0), (14, 3), (14, 8), (12, 12), (8, 16), (4, 12), (2, 8), (2, 3)))
    painter.drawLine(5, 8, 7, 10)
    painter.drawLine(7, 10, 11, 5)


def _draw_target(painter: QPainter) -> None:
    painter.drawRect(QRect(3, 3, 10, 10))
    painter.drawRect(QRect(6, 6, 4, 4))
    painter.drawLine(8, 0, 8, 4)
    painter.drawLine(8, 12, 8, 16)
    painter.drawLine(0, 8, 4, 8)
    painter.drawLine(12, 8, 16, 8)


def _draw_script(painter: QPainter) -> None:
    painter.drawRect(QRect(1, 1, 14, 14))
    painter.drawLine(4, 5, 7, 8)
    painter.drawLine(7, 8, 4, 11)
    painter.drawLine(9, 11, 13, 11)


def _draw_fallback(painter: QPainter) -> None:
    painter.drawRect(QRect(3, 3, 10, 10))
    painter.fillRect(QRect(7, 7, 2, 2), painter.pen().color())


def _renderer(name: str) -> Callable[[QPainter], None]:
    renderers: dict[str, Callable[[QPainter], None]] = {
        "new": _draw_new,
        "open": _draw_open,
        "save": _draw_save,
        "save_as": lambda painter: _draw_save(painter, save_as=True),
        "record": _draw_record,
        "stop": _draw_stop,
        "play": _draw_play,
        "pause": _draw_pause,
        "click": _draw_click,
        "emergency": _draw_emergency,
        "privacy": _draw_privacy,
        "target": _draw_target,
        "script": _draw_script,
    }
    return renderers.get(name, _draw_fallback)


def _render_pixel_icon(name: str, color: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    # Icons are drawn on a 16-unit integer grid.  Antialiasing stays off and
    # every requested size receives its own nearest-neighbour raster.
    unit = max(1, size // 16)
    extent = 16 * unit
    offset = (size - extent) // 2
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    painter.translate(offset, offset)
    painter.scale(unit, unit)
    pen = QPen(QColor(color), 1)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    _renderer(name)(painter)
    painter.end()
    return pixmap


def tool_icon(name: str, color: str = _INK) -> QIcon:
    """Return a crisp multi-size pixel icon without external assets."""

    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addPixmap(_render_pixel_icon(name, color, size))
    return icon


__all__ = ["tool_icon"]
