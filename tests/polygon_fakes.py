"""Identity-bearing, readable polygon COM fixtures; never attach to SolidWorks."""
from __future__ import annotations

import math
from types import SimpleNamespace

from test_circle_repair import Document, Entity

from sw_assistant.solidworks.gateway import PLANE_FRAMES, SolidWorksGateway


class Transform:
    def __init__(self, axes=(0, 1, 2), signs=(1, 1, 1), translation=(0, 0, 0)):
        self.axes, self.signs, self.translation = axes, signs, translation
        self.ArrayData = [0.0] * 16
        for index, axis in enumerate(axes):
            self.ArrayData[index * 3 + axis] = signs[index]
        self.ArrayData[9:12] = translation
        self.ArrayData[12] = 1

    def convert(self, coordinates):
        return [coordinates[axis] * sign + offset
                for axis, sign, offset in zip(self.axes, self.signs, self.translation, strict=True)]

    def Inverse(self):
        axes = [self.axes.index(index) for index in range(3)]
        signs = [self.signs[index] for index in axes]
        translation = [-self.translation[index] * self.signs[index] for index in axes]
        return Transform(axes, signs, translation)


class MathPoint:
    def __init__(self, coordinates):
        self.ArrayData = list(coordinates)

    def MultiplyTransform(self, transform):
        return MathPoint(transform.convert(self.ArrayData))


class Line(Entity):
    ConstructionGeometry = False

    def __init__(self, document, index, start, end):
        super().__init__(f"line-{index}")
        self.document, self.index = document, index
        self.start, self.end = list(start), list(end)

    def GetType(self):
        return 0

    def GetID(self):
        return [self.index, 0]

    def GetStartPoint2(self):
        return SimpleNamespace(**dict(zip(("X", "Y", "Z"), self.start, strict=True)))

    def GetEndPoint2(self):
        return SimpleNamespace(**dict(zip(("X", "Y", "Z"), self.end, strict=True)))

    def Select4(self, append, _data):
        if not append:
            self.document.selected.clear()
        self.document.selected.append(self)
        return True


class PolygonManager:
    def __init__(self, document, *, native=True, native_noops=0, line_failure=0):
        self.document = document
        self.ActiveSketch = document.sketch
        self.AddToDB = False
        self.DisplayWhenAdded = True
        self.native_enabled = native
        self.native_noops, self.line_failure = native_noops, line_failure
        self.native_calls, self.line_calls = [], []
        self.next_id = 1
        self.native_offset = (0, 0, 0)
        self.reverse_order = False

    def __getattribute__(self, name):
        if name == "CreatePolygon" and not self.native_enabled:
            raise RuntimeError("late-bound member is unavailable")
        return super().__getattribute__(name)

    def line(self, start, end):
        entity = Line(self.document, self.next_id, start, end)
        self.next_id += 1
        self.ActiveSketch.segments.append(entity)
        return entity

    def CreateLine(self, *arguments):
        assert self.AddToDB and not self.DisplayWhenAdded
        self.line_calls.append(arguments)
        if len(self.line_calls) == self.line_failure:
            return None
        return self.line(arguments[:3], arguments[3:])

    def CreatePolygon(self, *arguments):
        self.native_calls.append(arguments)
        if len(self.native_calls) <= self.native_noops:
            return [object()] * arguments[6]
        x, y, z, px, py, _pz, sides, _inscribed = arguments
        radius, rotation = math.hypot(px - x, py - y), math.atan2(py - y, px - x)
        points = [(x + radius * math.cos(rotation + 2 * math.pi * index / sides),
                   y + radius * math.sin(rotation + 2 * math.pi * index / sides), z)
                  for index in range(sides)]
        points = [tuple(value + offset for value, offset in zip(point, self.native_offset, strict=True))
                  for point in points]
        created = [self.line(point, points[(index + 1) % sides]) for index, point in enumerate(points)]
        if self.reverse_order:
            for entity in created:
                entity.start, entity.end = entity.end, entity.start
            self.ActiveSketch.segments.reverse()
        return created


def polygon_fixture(*, plane="front", native=True, native_noops=0, line_failure=0, transform=None):
    gateway, document = SolidWorksGateway(), Document()
    document.selected = []
    document.delete_calls = 0

    def delete():
        document.delete_calls += 1
        document.sketch.segments = [entity for entity in document.sketch.segments
                                    if entity not in document.selected]

    document.EditDelete = delete
    document.ClearSelection2 = lambda _all: document.selected.clear()
    document.ForceRebuild3 = lambda _top: True
    document.SelectionManager = SimpleNamespace(
        GetSelectedObjectCount2=lambda _mark: len(document.selected),
        GetSelectedObject6=lambda index, _mark: document.selected[index - 1],
    )
    document.sketch.ModelToSketchTransform = transform or Transform()
    manager = PolygonManager(document, native=native, native_noops=native_noops, line_failure=line_failure)
    document.SketchManager = manager

    def create_point(argument):
        import pythoncom
        from win32com.client import VARIANT

        assert isinstance(argument, VARIANT)
        assert argument.varianttype == pythoncom.VT_ARRAY | pythoncom.VT_R8
        return MathPoint(argument.value)

    utility = SimpleNamespace(CreatePoint=create_point)
    gateway._sw = SimpleNamespace(ActiveDoc=document, GetMathUtility=lambda: utility)
    gateway._bind_sketch_session(document, document.sketch)
    gateway._sketch_frame = dict(PLANE_FRAMES[plane])
    return gateway, document, manager
