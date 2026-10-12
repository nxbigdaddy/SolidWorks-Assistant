from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..domain import Plan


class PlanPreview(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.primitives: list[dict[str, Any]] = []
        self.plane = ""
        self.features: list[str] = []
        self.setMinimumWidth(280)
        self.setMinimumHeight(210)

    def set_plan(self, plan: Plan | None) -> None:
        self.primitives = []
        self.plane = ""
        self.features = []
        if plan:
            for call in plan.steps:
                args = call.arguments
                if call.tool == "sketch.begin":
                    self.plane = str(args.get("plane") or "")
                elif call.tool == "sketch.circle":
                    self.primitives.append(
                        {
                            "type": "circle",
                            "x": float(args.get("x_mm", 0.0)),
                            "y": float(args.get("y_mm", 0.0)),
                            "radius": float(args["radius_mm"]),
                        }
                    )
                elif call.tool == "sketch.rectangle":
                    self.primitives.append(
                        {
                            "type": "rectangle",
                            "x": float(args.get("center_x_mm", 0.0)),
                            "y": float(args.get("center_y_mm", 0.0)),
                            "width": float(args["width_mm"]),
                            "height": float(args["height_mm"]),
                        }
                    )
                elif call.tool == "sketch.polygon":
                    center_x = float(args.get("x_mm", 0.0))
                    center_y = float(args.get("y_mm", 0.0))
                    radius = float(args["radius_mm"])
                    sides = int(args.get("sides", 6))
                    rotation = math.radians(float(args.get("rotation_deg", 0.0)))
                    self.primitives.append(
                        {
                            "type": "polygon",
                            "points": [
                                (
                                    center_x
                                    + radius * math.cos(rotation + 2 * math.pi * index / sides),
                                    center_y
                                    + radius * math.sin(rotation + 2 * math.pi * index / sides),
                                )
                                for index in range(sides)
                            ],
                        }
                    )
                elif call.tool in {"sketch.line", "sketch.centerline"}:
                    self.primitives.append(
                        {
                            "type": "centerline" if call.tool.endswith("centerline") else "line",
                            "x1": float(args["x1_mm"]),
                            "y1": float(args["y1_mm"]),
                            "x2": float(args["x2_mm"]),
                            "y2": float(args["y2_mm"]),
                        }
                    )
                elif call.tool == "feature.extrude":
                    self.features.append(
                        f"拉伸 {float(args['depth_mm']):g} mm · {args.get('operation', 'boss')}"
                    )
                elif call.tool == "feature.revolve":
                    self.features.append(
                        f"旋转 {float(args.get('angle_deg', 360)):g}° · {args.get('operation', 'boss')}"
                    )
        self.update()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1421"))
        painter.setPen(QPen(QColor("#25364d"), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        painter.setFont(QFont(self.font().family(), 9))
        painter.setPen(QColor("#9fb0c5"))
        plane_name = {"front": "前视", "top": "上视", "right": "右视"}.get(
            self.plane, "未指定"
        )
        painter.drawText(14, 22, f"草图预览 · {plane_name}基准面")
        if not self.primitives:
            painter.setPen(QColor("#6f8299"))
            painter.drawText(
                self.rect().adjusted(18, 35, -18, -35),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "生成计划后显示受支持的二维轮廓",
            )
            return
        bounds = self._bounds()
        canvas = self.rect().adjusted(24, 38, -24, -48)
        width = max(bounds.width(), 1.0)
        height = max(bounds.height(), 1.0)
        scale = min(canvas.width() / width, canvas.height() / height) * 0.82
        center = bounds.center()
        canvas_center = canvas.center()

        def point(x: float, y: float) -> QPointF:
            return QPointF(
                canvas_center.x() + (x - center.x()) * scale,
                canvas_center.y() - (y - center.y()) * scale,
            )

        painter.setPen(QPen(QColor("#263b55"), 1, Qt.PenStyle.DashLine))
        origin = point(0, 0)
        painter.drawLine(
            QPointF(canvas.left(), origin.y()), QPointF(canvas.right(), origin.y())
        )
        painter.drawLine(
            QPointF(origin.x(), canvas.top()), QPointF(origin.x(), canvas.bottom())
        )
        for primitive in self.primitives:
            kind = primitive["type"]
            if kind == "centerline":
                painter.setPen(QPen(QColor("#f1c76c"), 1.5, Qt.PenStyle.DashDotLine))
            else:
                painter.setPen(QPen(QColor("#70aefe"), 2))
            if kind == "circle":
                center_point = point(primitive["x"], primitive["y"])
                radius = primitive["radius"] * scale
                painter.drawEllipse(center_point, radius, radius)
            elif kind == "rectangle":
                top_left = point(
                    primitive["x"] - primitive["width"] / 2,
                    primitive["y"] + primitive["height"] / 2,
                )
                painter.drawRect(
                    QRectF(
                        top_left.x(),
                        top_left.y(),
                        primitive["width"] * scale,
                        primitive["height"] * scale,
                    )
                )
            elif kind == "polygon":
                polygon = [point(x, y) for x, y in primitive["points"]]
                for index, start in enumerate(polygon):
                    painter.drawLine(start, polygon[(index + 1) % len(polygon)])
            else:
                painter.drawLine(
                    point(primitive["x1"], primitive["y1"]),
                    point(primitive["x2"], primitive["y2"]),
                )
        painter.setPen(QColor("#8799af"))
        caption = " · ".join(self.features) if self.features else "仅草图"
        painter.drawText(
            self.rect().adjusted(14, 0, -14, -12),
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
            caption,
        )

    def _bounds(self) -> QRectF:
        points: list[tuple[float, float]] = [(0.0, 0.0)]
        for primitive in self.primitives:
            kind = primitive["type"]
            if kind == "circle":
                points.extend(
                    [
                        (primitive["x"] - primitive["radius"], primitive["y"] - primitive["radius"]),
                        (primitive["x"] + primitive["radius"], primitive["y"] + primitive["radius"]),
                    ]
                )
            elif kind == "rectangle":
                points.extend(
                    [
                        (primitive["x"] - primitive["width"] / 2, primitive["y"] - primitive["height"] / 2),
                        (primitive["x"] + primitive["width"] / 2, primitive["y"] + primitive["height"] / 2),
                    ]
                )
            elif kind == "polygon":
                points.extend(primitive["points"])
            else:
                points.extend(
                    [(primitive["x1"], primitive["y1"]), (primitive["x2"], primitive["y2"])]
                )
        minimum_x = min(point[0] for point in points)
        maximum_x = max(point[0] for point in points)
        minimum_y = min(point[1] for point in points)
        maximum_y = max(point[1] for point in points)
        if minimum_x == maximum_x:
            minimum_x -= 1
            maximum_x += 1
        if minimum_y == maximum_y:
            minimum_y -= 1
            maximum_y += 1
        return QRectF(minimum_x, minimum_y, maximum_x - minimum_x, maximum_y - minimum_y)
