# -*- coding: utf-8 -*-
__title__ = u'Нумерация РК'
__doc__ = (
    u'Нумерует распределительные коробки в стабильном порядке. '
    u'Основной режим получает состав цепи из ElectricalSystem и ищет '
    u'физические пути от щита к приборам по соединителям коробов. '
    u'Резервный режим нумерует связанную ветку от выбранной РК.'
)
__version__ = '2.1'

import heapq
from collections import defaultdict, deque

from pyrevit import revit, DB, script, forms
from System.Collections.Generic import List
from Autodesk.Revit.UI.Selection import ObjectType


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# ============ НАСТРОЙКИ ============

# Достаточно фрагмента имени семейства. Сравнение без учёта регистра.
TARGET_BOX_FAMILY_NAMES = (
    u'SE_Коробка_ОткрытаяХ',
    u'SE_Коробка_ОткрытаяТ',
    u'SE_Коробка_ОткрытаяЭ',
    u'Galf_Коробка распределительная v.1.0.1',
)

CIRCUIT_PARAMETER_NAMES = (
    u'Номер электрической цепи',
    u'Номер цепи',
)

MODE_CIRCUITS = u'По электрическим цепям — от щита'
MODE_BRANCH = u'По связанной ветке — от выбранной РК'

# Формат нативного режима: РК-ЩР1-Гр.1-01.
CIRCUIT_MARK_FORMAT = u'РК-{panel}-{circuit}-{index}'

# Формат резервного режима: РК-Гр.1-01.
BRANCH_MARK_FORMAT = u'РК-{circuit}-{index}'

# Минимальная цена ребра нужна для стабильного обхода цепочек семейств,
# длина которых в API равна нулю.
MIN_EDGE_LENGTH = 0.001

ROUTE_CATEGORIES = (
    DB.BuiltInCategory.OST_CableTray,
    DB.BuiltInCategory.OST_CableTrayFitting,
    DB.BuiltInCategory.OST_Conduit,
    DB.BuiltInCategory.OST_ConduitFitting,
)

ROUTE_CATEGORY_IDS = set(int(category) for category in ROUTE_CATEGORIES)


# ============ 1. ПАРАМЕТРЫ И НАЗВАНИЯ ============

def text_value(value):
    if value is None:
        return u''
    try:
        return unicode(value).strip()
    except Exception:
        try:
            return str(value).strip()
        except Exception:
            return u''


def get_parameter_text(element, parameter_names, built_in_parameter=None):
    """Читает строку параметра только с переданного элемента."""
    if element is None:
        return u''

    parameters = []
    if built_in_parameter is not None:
        try:
            parameter = element.get_Parameter(built_in_parameter)
            if parameter is not None:
                parameters.append(parameter)
        except Exception:
            pass

    for parameter_name in parameter_names:
        try:
            parameter = element.LookupParameter(parameter_name)
            if parameter is not None:
                parameters.append(parameter)
        except Exception:
            pass

    for parameter in parameters:
        try:
            if not parameter.HasValue:
                continue
            if parameter.StorageType == DB.StorageType.String:
                value = text_value(parameter.AsString())
            else:
                value = text_value(parameter.AsValueString())
            if value:
                return value
        except Exception:
            pass
    return u''


def get_circuit_number_from_element(element):
    value = get_parameter_text(
        element,
        CIRCUIT_PARAMETER_NAMES,
        DB.BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER,
    )
    return value if value else u'БезЦепи'


def get_electrical_system_circuit_number(system):
    try:
        value = text_value(system.CircuitNumber)
        if value:
            return value
    except Exception:
        pass

    value = get_parameter_text(
        system,
        CIRCUIT_PARAMETER_NAMES,
        DB.BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER,
    )
    if value:
        return value

    try:
        value = text_value(system.Name)
        if value:
            return value
    except Exception:
        pass
    return u'Без номера'


def get_mark_parameter(element, writable=False):
    try:
        parameter = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if parameter is not None and (not writable or not parameter.IsReadOnly):
            return parameter
    except Exception:
        pass

    for parameter_name in (u'Марка', u'Mark'):
        try:
            parameter = element.LookupParameter(parameter_name)
            if parameter is not None and (not writable or not parameter.IsReadOnly):
                return parameter
        except Exception:
            pass
    return None


def get_current_mark(element):
    parameter = get_mark_parameter(element)
    if parameter is None:
        return u''
    try:
        return text_value(parameter.AsString())
    except Exception:
        return u''


def set_mark(element, mark_value):
    parameter = get_mark_parameter(element, writable=True)
    if parameter is None:
        raise Exception(
            u'У РК ID {0} отсутствует доступный для записи параметр «Марка».'.format(
                element.Id.IntegerValue
            )
        )
    parameter.Set(mark_value)


def get_family_name(element):
    try:
        return text_value(element.Symbol.Family.Name)
    except Exception:
        return u''


def is_target_box(element):
    if not isinstance(element, DB.FamilyInstance):
        return False
    family_name = get_family_name(element).lower()
    return any(name.lower() in family_name for name in TARGET_BOX_FAMILY_NAMES)


def element_display_name(element):
    if element is None:
        return u''

    for parameter_name in (u'Обозначение', u'ADSK_Обозначение', u'Марка'):
        value = get_parameter_text(element, (parameter_name,))
        if value:
            return value

    try:
        value = text_value(element.Name)
        if value:
            return value
    except Exception:
        pass

    return u'ID {0}'.format(element.Id.IntegerValue)


def get_panel_name(system):
    try:
        value = text_value(system.PanelName)
        if value:
            return value
    except Exception:
        pass

    try:
        panel = system.BaseEquipment
    except Exception:
        panel = None
    return element_display_name(panel) if panel is not None else u'БезЩита'


def get_load_name(system):
    try:
        value = text_value(system.LoadName)
        if value:
            return value
    except Exception:
        pass
    return u''


def clean_mark_part(value, fallback):
    result = text_value(value)
    for character in (u'\r', u'\n', u'\t', u'|', u';'):
        result = result.replace(character, u' ')
    result = u' '.join(result.split()).strip()
    return result if result else fallback


def make_circuit_mark(panel, circuit, index):
    return CIRCUIT_MARK_FORMAT.format(
        panel=clean_mark_part(panel, u'БезЩита'),
        circuit=clean_mark_part(circuit, u'БезЦепи'),
        index=u'{0:02d}'.format(index),
    )


def make_branch_mark(circuit, index):
    return BRANCH_MARK_FORMAT.format(
        circuit=clean_mark_part(circuit, u'БезЦепи'),
        index=u'{0:02d}'.format(index),
    )


# ============ 2. ФИЗИЧЕСКИЕ СОЕДИНИТЕЛИ ============

def get_raw_connectors(element):
    try:
        if hasattr(element, 'ConnectorManager') and element.ConnectorManager:
            return list(element.ConnectorManager.Connectors)
    except Exception:
        pass

    try:
        if element.MEPModel and element.MEPModel.ConnectorManager:
            return list(element.MEPModel.ConnectorManager.Connectors)
    except Exception:
        pass
    return []


def is_route_connector(connector):
    try:
        if connector.ConnectorType == DB.ConnectorType.Logical:
            return False
        return connector.Domain == DB.Domain.DomainCableTrayConduit
    except Exception:
        return False


def get_route_connectors(element):
    return [connector for connector in get_raw_connectors(element)
            if is_route_connector(connector)]


def is_graph_element(element):
    if element is None:
        return False
    try:
        if element.Category and element.Category.Id.IntegerValue in ROUTE_CATEGORY_IDS:
            return True
    except Exception:
        pass
    return isinstance(element, DB.FamilyInstance) and bool(get_route_connectors(element))


def get_family_tree(element):
    """Элемент и вложенные компоненты — только для поиска коннекторов."""
    if element is None:
        return []

    result = []
    queue = deque([element])
    visited = set()
    while queue:
        current = queue.popleft()
        current_id = current.Id.IntegerValue
        if current_id in visited:
            continue
        visited.add(current_id)
        result.append(current)
        try:
            subcomponent_ids = list(current.GetSubComponentIds())
        except Exception:
            subcomponent_ids = []
        for subcomponent_id in subcomponent_ids:
            subcomponent = doc.GetElement(subcomponent_id)
            if subcomponent is not None:
                queue.append(subcomponent)
    return result


def collect_route_elements():
    categories = List[DB.BuiltInCategory]()
    for category in ROUTE_CATEGORIES:
        categories.Add(category)
    category_filter = DB.ElementMulticategoryFilter(categories)
    return list(
        DB.FilteredElementCollector(doc)
        .WhereElementIsNotElementType()
        .WherePasses(category_filter)
        .ToElements()
    )


def collect_target_boxes():
    family_instances = (
        DB.FilteredElementCollector(doc)
        .OfClass(DB.FamilyInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    return [element for element in family_instances if is_target_box(element)]


def print_target_box_statistics(target_boxes):
    """Показывает, какие семейства распознаны как РК во всём проекте."""
    counts = defaultdict(int)
    for box in target_boxes:
        counts[get_family_name(box) or u'Без имени семейства'] += 1

    print(u'РК в проекте: {0}'.format(len(target_boxes)))
    for family_name in sorted(counts.keys(), key=lambda value: value.lower()):
        print(u'  - «{0}»: {1}'.format(family_name, counts[family_name]))


def add_element_and_family_tree(element_map, element):
    for current in get_family_tree(element):
        if is_graph_element(current) or current.Id == element.Id:
            element_map[current.Id.IntegerValue] = current


def build_connection_graph(initial_elements):
    """Строит замкнутый граф реальных соединителей коробов/труб."""
    element_map = {}
    for element in initial_elements:
        if element is not None:
            element_map[element.Id.IntegerValue] = element

    graph = defaultdict(set)
    queue = deque(element_map.values())
    processed = set()

    while queue:
        element = queue.popleft()
        element_id = element.Id.IntegerValue
        if element_id in processed:
            continue
        processed.add(element_id)
        graph[element_id]

        for connector in get_route_connectors(element):
            try:
                references = list(connector.AllRefs)
            except Exception:
                references = []

            for reference in references:
                try:
                    if not is_route_connector(reference):
                        continue
                    owner = reference.Owner
                    owner_id = owner.Id.IntegerValue
                    if owner_id == element_id or not is_graph_element(owner):
                        continue
                except Exception:
                    continue

                if owner_id not in element_map:
                    element_map[owner_id] = owner
                    queue.append(owner)
                graph[element_id].add(owner_id)
                graph[owner_id].add(element_id)

    return dict(graph), element_map


# ============ 3. РАССТОЯНИЯ И ПУТИ ============

def get_element_length(element):
    try:
        if isinstance(element.Location, DB.LocationCurve):
            return max(element.Location.Curve.Length, 0.0)
    except Exception:
        pass

    length_parameter_names = (
        'CURVE_ELEM_LENGTH',
        'RBS_CABLETRAY_LENGTH_PARAM',
        'RBS_CONDUIT_LENGTH_PARAM',
    )
    for parameter_name in length_parameter_names:
        built_in_parameter = getattr(DB.BuiltInParameter, parameter_name, None)
        if built_in_parameter is None:
            continue
        try:
            parameter = element.get_Parameter(built_in_parameter)
            if parameter and parameter.HasValue:
                return max(parameter.AsDouble(), 0.0)
        except Exception:
            pass
    return 0.0


def get_element_point(element):
    try:
        point = element.Location.Point
        if point is not None:
            return point
    except Exception:
        pass

    try:
        curve = element.Location.Curve
        return curve.Evaluate(0.5, True)
    except Exception:
        pass

    try:
        bounding_box = element.get_BoundingBox(None)
        if bounding_box is not None:
            return (bounding_box.Min + bounding_box.Max) * 0.5
    except Exception:
        pass
    return DB.XYZ(0.0, 0.0, 0.0)


def build_length_cache(element_map):
    return dict((element_id, get_element_length(element))
                for element_id, element in element_map.items())


def edge_length(first_id, second_id, length_cache):
    value = (length_cache.get(first_id, 0.0) +
             length_cache.get(second_id, 0.0)) * 0.5
    return max(value, MIN_EDGE_LENGTH)


def dijkstra(graph, element_map, source_ids):
    """Минимальная длина физического пути от одного или нескольких узлов."""
    length_cache = build_length_cache(element_map)
    distances = {}
    previous = {}
    heap = []

    for source_id in sorted(set(source_ids)):
        if source_id not in graph:
            continue
        distances[source_id] = 0.0
        previous[source_id] = None
        heapq.heappush(heap, (0.0, source_id))

    while heap:
        current_distance, current_id = heapq.heappop(heap)
        if current_distance > distances.get(current_id, float('inf')):
            continue

        for neighbor_id in sorted(graph.get(current_id, set())):
            candidate = current_distance + edge_length(
                current_id, neighbor_id, length_cache
            )
            known = distances.get(neighbor_id)
            if known is None or candidate < known - 1e-9:
                distances[neighbor_id] = candidate
                previous[neighbor_id] = current_id
                heapq.heappush(heap, (candidate, neighbor_id))
            elif known is not None and abs(candidate - known) <= 1e-9:
                # При одинаковой длине выбирается путь с меньшим ID.
                old_previous = previous.get(neighbor_id)
                if old_previous is not None and current_id < old_previous:
                    previous[neighbor_id] = current_id

    return distances, previous


def restore_path(previous, end_id):
    path = []
    current_id = end_id
    visited = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        path.append(current_id)
        current_id = previous.get(current_id)
    path.reverse()
    return path


def graph_ids_for_element(element, graph):
    return set(current.Id.IntegerValue for current in get_family_tree(element)
               if current.Id.IntegerValue in graph)


def box_graph_ids(box, graph):
    return graph_ids_for_element(box, graph)


def box_distance(box, distances, graph):
    values = [distances[node_id] for node_id in box_graph_ids(box, graph)
              if node_id in distances]
    return min(values) if values else None


def stable_box_sort_key(item):
    box, distance = item
    point = get_element_point(box)
    return (
        round(distance, 9),
        round(point.X, 9), round(point.Y, 9), round(point.Z, 9),
        box.Id.IntegerValue,
    )


# ============ 4. НАТИВНЫЕ ЭЛЕКТРИЧЕСКИЕ ЦЕПИ ============

def collect_electrical_systems():
    return list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.Electrical.ElectricalSystem)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def get_system_elements(system):
    try:
        return [element for element in system.Elements if element is not None]
    except Exception:
        return []


def circuit_choice_text(system):
    panel = get_panel_name(system)
    circuit = get_electrical_system_circuit_number(system)
    load = get_load_name(system)
    suffix = u' | {0}'.format(load) if load else u''
    return u'{0} | {1}{2} | ID {3}'.format(
        panel, circuit, suffix, system.Id.IntegerValue
    )


def select_electrical_systems():
    systems = collect_electrical_systems()
    systems.sort(key=lambda system: (
        get_panel_name(system).lower(),
        get_electrical_system_circuit_number(system).lower(),
        system.Id.IntegerValue,
    ))
    choices = [(circuit_choice_text(system), system) for system in systems]
    choice_map = dict(choices)
    selected = forms.SelectFromList.show(
        [label for label, system in choices],
        title=u'Выберите электрические цепи',
        button_name=u'Построить нумерацию',
        multiselect=True,
    )
    if not selected:
        return []
    return [choice_map[label] for label in selected]


def build_initial_elements(systems, target_boxes):
    element_map = {}

    for element in collect_route_elements():
        element_map[element.Id.IntegerValue] = element

    for box in target_boxes:
        add_element_and_family_tree(element_map, box)

    for system in systems:
        try:
            panel = system.BaseEquipment
        except Exception:
            panel = None
        if panel is not None:
            add_element_and_family_tree(element_map, panel)
        for device in get_system_elements(system):
            add_element_and_family_tree(element_map, device)

    return list(element_map.values())


def analyze_circuit(system, graph, element_map, target_boxes):
    report = {
        'system': system,
        'panel': get_panel_name(system),
        'circuit': get_electrical_system_circuit_number(system),
        'devices': get_system_elements(system),
        'reached_devices': [],
        'unreached_devices': [],
        'path_ids': set(),
        'boxes': {},
        'reason': u'',
    }

    try:
        panel = system.BaseEquipment
    except Exception:
        panel = None
    if panel is None:
        report['unreached_devices'] = list(report['devices'])
        report['reason'] = u'У цепи не назначен щит.'
        return report

    panel_ids = graph_ids_for_element(panel, graph)
    if not panel_ids:
        report['unreached_devices'] = list(report['devices'])
        report['reason'] = u'У щита не найден физический соединитель коробов.'
        return report

    distances, previous = dijkstra(graph, element_map, panel_ids)
    for device in report['devices']:
        endpoint_ids = graph_ids_for_element(device, graph)
        reached_ids = [element_id for element_id in endpoint_ids
                       if element_id in distances]
        if not reached_ids:
            report['unreached_devices'].append(device)
            continue

        end_id = min(reached_ids, key=lambda element_id: (
            distances[element_id], element_id
        ))
        report['reached_devices'].append(device)
        report['path_ids'].update(restore_path(previous, end_id))

    for box in target_boxes:
        box_nodes = box_graph_ids(box, graph)
        if not box_nodes.intersection(report['path_ids']):
            continue
        distance = box_distance(box, distances, graph)
        if distance is not None:
            report['boxes'][box.Id.IntegerValue] = distance

    if not report['devices']:
        report['reason'] = u'В ElectricalSystem отсутствуют электроприёмники.'
    elif not report['reached_devices']:
        report['reason'] = u'От щита не найден ни один физический путь к приборам.'
    elif report['unreached_devices']:
        report['reason'] = u'Трасса найдена частично.'
    else:
        report['reason'] = u'Физические пути ко всем приборам найдены.'
    return report


def build_circuit_numbering_plan(systems, graph, element_map, target_boxes):
    reports = [analyze_circuit(system, graph, element_map, target_boxes)
               for system in systems]

    memberships = defaultdict(set)
    for report in reports:
        system_id = report['system'].Id.IntegerValue
        for box_id in report['boxes']:
            memberships[box_id].add(system_id)

    conflicts = dict((box_id, system_ids)
                     for box_id, system_ids in memberships.items()
                     if len(system_ids) > 1)

    box_by_id = dict((box.Id.IntegerValue, box) for box in target_boxes)
    plan = []
    for report in reports:
        sortable = []
        for box_id, distance in report['boxes'].items():
            if box_id not in conflicts:
                sortable.append((box_by_id[box_id], distance))
        sortable.sort(key=stable_box_sort_key)

        for index, (box, distance) in enumerate(sortable, 1):
            plan.append({
                'box': box,
                'old_mark': get_current_mark(box),
                'new_mark': make_circuit_mark(
                    report['panel'], report['circuit'], index
                ),
                'panel': report['panel'],
                'circuit': report['circuit'],
                'distance': distance,
                'mode': MODE_CIRCUITS,
            })
    return plan, reports, conflicts


# ============ 5. РЕЗЕРВНЫЙ РЕЖИМ ОТ ВЫБРАННОЙ РК ============

def pick_start_box():
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            u'Кликните на стартовую распределительную коробку',
        )
    except Exception:
        return None
    element = doc.GetElement(reference.ElementId)
    if not is_target_box(element):
        forms.alert(
            u'Выбранный элемент не относится к настроенным семействам РК.',
            title=u'Неверный элемент',
            warn_icon=True,
        )
        return None
    return element


def build_branch_numbering_plan(start_box, graph, element_map, target_boxes):
    source_ids = box_graph_ids(start_box, graph)
    distances, previous = dijkstra(graph, element_map, source_ids)

    grouped = defaultdict(list)
    for box in target_boxes:
        distance = box_distance(box, distances, graph)
        if distance is None:
            continue
        circuit = get_circuit_number_from_element(box)
        grouped[circuit].append((box, distance))

    plan = []
    for circuit in sorted(grouped.keys(), key=lambda value: value.lower()):
        boxes = sorted(grouped[circuit], key=stable_box_sort_key)
        for index, (box, distance) in enumerate(boxes, 1):
            plan.append({
                'box': box,
                'old_mark': get_current_mark(box),
                'new_mark': make_branch_mark(circuit, index),
                'panel': u'—',
                'circuit': circuit,
                'distance': distance,
                'mode': MODE_BRANCH,
            })
    return plan


# ============ 6. ОТЧЁТ И ЗАПИСЬ ============

def markdown_cell(value):
    return text_value(value).replace(u'|', u'\\|').replace(u'\n', u' ')


def print_circuit_reports(reports, conflicts, target_boxes):
    output.print_md(u'## Анализ электрических цепей')
    report_by_id = dict((report['system'].Id.IntegerValue, report)
                        for report in reports)

    for report in reports:
        output.print_md(
            u'### {0} — {1}'.format(
                markdown_cell(report['panel']),
                markdown_cell(report['circuit']),
            )
        )
        print(u'Приборов в ElectricalSystem: {0}'.format(len(report['devices'])))
        print(u'Приборов с физическим путём: {0}'.format(
            len(report['reached_devices'])
        ))
        print(u'РК на найденных путях: {0}'.format(len(report['boxes'])))
        print(u'Результат: {0}'.format(report['reason']))
        if report['unreached_devices']:
            print(u'Приборы без физического пути:')
            for device in report['unreached_devices']:
                print(u'  - {0}, ID {1}'.format(
                    element_display_name(device), device.Id.IntegerValue
                ))

    if conflicts:
        boxes_by_id = dict((box.Id.IntegerValue, box) for box in target_boxes)
        output.print_md(u'## Конфликтные РК — автоматически пропущены')
        for box_id in sorted(conflicts):
            labels = []
            for system_id in sorted(conflicts[box_id]):
                report = report_by_id[system_id]
                labels.append(u'{0}/{1}'.format(
                    report['panel'], report['circuit']
                ))
            box = boxes_by_id[box_id]
            print(u'{0}, ID {1}: {2}'.format(
                get_family_name(box), box_id, u', '.join(labels)
            ))


def print_plan(plan):
    output.print_md(u'## Предварительная нумерация')
    output.print_md(
        u'| № | РК | Щит | Цепь | Расстояние, м | Старая марка | Новая марка |\n'
        u'|---:|---|---|---|---:|---|---|'
    )
    for row_number, item in enumerate(plan, 1):
        box = item['box']
        box_link = output.linkify(box.Id)
        print(u'| {0} | {1} | {2} | {3} | {4:.2f} | {5} | {6} |'.format(
            row_number,
            box_link,
            markdown_cell(item['panel']),
            markdown_cell(item['circuit']),
            item['distance'] * 0.3048,
            markdown_cell(item['old_mark']) if item['old_mark'] else u'—',
            markdown_cell(item['new_mark']),
        ))


def validate_plan(plan):
    errors = []
    seen_box_ids = set()
    new_marks = defaultdict(list)

    for item in plan:
        box = item['box']
        box_id = box.Id.IntegerValue
        if box_id in seen_box_ids:
            errors.append(u'РК ID {0} попала в план больше одного раза.'.format(box_id))
        seen_box_ids.add(box_id)

        parameter = get_mark_parameter(box, writable=True)
        if parameter is None:
            errors.append(
                u'РК ID {0}: параметр «Марка» отсутствует или только для чтения.'.format(
                    box_id
                )
            )
        new_marks[item['new_mark']].append(box_id)

    for mark, box_ids in new_marks.items():
        if len(box_ids) > 1:
            errors.append(u'Марка «{0}» назначена нескольким РК: {1}.'.format(
                mark, u', '.join(unicode(box_id) for box_id in box_ids)
            ))
    return errors


def apply_plan(plan):
    with revit.Transaction(u'Нумерация распределительных коробок'):
        for item in plan:
            set_mark(item['box'], item['new_mark'])


def confirm_and_apply(plan):
    if not plan:
        forms.alert(
            u'Распределительные коробки для нумерации не найдены.\n\n'
            u'Проверьте физические соединители либо используйте резервный режим.',
            title=u'Нумерация РК',
            warn_icon=True,
        )
        return

    errors = validate_plan(plan)
    if errors:
        output.print_md(u'## Ошибки предварительной проверки')
        for error in errors:
            print(u'- {0}'.format(error))
        forms.alert(
            u'Нумерация не выполнена: предварительная проверка обнаружила '
            u'{0} ошибок. Подробности находятся в окне pyRevit.'.format(len(errors)),
            title=u'Нумерация РК',
            warn_icon=True,
        )
        return

    print_plan(plan)
    changed_count = sum(1 for item in plan
                        if item['old_mark'] != item['new_mark'])
    unchanged_count = len(plan) - changed_count
    confirmed = forms.alert(
        u'Будет обработано РК: {0}.\n'
        u'Изменится марок: {1}.\n'
        u'Уже имеют правильную марку: {2}.\n\n'
        u'Предварительная таблица находится в окне pyRevit.\n'
        u'Записать марки?'.format(len(plan), changed_count, unchanged_count),
        title=u'Подтверждение нумерации',
        yes=True,
        no=True,
    )
    if not confirmed:
        print(u'Запись отменена пользователем. Модель не изменялась.')
        return

    try:
        apply_plan(plan)
    except Exception as error:
        forms.alert(
            u'Не удалось записать марки. Транзакция отменена полностью.\n\n{0}'.format(
                error
            ),
            title=u'Ошибка нумерации',
            warn_icon=True,
        )
        return

    output.print_md(u'## Готово')
    print(u'Обработано РК: {0}'.format(len(plan)))
    print(u'Изменено марок: {0}'.format(changed_count))
    print(u'Без изменения: {0}'.format(unchanged_count))


# ============ 7. ЗАПУСК ============

def run_circuit_mode():
    systems = select_electrical_systems()
    if not systems:
        return

    target_boxes = collect_target_boxes()
    if not target_boxes:
        forms.alert(
            u'В проекте не найдены настроенные семейства РК.',
            title=u'Нумерация РК',
            warn_icon=True,
        )
        return

    output.print_md(u'# Нумерация РК по электрическим цепям')
    print(u'Выбрано цепей: {0}'.format(len(systems)))
    print_target_box_statistics(target_boxes)
    print(u'Собирается физическая сеть всего проекта...')

    initial_elements = build_initial_elements(systems, target_boxes)
    graph, element_map = build_connection_graph(initial_elements)
    print(u'Элементов в физическом графе: {0}'.format(len(element_map)))

    plan, reports, conflicts = build_circuit_numbering_plan(
        systems, graph, element_map, target_boxes
    )
    print_circuit_reports(reports, conflicts, target_boxes)
    confirm_and_apply(plan)


def run_branch_mode():
    start_box = pick_start_box()
    if start_box is None:
        return

    target_boxes = collect_target_boxes()
    initial_elements = collect_route_elements()
    element_map = dict((element.Id.IntegerValue, element)
                       for element in initial_elements)
    for box in target_boxes:
        add_element_and_family_tree(element_map, box)

    output.print_md(u'# Нумерация РК от выбранной коробки')
    print(u'Стартовая РК: ID {0}'.format(start_box.Id.IntegerValue))
    print_target_box_statistics(target_boxes)
    print(u'Собирается физическая сеть всего проекта...')
    graph, graph_elements = build_connection_graph(list(element_map.values()))
    print(u'Элементов в физическом графе: {0}'.format(len(graph_elements)))

    plan = build_branch_numbering_plan(
        start_box, graph, graph_elements, target_boxes
    )
    confirm_and_apply(plan)


def main():
    mode = forms.CommandSwitchWindow.show(
        [MODE_CIRCUITS, MODE_BRANCH],
        title=u'Нумерация РК — версия {0}'.format(__version__),
        message=(
            u'Основной режим использует ElectricalSystem. '
            u'Резервный режим работает от выбранной РК.'
        ),
        recognize_access_key=False,
    )
    if mode == MODE_CIRCUITS:
        run_circuit_mode()
    elif mode == MODE_BRANCH:
        run_branch_mode()


if __name__ == '__main__':
    main()
