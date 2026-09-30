# -*- coding: utf-8 -*-
__title__ = 'Лист выбранных цепей'
__author__ = 'Антон Манаков'
__doc__ = '''Показывает все электрические цепи проекта и позволяет выбрать
одну или несколько цепей. Для выбранных цепей скрипт получает щит и
электроприемники из ElectricalSystem, восстанавливает физическую трассу
по соединителям и выделяет найденные элементы в Revit. Если непрерывный
путь непосредственно от щита отсутствует, трасса определяется по
связанному компоненту коробов и ElectricalSystem конечных элементов.

После анализа скрипт создаёт новый лист с тем же штампом, дублирует
основной план с детализацией и создаёт отдельный шаблон вида. Цветовые
фильтры приборов, оборудования и трассы копируются в новый шаблон,
но их правила заменяются на уникальные значения встроенного параметра
Примечание. Существующий текст Примечание сохраняется.

Остальные элементы ЭОМ скрываются точным фильтром по ID. Исходный лист,
исходный вид, исходный шаблон и исходные фильтры не изменяются.

Эталонный viewport указывается мышью на исходном листе. Детали внутри
вида копируются с WithDetailing, а листовые надписи и детали — с
Transform.Identity в исходных XY-координатах.

Рамка плана, аннотационная обрезка, положение штампа и видового
экрана восстанавливаются по исходному листу. Центр Crop Box переносится
в мировых координатах модели.

Version = 2.12
Date = 31.08.2026
'''
__min_revit_ver__ = 2023
__max_revit_ver__ = 2023

import collections
import clr
import heapq
import re
import System
import traceback

from Autodesk.Revit.DB import *
import Autodesk.Revit.UI.Selection as UISelection
from pyrevit import DB, revit, forms, script
from System.Collections.Generic import List


doc = __revit__.ActiveUIDocument.Document  # type: DB.Document
uidoc = __revit__.ActiveUIDocument         # type: DB.UIDocument
app = __revit__.Application                # type: DB.Application
output = script.get_output()


ROUTE_FILTER_PARAMETER = u'Примечание'
AUTO_TOKEN_PREFIX = u'PYREVIT_EOM'
TARGET_SHEET_TITLE_TEXT_TYPE = u'Galf_Текст_5'

try:
    COMMENTS_PARAMETER_ID = DB.ElementId(
        DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
    )
except Exception:
    COMMENTS_PARAMETER_ID = None


# Категории, которые считаются элементами физической трассы.
ROUTE_CATEGORIES = [
    DB.BuiltInCategory.OST_CableTray,
    DB.BuiltInCategory.OST_CableTrayFitting,
    DB.BuiltInCategory.OST_Conduit,
    DB.BuiltInCategory.OST_ConduitFitting
]

ROUTE_CATEGORY_IDS = set(
    int(category) for category in ROUTE_CATEGORIES
)

ROUTE_FITTING_CATEGORY_IDS = set([
    int(DB.BuiltInCategory.OST_CableTrayFitting),
    int(DB.BuiltInCategory.OST_ConduitFitting)
])

# Категории ЭОМ, которые можно скрывать на созданной копии вида.
# Названия используются вместо прямого обращения к enum, чтобы отсутствие
# редкой категории в конкретной сборке Revit не останавливало весь скрипт.
ELECTRICAL_CATEGORY_NAMES = [
    'OST_ElectricalEquipment',
    'OST_ElectricalFixtures',
    'OST_LightingFixtures',
    'OST_LightingDevices',
    'OST_DataDevices',
    'OST_CommunicationDevices',
    'OST_FireAlarmDevices',
    'OST_SecurityDevices',
    'OST_NurseCallDevices',
    'OST_TelephoneDevices'
]


def get_controlled_category_ids():
    """Формирует набор ID категорий ЭОМ для управления видимостью."""
    category_ids = set(ROUTE_CATEGORY_IDS)
    for category_name in ELECTRICAL_CATEGORY_NAMES:
        try:
            category = getattr(DB.BuiltInCategory, category_name)
            category_ids.add(int(category))
        except Exception:
            pass
    return category_ids


CONTROLLED_CATEGORY_IDS = get_controlled_category_ids()

# Минимальная условная стоимость фитинга или семейства при поиске пути.
# Длины прямых участков берутся из геометрии Revit.
MIN_PATH_COST_FT = 1.0 / 304.8


def get_id_value(element_or_id):
    """Возвращает числовое значение ElementId."""
    if element_or_id is None:
        return None

    try:
        element_id = element_or_id.Id
    except Exception:
        element_id = element_or_id

    try:
        return element_id.IntegerValue
    except Exception:
        return None


def get_element_name(element):
    """Получает понятное имя или марку элемента."""
    if element is None:
        return u'Без элемента'

    for built_in_parameter in [
            DB.BuiltInParameter.ALL_MODEL_MARK,
            DB.BuiltInParameter.ELEM_TYPE_PARAM]:
        try:
            parameter = element.get_Parameter(built_in_parameter)
            if parameter and parameter.HasValue:
                value = parameter.AsString()
                if not value:
                    value = parameter.AsValueString()
                if value and value.strip():
                    return value.strip()
        except Exception:
            pass

    try:
        if element.Name:
            return element.Name
    except Exception:
        pass

    try:
        symbol = element.Symbol
        if symbol and symbol.Family:
            return symbol.Family.Name
    except Exception:
        pass

    element_id = get_id_value(element)
    if element_id is not None:
        return u'Элемент {}'.format(element_id)
    return u'Без имени'


def get_connectors(element):
    """Возвращает соединители MEPCurve или FamilyInstance."""
    connectors = []
    if element is None:
        return connectors

    try:
        connector_manager = element.ConnectorManager
        if connector_manager:
            connectors.extend(
                connector for connector in connector_manager.Connectors
            )
            return connectors
    except Exception:
        pass

    try:
        mep_model = element.MEPModel
        if mep_model and mep_model.ConnectorManager:
            connectors.extend(
                connector
                for connector in mep_model.ConnectorManager.Connectors
            )
    except Exception:
        pass

    return connectors


def is_physical_connector(connector):
    """Исключает логические соединители электрической системы."""
    try:
        return connector.ConnectorType != DB.ConnectorType.Logical
    except Exception:
        return False


def is_route_element(element):
    """Проверяет, является ли элемент частью короба или трубы."""
    if element is None:
        return False

    try:
        category = element.Category
        if category is None:
            return False
        return category.Id.IntegerValue in ROUTE_CATEGORY_IDS
    except Exception:
        return False


def collect_electrical_circuits():
    """Собирает нативные объекты ElectricalSystem из текущего проекта."""
    collector = DB.FilteredElementCollector(doc)
    circuits = collector.OfClass(
        DB.Electrical.ElectricalSystem
    ).WhereElementIsNotElementType().ToElements()
    return list(circuits)


def collect_route_elements():
    """Собирает короба, трубы и их фитинги."""
    category_ids = List[DB.BuiltInCategory]()
    for category in ROUTE_CATEGORIES:
        category_ids.Add(category)

    category_filter = DB.ElementMulticategoryFilter(
        category_ids
    )
    collector = DB.FilteredElementCollector(doc)
    elements = collector.WhereElementIsNotElementType().WherePasses(
        category_filter
    ).ToElements()
    return list(elements)


def get_circuit_members(circuit):
    """Получает электроприемники непосредственно из ElectricalSystem."""
    members = []
    seen_ids = set()

    try:
        circuit_elements = circuit.Elements
    except Exception:
        circuit_elements = None

    if circuit_elements:
        for element in circuit_elements:
            element_id = get_id_value(element)
            if element_id is None or element_id in seen_ids:
                continue
            seen_ids.add(element_id)
            members.append(element)

    return members


def get_circuit_panel(circuit):
    """Получает питающий щит непосредственно из ElectricalSystem."""
    try:
        return circuit.BaseEquipment
    except Exception:
        return None


def safe_property_text(element, property_name, default_value):
    """Безопасно получает строковое свойство Revit API."""
    try:
        value = getattr(element, property_name)
        if value is None:
            return default_value
        text = unicode(value).strip()
        return text if text else default_value
    except Exception:
        return default_value


def natural_sort_key(value):
    """Создаёт ключ натуральной сортировки: 1, 2, 10 вместо 1, 10, 2."""
    parts = re.split(r'(\d+)', value)
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def make_circuit_label(circuit):
    """Создаёт уникальную подпись цепи для окна выбора."""
    panel = get_circuit_panel(circuit)
    panel_name = get_element_name(panel) if panel else u'Без щита'
    circuit_number = safe_property_text(
        circuit,
        'CircuitNumber',
        u'Без номера'
    )
    load_name = safe_property_text(
        circuit,
        'LoadName',
        u'Без имени нагрузки'
    )
    system_type = safe_property_text(
        circuit,
        'SystemType',
        u'Тип не определён'
    )
    circuit_id = get_id_value(circuit)

    return (
        u'{0} | группа {1} | {2} | {3} | ID {4}'
        .format(
            panel_name,
            circuit_number,
            load_name,
            system_type,
            circuit_id
        )
    )


def select_circuits(circuits):
    """Показывает окно со всеми найденными электрическими цепями."""
    circuits_by_label = {}

    for circuit in circuits:
        label = make_circuit_label(circuit)
        circuits_by_label[label] = circuit

    labels = sorted(circuits_by_label.keys(), key=natural_sort_key)

    selected_labels = forms.SelectFromList.show(
        labels,
        title=u'Электрические цепи проекта',
        button_name=u'Показать и выделить',
        multiselect=True,
        width=1000,
        height=650
    )

    if not selected_labels:
        return []

    return [circuits_by_label[label] for label in selected_labels]


def build_physical_graph(route_elements):
    """Строит граф реальных соединений трасс и подключённых семейств.

    В граф попадают все участки коробов/труб и каждый элемент, который
    физически присоединён к ним. Логические соединения ElectricalSystem
    намеренно игнорируются.
    """
    graph = collections.defaultdict(set)
    element_map = {}
    route_ids = set()

    for element in route_elements:
        element_id = get_id_value(element)
        if element_id is None:
            continue
        route_ids.add(element_id)
        element_map[element_id] = element
        graph[element_id]

    for element in route_elements:
        element_id = get_id_value(element)
        if element_id is None:
            continue

        for connector in get_connectors(element):
            if not is_physical_connector(connector):
                continue

            try:
                if not connector.IsConnected:
                    continue
            except Exception:
                continue

            try:
                references = connector.AllRefs
            except Exception:
                continue

            for reference in references:
                try:
                    owner = reference.Owner
                    owner_id = get_id_value(owner)
                except Exception:
                    continue

                if owner_id is None or owner_id == element_id:
                    continue

                # Элементы из связей не могут образовывать физическое
                # соединение с элементами текущего документа.
                try:
                    if owner.Document != doc:
                        continue
                except Exception:
                    continue

                graph[element_id].add(owner_id)
                graph[owner_id].add(element_id)
                element_map[owner_id] = owner

    return graph, element_map, route_ids


def get_element_electrical_system_ids(element):
    """Получает ID нативных ElectricalSystem подключённого элемента."""
    system_ids = set()
    if element is None:
        return system_ids

    try:
        mep_model = element.MEPModel
        if mep_model:
            systems = mep_model.GetElectricalSystems()
            if systems:
                for electrical_system in systems:
                    if isinstance(
                            electrical_system,
                            DB.Electrical.ElectricalSystem):
                        system_id = get_id_value(electrical_system)
                        if system_id is not None:
                            system_ids.add(system_id)
    except Exception:
        pass

    for connector in get_connectors(element):
        try:
            electrical_system = connector.MEPSystem
            if isinstance(
                    electrical_system,
                    DB.Electrical.ElectricalSystem):
                system_id = get_id_value(electrical_system)
                if system_id is not None:
                    system_ids.add(system_id)
        except Exception:
            pass

    return system_ids


def build_electrical_system_seed_map(element_map):
    """Связывает ElectricalSystem с физически подключёнными концами трасс."""
    seed_map = collections.defaultdict(set)

    for element_id, element in element_map.items():
        for system_id in get_element_electrical_system_ids(element):
            seed_map[system_id].add(element_id)

    return seed_map


def collect_connected_component(graph, seed_ids):
    """Собирает физически связанную сеть от одного или нескольких узлов."""
    valid_seeds = set(
        seed_id for seed_id in seed_ids
        if seed_id in graph
    )
    if not valid_seeds:
        return set()

    visited = set(valid_seeds)
    queue = collections.deque(valid_seeds)

    while queue:
        current_id = queue.popleft()
        for neighbor_id in graph.get(current_id, set()):
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            queue.append(neighbor_id)

    return visited


def get_path_cost(element, route_ids):
    """Возвращает стоимость прохождения элемента при поиске пути."""
    element_id = get_id_value(element)
    if element_id not in route_ids:
        return MIN_PATH_COST_FT

    try:
        if isinstance(element, DB.MEPCurve):
            length_parameter = element.get_Parameter(
                DB.BuiltInParameter.CURVE_ELEM_LENGTH
            )
            if length_parameter:
                length = length_parameter.AsDouble()
                if length > 0:
                    return length
    except Exception:
        pass

    return MIN_PATH_COST_FT


def find_paths_from_panel(graph, element_map, route_ids, panel_id):
    """Строит дерево кратчайших физических путей от щита."""
    distances = {panel_id: 0.0}
    previous = {}
    queue = [(0.0, panel_id)]

    while queue:
        current_distance, current_id = heapq.heappop(queue)

        if current_distance > distances.get(current_id, float('inf')):
            continue

        for neighbor_id in sorted(graph.get(current_id, [])):
            neighbor = element_map.get(neighbor_id)
            if neighbor is None:
                continue

            new_distance = (
                current_distance + get_path_cost(neighbor, route_ids)
            )

            if new_distance >= distances.get(neighbor_id, float('inf')):
                continue

            distances[neighbor_id] = new_distance
            previous[neighbor_id] = current_id
            heapq.heappush(queue, (new_distance, neighbor_id))

    return distances, previous


def restore_path(previous, source_id, target_id):
    """Восстанавливает путь между источником и найденной целью."""
    if target_id == source_id:
        return [source_id]

    if target_id not in previous:
        return []

    path = [target_id]
    current_id = target_id

    while current_id != source_id:
        current_id = previous.get(current_id)
        if current_id is None:
            return []
        path.append(current_id)

    path.reverse()
    return path


def find_circuit_route(
        circuit,
        graph,
        element_map,
        route_ids,
        panel_path_cache=None,
        electrical_system_seed_map=None):
    """Находит физические пути от щита до всех элементов одной цепи."""
    result = {
        'circuit': circuit,
        'panel': None,
        'members': [],
        'native_ids': set(),
        'path_ids': set(),
        'route_ids': set(),
        'transit_ids': set(),
        'connected_member_ids': set(),
        'unconnected_member_ids': set(),
        'detection_mode': u'Не найдено',
        'message': u''
    }

    panel = get_circuit_panel(circuit)
    members = get_circuit_members(circuit)
    result['panel'] = panel
    result['members'] = members

    panel_id = get_id_value(panel)
    member_ids = set(
        element_id
        for element_id in [get_id_value(item) for item in members]
        if element_id is not None
    )

    result['native_ids'].update(member_ids)
    if panel_id is not None:
        result['native_ids'].add(panel_id)

    # Сначала пробуем наиболее точный вариант: кратчайшие физические
    # пути непосредственно от BaseEquipment щита до приборов цепи.
    if panel_id is not None and panel_id in graph:
        if panel_path_cache is not None and panel_id in panel_path_cache:
            distances, previous = panel_path_cache[panel_id]
        else:
            distances, previous = find_paths_from_panel(
                graph,
                element_map,
                route_ids,
                panel_id
            )
            if panel_path_cache is not None:
                panel_path_cache[panel_id] = (distances, previous)

        for member_id in member_ids:
            if member_id not in distances:
                result['unconnected_member_ids'].add(member_id)
                continue

            path = restore_path(previous, panel_id, member_id)
            if not path:
                result['unconnected_member_ids'].add(member_id)
                continue

            result['connected_member_ids'].add(member_id)
            result['path_ids'].update(path)
    else:
        result['unconnected_member_ids'].update(member_ids)

    result['route_ids'] = result['path_ids'].intersection(route_ids)
    result['transit_ids'] = (
        result['path_ids']
        .difference(result['route_ids'])
        .difference(result['native_ids'])
    )

    if result['route_ids']:
        result['detection_mode'] = u'Кратчайшие пути от щита'

    # Если щит не имеет физического коннектора или ни один прибор не
    # дал непрерывного пути, используем логику исходного рабочего
    # скрипта: берём весь связанный компонент коробов от конечных
    # элементов, которые принадлежат этой нативной ElectricalSystem.
    if not result['route_ids']:
        circuit_id = get_id_value(circuit)
        component_seed_ids = set()

        for member_id in member_ids:
            if member_id in graph:
                component_seed_ids.add(member_id)

        if electrical_system_seed_map is not None:
            component_seed_ids.update(
                electrical_system_seed_map.get(circuit_id, set())
            )

        # Последний резерв: если у подключённых концов не удалось
        # прочитать систему, начинаем от физически подключённого щита.
        if not component_seed_ids and panel_id in graph:
            component_seed_ids.add(panel_id)

        component_ids = collect_connected_component(
            graph,
            component_seed_ids
        )
        component_route_ids = component_ids.intersection(route_ids)

        if component_route_ids:
            # В набор отображения добавляем саму трассу и только те
            # внешние узлы, которые послужили семенами выбранной цепи.
            # Остальные приборы других цепей из общего компонента сюда
            # попадать не должны.
            result['path_ids'].update(component_route_ids)
            result['path_ids'].update(component_seed_ids)
            result['route_ids'].update(component_route_ids)
            result['transit_ids'] = (
                result['path_ids']
                .difference(result['route_ids'])
                .difference(result['native_ids'])
            )
            result['detection_mode'] = u'Связанный компонент сети'

            for member_id in member_ids:
                if member_id in component_ids:
                    result['connected_member_ids'].add(member_id)
                    result['unconnected_member_ids'].discard(member_id)

    if not members:
        result['message'] = u'В цепи нет электроприемников.'
    elif not result['route_ids']:
        if panel is None or panel_id is None:
            result['message'] = u'У цепи не назначен щит; трасса не найдена.'
        elif panel_id not in graph:
            result['message'] = (
                u'Щит не подключён физическим коннектором, и связанный '
                u'компонент трассы по элементам цепи не найден.'
            )
        else:
            result['message'] = u'Физическая трасса цепи не найдена.'
    elif result['detection_mode'] == u'Связанный компонент сети':
        result['message'] = (
            u'Трасса найдена по связанному компоненту сети. '
            u'Непрерывный путь непосредственно от щита отсутствует.'
        )
    elif result['unconnected_member_ids']:
        result['message'] = (
            u'Трасса найдена частично: не все приборы физически '
            u'соединены со щитом.'
        )
    else:
        result['message'] = u'Трасса найдена полностью.'

    return result


def make_element_id_list(integer_ids):
    """Преобразует Python-набор ID в ICollection<ElementId>."""
    element_ids = List[DB.ElementId]()
    for integer_id in sorted(integer_ids):
        element_ids.Add(DB.ElementId(integer_id))
    return element_ids


def print_result(result):
    """Печатает подробный результат для одной цепи."""
    circuit = result['circuit']
    panel = result['panel']
    circuit_number = safe_property_text(
        circuit,
        'CircuitNumber',
        u'Без номера'
    )
    load_name = safe_property_text(
        circuit,
        'LoadName',
        u'Без имени нагрузки'
    )

    print(u'Группа: {0} | Нагрузка: {1}'.format(
        circuit_number,
        load_name
    ))
    print(u'  Щит: {0}'.format(get_element_name(panel)))
    print(u'  Приборов в ElectricalSystem: {0}'.format(
        len(result['members'])
    ))
    print(u'  Приборов с найденным путём: {0}'.format(
        len(result['connected_member_ids'])
    ))
    print(u'  Элементов коробов/труб: {0}'.format(
        len(result['route_ids'])
    ))
    print(u'  Промежуточных коробок/семейств: {0}'.format(
        len(result['transit_ids'])
    ))
    print(u'  Метод поиска: {0}'.format(
        result['detection_mode']
    ))
    print(u'  Результат: {0}'.format(result['message']))

    if result['unconnected_member_ids']:
        print(u'  Приборы без физического пути:')
        for element_id in sorted(result['unconnected_member_ids']):
            element = doc.GetElement(DB.ElementId(element_id))
            print(u'    - {0}, ID {1}'.format(
                get_element_name(element),
                element_id
            ))

    print(u'')


def select_and_show(integer_ids):
    """Выделяет элементы и пытается показать их на активном виде."""
    if not integer_ids:
        return False, u'Нет элементов для выделения.'

    element_ids = make_element_id_list(integer_ids)
    uidoc.Selection.SetElementIds(element_ids)

    try:
        uidoc.ShowElements(element_ids)
        uidoc.RefreshActiveView()
        return True, u'Элементы выделены и показаны.'
    except Exception as error:
        # Выделение уже выполнено. ShowElements может не сработать, если
        # элементы находятся на разных уровнях или не видны на текущем виде.
        return False, (
            u'Элементы выделены, но Revit не смог показать их все '
            u'на активном виде: {0}'.format(error)
        )


# ============ ДИАГНОСТИКА ЦВЕТОВЫХ ФИЛЬТРОВ ============


COLOR_OVERRIDE_PROPERTIES = [
    ('ProjectionLineColor', u'линия проекции'),
    ('CutLineColor', u'линия разреза'),
    (
        'SurfaceForegroundPatternColor',
        u'заливка поверхности, передний план'
    ),
    (
        'SurfaceBackgroundPatternColor',
        u'заливка поверхности, фон'
    ),
    (
        'CutForegroundPatternColor',
        u'заливка разреза, передний план'
    ),
    ('CutBackgroundPatternColor', u'заливка разреза, фон')
]


def get_assigned_view_template(view):
    """Возвращает назначенный виду шаблон или None."""
    try:
        template_id = view.ViewTemplateId
        if (
                template_id
                and template_id != DB.ElementId.InvalidElementId):
            template = doc.GetElement(template_id)
            if template is not None and template.IsTemplate:
                return template
    except Exception:
        pass
    return None


def template_controls_parameter(view_template, built_in_parameter):
    """Проверяет, включён ли раздел в управление шаблоном.

    None означает, что Revit API не дал надёжно определить
    состояние. В этом случае будут проверены и вид, и шаблон.
    """
    if view_template is None:
        return False

    try:
        parameter_id_value = int(built_in_parameter)
        noncontrolled_ids = view_template.GetNonControlledTemplateParameterIds()
        noncontrolled_values = set(
            element_id.IntegerValue for element_id in noncontrolled_ids
        )
        return parameter_id_value not in noncontrolled_values
    except Exception:
        return None


def get_filter_hosts(view):
    """Возвращает виды, где фактически хранятся настройки фильтров."""
    view_template = get_assigned_view_template(view)
    if view_template is None:
        return [(u'Вид', view)], None, False

    controls_filters = None
    try:
        controls_filters = template_controls_parameter(
            view_template,
            DB.BuiltInParameter.VIS_GRAPHICS_FILTERS
        )
    except Exception:
        pass

    if controls_filters is True:
        return [(u'Шаблон вида', view_template)], view_template, True

    if controls_filters is False:
        return [(u'Вид', view)], view_template, False

    return [
        (u'Шаблон вида', view_template),
        (u'Вид', view)
    ], view_template, None


def get_ordered_filter_ids(filter_host):
    """Получает фильтры в порядке, показанном Revit."""
    try:
        return list(filter_host.GetOrderedFilters())
    except Exception:
        try:
            return list(filter_host.GetFilters())
        except Exception:
            return []


def color_to_text(color):
    """Преобразует корректный DB.Color в RGB-строку."""
    if color is None:
        return None

    try:
        if not color.IsValid:
            return None
    except Exception:
        pass

    try:
        return u'RGB({0}, {1}, {2})'.format(
            color.Red,
            color.Green,
            color.Blue
        )
    except Exception:
        return None


def get_override_colors(override_settings):
    """Возвращает все явно заданные цвета OverrideGraphicSettings."""
    colors = []

    for property_name, label in COLOR_OVERRIDE_PROPERTIES:
        try:
            color = getattr(override_settings, property_name)
        except Exception:
            continue

        color_text = color_to_text(color)
        if color_text:
            colors.append(u'{0}: {1}'.format(label, color_text))

    return colors


def get_filter_enabled(filter_host, filter_id):
    """Безопасно читает флаг «Включить фильтр»."""
    try:
        return bool(filter_host.GetIsFilterEnabled(filter_id))
    except Exception:
        # В старых сборках API отдельного флага нет.
        return True


def get_filter_visibility(filter_host, filter_id):
    """Безопасно читает флаг видимости фильтра."""
    try:
        return bool(filter_host.GetFilterVisibility(filter_id))
    except Exception:
        return True


def get_parameter_filter_category_ids(filter_element):
    """Получает ID категорий ParameterFilterElement."""
    try:
        return set(
            category_id.IntegerValue
            for category_id in filter_element.GetCategories()
        )
    except Exception:
        return set()


def element_passes_parameter_filter(filter_element, element):
    """Проверяет один элемент по правилам ParameterFilterElement."""
    if element is None:
        return False

    category_ids = get_parameter_filter_category_ids(filter_element)
    if category_ids:
        try:
            if element.Category.Id.IntegerValue not in category_ids:
                return False
        except Exception:
            return False

    try:
        element_filter = filter_element.GetElementFilter()
    except Exception:
        return False

    # Сигнатура Document + ElementId менее двусмысленна
    # для IronPython, чем перегрузка с одним Element.
    try:
        return bool(element_filter.PassesFilter(doc, element.Id))
    except Exception:
        try:
            return bool(element_filter.PassesFilter(element))
        except Exception:
            return False


def get_filter_matched_ids(filter_element, integer_ids):
    """Возвращает ID элементов группы, попавших под фильтр."""
    matched_ids = set()

    if isinstance(filter_element, DB.SelectionFilterElement):
        try:
            filter_ids = set(
                element_id.IntegerValue
                for element_id in filter_element.GetElementIds()
            )
            return set(integer_ids).intersection(filter_ids)
        except Exception:
            return matched_ids

    if not isinstance(filter_element, DB.ParameterFilterElement):
        return matched_ids

    for integer_id in integer_ids:
        element = doc.GetElement(DB.ElementId(integer_id))
        if element_passes_parameter_filter(filter_element, element):
            matched_ids.add(integer_id)

    return matched_ids


def get_filter_kind(filter_element):
    """Возвращает понятный тип фильтра."""
    if isinstance(filter_element, DB.ParameterFilterElement):
        return u'фильтр по параметрам'
    if isinstance(filter_element, DB.SelectionFilterElement):
        return u'фильтр по ID'
    return filter_element.GetType().Name


def get_result_id_groups(result):
    """Разделяет результат цепи на приборы, щит и трассу."""
    member_ids = set()
    for member in result.get('members', []):
        member_id = get_id_value(member)
        if member_id is not None:
            member_ids.add(member_id)

    panel_ids = set()
    panel_id = get_id_value(result.get('panel'))
    if panel_id is not None:
        panel_ids.add(panel_id)

    route_ids = set(result.get('route_ids', set()))
    transit_ids = set(result.get('transit_ids', set()))

    all_ids = set(member_ids)
    all_ids.update(panel_ids)
    all_ids.update(route_ids)
    all_ids.update(transit_ids)

    return {
        'member_ids': member_ids,
        'panel_ids': panel_ids,
        'route_ids': route_ids,
        'transit_ids': transit_ids,
        'all_ids': all_ids
    }


def expand_family_instance_ids(integer_ids):
    """Добавляет родительские и вложенные экземпляры выбранных семейств.

    Электрическая цепь может содержать родительский FamilyInstance, тогда
    как видимая графика розетки выполнена вложенным общим семейством с
    отдельным ElementId. Без расширения вложенный элемент ошибочно попадает
    в фильтр «Скрыть остальные ЭОМ».
    """
    expanded_ids = set(integer_ids)
    queue = collections.deque(expanded_ids)

    while queue:
        current_id = queue.popleft()
        element = doc.GetElement(DB.ElementId(current_id))
        if element is None or not isinstance(element, DB.FamilyInstance):
            continue

        related_ids = []
        try:
            related_ids.extend(element.GetSubComponentIds())
        except Exception:
            pass

        try:
            super_component = element.SuperComponent
            if super_component is not None:
                related_ids.append(super_component.Id)
        except Exception:
            pass

        for related_id in related_ids:
            related_value = get_id_value(related_id)
            if related_value is None or related_value in expanded_ids:
                continue
            if doc.GetElement(DB.ElementId(related_value)) is None:
                continue
            expanded_ids.add(related_value)
            queue.append(related_value)

    return expanded_ids


def normalize_filter_pair_name(value):
    """Убирает из имени слова «короб», «лоток», «трасса» и «труба».

    Благодаря этому имена «Розеточная сеть» и
    «Короб_Розеточная сеть» считаются парой.
    """
    try:
        text = unicode(value)
    except Exception:
        text = u'{0}'.format(value)

    text = text.lower().replace(u'_', u' ').replace(u'-', u' ')
    route_words = re.compile(
        u'(?:кабельн[0-9a-zа-яё]*\\s+)?'
        u'(?:короб[0-9a-zа-яё]*|'
        u'лоток[0-9a-zа-яё]*|'
        u'трасс[0-9a-zа-яё]*|'
        u'труб[0-9a-zа-яё]*)',
        re.IGNORECASE | re.UNICODE
    )
    text = route_words.sub(u' ', text)
    non_word_characters = re.compile(
        u'[^0-9a-zа-яё]+',
        re.IGNORECASE | re.UNICODE
    )
    text = non_word_characters.sub(u'', text)
    return text


def get_rgb_values(color_descriptions):
    """Извлекает из текста набор RGB-троек для сравнения фильтров."""
    values = set()
    pattern = re.compile(
        r'RGB\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)'
    )

    for description in color_descriptions:
        match = pattern.search(description)
        if not match:
            continue
        values.add(tuple(int(value) for value in match.groups()))

    return values


def format_rgb_values(rgb_values):
    """Форматирует набор RGB-троек для отчёта."""
    if not rgb_values:
        return u'без RGB'

    return u', '.join(
        u'RGB({0}, {1}, {2})'.format(*rgb)
        for rgb in sorted(rgb_values)
    )


def choose_primary_device_filter(color_records):
    """Выбирает цветовой фильт с наибольшим охватом приборов цепи."""
    candidates = [
        record for record in color_records
        if record['matched_member_ids']
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda item: (
        -len(item['matched_member_ids']),
        item['order'],
        item['filter'].Id.IntegerValue
    ))
    return candidates[0]


def choose_primary_route_filter(color_records, device_record):
    """Выбирает фильтр трассы по паре имени, RGB и охвату коробов."""
    candidates = [
        record for record in color_records
        if record['matched_route_ids']
    ]
    if not candidates:
        return None, u'цветовые фильтры трассы не найдены'

    device_name = u''
    device_rgb = set()
    if device_record is not None:
        device_name = normalize_filter_pair_name(
            device_record['filter'].Name
        )
        device_rgb = device_record['rgb_values']

    for record in candidates:
        route_name = normalize_filter_pair_name(
            record['filter'].Name
        )
        record['pair_name_match'] = bool(
            device_name
            and route_name
            and route_name == device_name
        )
        record['pair_color_match'] = bool(
            device_rgb.intersection(record['rgb_values'])
        )

    candidates.sort(key=lambda item: (
        -int(item['pair_name_match']),
        -int(item['pair_color_match']),
        -len(item['matched_route_ids']),
        item['order'],
        item['filter'].Id.IntegerValue
    ))
    primary = candidates[0]

    if primary['pair_name_match'] and primary['pair_color_match']:
        reason = u'совпали имя сети и RGB фильтра приборов'
    elif primary['pair_name_match']:
        reason = u'совпало имя сети с фильтром приборов'
    elif primary['pair_color_match']:
        reason = u'совпал RGB с фильтром приборов'
    elif device_record is None:
        reason = u'фильтр приборов не найден; выбран наибольший охват трассы'
    else:
        reason = (
            u'совпадение имени и RGB не найдено; '
            u'выбран наибольший охват трассы'
        )

    return primary, reason


def analyze_filters_for_group(view, result):
    """Отдельно ищет цветовые фильтры приборов и физической трассы."""
    id_groups = get_result_id_groups(result)
    filter_hosts, view_template, template_controls_filters = (
        get_filter_hosts(view)
    )
    records = []
    seen_pairs = set()

    for host_kind, filter_host in filter_hosts:
        ordered_filter_ids = get_ordered_filter_ids(filter_host)

        for order_index, filter_id in enumerate(ordered_filter_ids):
            pair_key = (
                filter_host.Id.IntegerValue,
                filter_id.IntegerValue
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            filter_element = doc.GetElement(filter_id)
            if filter_element is None:
                continue

            matched_ids = get_filter_matched_ids(
                filter_element,
                id_groups['all_ids']
            )
            if not matched_ids:
                continue

            try:
                override_settings = filter_host.GetFilterOverrides(filter_id)
                colors = get_override_colors(override_settings)
            except Exception:
                colors = []

            enabled = get_filter_enabled(filter_host, filter_id)
            visible = get_filter_visibility(filter_host, filter_id)

            records.append({
                'host_kind': host_kind,
                'host': filter_host,
                'filter': filter_element,
                'filter_kind': get_filter_kind(filter_element),
                'order': order_index + 1,
                'matched_ids': matched_ids,
                'matched_member_ids': matched_ids.intersection(
                    id_groups['member_ids']
                ),
                'matched_panel_ids': matched_ids.intersection(
                    id_groups['panel_ids']
                ),
                'matched_route_ids': matched_ids.intersection(
                    id_groups['route_ids']
                ),
                'matched_transit_ids': matched_ids.intersection(
                    id_groups['transit_ids']
                ),
                'enabled': enabled,
                'visible': visible,
                'colors': colors,
                'rgb_values': get_rgb_values(colors),
                'is_color_candidate': bool(
                    enabled and visible and colors
                )
            })

    color_records = [
        record for record in records
        if record['is_color_candidate']
    ]
    primary_device_record = choose_primary_device_filter(color_records)
    primary_route_record, primary_route_reason = (
        choose_primary_route_filter(
            color_records,
            primary_device_record
        )
    )

    return {
        'records': records,
        'color_records': color_records,
        'primary_device_record': primary_device_record,
        'primary_route_record': primary_route_record,
        'primary_route_reason': primary_route_reason,
        'other_route_color_records': [
            record for record in color_records
            if (
                record['matched_route_ids']
                and record is not primary_route_record
            )
        ],
        'id_groups': id_groups,
        'view_template': view_template,
        'template_controls_filters': template_controls_filters
    }


def collect_direct_color_overrides(view, integer_ids):
    """Ищет индивидуальные цветовые переопределения элементов."""
    direct_overrides = []

    for integer_id in integer_ids:
        try:
            override_settings = view.GetElementOverrides(
                DB.ElementId(integer_id)
            )
            colors = get_override_colors(override_settings)
        except Exception:
            colors = []

        if colors:
            direct_overrides.append({
                'element_id': integer_id,
                'colors': colors
            })

    return direct_overrides


def collect_category_color_overrides(view, integer_ids):
    """Ищет цветовые переопределения категорий вида или шаблона."""
    view_template = get_assigned_view_template(view)
    category_host = view
    host_kind = u'Вид'

    if view_template is not None:
        controls_model = None
        try:
            controls_model = template_controls_parameter(
                view_template,
                DB.BuiltInParameter.VIS_GRAPHICS_MODEL
            )
        except Exception:
            pass

        if controls_model is True:
            category_host = view_template
            host_kind = u'Шаблон вида'

    categories = {}
    for integer_id in integer_ids:
        element = doc.GetElement(DB.ElementId(integer_id))
        try:
            category = element.Category
            if category is not None:
                categories[category.Id.IntegerValue] = category
        except Exception:
            pass

    category_overrides = []
    for category_id_value, category in categories.items():
        try:
            override_settings = category_host.GetCategoryOverrides(
                DB.ElementId(category_id_value)
            )
            colors = get_override_colors(override_settings)
        except Exception:
            colors = []

        if colors:
            category_overrides.append({
                'host_kind': host_kind,
                'host': category_host,
                'category': category,
                'colors': colors
            })

    return category_overrides


def make_result_element_ids(result):
    """Формирует полный набор ID одной выбранной цепи."""
    return get_result_id_groups(result)['all_ids']


def is_route_fitting_element(element):
    """Проверяет, является ли элемент фитингом короба или трубы."""
    if element is None:
        return False

    try:
        return (
            element.Category.Id.IntegerValue
            in ROUTE_FITTING_CATEGORY_IDS
        )
    except Exception:
        return False


def expand_route_with_adjacent_fittings(
        seed_route_ids,
        result_route_ids,
        graph,
        element_map):
    """Добавляет к отфильтрованным участкам смежные фитинги.

    Прямые участки MEPCurve, которые не попали под нужный
    цветовой фильтр, не добавляются. Это не даёт перейти
    из розеточной трассы в осветительную.
    """
    result_route_ids = set(result_route_ids)
    expanded_ids = set(seed_route_ids).intersection(result_route_ids)
    queue = collections.deque(expanded_ids)

    while queue:
        current_id = queue.popleft()
        for neighbor_id in graph.get(current_id, set()):
            if neighbor_id in expanded_ids:
                continue
            if neighbor_id not in result_route_ids:
                continue

            element = element_map.get(neighbor_id)
            if element is None:
                element = doc.GetElement(DB.ElementId(neighbor_id))

            if not is_route_fitting_element(element):
                continue

            expanded_ids.add(neighbor_id)
            queue.append(neighbor_id)

    return expanded_ids


def collect_allowed_route_from_seeds(
        graph,
        seed_ids,
        allowed_route_ids,
        bridge_ids=None):
    """Оставляет только разрешённую трассу, связанную с приборами цепи."""
    seed_ids = set(seed_ids).intersection(set(graph.keys()))
    allowed_route_ids = set(allowed_route_ids)
    bridge_ids = set(bridge_ids or set())

    if not seed_ids or not allowed_route_ids:
        return set()

    allowed_node_ids = set(allowed_route_ids)
    allowed_node_ids.update(seed_ids)
    allowed_node_ids.update(bridge_ids)

    visited = set(seed_ids)
    queue = collections.deque(seed_ids)

    while queue:
        current_id = queue.popleft()
        for neighbor_id in graph.get(current_id, set()):
            if neighbor_id in visited:
                continue
            if neighbor_id not in allowed_node_ids:
                continue
            visited.add(neighbor_id)
            queue.append(neighbor_id)

    return visited.intersection(allowed_route_ids)


def build_safe_selection_for_result(
        result,
        analysis,
        graph,
        element_map):
    """Формирует безопасный набор выделения для одной цепи.

    Кратчайшие пути от щита считаются точными и не меняются.
    В резервном режиме «Связанный компонент сети» вся сеть
    не выделяется: трасса ограничивается парным цветовым
    фильтром и физической связью с приборами выбранной цепи.
    """
    id_groups = analysis['id_groups']
    native_ids = set(id_groups['member_ids'])
    native_ids.update(id_groups['panel_ids'])

    raw_route_ids = set(id_groups['route_ids'])
    detection_mode = result.get('detection_mode', u'')

    if detection_mode != u'Связанный компонент сети':
        selected_ids = set(native_ids)
        selected_ids.update(raw_route_ids)
        selected_ids.update(id_groups['transit_ids'])
        return selected_ids, {
            'restricted': False,
            'raw_route_count': len(raw_route_ids),
            'filter_route_count': len(raw_route_ids),
            'fitting_count': 0,
            'selected_route_count': len(raw_route_ids),
            'message': u'использованы кратчайшие пути от щита'
        }

    primary_route_record = analysis['primary_route_record']
    if primary_route_record is None:
        return native_ids, {
            'restricted': True,
            'raw_route_count': len(raw_route_ids),
            'filter_route_count': 0,
            'fitting_count': 0,
            'selected_route_count': 0,
            'message': (
                u'вся общая сеть отброшена: '
                u'надёжный фильтр трассы не найден'
            )
        }

    filter_route_ids = set(
        primary_route_record['matched_route_ids']
    )
    expanded_route_ids = expand_route_with_adjacent_fittings(
        filter_route_ids,
        raw_route_ids,
        graph,
        element_map
    )

    seed_ids = set(id_groups['member_ids'])
    bridge_ids = set(id_groups['panel_ids'])
    bridge_ids.update(id_groups['transit_ids'])
    selected_route_ids = collect_allowed_route_from_seeds(
        graph,
        seed_ids,
        expanded_route_ids,
        bridge_ids
    )

    selected_ids = set(native_ids)
    selected_ids.update(selected_route_ids)

    added_fitting_count = len(
        selected_route_ids.difference(filter_route_ids)
    )

    if selected_route_ids:
        message = (
            u'общая сеть ограничена фильтром «{0}» '
            u'и связью с приборами'.format(
                primary_route_record['filter'].Name
            )
        )
    else:
        message = (
            u'вся общая сеть отброшена: участки '
            u'фильтра «{0}» не имеют физической связи '
            u'с приборами цепи'.format(
                primary_route_record['filter'].Name
            )
        )

    return selected_ids, {
        'restricted': True,
        'raw_route_count': len(raw_route_ids),
        'filter_route_count': len(filter_route_ids),
        'fitting_count': added_fitting_count,
        'selected_route_count': len(selected_route_ids),
        'message': message
    }


def print_selection_restriction(result, selection_info):
    """Печатает, как общая сеть была ограничена перед выделением."""
    circuit_number = safe_property_text(
        result['circuit'],
        'CircuitNumber',
        u'Без номера'
    )

    print(u'=== НАБОР ВЫДЕЛЕНИЯ: {0} ==='.format(
        circuit_number
    ))
    source_label = (
        u'Исходный связанный компонент'
        if selection_info['restricted']
        else u'Исходный набор трассы'
    )
    print(u'{0}: {1}'.format(
        source_label,
        selection_info['raw_route_count']
    ))
    print(u'Участков, совпавших с парным фильтром: {0}'.format(
        selection_info['filter_route_count']
    ))
    print(u'Добавлено смежных фитингов: {0}'.format(
        selection_info['fitting_count']
    ))
    print(u'Итого элементов трассы в выделении: {0}'.format(
        selection_info['selected_route_count']
    ))
    print(u'Добавлено вложенных/родительских компонентов семейств: '
          u'{0}'.format(
              selection_info.get('family_component_count', 0)
          ))
    print(u'Результат: {0}.'.format(
        selection_info['message']
    ))
    print(u'')


def print_filter_record(record, primary_roles):
    """Печатает одну запись диагностики фильтра."""
    if primary_roles:
        marker = u'  >>> ОСНОВНОЙ: {0}'.format(
            u' + '.join(primary_roles)
        )
    else:
        marker = u'  -'
    filter_element = record['filter']
    filter_host = record['host']

    print(u'{0}: «{1}», ID {2}'.format(
        marker,
        filter_element.Name,
        filter_element.Id.IntegerValue
    ))
    print(u'      Где задан: {0} «{1}», ID {2}'.format(
        record['host_kind'],
        filter_host.Name,
        filter_host.Id.IntegerValue
    ))
    print(u'      Позиция в списке: {0}; тип: {1}'.format(
        record['order'],
        record['filter_kind']
    ))
    print(u'      Всего попало элементов: {0}'.format(
        len(record['matched_ids'])
    ))
    print(
        u'      Из них: приборы {0}; щит {1}; '
        u'короба/трубы {2}; промежуточные семейства {3}'.format(
            len(record['matched_member_ids']),
            len(record['matched_panel_ids']),
            len(record['matched_route_ids']),
            len(record['matched_transit_ids'])
        )
    )
    print(u'      Включён: {0}; видимость: {1}'.format(
        u'да' if record['enabled'] else u'нет',
        u'показать' if record['visible'] else u'скрыть'
    ))

    if record['colors']:
        print(u'      Цвета: {0}'.format(
            u'; '.join(record['colors'])
        ))
    else:
        print(u'      Явно заданных RGB-цветов нет.')


def print_group_filter_report(view, result, analysis):
    """Печатает диагностику цвета для одной цепи."""
    circuit = result['circuit']
    circuit_number = safe_property_text(
        circuit,
        'CircuitNumber',
        u'Без номера'
    )
    load_name = safe_property_text(
        circuit,
        'LoadName',
        u'Без имени нагрузки'
    )

    print(u'=== ЦВЕТ ГРУППЫ: {0} | {1} ==='.format(
        circuit_number,
        load_name
    ))

    view_template = analysis['view_template']
    if view_template is None:
        print(u'Шаблон вида: не назначен.')
    else:
        print(u'Шаблон вида: «{0}», ID {1}'.format(
            view_template.Name,
            view_template.Id.IntegerValue
        ))
        controls = analysis['template_controls_filters']
        if controls is True:
            print(u'Раздел «Фильтры» управляется шаблоном.')
        elif controls is False:
            print(u'Раздел «Фильтры» не управляется шаблоном.')
        else:
            print(u'Состояние управления фильтрами не определено.')

    records = analysis['records']
    color_records = analysis['color_records']
    primary_device_record = analysis['primary_device_record']
    primary_route_record = analysis['primary_route_record']
    group_ids = make_result_element_ids(result)
    direct_overrides = collect_direct_color_overrides(
        view,
        group_ids
    )
    category_overrides = collect_category_color_overrides(
        view,
        group_ids
    )

    if not records:
        print(u'Ни один применённый фильтр не захватывает элементы этой группы.')
    else:
        print(u'Фильтров, захватывающих группу: {0}'.format(
            len(records)
        ))
        for record in records:
            primary_roles = []
            if record is primary_device_record:
                primary_roles.append(u'ПРИБОРЫ')
            if record is primary_route_record:
                primary_roles.append(u'ТРАССА')
            print_filter_record(record, primary_roles)

    print(u'--- ИТОГ ПО ЦЕПИ ---')
    if primary_device_record is None:
        print(u'Основной цветовой фильтр приборов: не найден.')
    else:
        print(u'Основной фильтр приборов: «{0}», ID {1}'.format(
            primary_device_record['filter'].Name,
            primary_device_record['filter'].Id.IntegerValue
        ))
        print(u'  Охват: {0} из {1} приборов; {2}'.format(
            len(primary_device_record['matched_member_ids']),
            len(analysis['id_groups']['member_ids']),
            format_rgb_values(primary_device_record['rgb_values'])
        ))

    if primary_route_record is None:
        print(u'Основной цветовой фильтр трассы: не найден.')
    else:
        print(u'Основной фильтр трассы: «{0}», ID {1}'.format(
            primary_route_record['filter'].Name,
            primary_route_record['filter'].Id.IntegerValue
        ))
        print(u'  Охват: {0} из {1} найденных элементов трассы; {2}'.format(
            len(primary_route_record['matched_route_ids']),
            len(analysis['id_groups']['route_ids']),
            format_rgb_values(primary_route_record['rgb_values'])
        ))
        print(u'  Почему выбран: {0}.'.format(
            analysis['primary_route_reason']
        ))

    other_route_records = analysis['other_route_color_records']
    if other_route_records:
        print(
            u'Прочие цветовые фильтры в захваченном '
            u'связанном компоненте:'
        )
        for record in other_route_records:
            print(u'  - «{0}»: {1} элементов; {2}'.format(
                record['filter'].Name,
                len(record['matched_route_ids']),
                format_rgb_values(record['rgb_values'])
            ))
        print(
            u'  Эти участки могли попасть в выбор из-за '
            u'режима «Связанный компонент сети».'
        )

    if color_records and direct_overrides:
        print(
            u'ВНИМАНИЕ: у {0} элементов есть индивидуальное '
            u'переопределение цвета; оно может перекрывать '
            u'графику фильтра.'.format(len(direct_overrides))
        )

    if not color_records:
        print(u'Активный видимый фильтр с RGB-переопределением не найден.')

        if direct_overrides:
            print(u'Найдены индивидуальные переопределения цвета: {0} элементов.'.format(
                len(direct_overrides)
            ))
            for item in direct_overrides[:10]:
                print(u'  - ID {0}: {1}'.format(
                    item['element_id'],
                    u'; '.join(item['colors'])
                ))

        if category_overrides:
            print(u'Найдены цветовые переопределения категорий:')
            for item in category_overrides:
                print(u'  - {0}, {1} «{2}»: {3}'.format(
                    item['category'].Name,
                    item['host_kind'],
                    item['host'].Name,
                    u'; '.join(item['colors'])
                ))

        if not direct_overrides and not category_overrides:
            print(
                u'Цвет может идти от материала, семейства, '
                u'фазового фильтра, системной графики или связанного вида.'
            )

    print(u'')


def build_filter_summary(view, results, analyses):
    """Формирует короткий текст для итогового окна."""
    lines = [u'Вид: {0}'.format(view.Name)]
    view_template = get_assigned_view_template(view)
    if view_template is None:
        lines.append(u'Шаблон: не назначен')
    else:
        lines.append(u'Шаблон: {0}'.format(view_template.Name))

    lines.append(u'')

    for result, analysis in zip(results, analyses):
        circuit = result['circuit']
        circuit_number = safe_property_text(
            circuit,
            'CircuitNumber',
            u'Без номера'
        )
        device_record = analysis['primary_device_record']
        route_record = analysis['primary_route_record']

        lines.append(u'{0}:'.format(circuit_number))

        if device_record is None:
            lines.append(u'  Приборы: цветовой фильтр не найден')
        else:
            lines.append(u'  Приборы: «{0}» — {1}'.format(
                device_record['filter'].Name,
                format_rgb_values(device_record['rgb_values'])
            ))

        if route_record is None:
            lines.append(u'  Трасса: цветовой фильтр не найден')
        else:
            lines.append(u'  Трасса: «{0}» — {1}'.format(
                route_record['filter'].Name,
                format_rgb_values(route_record['rgb_values'])
            ))

    lines.extend([
        u'',
        u'Все RGB-цвета, ID фильтров, их порядок и '
        u'количество попавших элементов показаны в окне вывода pyRevit.'
    ])

    return u'\n'.join(lines)


def get_duplicate_option(view):
    """Выбирает наиболее полный доступный способ копирования вида."""
    try:
        if view.CanViewBeDuplicated(
                DB.ViewDuplicateOption.WithDetailing):
            return DB.ViewDuplicateOption.WithDetailing
    except Exception:
        pass

    try:
        if view.CanViewBeDuplicated(DB.ViewDuplicateOption.Duplicate):
            return DB.ViewDuplicateOption.Duplicate
    except Exception:
        pass

    return None


def get_sheet_viewport_candidates(sheet):
    """Получает размещённые на листе дублируемые виды."""
    all_candidates = []
    plan_candidates = []

    try:
        viewport_ids = sheet.GetAllViewports()
    except Exception:
        viewport_ids = []

    for viewport_id in viewport_ids:
        viewport = doc.GetElement(viewport_id)
        if viewport is None:
            continue

        try:
            view = doc.GetElement(viewport.ViewId)
        except Exception:
            view = None

        if view is None:
            continue

        try:
            if view.IsTemplate:
                continue
        except Exception:
            pass

        duplicate_option = get_duplicate_option(view)
        if duplicate_option is None:
            continue

        candidate = (viewport, view, duplicate_option)
        all_candidates.append(candidate)
        if isinstance(view, DB.ViewPlan):
            plan_candidates.append(candidate)

    return plan_candidates if plan_candidates else all_candidates


def select_source_viewport(sheet):
    """Выбирает основной вид исходного листа."""
    candidates = get_sheet_viewport_candidates(sheet)
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    by_label = {}
    for viewport, view, duplicate_option in candidates:
        try:
            view_type = unicode(view.ViewType)
        except Exception:
            view_type = u'Тип не определён'

        label = u'{0} | {1} | ID {2}'.format(
            view.Name,
            view_type,
            get_id_value(view)
        )
        by_label[label] = (viewport, view, duplicate_option)

    selected_label = forms.SelectFromList.show(
        sorted(by_label.keys(), key=natural_sort_key),
        title=u'Выберите основной вид листа',
        button_name=u'Использовать этот вид',
        multiselect=False,
        width=850,
        height=500
    )

    if not selected_label:
        return None
    return by_label[selected_label]


def get_viewport_sheet_id(viewport):
    """Получает ID листа видового экрана в разных сборках API."""
    try:
        return viewport.SheetId
    except Exception:
        try:
            return viewport.OwnerViewId
        except Exception:
            return DB.ElementId.InvalidElementId


def find_sheet_context_for_model_view(view):
    """Ищет лист, на котором размещён уже открытый модельный вид."""
    candidates = []
    viewports = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.Viewport)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    for viewport in viewports:
        try:
            if viewport.ViewId != view.Id:
                continue
        except Exception:
            continue

        sheet = doc.GetElement(get_viewport_sheet_id(viewport))
        if sheet is None or not isinstance(sheet, DB.ViewSheet):
            continue
        candidates.append((sheet, viewport))

    if not candidates:
        return None

    if len(candidates) == 1:
        sheet, viewport = candidates[0]
        return sheet, viewport, view, get_duplicate_option(view)

    by_label = {}
    for sheet, viewport in candidates:
        label = u'{0} — {1}'.format(sheet.SheetNumber, sheet.Name)
        by_label[label] = (sheet, viewport, view, get_duplicate_option(view))

    selected_label = forms.SelectFromList.show(
        sorted(by_label.keys(), key=natural_sort_key),
        title=u'Выберите исходный лист',
        button_name=u'Копировать этот лист',
        multiselect=False,
        width=800,
        height=450
    )
    if not selected_label:
        return None
    return by_label[selected_label]


class SourceViewportSelectionFilter(UISelection.ISelectionFilter):
    """Разрешает указать на листе только viewport."""

    def AllowElement(self, element):
        return isinstance(element, DB.Viewport)

    def AllowReference(self, reference, point):
        return False


def pick_source_viewport_on_sheet(sheet):
    """Просит указать эталонный viewport мышью, как в рабочем скрипте."""
    try:
        with forms.WarningBar(
                title=u'Выберите ЭТАЛОННЫЙ видовой экран на листе'):
            reference = uidoc.Selection.PickObject(
                UISelection.ObjectType.Element,
                SourceViewportSelectionFilter(),
                u'Укажите эталонный 2D-viewport'
            )
    except Exception:
        return None

    try:
        viewport = doc.GetElement(reference.ElementId)
    except Exception:
        viewport = None
    if viewport is None or not isinstance(viewport, DB.Viewport):
        return None

    if get_viewport_sheet_id(viewport) != sheet.Id:
        return None

    try:
        view = doc.GetElement(viewport.ViewId)
    except Exception:
        view = None
    if view is None:
        return None

    duplicate_option = get_duplicate_option(view)
    if duplicate_option is None:
        return None
    return viewport, view, duplicate_option


def resolve_source_context():
    """Берёт координаты именно с указанного эталона."""
    active_view = uidoc.ActiveView

    if isinstance(active_view, DB.ViewSheet):
        selected = pick_source_viewport_on_sheet(active_view)
        if selected is None:
            return None
        viewport, view, duplicate_option = selected
        return active_view, viewport, view, duplicate_option

    duplicate_option = get_duplicate_option(active_view)
    if duplicate_option is None:
        return None

    return find_sheet_context_for_model_view(active_view)


def get_title_block_type_id(sheet):
    """Получает тип основной надписи исходного листа."""
    try:
        # На листе может быть несколько элементов категории.
        # Берём самый крупный BoundingBox — фактическую рамку.
        title_block = get_main_titleblock(sheet)
        if title_block is not None:
            return title_block.GetTypeId()
    except Exception:
        pass

    try:
        title_blocks = (
            DB.FilteredElementCollector(doc)
            .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
            .WhereElementIsNotElementType()
            .ToElements()
        )
        for title_block in title_blocks:
            try:
                if title_block.OwnerViewId == sheet.Id:
                    return title_block.GetTypeId()
            except Exception:
                pass
    except Exception:
        pass

    return DB.ElementId.InvalidElementId


def get_sheet_parameter_skip_ids():
    """Возвращает параметры, которые нельзя переносить напрямую."""
    names = [
        'SHEET_NUMBER',
        'SHEET_NAME',
        'SHEET_CURRENT_REVISION',
        'SHEET_CURRENT_REVISION_DATE',
        'SHEET_CURRENT_REVISION_DESCRIPTION',
        'SHEET_CURRENT_REVISION_ISSUED',
        'SHEET_CURRENT_REVISION_ISSUED_BY',
        'SHEET_CURRENT_REVISION_ISSUED_TO'
    ]
    result = set()
    for name in names:
        try:
            result.add(int(getattr(DB.BuiltInParameter, name)))
        except Exception:
            pass
    return result


SHEET_PARAMETER_SKIP_IDS = get_sheet_parameter_skip_ids()


SHEET_IDENTITY_PARAMETER_NAMES = set([
    u'номер листа',
    u'имя листа',
    u'наименование листа',
    u'sheet number',
    u'sheet name'
])


def is_sheet_identity_parameter(parameter):
    """Определяет параметры, запись которых меняет номер/имя листа."""
    if parameter is None:
        return False

    try:
        if parameter.Id.IntegerValue in SHEET_PARAMETER_SKIP_IDS:
            return True
    except Exception:
        pass

    try:
        built_in_parameter = parameter.Definition.BuiltInParameter
        if int(built_in_parameter) in SHEET_PARAMETER_SKIP_IDS:
            return True
    except Exception:
        pass

    try:
        definition_name = unicode(parameter.Definition.Name).strip().lower()
        definition_name = re.sub(r'\s+', u' ', definition_name)
        if definition_name in SHEET_IDENTITY_PARAMETER_NAMES:
            return True
    except Exception:
        pass

    return False


def copy_sheet_parameters(source_sheet, target_sheet):
    """Копирует доступные строковые и числовые параметры листа."""
    copied_count = 0

    for source_parameter in source_sheet.Parameters:
        try:
            if is_sheet_identity_parameter(source_parameter):
                continue
            if not source_parameter.HasValue:
                continue

            target_parameter = target_sheet.get_Parameter(
                source_parameter.Definition
            )
            if target_parameter is None or target_parameter.IsReadOnly:
                continue
            if target_parameter.StorageType != source_parameter.StorageType:
                continue

            storage_type = source_parameter.StorageType
            if storage_type == DB.StorageType.String:
                value = source_parameter.AsString()
                if value is None:
                    continue
                target_parameter.Set(value)
            elif storage_type == DB.StorageType.Integer:
                target_parameter.Set(source_parameter.AsInteger())
            elif storage_type == DB.StorageType.Double:
                target_parameter.Set(source_parameter.AsDouble())
            else:
                continue
            copied_count += 1
        except Exception:
            pass

    return copied_count


def get_element_type_name_safe(element):
    """Получает имя типа аннотации без зависимости от языка Revit."""
    try:
        element_type = doc.GetElement(element.GetTypeId())
        if element_type is None:
            return u''
        parameter = element_type.get_Parameter(
            DB.BuiltInParameter.SYMBOL_NAME_PARAM
        )
        if parameter is not None and parameter.HasValue:
            return parameter.AsString() or u''
        return element_type.Name or u''
    except Exception:
        return u''


def is_sheet_detail_copy_candidate(element, source_sheet):
    """Отбирает листовые детали и надписи, но не viewport/штамп."""
    if element is None:
        return False
    if isinstance(element, DB.Viewport):
        return False

    try:
        if (
                element.Category is not None
                and element.Category.Id.IntegerValue
                == int(DB.BuiltInCategory.OST_TitleBlocks)):
            return False
    except Exception:
        pass

    try:
        class_name = unicode(element.GetType().Name)
        if class_name in [
                u'ScheduleSheetInstance',
                u'PanelScheduleSheetInstance']:
            return False
    except Exception:
        pass

    try:
        if element.OwnerViewId != source_sheet.Id:
            return False
    except Exception:
        return False

    try:
        if not element.ViewSpecific:
            return False
    except Exception:
        pass

    # Члены группы не копируем отдельно: они переносятся
    # вместе с самим экземпляром группы.
    try:
        if (
                element.GroupId != DB.ElementId.InvalidElementId
                and not isinstance(element, DB.Group)):
            return False
    except Exception:
        pass

    return True


def copy_sheet_details_and_text(source_sheet, target_sheet):
    """Копирует листовые детали в тех же XY-координатах."""
    candidates = []
    try:
        elements = (
            DB.FilteredElementCollector(doc, source_sheet.Id)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        elements = []

    for element in elements:
        if is_sheet_detail_copy_candidate(element, source_sheet):
            candidates.append(element)

    copied_ids = []
    failed_ids = []
    renamed_title_count = 0
    options = DB.CopyPasteOptions()

    # Копируем по одному: одна некопируемая аннотация
    # не должна отменять перенос всех остальных.
    for element in candidates:
        try:
            source_ids = List[DB.ElementId]()
            source_ids.Add(element.Id)
            new_ids = DB.ElementTransformUtils.CopyElements(
                source_sheet,
                source_ids,
                target_sheet,
                DB.Transform.Identity,
                options
            )
            for new_id in new_ids:
                copied_ids.append(new_id.IntegerValue)
                copied_element = doc.GetElement(new_id)
                if (
                        isinstance(copied_element, DB.TextNote)
                        and get_element_type_name_safe(copied_element)
                        == TARGET_SHEET_TITLE_TEXT_TYPE):
                    copied_element.Text = target_sheet.Name
                    renamed_title_count += 1
        except Exception:
            failed_ids.append(element.Id.IntegerValue)

    return {
        'candidate_count': len(candidates),
        'copied_ids': copied_ids,
        'copied_count': len(copied_ids),
        'failed_ids': failed_ids,
        'renamed_title_count': renamed_title_count
    }


def sanitize_revit_name(value, max_length=220):
    """Удаляет из имени символы, запрещённые Revit."""
    try:
        text = unicode(value)
    except Exception:
        text = u'Новый вид'

    for character in [
            u'\\', u'/', u':', u'{', u'}', u'[', u']', u'|',
            u';', u'<', u'>', u'?', u'`', u'~']:
        text = text.replace(character, u'-')

    while u'  ' in text:
        text = text.replace(u'  ', u' ')

    text = text.strip()
    if not text:
        text = u'Новый вид'
    return text[:max_length]


def get_selected_group_text(selected_circuits):
    """Формирует короткое перечисление выбранных групп."""
    numbers = set()
    for circuit in selected_circuits:
        numbers.add(safe_property_text(
            circuit,
            'CircuitNumber',
            u'без номера'
        ))

    text = u', '.join(sorted(numbers, key=natural_sort_key))
    return text if len(text) <= 100 else text[:97] + u'...'


def normalize_sheet_number_key(value):
    """Нормализует номер так же строго, как требуется для проверки дублей."""
    try:
        text = unicode(value).strip()
    except Exception:
        text = u''

    try:
        text = re.sub(r'\s+', u' ', text)
    except Exception:
        pass

    try:
        return text.lower()
    except Exception:
        return text


def get_existing_sheet_number_keys(excluded_sheet=None):
    """Возвращает нормализованные номера, кроме создаваемого листа."""
    excluded_id = get_id_value(excluded_sheet)
    existing_numbers = set()
    for sheet in (
            DB.FilteredElementCollector(doc)
            .OfClass(DB.ViewSheet)
            .WhereElementIsNotElementType()
            .ToElements()):
        try:
            if (
                    excluded_id is not None
                    and get_id_value(sheet) == excluded_id):
                continue
            existing_numbers.add(
                normalize_sheet_number_key(sheet.SheetNumber)
            )
        except Exception:
            pass
    return existing_numbers


def get_sheet_number_base(source_sheet):
    """Формирует основу номера листа выбранных групп."""
    try:
        source_number = unicode(source_sheet.SheetNumber).strip()
    except Exception:
        source_number = u'ЛИСТ'

    if not source_number:
        source_number = u'ЛИСТ'

    # Оставляем запас для суффиксов -2, -3 и т. д.
    return (source_number + u'-ГР')[:240]


def get_unique_sheet_number(source_sheet, excluded_sheet=None):
    """Предварительно подбирает свободный номер нового листа."""
    existing_numbers = get_existing_sheet_number_keys(excluded_sheet)
    base_number = get_sheet_number_base(source_sheet)

    candidate = base_number
    index = 2
    while normalize_sheet_number_key(candidate) in existing_numbers:
        candidate = u'{0}-{1}'.format(base_number, index)
        index += 1
    return candidate


def assign_unique_sheet_number(target_sheet, source_sheet):
    """Назначает номер и при конфликте спрашивает сам Revit повторно."""
    existing_numbers = get_existing_sheet_number_keys(target_sheet)
    base_number = get_sheet_number_base(source_sheet)
    last_error = None

    # Проверка коллектора ускоряет подбор, но окончательное решение всегда
    # принимает Revit при записи SheetNumber.
    for attempt in range(1, 10001):
        if attempt == 1:
            candidate = base_number
        else:
            candidate = u'{0}-{1}'.format(base_number, attempt)

        key = normalize_sheet_number_key(candidate)
        if key in existing_numbers:
            continue

        try:
            target_sheet.SheetNumber = candidate
            doc.Regenerate()
            return unicode(target_sheet.SheetNumber)
        except Exception as error:
            last_error = error
            existing_numbers.add(key)

    message = u'Не удалось подобрать уникальный номер листа.'
    if last_error is not None:
        message += u' Последняя ошибка Revit: {0}'.format(last_error)
    raise Exception(message)


def get_unique_view_name(base_name):
    """Создаёт уникальное имя вида или шаблона."""
    existing_names = set()
    for view in (
            DB.FilteredElementCollector(doc)
            .OfClass(DB.View)
            .WhereElementIsNotElementType()
            .ToElements()):
        try:
            existing_names.add(view.Name)
        except Exception:
            pass

    clean_base = sanitize_revit_name(base_name)
    candidate = clean_base
    index = 2
    while candidate in existing_names:
        suffix = u' ({0})'.format(index)
        candidate = clean_base[:220 - len(suffix)] + suffix
        index += 1
    return candidate


def get_unique_filter_name(base_name):
    """Создаёт уникальное имя фильтра проекта."""
    existing_names = set()
    for filter_class in [
            DB.ParameterFilterElement,
            DB.SelectionFilterElement]:
        for filter_element in (
                DB.FilteredElementCollector(doc)
                .OfClass(filter_class)
                .WhereElementIsNotElementType()
                .ToElements()):
            try:
                existing_names.add(filter_element.Name)
            except Exception:
                pass

    clean_base = sanitize_revit_name(base_name)
    candidate = clean_base
    index = 2
    while candidate in existing_names:
        suffix = u' ({0})'.format(index)
        candidate = clean_base[:220 - len(suffix)] + suffix
        index += 1
    return candidate


def force_template_graphics_controls(view_template):
    """Включает управление категориями модели и фильтрами шаблоном."""
    controlled_parameter_ids = set()
    for name in ['VIS_GRAPHICS_MODEL', 'VIS_GRAPHICS_FILTERS']:
        try:
            controlled_parameter_ids.add(int(
                getattr(DB.BuiltInParameter, name)
            ))
        except Exception:
            pass

    try:
        noncontrolled_ids = list(
            view_template.GetNonControlledTemplateParameterIds()
        )
    except Exception:
        return 0

    updated_ids = List[DB.ElementId]()
    removed_count = 0
    for parameter_id in noncontrolled_ids:
        if parameter_id.IntegerValue in controlled_parameter_ids:
            removed_count += 1
            continue
        updated_ids.Add(parameter_id)

    if removed_count:
        view_template.SetNonControlledTemplateParameterIds(updated_ids)
    return removed_count


def clone_crop_box(crop_box):
    """Создаёт независимую копию BoundingBoxXYZ рамки вида."""
    if crop_box is None:
        return None

    result = DB.BoundingBoxXYZ()
    try:
        result.Transform = crop_box.Transform
    except Exception:
        pass
    result.Min = DB.XYZ(
        crop_box.Min.X,
        crop_box.Min.Y,
        crop_box.Min.Z
    )
    result.Max = DB.XYZ(
        crop_box.Max.X,
        crop_box.Max.Y,
        crop_box.Max.Z
    )
    return result


def capture_view_frame(view):
    """Сохраняет данные эталона по алгоритму 2D-видов."""
    data = {
        'scale': None,
        'crop_box': None,
        'crop_world_center': None,
        'crop_width': None,
        'crop_height': None,
        'crop_width_mm': None,
        'crop_height_mm': None,
        'crop_active': None,
        'crop_visible': None,
        'annotation_crop_active': None,
        'annotation_offsets': None,
        'annotation_offsets_mm': None,
        'scope_box_id': None
    }

    try:
        data['scale'] = view.Scale
    except Exception:
        pass

    try:
        crop_box = view.CropBox
        data['crop_box'] = clone_crop_box(crop_box)
        data['crop_width'] = crop_box.Max.X - crop_box.Min.X
        data['crop_height'] = crop_box.Max.Y - crop_box.Min.Y
        if data['scale']:
            data['crop_width_mm'] = (
                data['crop_width'] / data['scale'] * 304.8
            )
            data['crop_height_mm'] = (
                data['crop_height'] / data['scale'] * 304.8
            )
        local_center = DB.XYZ(
            (crop_box.Min.X + crop_box.Max.X) / 2.0,
            (crop_box.Min.Y + crop_box.Max.Y) / 2.0,
            (crop_box.Min.Z + crop_box.Max.Z) / 2.0
        )
        data['crop_world_center'] = (
            crop_box.Transform.OfPoint(local_center)
        )
    except Exception:
        pass

    try:
        data['crop_active'] = bool(view.CropBoxActive)
    except Exception:
        try:
            parameter = view.get_Parameter(
                DB.BuiltInParameter.VIEWER_CROP_REGION
            )
            if parameter is not None:
                data['crop_active'] = parameter.AsInteger() == 1
        except Exception:
            pass

    try:
        data['crop_visible'] = bool(view.CropBoxVisible)
    except Exception:
        try:
            parameter = view.get_Parameter(
                DB.BuiltInParameter.VIEWER_CROP_REGION_VISIBLE
            )
            if parameter is not None:
                data['crop_visible'] = parameter.AsInteger() == 1
        except Exception:
            pass

    try:
        parameter = view.get_Parameter(
            DB.BuiltInParameter.VIEWER_ANNOTATION_CROP_ACTIVE
        )
        if parameter is not None:
            data['annotation_crop_active'] = (
                parameter.AsInteger() == 1
            )
    except Exception:
        pass

    try:
        manager = view.GetCropRegionShapeManager()
        data['annotation_offsets'] = {
            'left': manager.LeftAnnotationCropOffset,
            'right': manager.RightAnnotationCropOffset,
            'top': manager.TopAnnotationCropOffset,
            'bottom': manager.BottomAnnotationCropOffset
        }
        data['annotation_offsets_mm'] = {
            'left': manager.LeftAnnotationCropOffset * 304.8,
            'right': manager.RightAnnotationCropOffset * 304.8,
            'top': manager.TopAnnotationCropOffset * 304.8,
            'bottom': manager.BottomAnnotationCropOffset * 304.8
        }
    except Exception:
        pass

    try:
        parameter = view.get_Parameter(
            DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP
        )
        if parameter is not None:
            scope_box_id = parameter.AsElementId()
            if scope_box_id is not None:
                data['scope_box_id'] = DB.ElementId(
                    scope_box_id.IntegerValue
                )
    except Exception:
        pass

    return data


def set_integer_parameter(view, built_in_parameter, value):
    """Безопасно задаёт целочисленный параметр рамки вида."""
    try:
        parameter = view.get_Parameter(built_in_parameter)
        if parameter is None or parameter.IsReadOnly:
            return False
        parameter.Set(int(value))
        return True
    except Exception:
        return False


def apply_view_frame(view, frame_data):
    """Буквально повторяет рабочий этап «Рамка 2D»."""
    applied = []
    failed = []

    scale = frame_data.get('scale')
    if scale is not None:
        try:
            view.Scale = scale
            applied.append(u'масштаб')
        except Exception:
            failed.append(u'масштаб')

    # Это критическое отличие рабочего скрипта: Scope Box
    # не копируется, а снимается перед установкой Crop Box.
    try:
        scope_parameter = view.get_Parameter(
            DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP
        )
        if scope_parameter is not None and not scope_parameter.IsReadOnly:
            scope_parameter.Set(DB.ElementId.InvalidElementId)
            applied.append(u'Scope Box снят')
        elif scope_parameter is not None:
            failed.append(u'Scope Box управляется шаблоном')
    except Exception:
        failed.append(u'снятие Scope Box')

    crop_was_set = set_integer_parameter(
        view,
        DB.BuiltInParameter.VIEWER_CROP_REGION,
        1
    )
    if not crop_was_set:
        try:
            view.CropBoxActive = True
            crop_was_set = True
        except Exception:
            pass
    if crop_was_set:
        applied.append(u'Crop Box включён')
    else:
        failed.append(u'включение Crop Box')

    try:
        doc.Regenerate()
    except Exception:
        pass

    crop_box = frame_data.get('crop_box')
    if crop_box is not None:
        try:
            current_box = view.CropBox
            world_center = frame_data.get('crop_world_center')
            width_mm = frame_data.get('crop_width_mm')
            height_mm = frame_data.get('crop_height_mm')
            target_scale = view.Scale

            # Точная логика из «Вставленный код(4).py».
            base_center = (
                current_box.Transform.Inverse.OfPoint(world_center)
            )
            new_width = width_mm / 304.8 * target_scale
            new_height = height_mm / 304.8 * target_scale

            new_box = DB.BoundingBoxXYZ()
            new_box.Transform = current_box.Transform
            new_box.Min = DB.XYZ(
                base_center.X - new_width / 2.0,
                base_center.Y - new_height / 2.0,
                current_box.Min.Z
            )
            new_box.Max = DB.XYZ(
                base_center.X + new_width / 2.0,
                base_center.Y + new_height / 2.0,
                current_box.Max.Z
            )
            view.CropBox = new_box
            applied.append(u'рамка 2D по эталону')
        except Exception:
            failed.append(u'рамка 2D')

    offsets = frame_data.get('annotation_offsets_mm')
    if offsets is not None:
        try:
            manager = view.GetCropRegionShapeManager()
            manager.LeftAnnotationCropOffset = offsets['left'] / 304.8
            manager.RightAnnotationCropOffset = offsets['right'] / 304.8
            manager.TopAnnotationCropOffset = offsets['top'] / 304.8
            manager.BottomAnnotationCropOffset = offsets['bottom'] / 304.8
            applied.append(u'аннотационная рамка')
        except Exception:
            failed.append(u'аннотационная рамка')

    if set_integer_parameter(
            view,
            DB.BuiltInParameter.VIEWER_ANNOTATION_CROP_ACTIVE,
            1):
        applied.append(u'аннотационная рамка включена')
    else:
        failed.append(u'включение аннотационной рамки')

    visibility_was_set = set_integer_parameter(
        view,
        DB.BuiltInParameter.VIEWER_CROP_REGION_VISIBLE,
        1
    )
    if not visibility_was_set:
        try:
            view.CropBoxVisible = True
            visibility_was_set = True
        except Exception:
            pass
    if visibility_was_set:
        applied.append(u'граница Crop Box показана')
    else:
        failed.append(u'видимость границы Crop Box')

    return {
        'applied': applied,
        'failed': failed
    }


def create_view_template_from_copy(
        source_view,
        target_view,
        sheet_number,
        group_text,
        frame_data):
    """Создаёт независимый шаблон из дубликата модельного вида."""
    source_template = get_assigned_view_template(source_view)
    target_view.ViewTemplateId = DB.ElementId.InvalidElementId
    doc.Regenerate()

    if source_template is not None:
        target_view.ApplyViewTemplateParameters(source_template)
        doc.Regenerate()

    frame_result = apply_view_frame(target_view, frame_data)
    doc.Regenerate()

    direct_error = None
    new_template = None
    try:
        # CreateViewTemplate является экземплярным методом без аргументов.
        new_template = target_view.CreateViewTemplate()
    except Exception as error:
        direct_error = error

    if new_template is None:
        try:
            # Резерв для особенностей привязки методов IronPython 2.7.
            view_type = clr.GetClrType(DB.View)
            create_method = view_type.GetMethod(
                'CreateViewTemplate',
                System.Type.EmptyTypes
            )
            if create_method is None:
                raise Exception(
                    u'метод View.CreateViewTemplate() не найден'
                )
            empty_arguments = System.Array.CreateInstance(
                System.Object,
                0
            )
            new_template = create_method.Invoke(
                target_view,
                empty_arguments
            )
        except Exception as reflection_error:
            raise Exception(
                u'Не удалось создать шаблон из копии вида «{0}». '
                u'Прямой вызов: {1}; резервный вызов: {2}'.format(
                    target_view.Name,
                    direct_error,
                    reflection_error
                )
            )

    if new_template is None or not new_template.IsTemplate:
        raise Exception(
            u'Revit не вернул новый шаблон для вида «{0}».'.format(
                target_view.Name
            )
        )

    source_name = (
        source_template.Name
        if source_template is not None
        else source_view.Name
    )
    new_template.Name = get_unique_view_name(
        u'AUTO_{0}_{1}_группы {2}'.format(
            source_name,
            sheet_number,
            group_text
        )
    )

    forced_count = force_template_graphics_controls(new_template)
    return {
        'source_template': source_template,
        'new_template': new_template,
        'forced_control_count': forced_count,
        'frame_result': frame_result
    }


def get_element_category_id(element):
    """Возвращает числовой ID категории элемента."""
    try:
        if element is not None and element.Category is not None:
            return element.Category.Id.IntegerValue
    except Exception:
        pass
    return None


def get_category_ids_for_elements(integer_ids):
    """Собирает категории указанного набора экземпляров."""
    category_ids = set()
    for integer_id in integer_ids:
        category_id = get_element_category_id(
            doc.GetElement(DB.ElementId(integer_id))
        )
        if category_id is not None:
            category_ids.add(category_id)
    return category_ids


def show_selected_categories(settings_view, keep_ids):
    """Включает в новом шаблоне категории выбранных элементов."""
    shown_count = 0
    failed_ids = []

    for category_id in sorted(get_category_ids_for_elements(keep_ids)):
        revit_category_id = DB.ElementId(category_id)
        try:
            if not settings_view.CanCategoryBeHidden(revit_category_id):
                continue
            if settings_view.GetCategoryHidden(revit_category_id):
                settings_view.SetCategoryHidden(revit_category_id, False)
                shown_count += 1
        except Exception:
            failed_ids.append(category_id)

    return shown_count, failed_ids


def choose_best_color_record_for_ids(color_records, integer_ids):
    """Выбирает цветовой фильтр с максимальным охватом набора ID."""
    integer_ids = set(integer_ids)
    candidates = []

    for record in color_records:
        matched_count = len(
            set(record['matched_ids']).intersection(integer_ids)
        )
        if not matched_count:
            continue
        candidates.append((matched_count, record))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (
        -item[0],
        item[1]['order'],
        item[1]['filter'].Id.IntegerValue
    ))
    return candidates[0][1]


def add_filter_assignment(assignments, record, integer_ids, role):
    """Добавляет элементы к копии одного исходного цветового фильтра."""
    integer_ids = set(integer_ids)
    if record is None or not integer_ids:
        return

    source_filter_id = record['filter'].Id.IntegerValue
    assignment = assignments.get(source_filter_id)
    if assignment is None:
        assignment = {
            'record': record,
            'element_ids': set(),
            'roles': set()
        }
        assignments[source_filter_id] = assignment

    assignment['element_ids'].update(integer_ids)
    assignment['roles'].add(role)


def get_ordered_assignment_ids(assignments):
    """Сохраняет порядок цветовых прототипов исходного вида."""
    return sorted(assignments.keys(), key=lambda source_filter_id: (
        assignments[source_filter_id]['record']['order'],
        source_filter_id
    ))


def build_filter_assignments(results, analyses, safe_selection_sets):
    """Связывает точные выбранные элементы с цветовыми прототипами."""
    assignments = {}
    unassigned_ids = set()

    for result, analysis, safe_ids in zip(
            results, analyses, safe_selection_sets):
        safe_ids = set(safe_ids)
        groups = analysis['id_groups']

        member_ids = safe_ids.intersection(
            expand_family_instance_ids(groups['member_ids'])
        )
        panel_ids = safe_ids.intersection(
            expand_family_instance_ids(groups['panel_ids'])
        )
        route_ids = safe_ids.intersection(groups['route_ids'])
        transit_ids = safe_ids.intersection(groups['transit_ids'])

        device_record = analysis['primary_device_record']
        route_record = analysis['primary_route_record']
        panel_record = choose_best_color_record_for_ids(
            analysis['color_records'],
            panel_ids
        )
        transit_record = choose_best_color_record_for_ids(
            analysis['color_records'],
            transit_ids
        )
        if transit_record is None:
            transit_record = route_record

        add_filter_assignment(
            assignments,
            device_record,
            member_ids,
            u'приборы'
        )
        add_filter_assignment(
            assignments,
            panel_record,
            panel_ids,
            u'оборудование'
        )
        add_filter_assignment(
            assignments,
            route_record,
            route_ids,
            u'трасса'
        )
        add_filter_assignment(
            assignments,
            transit_record,
            transit_ids,
            u'промежуточные элементы'
        )

        if device_record is None:
            unassigned_ids.update(member_ids)
        if panel_record is None:
            unassigned_ids.update(panel_ids)
        if route_record is None:
            unassigned_ids.update(route_ids)
        if transit_record is None:
            unassigned_ids.update(transit_ids)

    return assignments, unassigned_ids


def remove_conflicting_filters(
        settings_view,
        analyses,
        keep_ids,
        source_filter_ids):
    """Снимает с копии только заменяемые и скрывающие выбор фильтры."""
    remove_ids = set(source_filter_ids)
    keep_ids = set(keep_ids)

    for analysis in analyses:
        for record in analysis['records']:
            if record['visible']:
                continue
            if set(record['matched_ids']).intersection(keep_ids):
                remove_ids.add(record['filter'].Id.IntegerValue)

    try:
        applied_ids = set(
            filter_id.IntegerValue
            for filter_id in settings_view.GetFilters()
        )
    except Exception:
        applied_ids = set()

    removed_names = []
    failed_ids = []
    for integer_id in sorted(remove_ids.intersection(applied_ids)):
        filter_element = doc.GetElement(DB.ElementId(integer_id))
        try:
            settings_view.RemoveFilter(DB.ElementId(integer_id))
            removed_names.append(
                filter_element.Name
                if filter_element is not None
                else unicode(integer_id)
            )
        except Exception:
            failed_ids.append(integer_id)

    return removed_names, failed_ids


def remove_post_token_hiding_filters(settings_view, keep_ids):
    """Снимает фильтры, которые начали скрывать выбор после записи токенов.

    Некоторые унаследованные фильтры могут проверять «Примечание». До
    добавления PYREVIT_EOM-токена они не захватывают прибор, поэтому ранняя
    диагностика их не видит. После записи токена такой фильтр способен
    скрыть розетку, даже если новый цветовой фильтр показывает её.
    """
    keep_ids = set(keep_ids)
    removed_names = []
    failed_ids = []
    matched_element_ids = set()

    try:
        applied_filter_ids = list(settings_view.GetFilters())
    except Exception:
        applied_filter_ids = []

    for filter_id in applied_filter_ids:
        integer_id = filter_id.IntegerValue
        try:
            if get_filter_visibility(settings_view, filter_id):
                continue
        except Exception:
            continue

        filter_element = doc.GetElement(filter_id)
        if filter_element is None:
            continue

        matched_ids = get_filter_matched_ids(filter_element, keep_ids)
        if not matched_ids:
            continue

        try:
            settings_view.RemoveFilter(filter_id)
            matched_element_ids.update(matched_ids)
            removed_names.append(filter_element.Name)
        except Exception:
            failed_ids.append(integer_id)

    return {
        'removed_names': removed_names,
        'failed_ids': failed_ids,
        'released_element_ids': matched_element_ids
    }


def get_comments_parameter(element):
    """Получает доступный для записи экземплярный параметр Примечание."""
    parameter = None
    try:
        parameter = element.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
        )
    except Exception:
        pass

    if parameter is None:
        return None

    try:
        if parameter.IsReadOnly:
            return None
        if parameter.StorageType != DB.StorageType.String:
            return None
    except Exception:
        return None
    return parameter


def append_comments_token(integer_ids, token):
    """Дописывает служебный токен, не стирая существующее Примечание."""
    written_ids = set()
    unchanged_ids = set()
    failed_ids = set()

    for integer_id in sorted(set(integer_ids)):
        element = doc.GetElement(DB.ElementId(integer_id))
        parameter = get_comments_parameter(element)
        if parameter is None:
            failed_ids.add(integer_id)
            continue

        try:
            old_value = parameter.AsString() or u''
            if token in old_value:
                unchanged_ids.add(integer_id)
                written_ids.add(integer_id)
                continue

            separator = u' ' if old_value and not old_value.endswith(u' ') else u''
            parameter.Set(old_value + separator + token)
            written_ids.add(integer_id)
        except Exception:
            failed_ids.add(integer_id)

    return {
        'written_ids': written_ids,
        'unchanged_ids': unchanged_ids,
        'failed_ids': failed_ids
    }


def make_category_id_list(integer_ids):
    """Преобразует числовые ID категорий в ICollection<ElementId>."""
    result = List[DB.ElementId]()
    for integer_id in sorted(set(integer_ids)):
        result.Add(DB.ElementId(integer_id))
    return result


def create_contains_rule(parameter_id, value):
    """Создаёт строковое правило «содержит» для Revit 2023/2024+."""
    try:
        return DB.ParameterFilterRuleFactory.CreateContainsRule(
            parameter_id,
            value,
            True
        )
    except Exception:
        return DB.ParameterFilterRuleFactory.CreateContainsRule(
            parameter_id,
            value
        )


def apply_filter_graphics(
        target_host,
        target_filter,
        source_record,
        visible=True):
    """Копирует графику прототипа и включает новый фильтр."""
    target_host.SetFilterVisibility(target_filter.Id, visible)

    try:
        target_host.SetIsFilterEnabled(target_filter.Id, True)
    except Exception:
        pass

    try:
        overrides = source_record['host'].GetFilterOverrides(
            source_record['filter'].Id
        )
        target_host.SetFilterOverrides(target_filter.Id, overrides)
    except Exception:
        pass


def create_selection_filter_with_style(
        settings_view,
        filter_name,
        integer_ids,
        source_record,
        visible=True):
    """Создаёт точный фильтр по ID с графикой цветового прототипа."""
    integer_ids = set(integer_ids)
    if not integer_ids:
        return None

    selection_filter = DB.SelectionFilterElement.Create(
        doc,
        get_unique_filter_name(filter_name)
    )
    selection_filter.SetElementIds(make_element_id_list(integer_ids))
    settings_view.AddFilter(selection_filter.Id)
    apply_filter_graphics(
        settings_view,
        selection_filter,
        source_record,
        visible
    )
    return selection_filter


def create_parameter_filter_from_assignment(
        settings_view,
        sheet_number,
        assignment):
    """Создаёт копию цветового фильтра с правилом по Примечанию."""
    source_record = assignment['record']
    source_filter = source_record['filter']
    source_filter_id = source_filter.Id.IntegerValue
    token = u'|{0}:{1}:{2}|'.format(
        AUTO_TOKEN_PREFIX,
        settings_view.Id.IntegerValue,
        source_filter_id
    )

    write_result = append_comments_token(
        assignment['element_ids'],
        token
    )
    parameter_filter = None
    parameter_filter_error = None
    written_ids = set(write_result['written_ids'])

    if written_ids and COMMENTS_PARAMETER_ID is not None:
        category_ids = get_category_ids_for_elements(written_ids)
        try:
            rule = create_contains_rule(COMMENTS_PARAMETER_ID, token)
            element_filter = DB.ElementParameterFilter(rule)
            parameter_filter = DB.ParameterFilterElement.Create(
                doc,
                get_unique_filter_name(
                    u'AUTO_{0}_{1}'.format(
                        sheet_number,
                        source_filter.Name
                    )
                ),
                make_category_id_list(category_ids),
                element_filter
            )
            settings_view.AddFilter(parameter_filter.Id)
            apply_filter_graphics(
                settings_view,
                parameter_filter,
                source_record,
                True
            )
        except Exception as error:
            parameter_filter_error = error
            if parameter_filter is not None:
                try:
                    doc.Delete(parameter_filter.Id)
                except Exception:
                    pass
            parameter_filter = None

    fallback_ids = set(write_result['failed_ids'])
    if parameter_filter is None:
        fallback_ids.update(assignment['element_ids'])

    selection_filter = create_selection_filter_with_style(
        settings_view,
        u'AUTO_{0}_{1}_по ID'.format(
            sheet_number,
            source_filter.Name
        ),
        fallback_ids,
        source_record,
        True
    )

    return {
        'source_filter': source_filter,
        'parameter_filter': parameter_filter,
        'selection_filter': selection_filter,
        'token': token,
        'roles': sorted(assignment['roles'], key=natural_sort_key),
        'target_count': len(assignment['element_ids']),
        'written_count': len(written_ids),
        'unchanged_count': len(write_result['unchanged_ids']),
        'fallback_count': len(fallback_ids),
        'parameter_filter_error': parameter_filter_error
    }


def collect_all_circuit_element_ids(circuits):
    """Собирает щиты и приборы всех цепей проекта."""
    result = set()
    for circuit in circuits:
        panel_id = get_id_value(get_circuit_panel(circuit))
        if panel_id is not None:
            result.add(panel_id)
        for member in get_circuit_members(circuit):
            member_id = get_id_value(member)
            if member_id is not None:
                result.add(member_id)
    return result


def collect_hide_filter_ids(
        new_view,
        keep_ids,
        all_circuit_ids,
        all_route_ids):
    """Находит остальные элементы ЭОМ для точного скрывающего фильтра."""
    keep_ids = set(keep_ids)
    candidates = set(all_circuit_ids)
    candidates.update(all_route_ids)

    try:
        visible_elements = (
            DB.FilteredElementCollector(doc, new_view.Id)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        visible_elements = []

    for element in visible_elements:
        category_id = get_element_category_id(element)
        if category_id in CONTROLLED_CATEGORY_IDS:
            element_id = get_id_value(element)
            if element_id is not None:
                candidates.add(element_id)

    return candidates.difference(keep_ids)


def create_hide_others_filter(settings_view, sheet_number, integer_ids):
    """Создаёт SelectionFilterElement, скрывающий остальные элементы ЭОМ."""
    integer_ids = set(integer_ids)
    if not integer_ids:
        return None

    hide_filter = DB.SelectionFilterElement.Create(
        doc,
        get_unique_filter_name(
            u'AUTO_{0}_Скрыть остальные ЭОМ'.format(sheet_number)
        )
    )
    hide_filter.SetElementIds(make_element_id_list(integer_ids))
    settings_view.AddFilter(hide_filter.Id)
    settings_view.SetFilterVisibility(hide_filter.Id, False)
    try:
        settings_view.SetIsFilterEnabled(hide_filter.Id, True)
    except Exception:
        pass
    return hide_filter


def exclude_ids_from_selection_filter(selection_filter, protected_ids):
    """Гарантированно удаляет выбранные ID из скрывающего фильтра."""
    if selection_filter is None:
        return set()

    protected_ids = set(protected_ids)
    try:
        current_ids = set(
            element_id.IntegerValue
            for element_id in selection_filter.GetElementIds()
        )
    except Exception:
        return set()

    conflicting_ids = current_ids.intersection(protected_ids)
    if not conflicting_ids:
        return set()

    selection_filter.SetElementIds(
        make_element_id_list(current_ids.difference(conflicting_ids))
    )
    return conflicting_ids


def unhide_selected_elements(view, keep_ids):
    """Снимает индивидуальное скрытие с выбранных элементов."""
    unhide_ids = set()
    for integer_id in keep_ids:
        element = doc.GetElement(DB.ElementId(integer_id))
        if element is None:
            continue
        try:
            if element.IsHidden(view):
                unhide_ids.add(integer_id)
        except Exception:
            pass

    if not unhide_ids:
        return 0

    try:
        view.UnhideElements(make_element_id_list(unhide_ids))
        return len(unhide_ids)
    except Exception:
        return 0


def copy_viewport_properties(source_viewport, target_viewport):
    """Переносит тип, поворот и оформление подписи видового экрана."""
    try:
        source_type_id = source_viewport.GetTypeId()
        if source_type_id != target_viewport.GetTypeId():
            target_viewport.ChangeTypeId(source_type_id)
    except Exception:
        pass

    try:
        target_viewport.Rotation = source_viewport.Rotation
    except Exception:
        pass

    try:
        target_viewport.LabelOffset = source_viewport.LabelOffset
    except Exception:
        pass

    try:
        target_viewport.LabelLineLength = source_viewport.LabelLineLength
    except Exception:
        pass


def get_titleblock_frame_outline(title_block, sheet):
    """Возвращает фактический XY-BoundingBox экземпля основной надписи."""
    if title_block is None:
        return None

    bounding_box = None
    try:
        bounding_box = title_block.get_BoundingBox(sheet)
    except Exception:
        pass
    if bounding_box is None:
        try:
            bounding_box = title_block.get_BoundingBox(None)
        except Exception:
            pass
    if bounding_box is None:
        return None

    points = []
    for x_value in [bounding_box.Min.X, bounding_box.Max.X]:
        for y_value in [bounding_box.Min.Y, bounding_box.Max.Y]:
            for z_value in [bounding_box.Min.Z, bounding_box.Max.Z]:
                point = DB.XYZ(x_value, y_value, z_value)
                try:
                    point = bounding_box.Transform.OfPoint(point)
                except Exception:
                    pass
                points.append(point)

    if not points:
        return None

    minimum_x = min(point.X for point in points)
    minimum_y = min(point.Y for point in points)
    maximum_x = max(point.X for point in points)
    maximum_y = max(point.Y for point in points)
    minimum = DB.XYZ(minimum_x, minimum_y, 0.0)
    maximum = DB.XYZ(maximum_x, maximum_y, 0.0)
    center = DB.XYZ(
        (minimum_x + maximum_x) / 2.0,
        (minimum_y + maximum_y) / 2.0,
        0.0
    )
    return {
        'minimum': minimum,
        'maximum': maximum,
        'center': center,
        'width': maximum_x - minimum_x,
        'height': maximum_y - minimum_y
    }


def get_main_titleblock(sheet):
    """Выбирает именно рамку листа — самый крупный элемент категории."""
    try:
        title_blocks = (
            DB.FilteredElementCollector(doc, sheet.Id)
            .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        title_blocks = []

    if not title_blocks:
        return None

    best_titleblock = None
    best_area = -1.0
    for title_block in title_blocks:
        outline = get_titleblock_frame_outline(title_block, sheet)
        if outline is None:
            if best_titleblock is None:
                best_titleblock = title_block
            continue
        area = outline['width'] * outline['height']
        if area > best_area:
            best_area = area
            best_titleblock = title_block
    return best_titleblock


def copy_titleblock_instance_parameters(source_sheet, target_sheet):
    """Копирует изменяемые экземплярные параметры рамки/штампа."""
    source_titleblock = get_main_titleblock(source_sheet)
    target_titleblock = get_main_titleblock(target_sheet)
    result = {
        'copied_count': 0,
        'failed_count': 0,
        'skipped_identity_count': 0,
        'source_id': (
            source_titleblock.Id.IntegerValue
            if source_titleblock is not None else None
        ),
        'target_id': (
            target_titleblock.Id.IntegerValue
            if target_titleblock is not None else None
        )
    }
    if source_titleblock is None or target_titleblock is None:
        return result

    try:
        if target_titleblock.GetTypeId() != source_titleblock.GetTypeId():
            target_titleblock.ChangeTypeId(source_titleblock.GetTypeId())
            doc.Regenerate()
    except Exception:
        result['failed_count'] += 1

    for source_parameter in source_titleblock.Parameters:
        try:
            # У основной надписи параметры «Номер листа» и «Имя листа»
            # проксируют свойства ViewSheet. Их запись создаёт отложенную
            # ошибку дубля номера при Commit транзакции.
            if is_sheet_identity_parameter(source_parameter):
                result['skipped_identity_count'] += 1
                continue
            if not source_parameter.HasValue:
                continue
            target_parameter = target_titleblock.get_Parameter(
                source_parameter.Definition
            )
            if target_parameter is None or target_parameter.IsReadOnly:
                continue
            if target_parameter.StorageType != source_parameter.StorageType:
                continue

            storage_type = source_parameter.StorageType
            if storage_type == DB.StorageType.String:
                value = source_parameter.AsString()
                if value is None:
                    continue
                target_parameter.Set(value)
            elif storage_type == DB.StorageType.Integer:
                target_parameter.Set(source_parameter.AsInteger())
            elif storage_type == DB.StorageType.Double:
                target_parameter.Set(source_parameter.AsDouble())
            elif storage_type == DB.StorageType.ElementId:
                target_parameter.Set(source_parameter.AsElementId())
            else:
                continue
            result['copied_count'] += 1
        except Exception:
            result['failed_count'] += 1

    return result


def get_titleblock_data(sheet):
    """Возвращает рамку и её видимый нижний левый угол, а не LocationPoint."""
    title_block = get_main_titleblock(sheet)
    if title_block is None:
        return None, None

    outline = get_titleblock_frame_outline(title_block, sheet)
    if outline is not None:
        return title_block, outline['minimum']

    try:
        if isinstance(title_block.Location, DB.LocationPoint):
            return title_block, title_block.Location.Point
    except Exception:
        pass
    return title_block, None


def measure_titleblock_alignment(source_sheet, target_sheet):
    """Сравнивает рамки категории «Основные надписи» после Commit()."""
    result = {
        'success': False,
        'method': u'нижний левый угол BoundingBox основной надписи',
        'deviation': None,
        'deviation_x': None,
        'deviation_y': None,
        'source_width': None,
        'source_height': None,
        'target_width': None,
        'target_height': None,
        'width_difference': None,
        'height_difference': None,
        'error': None
    }

    source_titleblock = get_main_titleblock(source_sheet)
    target_titleblock = get_main_titleblock(target_sheet)
    source_outline = get_titleblock_frame_outline(
        source_titleblock,
        source_sheet
    )
    target_outline = get_titleblock_frame_outline(
        target_titleblock,
        target_sheet
    )
    if source_outline is None or target_outline is None:
        result['error'] = u'не найден BoundingBox рамки основной надписи'
        return result

    deviation_x = (
        source_outline['minimum'].X - target_outline['minimum'].X
    )
    deviation_y = (
        source_outline['minimum'].Y - target_outline['minimum'].Y
    )
    deviation = (
        deviation_x * deviation_x
        + deviation_y * deviation_y
    ) ** 0.5
    width_difference = abs(
        source_outline['width'] - target_outline['width']
    )
    height_difference = abs(
        source_outline['height'] - target_outline['height']
    )
    result.update({
        'deviation': deviation,
        'deviation_x': deviation_x,
        'deviation_y': deviation_y,
        'source_width': source_outline['width'],
        'source_height': source_outline['height'],
        'target_width': target_outline['width'],
        'target_height': target_outline['height'],
        'width_difference': width_difference,
        'height_difference': height_difference,
        'success': deviation <= (0.1 / 304.8)
    })
    return result


def align_titleblock_to_source(source_sheet, target_sheet):
    """Итеративно совмещает фактические BoundingBox основных надписей."""
    source_titleblock = get_main_titleblock(source_sheet)
    target_titleblock = get_main_titleblock(target_sheet)
    tolerance = 0.1 / 304.8
    if source_titleblock is None or target_titleblock is None:
        return measure_titleblock_alignment(source_sheet, target_sheet)

    try:
        target_was_pinned = bool(target_titleblock.Pinned)
    except Exception:
        target_was_pinned = False

    try:
        target_titleblock.Pinned = False
    except Exception:
        pass

    last_error = None
    for attempt in range(5):
        try:
            doc.Regenerate()
            source_outline = get_titleblock_frame_outline(
                source_titleblock,
                source_sheet
            )
            target_outline = get_titleblock_frame_outline(
                target_titleblock,
                target_sheet
            )
            if source_outline is None or target_outline is None:
                break
            move_vector = DB.XYZ(
                source_outline['minimum'].X
                - target_outline['minimum'].X,
                source_outline['minimum'].Y
                - target_outline['minimum'].Y,
                0.0
            )
            if move_vector.GetLength() <= tolerance:
                break
            DB.ElementTransformUtils.MoveElement(
                doc,
                target_titleblock.Id,
                move_vector
            )
        except Exception as error:
            last_error = unicode(error)
            break

    try:
        target_titleblock.Pinned = target_was_pinned
    except Exception:
        pass

    result = measure_titleblock_alignment(source_sheet, target_sheet)
    if last_error is not None:
        result['error'] = last_error
    return result


def get_crop_box_diagnostics(source_view, target_view):
    """Сравнивает Crop Box в мировых координатах модели."""
    result = {
        'center_deviation': None,
        'center_deviation_x': None,
        'center_deviation_y': None,
        'width_difference': None,
        'height_difference': None,
        'error': None
    }

    try:
        source_box = source_view.CropBox
        target_box = target_view.CropBox

        source_local_center = DB.XYZ(
            (source_box.Min.X + source_box.Max.X) / 2.0,
            (source_box.Min.Y + source_box.Max.Y) / 2.0,
            (source_box.Min.Z + source_box.Max.Z) / 2.0
        )
        target_local_center = DB.XYZ(
            (target_box.Min.X + target_box.Max.X) / 2.0,
            (target_box.Min.Y + target_box.Max.Y) / 2.0,
            (target_box.Min.Z + target_box.Max.Z) / 2.0
        )
        source_world_center = source_box.Transform.OfPoint(
            source_local_center
        )
        target_world_center = target_box.Transform.OfPoint(
            target_local_center
        )

        deviation_x = source_world_center.X - target_world_center.X
        deviation_y = source_world_center.Y - target_world_center.Y
        result['center_deviation_x'] = deviation_x
        result['center_deviation_y'] = deviation_y
        result['center_deviation'] = (
            deviation_x * deviation_x
            + deviation_y * deviation_y
        ) ** 0.5
        result['width_difference'] = abs(
            (source_box.Max.X - source_box.Min.X)
            - (target_box.Max.X - target_box.Min.X)
        )
        result['height_difference'] = abs(
            (source_box.Max.Y - source_box.Min.Y)
            - (target_box.Max.Y - target_box.Min.Y)
        )
    except Exception as error:
        result['error'] = unicode(error)

    return result


def capture_viewport_layout(source_sheet, source_viewport):
    """Сохраняет положение viewport относительно рамки штампа."""
    source_center = source_viewport.GetBoxCenter()
    source_titleblock, source_anchor = get_titleblock_data(source_sheet)

    offset_from_titleblock = None
    if source_titleblock is not None and source_anchor is not None:
        offset_from_titleblock = source_center - source_anchor

    source_width = None
    source_height = None
    source_minimum = None
    source_maximum = None
    try:
        outline = source_viewport.GetBoxOutline()
        source_minimum = outline.MinimumPoint
        source_maximum = outline.MaximumPoint
        source_width = outline.MaximumPoint.X - outline.MinimumPoint.X
        source_height = outline.MaximumPoint.Y - outline.MinimumPoint.Y
    except Exception:
        pass

    minimum_offset_from_titleblock = None
    if source_anchor is not None and source_minimum is not None:
        minimum_offset_from_titleblock = (
            source_minimum - source_anchor
        )

    try:
        pinned = bool(source_viewport.Pinned)
    except Exception:
        pinned = False

    return {
        'source_center': source_center,
        'offset_from_titleblock': offset_from_titleblock,
        'source_minimum': source_minimum,
        'source_maximum': source_maximum,
        'minimum_offset_from_titleblock': (
            minimum_offset_from_titleblock
        ),
        'source_width': source_width,
        'source_height': source_height,
        'pinned': pinned
    }


def get_target_viewport_center(target_sheet, layout_data):
    """Вычисляет центр viewport на новом листе относительно его штампа."""
    offset = layout_data.get('offset_from_titleblock')
    if offset is not None:
        target_titleblock, target_anchor = get_titleblock_data(target_sheet)
        if target_titleblock is not None and target_anchor is not None:
            return target_anchor + offset, u'относительно штампа'

    return layout_data['source_center'], u'по абсолютным координатам'


def get_target_viewport_anchor(target_sheet, layout_data):
    """Получает целевой нижний левый угол фактической рамки viewport."""
    minimum_offset = layout_data.get(
        'minimum_offset_from_titleblock'
    )
    if minimum_offset is not None:
        target_titleblock, target_anchor = get_titleblock_data(target_sheet)
        if target_titleblock is not None and target_anchor is not None:
            return (
                target_anchor + minimum_offset,
                u'по углу рамки относительно штампа',
                True
            )

    source_minimum = layout_data.get('source_minimum')
    if source_minimum is not None:
        return (
            source_minimum,
            u'по абсолютному углу рамки',
            True
        )

    return (
        layout_data['source_center'],
        u'по центру viewport',
        False
    )


def measure_viewport_alignment(
        target_sheet,
        target_viewport,
        layout_data):
    """Измеряет фактическое положение уже после Commit()."""
    desired_center, placement_method = get_target_viewport_center(
        target_sheet,
        layout_data
    )
    actual_center = target_viewport.GetBoxCenter()
    deviation_x = desired_center.X - actual_center.X
    deviation_y = desired_center.Y - actual_center.Y
    deviation_z = desired_center.Z - actual_center.Z
    deviation = (
        deviation_x * deviation_x
        + deviation_y * deviation_y
    ) ** 0.5

    target_width = None
    target_height = None
    try:
        target_outline = target_viewport.GetBoxOutline()
        target_width = (
            target_outline.MaximumPoint.X
            - target_outline.MinimumPoint.X
        )
        target_height = (
            target_outline.MaximumPoint.Y
            - target_outline.MinimumPoint.Y
        )
    except Exception:
        pass

    return {
        'method': u'по центру viewport относительно штампа',
        'placement_method': placement_method,
        'desired_anchor': desired_center,
        'actual_anchor': actual_center,
        'actual_center': actual_center,
        'deviation': deviation,
        'deviation_x': deviation_x,
        'deviation_y': deviation_y,
        'ignored_z_difference': deviation_z,
        'attempts': 0,
        'success': deviation <= (0.1 / 304.8),
        'error': None,
        'source_width': layout_data.get('source_width'),
        'source_height': layout_data.get('source_height'),
        'target_width': target_width,
        'target_height': target_height
    }


def align_viewport_to_source(
        target_sheet,
        target_viewport,
        layout_data):
    """Повторяет рабочий этап «Выровнять видовые экраны»."""
    desired_center, placement_method = get_target_viewport_center(
        target_sheet,
        layout_data
    )
    method = u'по центру viewport относительно штампа'

    try:
        target_viewport.Pinned = False
    except Exception:
        pass

    tolerance = 0.1 / 304.8
    attempts = 0
    last_error = None

    for attempt in range(3):
        attempts = attempt + 1
        doc.Regenerate()
        current_center = target_viewport.GetBoxCenter()
        # В рабочем скрипте используется полный вектор
        # desired GetBoxCenter - current GetBoxCenter.
        move_vector = desired_center - current_center
        if move_vector.GetLength() <= tolerance:
            break

        try:
            DB.ElementTransformUtils.MoveElement(
                doc,
                target_viewport.Id,
                move_vector
            )
        except Exception as move_error:
            last_error = unicode(move_error)
            break

    doc.Regenerate()
    actual_center = target_viewport.GetBoxCenter()
    actual_anchor = actual_center
    deviation_x = desired_center.X - actual_center.X
    deviation_y = desired_center.Y - actual_center.Y
    ignored_z_difference = desired_center.Z - actual_center.Z
    deviation = (
        deviation_x * deviation_x
        + deviation_y * deviation_y
    ) ** 0.5

    target_width = None
    target_height = None
    try:
        target_outline = target_viewport.GetBoxOutline()
        target_width = (
            target_outline.MaximumPoint.X
            - target_outline.MinimumPoint.X
        )
        target_height = (
            target_outline.MaximumPoint.Y
            - target_outline.MinimumPoint.Y
        )
    except Exception:
        pass

    try:
        target_viewport.Pinned = layout_data.get('pinned', False)
    except Exception:
        pass

    return {
        'method': method,
        'placement_method': placement_method,
        'desired_anchor': desired_center,
        'actual_anchor': actual_anchor,
        'actual_center': actual_center,
        'deviation': deviation,
        'deviation_x': deviation_x,
        'deviation_y': deviation_y,
        'ignored_z_difference': ignored_z_difference,
        'attempts': attempts,
        'success': deviation <= tolerance,
        'error': last_error,
        'source_width': layout_data.get('source_width'),
        'source_height': layout_data.get('source_height'),
        'target_width': target_width,
        'target_height': target_height
    }


def create_selected_groups_sheet(
        source_sheet,
        source_viewport,
        source_view,
        duplicate_option,
        selected_circuits,
        all_circuits,
        results,
        analyses,
        safe_selection_sets,
        keep_ids,
        all_route_ids):
    """Создаёт новый лист, план, шаблон и фильтры выбранных групп."""
    creation_result = None
    frame_data = capture_view_frame(source_view)
    viewport_layout = capture_viewport_layout(
        source_sheet,
        source_viewport
    )

    transaction_group = DB.TransactionGroup(
        doc,
        u'Создание листа выбранных цепей'
    )
    transaction_group.Start()

    create_transaction = DB.Transaction(
        doc,
        u'1. Создание листа, вида и фильтров'
    )
    create_transaction.Start()
    frame_transaction = None
    placement_transaction = None
    correction_transaction = None

    try:
        title_block_type_id = get_title_block_type_id(source_sheet)
        new_sheet = DB.ViewSheet.Create(doc, title_block_type_id)

        # Номер назначаем сразу после создания. Обычного сравнения строк
        # недостаточно: окончательную проверку уникальности выполняет Revit.
        group_text = get_selected_group_text(selected_circuits)
        assigned_sheet_number = assign_unique_sheet_number(
            new_sheet,
            source_sheet
        )

        copied_parameter_count = copy_sheet_parameters(
            source_sheet,
            new_sheet
        )

        # Защита от пользовательского параметра с похожим названием:
        # после копирования номер должен остаться назначенным выше.
        if unicode(new_sheet.SheetNumber) != assigned_sheet_number:
            assign_unique_sheet_number(new_sheet, source_sheet)

        new_sheet.Name = sanitize_revit_name(
            u'{0} — группы {1}'.format(source_sheet.Name, group_text),
            max_length=240
        )
        doc.Regenerate()

        titleblock_parameter_copy_result = (
            copy_titleblock_instance_parameters(
                source_sheet,
                new_sheet
            )
        )
        doc.Regenerate()

        # Как в эталонном скрипте: листовые надписи и детали
        # копируются с Transform.Identity, то есть без сдвига XY.
        sheet_detail_copy_result = copy_sheet_details_and_text(
            source_sheet,
            new_sheet
        )
        doc.Regenerate()

        new_view_id = source_view.Duplicate(duplicate_option)
        new_view = doc.GetElement(new_view_id)
        if new_view is None:
            raise Exception(u'Revit не создал копию основного вида.')

        new_view.Name = get_unique_view_name(
            u'{0} — группы {1}'.format(source_view.Name, group_text)
        )
        doc.Regenerate()

        template_result = create_view_template_from_copy(
            source_view,
            new_view,
            new_sheet.SheetNumber,
            group_text,
            frame_data
        )
        new_template = template_result['new_template']
        doc.Regenerate()

        shown_category_count, failed_category_ids = (
            show_selected_categories(new_template, keep_ids)
        )

        assignments, unassigned_ids = build_filter_assignments(
            results,
            analyses,
            safe_selection_sets
        )
        removed_filter_names, failed_removed_filter_ids = (
            remove_conflicting_filters(
                new_template,
                analyses,
                keep_ids,
                assignments.keys()
            )
        )

        cloned_filter_results = []
        for source_filter_id in get_ordered_assignment_ids(assignments):
            cloned_filter_results.append(
                create_parameter_filter_from_assignment(
                    new_template,
                    new_sheet.SheetNumber,
                    assignments[source_filter_id]
                )
            )

        # Повторная проверка обязательна именно после записи токенов в
        # «Примечание»: унаследованный скрывающий фильтр мог начать
        # захватывать розетки только на этом этапе.
        post_token_filter_result = remove_post_token_hiding_filters(
            new_template,
            keep_ids
        )
        removed_filter_names.extend(
            post_token_filter_result['removed_names']
        )
        failed_removed_filter_ids.extend(
            post_token_filter_result['failed_ids']
        )

        all_circuit_ids = collect_all_circuit_element_ids(all_circuits)
        protected_keep_ids = set(keep_ids)
        for assignment in assignments.values():
            protected_keep_ids.update(assignment['element_ids'])
        hide_ids = collect_hide_filter_ids(
            new_view,
            protected_keep_ids,
            all_circuit_ids,
            all_route_ids
        )
        hide_filter = create_hide_others_filter(
            new_template,
            new_sheet.SheetNumber,
            hide_ids
        )
        hide_filter_conflicts = exclude_ids_from_selection_filter(
            hide_filter,
            protected_keep_ids
        )
        hide_ids.difference_update(hide_filter_conflicts)

        new_view.ViewTemplateId = new_template.Id
        doc.Regenerate()
        unhidden_count = unhide_selected_elements(
            new_view,
            protected_keep_ids
        )
        doc.Regenerate()

        # В первой транзакции viewport только создаётся.
        # Точное положение задаётся после Commit(), когда Revit
        # уже пересчитал его фактические границы.
        new_viewport = DB.Viewport.Create(
            doc,
            new_sheet.Id,
            new_view.Id,
            viewport_layout['source_center']
        )
        copy_viewport_properties(source_viewport, new_viewport)
        doc.Regenerate()
        create_transaction.Commit()

        # Этап 2 рабочего скрипта: Crop Box применяется
        # к уже созданному и зафиксированному виду.
        frame_transaction = DB.Transaction(
            doc,
            u'2. Рамка 2D по эталону'
        )
        frame_transaction.Start()
        new_view.ViewTemplateId = DB.ElementId.InvalidElementId
        doc.Regenerate()
        final_frame_result = apply_view_frame(new_view, frame_data)
        doc.Regenerate()
        new_view.ViewTemplateId = new_template.Id
        doc.Regenerate()
        frame_transaction.Commit()

        # Этап 3: штамп и viewport двигаются только после
        # окончательного пересчёта рамки в предыдущей транзакции.
        placement_transaction = DB.Transaction(
            doc,
            u'3. Выравнивание штампа и viewport'
        )
        placement_transaction.Start()
        titleblock_alignment_result = align_titleblock_to_source(
            source_sheet,
            new_sheet
        )
        doc.Regenerate()
        viewport_alignment_result = align_viewport_to_source(
            new_sheet,
            new_viewport,
            viewport_layout
        )
        doc.Regenerate()
        placement_transaction.Commit()

        # Это уже проверка ПОСЛЕ Commit(), а не внутри него.
        titleblock_alignment_result = measure_titleblock_alignment(
            source_sheet,
            new_sheet
        )
        viewport_alignment_result = measure_viewport_alignment(
            new_sheet,
            new_viewport,
            viewport_layout
        )

        # Если Revit сдвинул viewport именно в момент Commit(),
        # выполняем один посттранзакционный корректирующий шаг.
        post_commit_correction_count = 0
        for correction_index in range(3):
            if (
                    titleblock_alignment_result['success']
                    and viewport_alignment_result['success']):
                break
            correction_transaction = DB.Transaction(
                doc,
                u'4. Коррекция рамки и viewport '
                u'после Commit ({0})'.format(
                    correction_index + 1
                )
            )
            correction_transaction.Start()
            align_titleblock_to_source(source_sheet, new_sheet)
            doc.Regenerate()
            align_viewport_to_source(
                new_sheet,
                new_viewport,
                viewport_layout
            )
            correction_transaction.Commit()
            post_commit_correction_count += 1
            titleblock_alignment_result = measure_titleblock_alignment(
                source_sheet,
                new_sheet
            )
            viewport_alignment_result = measure_viewport_alignment(
                new_sheet,
                new_viewport,
                viewport_layout
            )

        crop_alignment_result = get_crop_box_diagnostics(
            source_view,
            new_view
        )
        placement_method = viewport_alignment_result['method']

        creation_result = {
            'sheet': new_sheet,
            'view': new_view,
            'viewport': new_viewport,
            'template_result': template_result,
            'final_frame_result': final_frame_result,
            'crop_alignment_result': crop_alignment_result,
            'titleblock_alignment_result': titleblock_alignment_result,
            'viewport_alignment_result': viewport_alignment_result,
            'placement_method': placement_method,
            'alignment_after_commit': True,
            'post_commit_correction_count': (
                post_commit_correction_count
            ),
            'cloned_filter_results': cloned_filter_results,
            'hide_filter': hide_filter,
            'hide_filter_conflicts': hide_filter_conflicts,
            'hidden_count': len(hide_ids),
            'unhidden_count': unhidden_count,
            'copied_parameter_count': copied_parameter_count,
            'titleblock_parameter_copy_result': (
                titleblock_parameter_copy_result
            ),
            'sheet_detail_copy_result': sheet_detail_copy_result,
            'shown_category_count': shown_category_count,
            'failed_category_ids': failed_category_ids,
            'removed_filter_names': removed_filter_names,
            'failed_removed_filter_ids': failed_removed_filter_ids,
            'post_token_filter_result': post_token_filter_result,
            'unassigned_ids': unassigned_ids,
            'with_detailing': (
                duplicate_option == DB.ViewDuplicateOption.WithDetailing
            )
        }

        transaction_group.Assimilate()
    except Exception:
        try:
            correction_transaction.RollBack()
        except Exception:
            pass
        try:
            placement_transaction.RollBack()
        except Exception:
            pass
        try:
            frame_transaction.RollBack()
        except Exception:
            pass
        try:
            create_transaction.RollBack()
        except Exception:
            pass
        try:
            transaction_group.RollBack()
        except Exception:
            pass
        raise

    return creation_result


def main():
    """Создаёт лист и отдельный шаблон только для выбранных цепей."""
    try:
        source_context = resolve_source_context()
        if source_context is None:
            forms.alert(
                u'Откройте исходный лист или размещённый на нём план '
                u'и запустите скрипт ещё раз.\n\n'
                u'На исходном листе должен находиться дублируемый '
                u'модельный вид.',
                title=u'Лист выбранных цепей',
                warn_icon=True
            )
            return

        source_sheet, source_viewport, source_view, duplicate_option = (
            source_context
        )
        if duplicate_option is None:
            forms.alert(
                u'Основной вид исходного листа нельзя дублировать.',
                title=u'Лист выбранных цепей',
                warn_icon=True
            )
            return

        circuits = collect_electrical_circuits()
        if not circuits:
            forms.alert(
                u'В текущем проекте не найдены электрические цепи.',
                title=u'Лист выбранных цепей'
            )
            return

        selected_circuits = select_circuits(circuits)
        if not selected_circuits:
            return

        route_elements = collect_route_elements()
        graph, element_map, route_ids = build_physical_graph(route_elements)
        electrical_system_seed_map = build_electrical_system_seed_map(
            element_map
        )
        panel_path_cache = {}

        results = []
        analyses = []
        safe_selection_sets = []
        selection_infos = []
        all_selected_ids = set()

        print(u'=== ЦЕПИ И ФИЗИЧЕСКИЕ ТРАССЫ ===')
        print(u'Исходный лист: {0} — {1}'.format(
            source_sheet.SheetNumber,
            source_sheet.Name
        ))
        print(u'Анализируемый вид: {0}, ID {1}'.format(
            source_view.Name,
            source_view.Id.IntegerValue
        ))
        print(u'Найдено цепей в проекте: {0}'.format(len(circuits)))
        print(u'Выбрано цепей: {0}'.format(len(selected_circuits)))
        print(u'Элементов физической трассы в проекте: {0}'.format(
            len(route_elements)
        ))
        print(u'')

        for circuit in selected_circuits:
            result = find_circuit_route(
                circuit,
                graph,
                element_map,
                route_ids,
                panel_path_cache,
                electrical_system_seed_map
            )
            results.append(result)
            print_result(result)

        print(u'=== ДИАГНОСТИКА ЦВЕТА ===')
        for result in results:
            analysis = analyze_filters_for_group(source_view, result)
            analyses.append(analysis)
            print_group_filter_report(source_view, result, analysis)

            safe_ids, selection_info = build_safe_selection_for_result(
                result,
                analysis,
                graph,
                element_map
            )

            # Сохраняем не только экземпляр из ElectricalSystem, но и его
            # вложенную видимую графику с отдельными ElementId.
            family_seed_ids = set(
                analysis['id_groups']['member_ids']
            )
            family_seed_ids.update(
                analysis['id_groups']['panel_ids']
            )
            family_instance_ids = expand_family_instance_ids(
                family_seed_ids
            )
            added_family_ids = family_instance_ids.difference(safe_ids)
            safe_ids.update(family_instance_ids)
            selection_info['family_component_count'] = len(
                added_family_ids
            )

            safe_selection_sets.append(safe_ids)
            selection_infos.append(selection_info)
            all_selected_ids.update(safe_ids)
            print_selection_restriction(result, selection_info)

        if not all_selected_ids:
            forms.alert(
                u'В выбранных цепях не найдено элементов для переноса.',
                title=u'Лист выбранных цепей',
                warn_icon=True
            )
            return

        preview_assignments, preview_unassigned_ids = (
            build_filter_assignments(
                results,
                analyses,
                safe_selection_sets
            )
        )

        filter_lines = []
        for source_filter_id in get_ordered_assignment_ids(
                preview_assignments):
            assignment = preview_assignments[source_filter_id]
            filter_lines.append(
                u'• «{0}» — {1}; элементов: {2}'.format(
                    assignment['record']['filter'].Name,
                    u', '.join(sorted(
                        assignment['roles'],
                        key=natural_sort_key
                    )),
                    len(assignment['element_ids'])
                )
            )

        if not filter_lines:
            filter_lines.append(
                u'• Цветовые прототипы не найдены; элементы будут '
                u'оставлены видимыми без новых цветовых фильтров.'
            )

        selected_route_count = len(
            set(all_selected_ids).intersection(route_ids)
        )
        confirmation = (
            u'Исходный лист: {0} — {1}\n'
            u'Основной вид: {2}\n\n'
            u'Выбрано цепей: {3}\n'
            u'Элементов выбранных групп: {4}\n'
            u'Из них элементов трассы: {5}\n\n'
            u'Будут созданы копии фильтров:\n{6}\n\n'
            u'Будет создан новый лист, копия плана с детализацией и '
            u'отдельный дубликат шаблона. В «Примечание» выбранных '
            u'элементов будут ДОПИСАНЫ уникальные служебные токены; '
            u'существующий текст не удаляется. Остальные элементы ЭОМ '
            u'будут скрыты точным фильтром по ID. Рамка плана и '
            u'положение viewport относительно штампа будут повторены '
            u'по исходному листу.\n\n'
            u'Элементов без найденного цветового прототипа: {7}\n\n'
            u'Создать лист?'
        ).format(
            source_sheet.SheetNumber,
            source_sheet.Name,
            source_view.Name,
            len(selected_circuits),
            len(all_selected_ids),
            selected_route_count,
            u'\n'.join(filter_lines),
            len(preview_unassigned_ids)
        )

        if not forms.alert(
                confirmation,
                title=u'Лист выбранных цепей',
                yes=True,
                no=True):
            shown, message = select_and_show(all_selected_ids)
            forms.alert(
                u'Создание листа отменено.\n\n{0}'.format(message),
                title=u'Лист выбранных цепей'
            )
            return

        creation_result = create_selected_groups_sheet(
            source_sheet,
            source_viewport,
            source_view,
            duplicate_option,
            selected_circuits,
            circuits,
            results,
            analyses,
            safe_selection_sets,
            all_selected_ids,
            route_ids
        )

        new_sheet = creation_result['sheet']
        new_view = creation_result['view']
        new_template = creation_result['template_result']['new_template']

        try:
            uidoc.ActiveView = new_sheet
            uidoc.Selection.SetElementIds(
                make_element_id_list(all_selected_ids)
            )
            uidoc.RefreshActiveView()
        except Exception:
            pass

        created_filter_lines = []
        total_written = 0
        total_fallback = 0
        for filter_result in creation_result['cloned_filter_results']:
            total_written += filter_result['written_count']
            total_fallback += filter_result['fallback_count']

            parameter_filter = filter_result['parameter_filter']
            selection_filter = filter_result['selection_filter']
            if parameter_filter is not None:
                target_name = parameter_filter.Name
            elif selection_filter is not None:
                target_name = selection_filter.Name
            else:
                target_name = u'не создан'

            created_filter_lines.append(
                u'• «{0}» → «{1}»'.format(
                    filter_result['source_filter'].Name,
                    target_name
                )
            )

        print(u'=== СОЗДАННЫЙ ЛИСТ ===')
        print(u'Лист: {0} — {1}'.format(
            new_sheet.SheetNumber,
            new_sheet.Name
        ))
        print(u'Вид: {0}'.format(new_view.Name))
        print(u'Шаблон: {0}'.format(new_template.Name))
        print(u'Копий цветовых фильтров: {0}'.format(
            len(creation_result['cloned_filter_results'])
        ))
        print(u'Записано токенов в Примечание: {0}'.format(total_written))
        print(u'Элементов в резервных фильтрах по ID: {0}'.format(
            total_fallback
        ))
        print(u'Скрыто остальных элементов ЭОМ: {0}'.format(
            creation_result['hidden_count']
        ))
        print(u'Удалено выбранных ID из скрывающего фильтра при '
              u'контрольной проверке: {0}'.format(
                  len(creation_result['hide_filter_conflicts'])
              ))
        print(u'Снято конфликтующих фильтров всего: {0}'.format(
            len(creation_result['removed_filter_names'])
        ))
        post_token_filter_result = creation_result[
            'post_token_filter_result'
        ]
        print(u'Из них обнаружено после записи токенов: {0}'.format(
            len(post_token_filter_result['removed_names'])
        ))
        print(u'Освобождено выбранных элементов от таких фильтров: '
              u'{0}'.format(
                  len(post_token_filter_result['released_element_ids'])
              ))
        sheet_copy_result = creation_result['sheet_detail_copy_result']
        print(u'Листовых деталей и надписей найдено: {0}'.format(
            sheet_copy_result['candidate_count']
        ))
        print(u'Скопировано в исходных XY-координатах: {0}'.format(
            sheet_copy_result['copied_count']
        ))
        print(u'Обновлено листовых заголовков Galf_Текст_5: {0}'.format(
            sheet_copy_result['renamed_title_count']
        ))
        if sheet_copy_result['failed_ids']:
            print(u'Не скопировано листовых элементов: {0}'.format(
                len(sheet_copy_result['failed_ids'])
            ))
        print(u'Алгоритм размещения: '
              u'отдельные транзакции после Commit')
        titleblock_copy_result = creation_result[
            'titleblock_parameter_copy_result'
        ]
        print(u'Скопировано экземплярных параметров '
              u'основной надписи: {0}'.format(
                  titleblock_copy_result['copied_count']
              ))
        print(u'Пропущено связанных параметров номера/имени листа: '
              u'{0}'.format(
                  titleblock_copy_result['skipped_identity_count']
              ))
        print(u'Коррекций рамки и viewport после Commit: {0}'.format(
            creation_result['post_commit_correction_count']
        ))

        titleblock_alignment = creation_result[
            'titleblock_alignment_result'
        ]
        if titleblock_alignment['deviation'] is not None:
            print(u'Опора основной надписи: {0}'.format(
                titleblock_alignment['method']
            ))
            print(u'Отклонение BoundingBox основной '
                  u'надписи от эталона: '
                  u'{0:.3f} мм'.format(
                      titleblock_alignment['deviation'] * 304.8
                  ))
            print(u'Разница размеров BoundingBox основной '
                  u'надписи: X {0:.3f} мм; Y {1:.3f} мм'.format(
                      titleblock_alignment['width_difference'] * 304.8,
                      titleblock_alignment['height_difference'] * 304.8
                  ))
        else:
            print(u'Положение штампа не проверено: {0}'.format(
                titleblock_alignment['error'] or u'неизвестно'
            ))

        crop_alignment = creation_result['crop_alignment_result']
        if crop_alignment['center_deviation'] is not None:
            print(u'Смещение центра Crop Box в модели: '
                  u'{0:.3f} мм'.format(
                      crop_alignment['center_deviation'] * 304.8
                  ))
            print(u'Разница Crop Box в модели: '
                  u'X {0:.3f} мм; Y {1:.3f} мм'.format(
                      crop_alignment['width_difference'] * 304.8,
                      crop_alignment['height_difference'] * 304.8
                  ))
        else:
            print(u'Crop Box не проверен: {0}'.format(
                crop_alignment['error'] or u'неизвестно'
            ))

        frame_failures = creation_result[
            'final_frame_result'
        ].get('failed', [])
        if frame_failures:
            print(u'Не удалось восстановить: {0}'.format(
                u', '.join(frame_failures)
            ))

        alignment_result = creation_result[
            'viewport_alignment_result'
        ]
        deviation_mm = alignment_result['deviation'] * 304.8
        deviation_x_mm = alignment_result['deviation_x'] * 304.8
        deviation_y_mm = alignment_result['deviation_y'] * 304.8
        print(u'Положение рамки: {0}'.format(
            alignment_result['method']
        ))
        print(u'Отклонение рамки viewport по XY: {0:.3f} мм'.format(
            deviation_mm
        ))
        print(u'Отклонение по осям: X {0:+.3f} мм; '
              u'Y {1:+.3f} мм'.format(
                  deviation_x_mm,
                  deviation_y_mm
              ))

        source_width = alignment_result['source_width']
        source_height = alignment_result['source_height']
        target_width = alignment_result['target_width']
        target_height = alignment_result['target_height']
        if None not in [
                source_width,
                source_height,
                target_width,
                target_height]:
            width_difference_mm = abs(
                source_width - target_width
            ) * 304.8
            height_difference_mm = abs(
                source_height - target_height
            ) * 304.8
            print(u'Разница размеров рамки: X {0:.3f} мм; '
                  u'Y {1:.3f} мм'.format(
                      width_difference_mm,
                      height_difference_mm
                  ))
        print(u'')

        detailing_text = (
            u'План скопирован с детализацией и аннотациями.'
            if creation_result['with_detailing']
            else u'План скопирован без детализации: исходный вид не '
                 u'поддерживает режим «С детализацией».'
        )

        crop_deviation = crop_alignment['center_deviation']
        crop_is_exact = (
            crop_deviation is not None
            and crop_deviation <= (0.1 / 304.8)
            and not frame_failures
        )
        titleblock_is_exact = titleblock_alignment['success']

        if (
                alignment_result['success']
                and crop_is_exact
                and titleblock_is_exact):
            frame_text = (
                u'Штамп, мировой центр Crop Box и viewport '
                u'восстановлены по эталону. '
                u'Отклонение viewport по XY: {0:.3f} мм.'.format(
                    deviation_mm
                )
            )
        else:
            crop_deviation_mm = (
                crop_deviation * 304.8
                if crop_deviation is not None
                else -1.0
            )
            frame_text = (
                u'ВНИМАНИЕ: Revit не подтвердил полное '
                u'совпадение с эталоном. Viewport: {0:.3f} мм; '
                u'центр Crop Box в модели: {1:.3f} мм. '
                u'Подробности записаны в вывод pyRevit.'.format(
                    deviation_mm,
                    crop_deviation_mm
                )
            )

        result_message = (
            u'Лист создан успешно.\n\n'
            u'Номер: {0}\n'
            u'Имя: {1}\n'
            u'Вид: {2}\n'
            u'Шаблон: {3}\n\n'
            u'Созданные цветовые фильтры:\n{4}\n\n'
            u'Токены записаны в «Примечание»: {5}\n'
            u'Резервных элементов по ID: {6}\n'
            u'Скрыто остальных элементов ЭОМ: {7}\n'
            u'Элементов без цветового прототипа: {8}\n\n'
            u'{9}\n'
            u'{10}\n\n'
            u'В этой версии копируются штамп и один основной вид. '
            u'Легенды, спецификации и элементы непосредственно на '
            u'листе пока не копируются.'
        ).format(
            new_sheet.SheetNumber,
            new_sheet.Name,
            new_view.Name,
            new_template.Name,
            u'\n'.join(created_filter_lines) if created_filter_lines
            else u'не требовались',
            total_written,
            total_fallback,
            creation_result['hidden_count'],
            len(creation_result['unassigned_ids']),
            detailing_text,
            frame_text
        )
        forms.alert(result_message, title=u'Лист выбранных цепей')

    except Exception as error:
        print(traceback.format_exc())
        forms.alert(
            u'Ошибка выполнения:\n{0}\n\n'
            u'Изменения текущей операции отменены. Подробности '
            u'находятся в окне вывода pyRevit.'.format(error),
            title=u'Лист выбранных цепей',
            warn_icon=True
        )


if __name__ == '__main__':
    main()
