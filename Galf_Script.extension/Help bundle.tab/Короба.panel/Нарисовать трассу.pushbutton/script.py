# -*- coding: utf-8 -*-
"""
Пучок коробов по линии. pyRevit / Revit 2023.
Выберите ровно один короб Conduit или кабельный лоток CableTray как образец.
Копируются тип, диаметр (Conduit) или ширина/высота (CableTray), отметка оси.
Образцы не изменяются.
Древовидная трасса с ответвлениями; зазор между коробами 6 мм.
Первый клик задаёт общий конец пучка. На каждую конечную ветку строится один короб.
Повороты создаются соединительными деталями выбранного типа коробов.
"""

__title__ = u"Пучок коробов"
__author__ = u"Standalone pyRevit port"
__persistentengine__ = True

import math
from collections import deque

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from System import Convert, Enum
from System.Collections.Generic import List

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    Color,
    CurveElement,
    DetailCurve,
    ElementId,
    FilteredElementCollector,
    GraphicsStyleType,
    Line,
    LocationCurve,
    Transaction,
    TransactionStatus,
    SubTransaction,
    ViewPlan,
    XYZ,
)
from Autodesk.Revit.DB.Electrical import CableTray, Conduit
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler, PostableCommand, RevitCommandId
from System.Windows.Forms import Form, Button, Label, FormStartPosition, FormBorderStyle
from System.Drawing import Point, Size

from pyrevit import revit, forms, script


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

MM_PER_FT = 304.8
TRACE_STYLE_NAME = u"Трасса коробов"
TITLE = u"Пучок коробов"

# 5 mm — исходный допуск BundleGraph
GRAPH_TOL_FT = 0.016404199475065617

# Геометрия исходного BundleOffsets
STRAIGHT_COS = 0.9998
BUNDLE_CLEAR_GAP_MM = 6.0

# Для поиска коннектора созданного участка возле вершины
CONNECTOR_TOL_FT = 0.1

# Размеры прямоугольного короба.
WIDTH_PARAM = BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM
HEIGHT_PARAM = BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM
DIAMETER_PARAM = BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM

logger = script.get_logger()

try:
    text_type = unicode
except NameError:
    text_type = str


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _bip(integer_value):
    return Enum.ToObject(BuiltInParameter, int(integer_value))


def _eid_value(eid):
    if eid is None:
        return -1
    try:
        return eid.IntegerValue
    except Exception:
        return -1


def _net_element_ids(ids):
    result = List[ElementId]()
    for eid in ids:
        if eid is not None:
            result.Add(eid)
    return result


def tray_size_ft(tray, parameter_id):
    parameter = tray.get_Parameter(parameter_id)
    if parameter is None or parameter.AsDouble() <= 0:
        raise BundleException(u"Не удалось прочитать размеры короба ID {0}.".format(_eid_value(tray.Id)))
    return parameter.AsDouble()


def connector_near(pipe, point, tolerance_ft=CONNECTOR_TOL_FT):
    result = None
    best_sq = tolerance_ft * tolerance_ft
    try:
        for connector in pipe.ConnectorManager.Connectors:
            d = connector.Origin.DistanceTo(point)
            d_sq = d * d
            if d_sq < best_sq:
                best_sq = d_sq
                result = connector
    except Exception:
        pass
    return result


class BundleException(Exception):
    pass


# -----------------------------------------------------------------------------
# 2D vector — V2.cs
# -----------------------------------------------------------------------------

class V2(object):
    __slots__ = ("X", "Y")

    def __init__(self, x, y):
        self.X = float(x)
        self.Y = float(y)

    def __add__(self, other):
        return V2(self.X + other.X, self.Y + other.Y)

    def __sub__(self, other):
        return V2(self.X - other.X, self.Y - other.Y)

    def __mul__(self, k):
        return V2(self.X * k, self.Y * k)

    def __rmul__(self, k):
        return self.__mul__(k)

    def dot(self, other):
        return self.X * other.X + self.Y * other.Y

    def cross(self, other):
        return self.X * other.Y - self.Y * other.X

    @property
    def length(self):
        return math.sqrt(self.X * self.X + self.Y * self.Y)

    def dist(self, other):
        return (self - other).length

    def unit(self):
        ln = self.length
        if ln < 1E-12:
            return V2(self.X, self.Y)
        return V2(self.X / ln, self.Y / ln)

    def left(self):
        return V2(-self.Y, self.X)

    def text(self):
        return u"({0:.1f}; {1:.1f}) мм".format(self.X * MM_PER_FT, self.Y * MM_PER_FT)


class TraceNode(object):
    def __init__(self):
        self.At = None
        self.Parent = None
        self.Children = []

    @property
    def is_leaf(self):
        return self.Parent is not None and len(self.Children) == 0

    @property
    def dir_in(self):
        return (self.At - self.Parent.At).unit()


class TrayTemplate(object):
    def __init__(self):
        self.SourceId = None
        self.TrayTypeId = None
        self.WidthFt = 0.0
        self.WidthMm = 0.0
        self.HeightFt = 0.0
        self.IsConduit = False
        self.SourceEnds = []
        self.Z = 0.0


# -----------------------------------------------------------------------------
# Graph from BundleGraph.cs
# -----------------------------------------------------------------------------

class TraceGraph(object):
    @staticmethod
    def build(segments, root_hint):
        pts = []

        def point_id(point):
            for i in range(len(pts)):
                if pts[i].dist(point) < GRAPH_TOL_FT:
                    return i
            pts.append(point)
            return len(pts) - 1

        edges = []
        for segment in segments:
            if segment[0].dist(segment[1]) >= GRAPH_TOL_FT:
                edges.append([point_id(segment[0]), point_id(segment[1])])

        if not edges:
            raise BundleException(u"Нет линий трассы.")

        TraceGraph.split_at_joints(pts, edges)

        adjacency = [[] for _ in pts]
        for edge in edges:
            a = edge[0]
            b = edge[1]
            if a == b or b in adjacency[a]:
                continue
            adjacency[a].append(b)
            adjacency[b].append(a)

        root_index = min(range(len(pts)), key=lambda i: pts[i].dist(root_hint))
        if len(adjacency[root_index]) != 1:
            raise BundleException(
                u"Начало первой линии должно быть свободным концом трассы, без ответвлений."
            )

        nodes = [None for _ in pts]
        root = TraceNode()
        root.At = pts[root_index]
        nodes[root_index] = root

        q = deque([root_index])
        while q:
            current_index = q.popleft()
            current = nodes[current_index]

            for next_index in adjacency[current_index]:
                if current.Parent is not None and nodes[next_index] is current.Parent:
                    continue

                if nodes[next_index] is not None:
                    raise BundleException(
                        u"Линии трассы замкнуты в кольцо около точки {0}.".format(
                            pts[next_index].text()
                        )
                    )

                child = TraceNode()
                child.At = pts[next_index]
                child.Parent = current
                nodes[next_index] = child
                current.Children.append(child)
                q.append(next_index)

        for i in range(len(pts)):
            if nodes[i] is None and len(adjacency[i]) > 0:
                raise BundleException(
                    u"Линии не соединены в одну трассу: разрыв около точки {0}.".format(
                        pts[i].text()
                    )
                )

        return root

    @staticmethod
    def split_at_joints(pts, edges):
        changed = True
        while changed:
            changed = False
            edge_index = 0

            while edge_index < len(edges) and not changed:
                b = pts[edges[edge_index][0]]
                a = pts[edges[edge_index][1]]
                direction = a - b
                length = direction.length

                if length < 1E-12:
                    edge_index += 1
                    continue

                for i in range(len(pts)):
                    if i == edges[edge_index][0] or i == edges[edge_index][1]:
                        continue

                    projection = (pts[i] - b).dot(direction) / (length * length)
                    if (projection * length < GRAPH_TOL_FT or
                            (1.0 - projection) * length < GRAPH_TOL_FT):
                        continue

                    lateral = abs(direction.unit().cross(pts[i] - b))
                    if lateral >= GRAPH_TOL_FT:
                        continue

                    old_end = edges[edge_index][1]
                    edges[edge_index] = [edges[edge_index][0], i]
                    edges.append([i, old_end])
                    changed = True
                    break

                edge_index += 1

    @staticmethod
    def all_nodes(root):
        yield root
        for child in root.Children:
            for node in TraceGraph.all_nodes(child):
                yield node

    @staticmethod
    def ordered_chain(root):
        # Без назначения коробов конечным веткам ветвление неоднозначно.
        branch_nodes = [n for n in TraceGraph.all_nodes(root) if len(n.Children) > 1]
        if branch_nodes:
            raise BundleException(
                u"В трассе есть ветвление. В режиме без радиаторов и без назначения "
                u"конечных коробов скрипт не может определить, какие короба пучка должны "
                u"уйти в какую ветку. Нарисуйте одну непрерывную трассу без Т/Y-ответвлений."
            )

        leaves = [n for n in TraceGraph.all_nodes(root) if n.is_leaf]
        if len(leaves) != 1:
            raise BundleException(u"Не удалось определить единственный конец трассы.")

        chain = []
        node = leaves[0]
        while node is not None:
            chain.append(node)
            node = node.Parent
        chain.reverse()
        return chain


# -----------------------------------------------------------------------------
# Bundle offset geometry — simplified from BundleOffsets.PathOf/Centred
# -----------------------------------------------------------------------------

def bundle_offsets(templates):
    """Centred() from original BundleOffsets, preserving width + 6 mm spacing."""
    if not templates:
        return []

    values = [0.0 for _ in templates]
    for i in range(1, len(templates)):
        step_ft = (
            (templates[i - 1].WidthMm + templates[i].WidthMm) / 2.0
            + BUNDLE_CLEAR_GAP_MM
        ) / MM_PER_FT
        values[i] = values[i - 1] - step_ft

    centre = (values[0] + values[-1]) / 2.0
    return [v - centre for v in values]


def offset_path(chain, offset):
    """
    One constant-offset path through a polyline.
    At bends uses the same offset-line intersection formula as BundleOffsets.PathOf.
    """
    if chain is None or len(chain) < 2:
        raise BundleException(u"Трасса должна содержать хотя бы один отрезок.")

    # Merge exactly collinear segments so no elbow is requested on a straight run.
    simplified = []
    for node in chain:
        while len(simplified) >= 2:
            d1 = (simplified[-1].At - simplified[-2].At).unit()
            d2 = (node.At - simplified[-1].At).unit()
            if d1.dot(d2) < 1.0 - 1E-10:
                break
            simplified.pop()
        simplified.append(node)
    chain = simplified

    directions = []
    for i in range(len(chain) - 1):
        d = (chain[i + 1].At - chain[i].At).unit()
        if d.length < 1E-12:
            raise BundleException(u"Нулевой участок трассы.")
        directions.append(d)

    result = [chain[0].At + directions[0].left() * offset]

    # Внутренние вершины.
    for i in range(1, len(chain) - 1):
        joint = chain[i].At
        d1 = directions[i - 1]
        d2 = directions[i]

        if d1.dot(d2) > STRAIGHT_COS:
            p = joint + d2.left() * offset
            if result[-1].dist(p) >= 1E-09:
                result.append(p)
            continue

        cross = d1.cross(d2)
        if abs(cross) < 1E-06:
            raise BundleException(
                u"Линия поворачивает назад в точке {0}.".format(joint.text())
            )

        # Пересечение двух параллельно смещённых осей.
        v = joint + d1.left() * offset
        a = joint + d2.left() * offset
        k = (a - v).cross(d2) / cross
        p = v + d1 * k
        if result[-1].dist(p) >= 1E-09:
            result.append(p)

    end = chain[-1].At + directions[-1].left() * offset
    if result[-1].dist(end) >= 1E-09:
        result.append(end)

    for index in range(len(result) - 1):
        if (result[index + 1] - result[index]).dot(directions[index]) < GRAPH_TOL_FT:
            raise BundleException(
                u"Повороты слишком близко: смещённая ось короба идёт назад или участок короче 5 мм. "
                u"Увеличьте расстояние между поворотами.")

    return result


# -----------------------------------------------------------------------------
# Cable tray templates
# -----------------------------------------------------------------------------

def selected_tray_templates(uidoc, doc):
    trays = [doc.GetElement(eid) for eid in uidoc.Selection.GetElementIds()]
    selected = [item for item in trays if item is not None]
    trays = sorted([item for item in selected if isinstance(item, (CableTray, Conduit))],
                   key=lambda item: _eid_value(item.Id))
    if len(trays) != 1:
        categories = u", ".join(sorted(set(
            item.Category.Name if item.Category is not None else item.GetType().Name
            for item in selected))) or u"нет выбранных элементов"
        raise BundleException(
            u"Перед запуском выделите ровно один короб (Conduit) или кабельный лоток как образец всего пучка.\n"
            u"Распознано: {0}. Выбрано всего: {1}. Категории: {2}.\n"
            u"Образцы не изменяются.".format(len(trays), len(selected), categories))
    templates = []
    for tray in trays:
        location = tray.Location
        if not isinstance(location, LocationCurve):
            raise BundleException(u"У короба нет осевой линии.")
        curve = location.Curve
        if abs(curve.GetEndPoint(0).Z - curve.GetEndPoint(1).Z) > 1.0 / MM_PER_FT:
            raise BundleException(
                u"Короб ID {0} наклонный. Выберите горизонтальные образцы.".format(_eid_value(tray.Id)))
        template = TrayTemplate()
        template.SourceId = tray.Id
        template.TrayTypeId = tray.GetTypeId()
        template.IsConduit = isinstance(tray, Conduit)
        template.WidthFt = tray_size_ft(tray, DIAMETER_PARAM if template.IsConduit else WIDTH_PARAM)
        template.WidthMm = template.WidthFt * MM_PER_FT
        template.HeightFt = template.WidthFt if template.IsConduit else tray_size_ft(tray, HEIGHT_PARAM)
        template.Z = curve.GetEndPoint(0).Z
        template.SourceEnds = [V2(curve.GetEndPoint(i).X, curve.GetEndPoint(i).Y) for i in (0, 1)]
        templates.append(template)
    return templates


def create_elbow(doc, tray_a, tray_b, at, counters):
    # Revit chooses the cable-tray fitting from the connector domain and tray type.
    fitting_transaction = SubTransaction(doc)
    fitting_transaction.Start()
    try:
        doc.Regenerate()
        connector_a = connector_near(tray_a, at)
        connector_b = connector_near(tray_b, at)
        if connector_a is None or connector_b is None:
            raise Exception("connector not found")
        fitting = doc.Create.NewElbowFitting(connector_a, connector_b)
        if fitting is None:
            raise Exception("fitting not created")
        fitting_id = fitting.Id
        fitting_transaction.Commit()
        counters["elbows"] += 1
        return fitting_id
    except Exception as ex:
        if fitting_transaction.GetStatus() == TransactionStatus.Started:
            fitting_transaction.RollBack()
        counters["elbow_fails"] += 1
        logger.warning(u"Соединительная деталь короба: {0}".format(text_type(ex)))
        return None


def branch_paths(root, templates):
    if len(templates) != 1:
        raise BundleException(u"Для всего пучка нужен ровно один короб-образец.")
    # One lane per terminal branch. Order branches geometrically, not by sample location.
    leaves = []
    def visit(node):
        if node.is_leaf:
            leaves.append(node)
            return
        incoming = node.dir_in if node.Parent is not None else node.Children[0].dir_in
        for child in sorted(node.Children, key=lambda child: math.atan2(
                incoming.cross(child.dir_in), incoming.dot(child.dir_in)), reverse=True):
            visit(child)
    visit(root)
    paths = []
    offsets = bundle_offsets([templates[0]] * len(leaves))
    for leaf, offset in zip(leaves, offsets):
        chain = []
        node = leaf
        while node is not None:
            chain.append(node)
            node = node.Parent
        chain.reverse()
        paths.append(offset_path(chain, offset))
    return paths, len(leaves)


def create_bundle(doc, view, line_ids, templates, root_hint):
    curves = []
    for eid in line_ids:
        element = doc.GetElement(eid)
        if isinstance(element, CurveElement) and isinstance(element.GeometryCurve, Line):
            curves.append(element)

    curves = sorted(curves, key=lambda c: c.Id.IntegerValue)

    if not curves:
        raise BundleException(u"Нет прямых линий трассы.")
    if len(curves) < len(line_ids):
        raise BundleException(u"Трасса — только прямые линии, без дуг.")

    segments = []
    for curve_element in curves:
        curve = curve_element.GeometryCurve
        segments.append([
            V2(curve.GetEndPoint(0).X, curve.GetEndPoint(0).Y),
            V2(curve.GetEndPoint(1).X, curve.GetEndPoint(1).Y),
        ])

    # The explicitly picked destination, never element IDs, determines the root.
    if min(root_hint.dist(point) for segment in segments for point in segment) > GRAPH_TOL_FT:
        raise BundleException(u"Первая точка должна совпадать с общим концом трассы (допуск 5 мм).")
    root = TraceGraph.build(segments, root_hint)

    level = view.GenLevel
    if level is None:
        raise BundleException(u"Откройте план этажа.")

    paths, branch_count = branch_paths(root, templates)
    templates = [templates[0]] * branch_count

    counters = {"trays": 0, "elbows": 0, "elbow_fails": 0}
    built_ids = []

    transaction = Transaction(doc, u"Пучок коробов")
    transaction.Start()

    try:
        for index in range(len(templates)):
            template = templates[index]
            path = paths[index]
            previous_tray = None
            made_tray_ids = []

            for i in range(len(path) - 1):
                p0 = path[i]
                p1 = path[i + 1]
                start = XYZ(p0.X, p0.Y, template.Z)
                end = XYZ(p1.X, p1.Y, template.Z)

                if start.DistanceTo(end) <= max(GRAPH_TOL_FT, doc.Application.ShortCurveTolerance):
                    raise BundleException(u"Участок короба слишком короткий для Revit.")

                element_class = Conduit if template.IsConduit else CableTray
                new_tray = element_class.Create(
                    doc, template.TrayTypeId, start, end, level.Id)
                dimensions = ((DIAMETER_PARAM, template.WidthFt),) if template.IsConduit else (
                    (WIDTH_PARAM, template.WidthFt), (HEIGHT_PARAM, template.HeightFt))
                for parameter_id, value in dimensions:
                    parameter = new_tray.get_Parameter(parameter_id)
                    if parameter is None or parameter.IsReadOnly or not parameter.Set(value):
                        raise BundleException(u"Не удалось задать размеры нового короба.")

                counters["trays"] += 1
                built_ids.append(new_tray.Id)
                made_tray_ids.append(new_tray.Id)

                if previous_tray is not None:
                    fitting_id = create_elbow(doc, previous_tray, new_tray, start, counters)
                    if fitting_id is not None:
                        built_ids.append(fitting_id)

                previous_tray = new_tray

        if transaction.Commit() != TransactionStatus.Committed:
            raise BundleException(u"Revit отменил построение. Проверьте сообщения об ошибках.")

    except Exception:
        if transaction.HasStarted():
            transaction.RollBack()
        raise

    message = u"Пучок построен. Участков коробов: {0}, соединительных деталей: {1}, коробов в пучке: {2}.".format(
        counters["trays"], counters["elbows"], len(templates)
    )
    message += u" Конечных веток: {0}.".format(branch_count)

    if counters["elbow_fails"]:
        message += (
            u"\nНе встало соединительных деталей: {0}. Обычно причина — слишком короткий участок "
            u"около поворота или неподходящая трассировка/фитинг типа короба."
        ).format(counters["elbow_fails"])

    return message, built_ids


# -----------------------------------------------------------------------------
# Trace lines
# -----------------------------------------------------------------------------

def ensure_trace_style(doc):
    try:
        lines_category = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
    except Exception:
        lines_category = None
        for category in doc.Settings.Categories:
            if category.Id.IntegerValue == -2000051:
                lines_category = category
                break

    if lines_category is None:
        raise BundleException(u"Не найдена категория линий Revit.")

    trace_category = None
    for subcategory in lines_category.SubCategories:
        if subcategory.Name == TRACE_STYLE_NAME:
            trace_category = subcategory
            break

    if trace_category is None:
        transaction = Transaction(doc, u"Стиль «Трасса коробов»")
        transaction.Start()
        try:
            trace_category = doc.Settings.Categories.NewSubcategory(
                lines_category, TRACE_STYLE_NAME
            )
            trace_category.LineColor = Color(255, 0, 0)
            trace_category.SetLineWeight(5, GraphicsStyleType.Projection)
            transaction.Commit()
        except Exception:
            if transaction.HasStarted():
                transaction.RollBack()
            raise

    return trace_category.GetGraphicsStyle(GraphicsStyleType.Projection)


def collect_trace_lines(doc, view, style_id):
    result = []
    collector = FilteredElementCollector(doc).OfClass(CurveElement)
    for curve_element in collector:
        try:
            if _eid_value(curve_element.OwnerViewId) != _eid_value(view.Id):
                continue
            line_style = curve_element.LineStyle
            if line_style is not None and line_style.Id.IntegerValue == style_id.IntegerValue:
                result.append(curve_element.Id)
        except Exception:
            pass
    return result


def unhide_trace_lines(doc, view, line_ids):
    hidden = []
    for eid in line_ids:
        element = doc.GetElement(eid)
        if element is not None:
            try:
                if element.IsHidden(view):
                    hidden.append(eid)
            except Exception:
                pass

    if not hidden:
        return

    transaction = Transaction(doc, u"Показать трассу пучка")
    transaction.Start()
    try:
        view.UnhideElements(_net_element_ids(hidden))
        transaction.Commit()
    except Exception:
        if transaction.HasStarted():
            transaction.RollBack()
        raise


def hide_trace_lines(doc, view, line_ids):
    valid = [eid for eid in line_ids if doc.GetElement(eid) is not None]
    if not valid:
        return

    transaction = Transaction(doc, u"Спрятать трассу пучка")
    transaction.Start()
    try:
        view.HideElements(_net_element_ids(valid))
        transaction.Commit()
    except Exception:
        if transaction.HasStarted():
            transaction.RollBack()
        raise


def delete_trace_lines(doc, line_ids):
    valid = [eid for eid in line_ids if doc.GetElement(eid) is not None]
    if not valid:
        return

    transaction = Transaction(doc, u"Удалить трассу пучка")
    transaction.Start()
    try:
        doc.Delete(_net_element_ids(valid))
        transaction.Commit()
    except Exception:
        if transaction.HasStarted():
            transaction.RollBack()
        raise




# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def finish_route(doc, view, line_ids, templates):
    if not line_ids:
        forms.alert(u"Трасса не создана.", title=TITLE)
        return
    first = doc.GetElement(sorted(line_ids, key=_eid_value)[0])
    # Native DetailLine preserves the first picked endpoint. No second point prompt.
    origin = first.GeometryCurve.GetEndPoint(0)
    root_hint = V2(origin.X, origin.Y)
    if not forms.alert(
            u"Трасса: {0} отрезков. Образец: один.\n"
            u"На каждую конечную ветку будет построен один короб этого типа и размера.\n"
            u"Общий конец пучка — начало первого отрезка.\nПостроить?".format(
                len(line_ids)), title=TITLE, yes=True, no=True, warn_icon=False):
        return
    message, built_ids = create_bundle(doc, view, line_ids, templates, root_hint)
    hide_trace_lines(doc, view, line_ids)
    forms.alert(message + u"\nОсь трассы скрыта.", title=TITLE, warn_icon=False)


class NativeRouteHandler(IExternalEventHandler):
    def __init__(self, uiapp, doc, view, templates, style):
        self.uiapp = uiapp
        self.doc = doc
        self.view = view
        self.templates = templates
        self.style_id = style.Id
        self.before = set(_eid_value(item.Id) for item in
                          FilteredElementCollector(doc).OfClass(CurveElement))
        self.cancel = False
        self.window = None

    def GetName(self):
        return "Cable bundle native drawing"

    def Execute(self, uiapp):
        try:
            if not self.doc.IsValidObject or not self.view.IsValidObject:
                return
            if uiapp.ActiveUIDocument is None or not uiapp.ActiveUIDocument.Document.Equals(self.doc):
                forms.alert(u"Вернитесь в документ, в котором начали трассу. Нарисованные линии сохранены.", title=TITLE)
                return
            lines = [item for item in FilteredElementCollector(self.doc).OfClass(CurveElement)
                     if _eid_value(item.Id) not in self.before
                     and _eid_value(item.OwnerViewId) == _eid_value(self.view.Id)
                     and isinstance(item, DetailCurve)]
            lines.sort(key=lambda item: _eid_value(item.Id))
            if lines:
                transaction = Transaction(self.doc, u"Оформить трассу коробов")
                transaction.Start()
                try:
                    style = self.doc.GetElement(self.style_id)
                    for item in lines:
                        item.LineStyle = style
                    transaction.Commit()
                except Exception:
                    transaction.RollBack()
                    raise
            if not self.cancel:
                finish_route(self.doc, self.view, [item.Id for item in lines], self.templates)
        except OperationCanceledException:
            pass
        except Exception as error:
            logger.exception("Native route failed")
            forms.alert(text_type(error), title=TITLE, warn_icon=True)
        finally:
            if self.window is not None:
                self.window.Close()


class NativeRouteWindow(Form):
    def __init__(self, handler):
        self.handler = handler
        self.event = ExternalEvent.Create(handler)
        self.Text = u"Рисование пучка коробов"
        self.ClientSize = Size(430, 145)
        self.FormBorderStyle = FormBorderStyle.FixedToolWindow
        self.StartPosition = FormStartPosition.CenterScreen
        self.TopMost = True
        self.requested = False
        label = Label()
        label.Text = (u"Первый клик — куда должен прийти пучок.\n"
                      u"Рисуйте прямые линии и ответвления с привязками Revit.\n"
                      u"Для завершения: Esc дважды, затем «Построить».")
        label.Location = Point(12, 12)
        label.Size = Size(406, 75)
        self.Controls.Add(label)
        accept = Button()
        accept.Text = u"Построить"
        accept.Location = Point(12, 98)
        accept.Size = Size(185, 32)
        accept.Click += self.accept
        self.Controls.Add(accept)
        cancel = Button()
        cancel.Text = u"Оставить только линии"
        cancel.Location = Point(210, 98)
        cancel.Size = Size(205, 32)
        cancel.Click += self.cancel
        self.Controls.Add(cancel)

    def accept(self, sender, args):
        self.submit(False)

    def cancel(self, sender, args):
        self.submit(True)

    def submit(self, cancel):
        if self.requested:
            return
        self.handler.cancel = cancel
        self.event.Raise()
        self.requested = True
        self.Hide()


def start_native_route(uiapp, doc, view, templates, style):
    # Keep the form, handler and ExternalEvent alive after the command returns.
    global _native_route_handler, _native_route_window
    command = RevitCommandId.LookupPostableCommandId(PostableCommand.DetailLine)
    if not uiapp.CanPostCommand(command):
        raise BundleException(u"Завершите текущую команду Revit и запустите кнопку снова.")
    try:
        previous = _native_route_window
    except NameError:
        previous = None
    if previous is not None and not previous.IsDisposed:
        previous.Show()
        previous.Activate()
        return
    handler = NativeRouteHandler(uiapp, doc, view, templates, style)
    window = NativeRouteWindow(handler)
    handler.window = window
    _native_route_handler = handler
    _native_route_window = window
    forms.alert(
        u"Откроется штатное рисование линий Revit с предпросмотром и привязками.\n\n"
        u"Первый клик первой линии — общий конец пучка. Далее рисуйте трассу от него к веткам.\n"
        u"Используйте режим прямой линии. Для ответвления начните новый отрезок на трассе.\n"
        u"В конце нажмите Esc дважды и кнопку «Построить» в плавающем окне.\n"
        u"Линии получат красный стиль при завершении.",
        title=TITLE, warn_icon=False)
    window.Show()
    uiapp.PostCommand(command)


def main():
    uidoc = revit.uidoc
    doc = revit.doc
    view = doc.ActiveView if doc is not None else None
    if uidoc is None or doc is None or view is None:
        forms.alert(u"Нет активного документа Revit.", title=TITLE)
        return
    if not isinstance(view, ViewPlan) or view.GenLevel is None:
        forms.alert(u"Откройте план этажа.", title=TITLE)
        return
    try:
        templates = selected_tray_templates(uidoc, doc)
        style = ensure_trace_style(doc)
        existing = collect_trace_lines(doc, view, style.Id)
        if existing:
            unhide_trace_lines(doc, view, existing)
            if forms.alert(
                    u"На виде есть трасса. Использовать её?\n"
                    u"Да — общий конец останется в начале первого отрезка.\n"
                    u"Нет — удалить линии и нарисовать новую трассу.",
                    title=TITLE, yes=True, no=True, warn_icon=False):
                finish_route(doc, view, existing, templates)
                return
            delete_trace_lines(doc, existing)
        start_native_route(__revit__, doc, view, templates, style)
    except OperationCanceledException:
        pass
    except Exception as error:
        logger.exception("Cable bundle failed")
        forms.alert(text_type(error), title=TITLE, warn_icon=True)


if __name__ == "__main__":
    main()
