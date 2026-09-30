# -*- coding: utf-8 -*-
"""Insert a pass-through junction box into a conduit run."""

__title__ = u'Внедрить РК'
__doc__ = (
    u'По клику на трассе размещает РК Galf с выбранной стороны, '
    u'разбивает трассу и подключает концы к коробке. '
    u'Команда работает циклически до Esc.'
)
__version__ = '0.2.0'

import math
import traceback

from pyrevit import DB, forms, revit, script
from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    ConnectorType,
    Domain,
    ElementId,
    ElementTransformUtils,
    FamilyInstance,
    FamilySymbol,
    FilteredElementCollector,
    Line,
    StorageType,
    Transaction,
    TransactionStatus,
    XYZ,
)
from Autodesk.Revit.DB.Electrical import Conduit
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

FAMILY_NAME = u'Galf_Коробка распределительная v.1.0.1'
MM_PER_FOOT = 304.8
OFFSET_FT = 100.0 / MM_PER_FOOT  # Смещение коробки от оси трассы
POINT_TOLERANCE_FT = 1.0 / MM_PER_FOOT
DIRECTION_TOLERANCE = 0.98
SYSTEM_TYPE_PARAMETER_NAME = u'Тип системы'
NETWORK_TYPE_PARAMETER_NAME = u'GLF_Тип сети'

SIDE_TOP = u'Сверху'
SIDE_BOTTOM = u'Снизу'
SIDE_LEFT = u'Слева'
SIDE_RIGHT = u'Справа'
SIDE_OPTIONS = [SIDE_TOP, SIDE_BOTTOM, SIDE_LEFT, SIDE_RIGHT]


class RkError(Exception):
    pass


class ConduitFilter(ISelectionFilter):
    def AllowElement(self, element):
        if element.Category and element.Category.Id.IntegerValue == int(BuiltInCategory.OST_Conduit):
            return True
        return False

    def AllowReference(self, ref, point):
        return True  # Разрешаем выбор конкретной точки на элементе


def side_direction(side_name):
    if side_name == SIDE_TOP:
        return XYZ.BasisY
    if side_name == SIDE_BOTTOM:
        return -XYZ.BasisY
    if side_name == SIDE_LEFT:
        return -XYZ.BasisX
    if side_name == SIDE_RIGHT:
        return XYZ.BasisX
    raise RkError(u'Неизвестная сторона РК: {0}.'.format(side_name))


def vector_xy(vector):
    projected = XYZ(vector.X, vector.Y, 0.0)
    if projected.GetLength() <= 1e-9:
        return None
    return projected.Normalize()


def connector_direction(connector):
    try:
        return connector.CoordinateSystem.BasisZ.Normalize()
    except Exception:
        return None


def physical_conduit_connectors(element):
    if element is None:
        return []

    manager = None
    try:
        mep_model = getattr(element, 'MEPModel', None)
        if mep_model is not None:
            manager = mep_model.ConnectorManager
    except Exception:
        manager = None

    if manager is None:
        try:
            manager = element.ConnectorManager
        except Exception:
            manager = None

    if manager is None:
        return []

    result = []
    for connector in manager.Connectors:
        try:
            if connector.Domain != Domain.DomainCableTrayConduit:
                continue
            connector_type = int(connector.ConnectorType)
            physical_mask = int(ConnectorType.Physical)
            if connector_type & physical_mask == 0:
                continue
        except Exception:
            continue
        result.append(connector)
    return result


def family_tree(root):
    result = []
    queue = [root]
    visited = set()
    while queue:
        element = queue.pop(0)
        if element is None:
            continue
        element_id = element.Id.IntegerValue
        if element_id in visited:
            continue
        visited.add(element_id)
        result.append(element)
        if not isinstance(element, FamilyInstance):
            continue
        try:
            for sub_id in element.GetSubComponentIds():
                child = doc.GetElement(sub_id)
                if child is not None:
                    queue.append(child)
        except Exception:
            pass
    return result


def family_conduit_connectors(instance):
    result = []
    for element in family_tree(instance):
        result.extend(physical_conduit_connectors(element))
    return result


def direction_groups(connectors):
    groups = []
    for connector in connectors:
        direction = connector_direction(connector)
        direction = vector_xy(direction) if direction is not None else None
        if direction is None:
            continue

        target_group = None
        for group in groups:
            if direction.DotProduct(group['direction']) >= DIRECTION_TOLERANCE:
                target_group = group
                break

        if target_group is None:
            target_group = {'direction': direction, 'connectors': []}
            groups.append(target_group)
        target_group['connectors'].append(connector)
    return groups


def group_span(group):
    direction = group['direction']
    tangent = XYZ(-direction.Y, direction.X, 0.0)
    values = [connector.Origin.DotProduct(tangent)
              for connector in group['connectors']]
    return max(values) - min(values) if values else 0.0


def main_connector_group(connectors):
    candidates = [group for group in direction_groups(connectors)
                  if len(group['connectors']) >= 2]
    if not candidates:
        raise RkError(
            u'В семействе РК не найдены минимум два сонаправленных '
            u'физических соединителя коробов.'
        )
    return max(candidates, key=lambda group: (
        len(group['connectors']), group_span(group)
    ))


def signed_angle_xy(source, target):
    cross_z = source.X * target.Y - source.Y * target.X
    dot = source.X * target.X + source.Y * target.Y
    return math.atan2(cross_z, dot)


def rotate_connectors_to_side(instance, target_vector, insertion_point):
    connectors = family_conduit_connectors(instance)
    group = main_connector_group(connectors)
    angle = signed_angle_xy(group['direction'], target_vector)
    if abs(angle) > 1e-9:
        axis = Line.CreateBound(
            insertion_point,
            insertion_point + XYZ.BasisZ,
        )
        ElementTransformUtils.RotateElement(doc, instance.Id, axis, angle)
        doc.Regenerate()

    aligned = []
    for connector in family_conduit_connectors(instance):
        direction = connector_direction(connector)
        direction = vector_xy(direction) if direction is not None else None
        if direction is not None and (
                direction.DotProduct(target_vector) >= DIRECTION_TOLERANCE):
            aligned.append(connector)

    if len(aligned) < 2:
        raise RkError(u'После поворота у РК нет двух соединителей в нужном направлении.')
    return aligned


def connector_diameter(connector):
    try:
        diameter = connector.Radius * 2.0
    except Exception:
        diameter = 0.0
    if diameter <= 1e-9:
        raise RkError(u'У соединителя РК не задан круглый диаметр.')
    return diameter


def connector_info(connector):
    direction = connector_direction(connector)
    if direction is None:
        raise RkError(u'У соединителя РК отсутствует направление BasisZ.')
    if abs(direction.Z) > 1e-6:
        raise RkError(u'Соединители РК должны быть горизонтальными.')
    return {
        'owner_id': connector.Owner.Id,
        'origin': connector.Origin,
        'direction': direction.Normalize(),
        'diameter': connector_diameter(connector),
    }


def select_outer_connector_infos(connectors, target_vector):
    tangent = XYZ(-target_vector.Y, target_vector.X, 0.0)
    ordered = sorted(connectors,
                     key=lambda item: item.Origin.DotProduct(tangent))
    if len(ordered) < 2:
        raise RkError(u'Для проходной РК нужны два соединителя коробов.')
    return [connector_info(ordered[0]), connector_info(ordered[-1])]


def family_name_of_symbol(symbol):
    try:
        parameter = symbol.get_Parameter(
            BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM
        )
        if parameter is not None:
            return parameter.AsString() or u''
    except Exception:
        pass
    return u''


def find_family_symbol():
    matches = []
    collector = FilteredElementCollector(doc).OfClass(FamilySymbol)
    for symbol in collector:
        category = symbol.Category
        if (category is None
                or category.Id.IntegerValue
                != int(BuiltInCategory.OST_ElectricalFixtures)):
            continue
        family_name = family_name_of_symbol(symbol)
        if family_name.lower() == FAMILY_NAME.lower():
            matches.append(symbol)

    if not matches:
        raise RkError(
            u'В категории «Электрические приборы» не загружено '
            u'семейство «{0}».'.format(FAMILY_NAME)
        )
    matches.sort(key=lambda item: item.Id.IntegerValue)
    return matches[0]


def create_family_instance(symbol, point, level):
    if not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()

    insertion_point = XYZ(point.X, point.Y, point.Z)
    try:
        instance = doc.Create.NewFamilyInstance(
            insertion_point,
            symbol,
            level,
            StructuralType.NonStructural,
        )
    except Exception:
        instance = doc.Create.NewFamilyInstance(
            insertion_point,
            symbol,
            StructuralType.NonStructural,
        )
    if instance is None:
        raise RkError(u'Revit не создал экземпляр РК.')
    doc.Regenerate()
    return instance, insertion_point


def create_conduit(conduit_type, level, start, end, diameter):
    minimum = max(doc.Application.ShortCurveTolerance, 1e-6)
    if start.DistanceTo(end) <= minimum:
        raise RkError(u'Участок короба слишком короткий для Revit.')
    conduit = Conduit.Create(doc, conduit_type.Id, start, end, level.Id)
    if conduit is None:
        raise RkError(u'Revit не создал участок короба.')
    parameter = conduit.get_Parameter(BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM)
    if parameter is None or parameter.IsReadOnly or not parameter.Set(diameter):
        raise RkError(u'Не удалось задать диаметр созданного короба.')
    return conduit


def closest_connector(element, point):
    connectors = physical_conduit_connectors(element)
    if not connectors:
        return None
    return min(connectors, key=lambda item: item.Origin.DistanceTo(point))


def get_connected_refs(connector):
    refs = []
    try:
        for ref in connector.AllRefs:
            if ref.Owner.Id != connector.Owner.Id and int(ref.ConnectorType) != int(ConnectorType.Logical):
                refs.append(ref)
    except Exception:
        pass
    return refs


def copy_parameter_value(source, target, source_name, target_name=None):
    target_name = target_name or source_name
    source_parameter = source.LookupParameter(source_name)
    target_parameter = target.LookupParameter(target_name)
    if (source_parameter is None or target_parameter is None
            or target_parameter.IsReadOnly):
        return False
    try:
        if target_parameter.StorageType == StorageType.String:
            return bool(target_parameter.Set(parameter_text(source_parameter)))
        storage_type = source_parameter.StorageType
        if storage_type != target_parameter.StorageType:
            return False
        if storage_type == StorageType.Double:
            return bool(target_parameter.Set(source_parameter.AsDouble()))
        elif storage_type == StorageType.Integer:
            return bool(target_parameter.Set(source_parameter.AsInteger()))
        elif storage_type == StorageType.String:
            return bool(target_parameter.Set(source_parameter.AsString() or u''))
        elif storage_type == StorageType.ElementId:
            return bool(target_parameter.Set(source_parameter.AsElementId()))
    except Exception:
        return False
    return False


def parameter_text(parameter):
    if parameter is None:
        return u''
    if parameter.StorageType == StorageType.String:
        return parameter.AsString() or u''
    try:
        value = parameter.AsValueString()
        if value:
            return value
    except Exception:
        pass
    if parameter.StorageType == StorageType.ElementId:
        element = doc.GetElement(parameter.AsElementId())
        if element is not None:
            name_parameter = element.get_Parameter(
                BuiltInParameter.SYMBOL_NAME_PARAM
            )
            if name_parameter is not None:
                return name_parameter.AsString() or u''
    return u''


def copy_route_parameters(source, targets):
    for target in targets:
        if target is None:
            continue
        copy_parameter_value(source, target, SYSTEM_TYPE_PARAMETER_NAME)
        copy_parameter_value(
            source,
            target,
            SYSTEM_TYPE_PARAMETER_NAME,
            NETWORK_TYPE_PARAMETER_NAME,
        )


def refresh_family_connector(info):
    owner = doc.GetElement(info['owner_id'])
    connector = closest_connector(owner, info['origin'])
    if connector is None or connector.Origin.DistanceTo(info['origin']) > POINT_TOLERANCE_FT:
        raise RkError(u'После создания коробов потерян соединитель РК.')
    return connector


def connect_family_to_conduit(family_connector, conduit_connector):
    if family_connector.Origin.DistanceTo(conduit_connector.Origin) > POINT_TOLERANCE_FT:
        raise RkError(u'Короб не дошёл до соединителя РК.')
    try:
        family_connector.ConnectTo(conduit_connector)
        fitting = None
    except Exception:
        try:
            fitting = doc.Create.NewUnionFitting(
                family_connector, conduit_connector
            )
        except Exception as error:
            raise RkError(u'Не удалось подключить короб к соединителю РК: {0}'.format(error))
    doc.Regenerate()
    return fitting


def project_point_on_line(point, line):
    origin = line.GetEndPoint(0)
    direction = line.Direction
    dist = (point - origin).DotProduct(direction)
    return origin + direction * dist


def insert_rk_into_route(symbol, ref, side_name):
    main_conduit = doc.GetElement(ref.ElementId)
    conduit_type = doc.GetElement(main_conduit.GetTypeId())

    level_param = main_conduit.get_Parameter(BuiltInParameter.RBS_START_LEVEL_PARAM)
    level = doc.GetElement(level_param.AsElementId()) if level_param else doc.ActiveView.GenLevel
    
    main_curve = main_conduit.Location.Curve
    if not isinstance(main_curve, Line):
        raise RkError(u'Трасса должна быть прямой линией.')
        
    E0 = main_curve.GetEndPoint(0)
    E1 = main_curve.GetEndPoint(1)
    V_c = main_curve.Direction
    
    click_point = ref.GlobalPoint
    P_center = project_point_on_line(click_point, main_curve)
    
    # Вычисляем вектор смещения перпендикулярно трассе
    V_side_raw = side_direction(side_name)
    V_c_xy = vector_xy(V_c)
    if V_c_xy is None:
        raise RkError(u'Команда работает только с горизонтальными трассами.')
        
    P_1 = XYZ(-V_c_xy.Y, V_c_xy.X, 0)
    P_2 = XYZ(V_c_xy.Y, -V_c_xy.X, 0)
    V_perp = P_1 if P_1.DotProduct(V_side_raw) > P_2.DotProduct(V_side_raw) else P_2
    
    # Точка вставки коробки смещена от трассы
    box_point = P_center + V_perp * OFFSET_FT

    transaction = Transaction(doc, u'Внедрить РК в трассу')
    transaction.Start()
    try:
        instance, insertion_point = create_family_instance(symbol, box_point, level)
        
        # Поворачиваем коробку так, чтобы коннекторы смотрели В СТОРОНУ трассы
        aligned = rotate_connectors_to_side(instance, -V_perp, insertion_point)
        infos = select_outer_connector_infos(aligned, -V_perp)

        # Проецируем выходы коробки обратно на трассу
        for info in infos:
            info['proj'] = project_point_on_line(info['origin'], main_curve)
            info['t'] = (info['proj'] - E0).DotProduct(V_c)
            
        infos.sort(key=lambda x: x['t'])
        info1, info2 = infos[0], infos[1]

        # Проверка, что врезка не выходит за концы трубы
        min_len = max(doc.Application.ShortCurveTolerance, 150.0 / MM_PER_FOOT)
        if info1['t'] < min_len or info2['t'] > main_curve.Length - min_len:
            raise RkError(u'Место вставки слишком близко к концам трассы или трасса слишком короткая.')

        conduit_A = main_conduit
        conn_E1 = closest_connector(conduit_A, E1)
        refs_E1 = get_connected_refs(conn_E1) if conn_E1 else []

        # Обрезаем первую часть трассы до точки P1
        conduit_A.Location.Curve = Line.CreateBound(E0, info1['proj'])

        # Дублируем трассу для второй части (P2 -> E1)
        copied_ids = ElementTransformUtils.CopyElement(doc, conduit_A.Id, XYZ.Zero)
        conduit_B = None
        for cid in copied_ids:
            conduit_B = doc.GetElement(cid)
            break
        conduit_B.Location.Curve = Line.CreateBound(info2['proj'], E1)

        # Восстанавливаем подключение на дальнем конце второй части трассы
        conn_B_E1 = closest_connector(conduit_B, E1)
        if conn_B_E1:
            for r in refs_E1:
                try: conn_B_E1.ConnectTo(r)
                except Exception: pass

        # Создаем две перемычки от коробки к разрезанной трассе
        stub1 = create_conduit(conduit_type, level, info1['origin'], info1['proj'], info1['diameter'])
        stub2 = create_conduit(conduit_type, level, info2['origin'], info2['proj'], info2['diameter'])
        doc.Regenerate()

        # Подключаем перемычки к коробке
        fam_conn1 = refresh_family_connector(info1)
        stub1_fam = closest_connector(stub1, info1['origin'])
        union1 = connect_family_to_conduit(fam_conn1, stub1_fam)

        fam_conn2 = refresh_family_connector(info2)
        stub2_fam = closest_connector(stub2, info2['origin'])
        union2 = connect_family_to_conduit(fam_conn2, stub2_fam)

        # Ставим угловые фитинги между перемычками и основной трассой
        stub1_main = closest_connector(stub1, info1['proj'])
        conduit_A_main = closest_connector(conduit_A, info1['proj'])
        elbow1 = doc.Create.NewElbowFitting(stub1_main, conduit_A_main)

        stub2_main = closest_connector(stub2, info2['proj'])
        conduit_B_main = closest_connector(conduit_B, info2['proj'])
        elbow2 = doc.Create.NewElbowFitting(stub2_main, conduit_B_main)

        doc.Regenerate()
        copy_route_parameters(main_conduit, [
            instance,
            conduit_B,
            stub1,
            stub2,
            union1,
            union2,
            elbow1,
            elbow2,
        ])

        if transaction.Commit() != TransactionStatus.Committed:
            raise RkError(u'Revit отменил создание РК.')
            
    except Exception:
        if transaction.GetStatus() == TransactionStatus.Started:
            transaction.RollBack()
        raise


def show_dark_side_window():
    import clr
    clr.AddReference('System.Drawing')
    clr.AddReference('System.Windows.Forms')
    from System.Drawing import Color, ContentAlignment, Font, FontStyle, Point, Size
    from System.Windows.Forms import (
        Button,
        DialogResult,
        FlatStyle,
        Form,
        FormBorderStyle,
        FormStartPosition,
        Label,
    )

    form = Form()
    form.Text = u'Сторона размещения РК'
    form.ClientSize = Size(400, 285)
    form.BackColor = Color.FromArgb(30, 30, 30)
    form.ForeColor = Color.White
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.StartPosition = FormStartPosition.CenterScreen
    form.MaximizeBox = False
    form.MinimizeBox = False
    form.ShowInTaskbar = False
    form.TopMost = True

    label = Label()
    label.Text = u'Выберите сторону размещения РК'
    label.Font = Font('Segoe UI', 11, FontStyle.Regular)
    label.ForeColor = Color.White
    label.Location = Point(20, 18)
    label.Size = Size(360, 35)
    label.TextAlign = ContentAlignment.MiddleCenter
    form.Controls.Add(label)

    selected = [None]
    handlers = []

    def add_side_button(text, x, y):
        button = Button()
        button.Text = text
        button.Font = Font('Segoe UI', 10, FontStyle.Regular)
        button.Location = Point(x, y)
        button.Size = Size(130, 42)
        button.FlatStyle = FlatStyle.Flat
        button.UseVisualStyleBackColor = False
        button.BackColor = Color.FromArgb(45, 45, 48)
        button.ForeColor = Color.White
        button.FlatAppearance.BorderColor = Color.FromArgb(105, 105, 105)
        button.FlatAppearance.MouseOverBackColor = Color.FromArgb(62, 62, 66)

        def on_click(sender, args):
            selected[0] = text
            form.DialogResult = DialogResult.OK
            form.Close()

        handlers.append(on_click)
        button.Click += on_click
        form.Controls.Add(button)

    add_side_button(SIDE_TOP, 135, 65)
    add_side_button(SIDE_LEFT, 35, 117)
    add_side_button(SIDE_RIGHT, 235, 117)
    add_side_button(SIDE_BOTTOM, 135, 169)

    cancel_button = Button()
    cancel_button.Text = u'Отмена'
    cancel_button.Font = Font('Segoe UI', 9, FontStyle.Regular)
    cancel_button.Location = Point(135, 230)
    cancel_button.Size = Size(130, 32)
    cancel_button.FlatStyle = FlatStyle.Flat
    cancel_button.UseVisualStyleBackColor = False
    cancel_button.BackColor = Color.FromArgb(55, 55, 58)
    cancel_button.ForeColor = Color.White
    cancel_button.DialogResult = DialogResult.Cancel
    form.CancelButton = cancel_button
    form.Controls.Add(cancel_button)

    result = form.ShowDialog()
    form.Dispose()
    if result == DialogResult.OK:
        return selected[0]
    return None


def choose_side():
    try:
        return forms.CommandSwitchWindow.show(
            SIDE_OPTIONS,
            message=u'Выберите сторону размещения РК',
        )
    except Exception:
        logger.exception(u'Не удалось открыть CommandSwitchWindow.')
        return show_dark_side_window()


def preflight():
    try:
        symbol = find_family_symbol()
    except Exception as error:
        raise RkError(u'Поиск семейства РК: {0!r}'.format(error))
    return symbol


def run_loop(symbol):
    while True:
        try:
            with forms.WarningBar(title=u'Укажите точку на трассе для врезки РК. Esc — завершить'):
                ref = uidoc.Selection.PickObject(
                    ObjectType.PointOnElement, 
                    ConduitFilter(), 
                    u'Укажите точку на трассе'
                )
        except OperationCanceledException:
            break

        side_name = choose_side()
        if side_name is None:
            break

        try:
            insert_rk_into_route(symbol, ref, side_name)
        except Exception as error:
            logger.exception(u'Не удалось внедрить РК.')
            forms.alert(
                u'Врезка не удалась. Все изменения отменены.\n\n{0}'.format(error),
                title=u'Внедрить РК',
                warn_icon=True,
            )


def main():
    try:
        symbol = preflight()
    except Exception as error:
        forms.alert(
            u'{0}\n\n{1}'.format(error, traceback.format_exc()),
            title=u'Внедрить РК',
            warn_icon=True,
        )
        return
    run_loop(symbol)


if __name__ == '__main__':
    main()
