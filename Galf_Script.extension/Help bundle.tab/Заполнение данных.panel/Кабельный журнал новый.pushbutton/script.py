# -*- coding: utf-8 -*-
"""Cable journal for pyRevit / Revit 2023 (IronPython 2.7).

ElectricalSystem identifies a panel and its loads; physical connectors
identify candidate paths. The journal never assumes that a shortest path is
the actual cable route when alternatives exist. All model changes require a
successful preflight and explicit preview/confirmation.

v0.2.1: collect_network() now pre-filters the element collector to
CONNECTOR_CATS via ElementMulticategoryFilter instead of visiting every
FamilyInstance in the project. Previously every door, furniture item,
generic model, etc. was probed for a ConnectorManager, and each failed
probe crossed the .NET/IronPython interop boundary; on large models this
made the command appear to hang or crash. See CONNECTOR_CATS for the
category list — extend it if a category with connector-bearing families
(e.g. custom РК boxes) is missing.
"""

__title__ = u'Кабельный\nжурнал v2'
__doc__ = u'Просмотр, проверка и заполнение существующей спецификации «Кабельный журнал».'
__version__ = '0.2.1'

import math
import os
import re
import sys
import tempfile
import traceback
from collections import defaultdict

from pyrevit import DB, forms, revit, script
from System.Collections.Generic import List
import xlsxwriter

sys.path.insert(0, os.path.dirname(__file__))
from cable_journal_core import build_journal, natural_key
from schedule_sync import build_schedule_plan, apply_schedule_plan


FT_TO_M = 0.3048
CONDUCTOR_PARAM = u'Выбор проводника'
METHOD_PARAM = u'Выбор короба'
BREAK_PARAM = u'Разрыв кабеля'
BOX_FAMILIES = (
    u'SE_Коробка_ОткрытаяХ', u'SE_Коробка_ОткрытаяТ',
    u'SE_Коробка_ОткрытаяЭ', u'Galf_Коробка распределительная v.1.0.1',
)
ROUTE_CATS = set(int(category) for category in (
    DB.BuiltInCategory.OST_Conduit, DB.BuiltInCategory.OST_ConduitFitting,
    DB.BuiltInCategory.OST_CableTray, DB.BuiltInCategory.OST_CableTrayFitting,
))
LINE_CATS = set(int(category) for category in (
    DB.BuiltInCategory.OST_Conduit, DB.BuiltInCategory.OST_CableTray,
))

# Categories whose instances can plausibly own a DomainCableTrayConduit
# connector: the route/fitting categories themselves, plus every terminal
# device category electrical circuits normally connect to. Restricting the
# element collector to this set (below, in collect_network) avoids visiting
# every FamilyInstance in the project — doors, furniture, generic models,
# rebar, etc. — each of which would otherwise trigger a failed
# .ConnectorManager / .MEPModel access. That access fails through the
# .NET/IronPython interop boundary, which is far more expensive than an
# ordinary Python exception; on a large model, doing it for thousands of
# unrelated elements is what makes the command appear to hang or crash.
# If your РК (junction box) families sit in a category not listed here
# (e.g. a custom Generic Models family), add that category as well, or
# those boxes will silently stop appearing in the network.
CONNECTOR_CATS = ROUTE_CATS | set(int(category) for category in (
    DB.BuiltInCategory.OST_ElectricalFixtures,
    DB.BuiltInCategory.OST_ElectricalEquipment,
    DB.BuiltInCategory.OST_LightingFixtures,
    DB.BuiltInCategory.OST_LightingDevices,
    DB.BuiltInCategory.OST_DataDevices,
    DB.BuiltInCategory.OST_FireAlarmDevices,
    DB.BuiltInCategory.OST_CommunicationDevices,
    DB.BuiltInCategory.OST_SecurityDevices,
    DB.BuiltInCategory.OST_NurseCallDevices,
    DB.BuiltInCategory.OST_TelephoneDevices,
    DB.BuiltInCategory.OST_GenericModel,
))

RK_STRICT = u'Неизвестная РК — в проверку (рекомендуется)'
RK_TRANSIT = u'Неизвестная РК — кабель проходит транзитом'
RK_BREAK = u'Неизвестная РК — кабель заканчивается в РК'

CORE_MESSAGES = {
    'invalid_node_kind': u'Неизвестный тип узла физической сети.',
    'invalid_length': u'Некорректная геометрическая длина элемента.',
    'invalid_edge': u'Некорректная физическая связь.',
    'invalid_panel': u'Щит цепи отсутствует в физической сети.',
    'invalid_destination': u'Приёмник цепи отсутствует или не является прибором.',
    'no_destinations': u'У цепи нет оконечных приборов.',
    'missing_path': u'Не найден непрерывный физический путь от щита до прибора.',
    'ambiguous_path': u'Есть несколько физических путей; маршрут нужно указать явно.',
    'unknown_box_break': u'Неизвестно, заканчивается ли кабель в этой РК.',
    'shared_branch_without_break': u'Приборы делят общий участок без явной точки разделки; количество кабелей неоднозначно.',
    'duplicate_system_id': u'Повторяется ID электрической цепи.',
}

# Project-specific allowances. Zero means that only measured straight geometry
# is included. No percentage or arbitrary fitting length is silently added.
RESERVE_PER_CABLE_END_M = 0.0
RESERVE_PERCENT = 0.0


def eid(element):
    return element.Id.IntegerValue


def as_text(value):
    return u'' if value is None else unicode(value).strip()


def meaningful(value):
    value = as_text(value)
    return value if value.lower() not in (u'', u'?', u'-', u'—', u'не указано', u'нет') else u''


def add_issue(issues, code, message, system_id=u'', element_ids=None):
    ids = sorted(set(element_ids or []))
    issues.append({'code': code, 'message': as_text(message),
                   'system_id': system_id, 'node_ids': ids})


def unique_issues(issues):
    result, seen = [], set()
    for issue in issues:
        key = (as_text(issue.get('code')), as_text(issue.get('system_id')),
               tuple(sorted(as_text(i) for i in issue.get('node_ids', []))),
               as_text(issue.get('message')))
        if key not in seen:
            result.append(issue)
            seen.add(key)
    return result


def cat_id(element):
    return element.Category.Id.IntegerValue if element.Category else None


def family_name(element):
    try:
        return as_text(element.Symbol.Family.Name)
    except Exception:
        return u''


def parameter_text(element, name, issues=None):
    if element is None:
        return u''
    parameters = list(element.GetParameters(name))
    if len(parameters) > 1:
        if issues is not None:
            add_issue(issues, u'Дубли параметра',
                      u'Несколько параметров «{0}»; значение не используется.'.format(name),
                      element_ids=[eid(element)])
        return u''
    if not parameters or not parameters[0].HasValue:
        return u''
    parameter = parameters[0]
    if parameter.StorageType == DB.StorageType.String:
        return as_text(parameter.AsString())
    try:
        return as_text(parameter.AsValueString())
    except Exception:
        return u''


def mark(element):
    try:
        parameter = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if parameter is not None and parameter.HasValue:
            value = as_text(parameter.AsString())
            if value:
                return value
    except Exception:
        pass
    return parameter_text(element, u'Марка')


def label(element):
    name = mark(element)
    if name:
        return name
    try:
        name = family_name(element) or as_text(element.Name)
    except Exception:
        name = family_name(element)
    return u'{0} [ID {1}]'.format(name or u'Узел', eid(element))


def canonical(element):
    """Map nested connectors to their root family, without joining geometry."""
    seen = set()
    while isinstance(element, DB.FamilyInstance) and cat_id(element) not in ROUTE_CATS:
        if eid(element) in seen:
            raise ValueError(u'Циклическая вложенность семейства ID {0}.'.format(eid(element)))
        seen.add(eid(element))
        parent = element.SuperComponent
        if parent is None or cat_id(parent) in ROUTE_CATS:
            break
        element = parent
    return element


def connectors(element):
    manager = None
    try:
        manager = element.ConnectorManager
    except Exception:
        pass
    if manager is None:
        try:
            manager = element.MEPModel.ConnectorManager
        except Exception:
            pass
    if manager is None:
        return []
    result = []
    for connector in manager.Connectors:
        if connector.ConnectorType in (DB.ConnectorType.Logical, DB.ConnectorType.Reference):
            continue
        if connector.Domain == DB.Domain.DomainCableTrayConduit:
            result.append(connector)
    return result


def is_box(element):
    name = family_name(element).lower()
    value = mark(element).upper()
    return any(fragment.lower() in name for fragment in BOX_FAMILIES) or value.startswith((u'РК', u'RK'))


def box_break_value(element, unknown_mode, issues):
    parameters = list(element.GetParameters(BREAK_PARAM))
    if len(parameters) > 1:
        add_issue(issues, u'Дубли параметра',
                  u'У РК несколько параметров «{0}».'.format(BREAK_PARAM),
                  element_ids=[eid(element)])
        return None
    if parameters and parameters[0].HasValue:
        p = parameters[0]
        if p.StorageType == DB.StorageType.Integer:
            number = p.AsInteger()
            if number in (0, 1):
                return bool(number)
        value = parameter_text(element, BREAK_PARAM).lower()
        if value in (u'да', u'истина', u'true', u'1', u'разрыв', u'разделка'):
            return True
        if value in (u'нет', u'ложь', u'false', u'0', u'транзит'):
            return False
        add_issue(issues, u'Значение РК',
                  u'Не распознано «{0}»: {1}.'.format(BREAK_PARAM, value),
                  element_ids=[eid(element)])
        return None
    if unknown_mode == RK_TRANSIT:
        return False
    if unknown_mode == RK_BREAK:
        return True
    return None


def route_length_m(element, issues):
    try:
        length = float(element.Location.Curve.Length) * FT_TO_M
        if math.isnan(length) or math.isinf(length) or length <= 0:
            raise ValueError(u'Длина равна нулю или нечисловая.')
        return length
    except Exception as error:
        add_issue(issues, u'Геометрия',
                  u'Нельзя определить длину элемента: {0}'.format(error),
                  element_ids=[eid(element)])
        return 0.0


def collect_electrical_systems(doc, issues):
    result = []
    collector = DB.FilteredElementCollector(doc).OfClass(DB.Electrical.ElectricalSystem)
    for system in collector:
        try:
            number = as_text(system.CircuitNumber)
            panel = system.BaseEquipment
            members = list(system.Elements)
        except Exception as error:
            add_issue(issues, u'Электрическая цепь',
                      u'Нельзя прочитать цепь: {0}'.format(error), eid(system))
            continue
        if not number:
            add_issue(issues, u'Номер цепи', u'Не указан номер электрической цепи.', eid(system))
            continue
        if panel is None:
            add_issue(issues, u'Щит', u'У цепи не назначен щит.', eid(system))
            continue
        result.append({'element': system, 'id': eid(system), 'number': number,
                       'panel_element': canonical(panel), 'members': members})
    return result


def collect_network(doc, electrical_systems, unknown_mode, issues):
    panel_ids = set(eid(item['panel_element']) for item in electrical_systems)
    roots = {}
    actual = {}
    category_filter = DB.ElementMulticategoryFilter(
        List[DB.BuiltInCategory]([DB.BuiltInCategory(c) for c in CONNECTOR_CATS]))
    all_elements = (DB.FilteredElementCollector(doc)
                    .WhereElementIsNotElementType()
                    .WherePasses(category_filter))
    for element in all_elements:
        category = cat_id(element)
        if category not in ROUTE_CATS and not isinstance(element, DB.FamilyInstance):
            # Kept as a safety net: a category in CONNECTOR_CATS can still
            # contain non-FamilyInstance elements (e.g. in-place families),
            # which never carry a usable ConnectorManager here.
            continue
        try:
            route_conns = connectors(element)
            if not route_conns and category not in ROUTE_CATS:
                continue
            root = canonical(element)
            root_id = eid(root)
            roots[root_id] = root
            actual[eid(element)] = (root_id, route_conns)
        except Exception as error:
            add_issue(issues, u'Соединители',
                      u'Не удалось прочитать соединители: {0}'.format(error),
                      element_ids=[eid(element)])

    nodes = {}
    for node_id, element in roots.items():
        category = cat_id(element)
        if category in LINE_CATS:
            kind = 'route'
        elif category in ROUTE_CATS:
            kind = 'fitting'
        elif node_id in panel_ids:
            kind = 'panel'
        elif is_box(element):
            kind = 'box'
        else:
            kind = 'device'
        nodes[node_id] = {
            'kind': kind, 'label': label(element),
            'is_conduit': category == int(DB.BuiltInCategory.OST_Conduit),
            'length_m': route_length_m(element, issues) if kind == 'route' else 0.0,
            'method': parameter_text(element, METHOD_PARAM, issues) if kind == 'route' else u'',
            'conductor': parameter_text(element, CONDUCTOR_PARAM, issues)
                         if kind == 'route' and category == int(DB.BuiltInCategory.OST_Conduit)
                         else u'',
            'break_cable': box_break_value(element, unknown_mode, issues) if kind == 'box' else False,
        }

    # Names are display-only. Duplicate marks are disambiguated by ElementId.
    labels = defaultdict(list)
    for node_id, node in nodes.items():
        if node['kind'] not in ('route', 'fitting'):
            labels[node['label']].append(node_id)
    for value, ids in labels.items():
        if len(ids) > 1:
            for node_id in ids:
                nodes[node_id]['label'] = u'{0} [ID {1}]'.format(value, node_id)

    pairs = defaultdict(set)
    for actual_id, (root_id, route_conns) in actual.items():
        for connector in route_conns:
            try:
                if not connector.IsConnected:
                    continue
                for reference in connector.AllRefs:
                    if reference.ConnectorType in (DB.ConnectorType.Logical, DB.ConnectorType.Reference):
                        continue
                    if reference.Domain != DB.Domain.DomainCableTrayConduit:
                        continue
                    if not connector.IsConnectedTo(reference):
                        continue
                    owner = reference.Owner
                    if owner is None or eid(owner) not in actual:
                        continue
                    other_id = actual[eid(owner)][0]
                    if other_id == root_id:
                        continue
                    pair = tuple(sorted((root_id, other_id)))
                    ports = tuple(sorted(((actual_id, connector.Id),
                                          (eid(owner), reference.Id))))
                    pairs[pair].add(ports)
            except Exception as error:
                add_issue(issues, u'Физическая связь',
                          u'Ошибка чтения физической связи: {0}'.format(error),
                          element_ids=[root_id])
    edges = []
    for pair, links in pairs.items():
        if len(links) == 1:
            edges.append(pair)
        else:
            # Collapsing parallel physical connections into one edge would
            # conceal an alternative route. Exclude the edge instead.
            add_issue(issues, u'Параллельная связь',
                      u'Между элементами несколько физических соединений; путь неоднозначен.',
                      element_ids=list(pair))
    return nodes, edges, roots


def normalize_systems(raw_systems, nodes, issues):
    result = []
    for item in raw_systems:
        system_id = item['id']
        panel_id = eid(item['panel_element'])
        if panel_id not in nodes:
            add_issue(issues, u'Подключение щита',
                      u'У щита не найден физический соединитель коробов/труб.',
                      system_id, [panel_id])
            continue
        destinations = []
        for member in item['members']:
            try:
                target = canonical(member)
                target_id = eid(target)
            except Exception as error:
                add_issue(issues, u'Приёмник',
                          u'Не удалось прочитать прибор цепи: {0}'.format(error),
                          system_id, [eid(member)])
                continue
            if target_id == panel_id:
                continue
            if target_id not in nodes:
                add_issue(issues, u'Подключение прибора',
                          u'У прибора нет физического соединителя коробов/труб.',
                          system_id, [target_id])
                continue
            if nodes[target_id]['kind'] in ('route', 'fitting'):
                add_issue(issues, u'Приёмник',
                          u'Элемент электрической системы не является оконечным прибором.',
                          system_id, [target_id])
                continue
            if (nodes[target_id]['kind'] == 'box' and
                    nodes[target_id]['break_cable'] is False):
                # A transit box may be an ElectricalSystem member, but it is
                # a waypoint, not a cable termination.
                continue
            destinations.append(target_id)
        destinations = sorted(set(destinations))
        if not destinations:
            add_issue(issues, u'Приёмники', u'Нет приборов с физическими подключениями.', system_id)
            continue
        result.append({'id': system_id, 'number': item['number'],
                       'panel': panel_id, 'destinations': destinations})
    return result


def method_summary(node_ids, nodes):
    totals = defaultdict(float)
    missing = []
    for node_id in node_ids:
        node = nodes[node_id]
        if node['kind'] != 'route':
            continue
        value = meaningful(node['method'])
        if value:
            totals[value] += node['length_m']
        else:
            missing.append(node_id)
    parts = [u'{0} — {1} м'.format(
                 value, u'{0:.2f}'.format(totals[value]).replace(u'.', u','))
             for value in sorted(totals, key=natural_key)]
    return u'; '.join(parts), missing


def conductor_for_run(run, nodes, issues):
    """Only straight cable boxes/pipes (Conduit) define the cable mark.

    A value on ElectricalSystem, distribution box, cable tray or fitting is
    deliberately ignored. One logical cable must not silently merge different
    marks from its constituent straight Conduit elements.
    """
    values = defaultdict(list)
    conduit_ids = []
    for node_id in run['route_node_ids']:
        node = nodes[node_id]
        if node.get('kind') != 'route' or not node.get('is_conduit'):
            continue
        conduit_ids.append(node_id)
        value = meaningful(node.get('conductor'))
        if value:
            values[value].append(node_id)
    if not conduit_ids:
        add_issue(issues, u'Марка кабеля',
                  u'В кабельном пути нет прямого элемента кабельного короба/трубы; '
                  u'марку с лотка или РК брать нельзя.', run['system_id'], run['route_node_ids'])
        return u''
    missing = [node_id for node_id in conduit_ids
               if not meaningful(nodes[node_id].get('conductor'))]
    if missing:
        add_issue(issues, u'Марка кабеля',
                  u'Не заполнен «{0}» на кабельном коробе/трубе.'.format(CONDUCTOR_PARAM),
                  run['system_id'], missing)
    if len(values) > 1:
        add_issue(issues, u'Марка кабеля',
                  u'На одном кабельном пути обнаружены разные значения «{0}»: {1}.'.format(
                      CONDUCTOR_PARAM, u'; '.join(sorted(values, key=natural_key))),
                  run['system_id'], conduit_ids)
    return next(iter(values)) if len(values) == 1 and not missing else u''


def create_display_rows(result, systems, nodes, issues):
    system_by_id = dict((system['id'], system) for system in systems)
    bad_geometry = set()
    for issue in issues:
        if issue.get('code') == u'Геометрия':
            bad_geometry.update(issue.get('node_ids', []))
    valid_runs, valid_segments = [], []
    segments_by_run = defaultdict(list)
    for segment in result['segments']:
        segments_by_run[segment['run_id']].append(segment)
    for run in result['runs']:
        run_id = run['id']
        system = system_by_id[run['system_id']]
        display_id = u'К-{0}-{1:02d}'.format(system['number'], run['route_index'])
        run_segments = sorted(segments_by_run[run_id], key=lambda row: row['sequence'])
        invalid = False
        conductor = conductor_for_run(run, nodes, issues)
        if not conductor:
            invalid = True
        if not run_segments:
            add_issue(issues, u'Участки', u'У кабеля нет физических участков.', system['id'])
            continue
        display_segments = []
        for segment in run_segments:
            route_ids = segment['route_node_ids']
            summary, missing = method_summary(route_ids, nodes)
            if missing:
                add_issue(issues, u'Способ прокладки',
                          u'Не заполнен «{0}» на участке.'.format(METHOD_PARAM),
                          system['id'], missing)
                invalid = True
            if segment['length_m'] <= 0:
                add_issue(issues, u'Длина участка',
                          u'Участок без измеримой длины.', system['id'], route_ids)
                invalid = True
            if bad_geometry.intersection(route_ids):
                add_issue(issues, u'Длина участка',
                          u'Часть геометрии участка не прочитана; итоговая длина неполна.',
                          system['id'], bad_geometry.intersection(route_ids))
                invalid = True
            # Workbook numbering follows the route, independently of stale
            # values on physical elements and without changing the model.
            number = u'ТР{0}-{1:03d}'.format(system['number'], segment['route_index'])
            display_segments.append({
                'run_id': run_id, 'display_id': display_id, 'system_id': system['id'],
                'panel': nodes[system['panel']]['label'], 'circuit': system['number'],
                'sequence': segment['sequence'], 'route_index': segment['route_index'],
                'number': number,
                'source': nodes[segment['from_id']]['label'],
                'dest': nodes[segment['to_id']]['label'],
                'length_m': segment['length_m'], 'method': summary,
                'conductor': conductor,
                'element_ids': route_ids,
            })
        if invalid:
            continue
        route_labels = [display_segments[0]['source']]
        route_labels.extend(row['dest'] for row in display_segments)
        length_m = sum(row['length_m'] for row in display_segments)
        reserve_m = 2 * RESERVE_PER_CABLE_END_M + length_m * RESERVE_PERCENT / 100.0
        summary, _ = method_summary(
            [node_id for segment in run_segments for node_id in segment['route_node_ids']], nodes)
        valid_runs.append({
            'id': run_id, 'system_id': system['id'],
            'display_id': display_id, 'route_index': run['route_index'],
            'panel': nodes[system['panel']]['label'], 'circuit': system['number'],
            'source': route_labels[0], 'dest': route_labels[-1],
            'conductor': conductor, 'length_m': length_m,
            'reserve_m': reserve_m, 'total_m': length_m + reserve_m,
            'method': summary, 'route': u' → '.join(route_labels),
        })
        valid_segments.extend(display_segments)
    valid_runs.sort(key=lambda row: (
        natural_key(row['panel']), natural_key(row['circuit']),
        row['route_index'], as_text(row['id'])))
    run_order = dict((row['id'], index) for index, row in enumerate(valid_runs))
    valid_segments.sort(key=lambda row: (run_order[row['run_id']], row['sequence']))
    return valid_runs, valid_segments


def unique_destination(path):
    """Never overwrite an existing workbook (including one open in Excel)."""
    base, ext = os.path.splitext(path)
    if ext.lower() != '.xlsx':
        ext = '.xlsx'
    candidate = base + ext
    index = 2
    while os.path.exists(candidate):
        candidate = u'{0} ({1}){2}'.format(base, index, ext)
        index += 1
    return candidate


def write_book(path, doc_name, rk_mode, runs, segments, issues):
    parent = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(prefix='cable_journal_', suffix='.xlsx', dir=parent)
    os.close(fd)
    try:
        book = xlsxwriter.Workbook(temp_path)
        title_fmt = book.add_format({'bold': True, 'font_size': 16, 'font_color': '#17365D'})
        note_fmt = book.add_format({'font_color': '#40536B'})
        alert_fmt = book.add_format({'bold': True, 'font_color': '#9C0006',
                                     'bg_color': '#FFC7CE'})
        header_fmt = book.add_format({'bold': True, 'font_color': '#FFFFFF',
                                      'bg_color': '#234B70', 'border': 1,
                                      'text_wrap': True, 'valign': 'vcenter'})
        text_fmt = book.add_format({'valign': 'top', 'text_wrap': True})
        number_fmt = book.add_format({'num_format': '0.00', 'valign': 'top'})
        step_fmt = book.add_format({'num_format': '0', 'valign': 'top'})

        def set_header(ws, title, headers, widths):
            ws.hide_gridlines(2)
            ws.merge_range(0, 0, 0, len(headers) - 1, title, title_fmt)
            ws.merge_range(1, 0, 1, len(headers) - 1,
                           u'Модель: {0}. РК без параметра «{1}»: {2}.'.format(
                               doc_name, BREAK_PARAM, rk_mode), note_fmt)
            ws.merge_range(2, 0, 2, len(headers) - 1,
                           u'Статус: {0}; замечаний: {1}. Длины — по геометрии прямых элементов; '
                           u'запас на конец {2:g} м, процент {3:g}%, фитинги не добавлены.'.format(
                               u'черновик' if issues else u'готово', len(issues),
                               RESERVE_PER_CABLE_END_M, RESERVE_PERCENT),
                           alert_fmt if issues else note_fmt)
            for col, value in enumerate(headers):
                ws.write_string(4, col, value, header_fmt)
                ws.set_column(col, col, widths[col])
            ws.set_row(4, 34)
            ws.freeze_panes(5, 0)
            ws.set_landscape()
            ws.fit_to_pages(1, 0)
            ws.repeat_rows(4)

        ws = book.add_worksheet(u'Кабельный журнал')
        headers = [u'Щит', u'Цепь', u'№ кабеля', u'Начало', u'Конец',
                   u'Марка кабеля', u'Длина трассы, м', u'Запас, м',
                   u'Итого, м', u'Способ прокладки', u'Полный путь']
        set_header(ws, u'Кабельный журнал', headers,
                   [18, 15, 20, 23, 23, 23, 18, 14, 14, 38, 58])
        for offset, row in enumerate(runs):
            index = 5 + offset
            values = [row['panel'], row['circuit'], row['display_id'], row['source'],
                      row['dest'], row['conductor']]
            for col, value in enumerate(values):
                ws.write_string(index, col, as_text(value), text_fmt)
            for col, key in ((6, 'length_m'), (7, 'reserve_m'), (8, 'total_m')):
                ws.write_number(index, col, row[key], number_fmt)
            ws.write_string(index, 9, row['method'], text_fmt)
            ws.write_string(index, 10, row['route'], text_fmt)
        ws.autofilter(4, 0, max(5, 4 + len(runs)), len(headers) - 1)
        ws.print_area(0, 0, max(5, 4 + len(runs)), len(headers) - 1)

        ws = book.add_worksheet(u'Участки')
        headers = [u'Щит', u'Цепь', u'№ кабеля', u'Порядок', u'№ участка',
                   u'Начало', u'Конец', u'Длина, м', u'Способ прокладки',
                   u'ID элементов Revit']
        set_header(ws, u'Участки кабельных путей', headers,
                   [18, 15, 20, 11, 20, 25, 25, 15, 42, 38])
        for offset, row in enumerate(segments):
            index = 5 + offset
            for col, key in ((0, 'panel'), (1, 'circuit'), (2, 'display_id'),
                             (4, 'number'), (5, 'source'), (6, 'dest'), (8, 'method')):
                ws.write_string(index, col, as_text(row[key]), text_fmt)
            ws.write_number(index, 3, row['sequence'], step_fmt)
            ws.write_number(index, 7, row['length_m'], number_fmt)
            ws.write_string(index, 9, u', '.join(unicode(i) for i in row['element_ids']), text_fmt)
        ws.autofilter(4, 0, max(5, 4 + len(segments)), len(headers) - 1)
        ws.print_area(0, 0, max(5, 4 + len(segments)), len(headers) - 1)

        ws = book.add_worksheet(u'Проверка')
        headers = [u'Код', u'Цепь ID', u'Элементы ID', u'Что проверить']
        set_header(ws, u'Проверка модели', headers, [24, 15, 42, 90])
        for offset, issue in enumerate(issues):
            index = 5 + offset
            values = [issue.get('code', u''), issue.get('system_id', u''),
                      u', '.join(unicode(i) for i in issue.get('node_ids', [])),
                      issue.get('message', u'')]
            for col, value in enumerate(values):
                ws.write_string(index, col, as_text(value), text_fmt)
        ws.autofilter(4, 0, max(5, 4 + len(issues)), len(headers) - 1)
        ws.print_area(0, 0, max(5, 4 + len(issues)), len(headers) - 1)
        book.close()
        os.rename(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def preview_parameter_value(operation, value):
    if operation['role'] != 'total' or value in (None, u''):
        return as_text(value)
    parameter = operation['parameter']
    try:
        if (parameter.StorageType == DB.StorageType.Double and
                parameter.Definition.GetDataType() == DB.SpecTypeId.Length):
            metres = float(value) * FT_TO_M
        else:
            metres = float(value) / 1000.0
        return u'{0:.3f} м'.format(metres).replace(u'.', u',')
    except Exception:
        return as_text(value)


def preview_schedule(output, plan):
    """Show every proposed row and parameter change before confirmation."""
    output.print_md(u'## Существующая спецификация «Кабельный журнал»')
    if plan['preview']:
        output.print_table(
            table_data=[[row['circuit'], row['number'], row['source'], row['dest'],
                         row['conductor'], u'{0:.2f}'.format(row['length_m']),
                         row['method'], row['carrier_id']]
                        for row in plan['preview']],
            columns=[u'Группа', u'№ участка', u'Начало', u'Конец',
                     u'Проводник', u'Длина, м', u'Способ прокладки', u'ID носителя'],
            title=u'Строки после записи — в порядке логического пути')
    if plan['schedule_changes']:
        output.print_md(u'### Изменения формы спецификации')
        for change in plan['schedule_changes']:
            print(change)
    if plan['operations']:
        output.print_table(
            table_data=[[operation['element_id'], operation['parameter_name'],
                         preview_parameter_value(operation, operation['old']),
                         preview_parameter_value(operation, operation['new'])]
                        for operation in plan['operations']],
            columns=[u'ID элемента', u'Параметр', u'Сейчас', u'Будет'],
            title=u'Все записи в параметры')
    if plan.get('stale_preview'):
        output.print_table(
            table_data=[[row['element_id'], row['old_number'], row['new_number']]
                        for row in plan['stale_preview']],
            columns=[u'ID элемента', u'Старый номер', u'Новый номер'],
            title=u'Очистка старых строк текущей спецификации')
    if plan['issues']:
        output.print_md(u'### Запись в модель запрещена')
        for problem in plan['issues']:
            print(problem)


def optional_excel(doc, rk_mode, runs, segments):
    """A separate optional copy, never an intermediate upload to Revit."""
    if not forms.alert(u'Спецификация Revit обновлена. Сохранить также копию в Excel?',
                       yes=True, no=True, title=u'Кабельный журнал'):
        return
    model_name = os.path.splitext(as_text(doc.Title))[0] or u'Проект'
    model_name = re.sub(u'[\\/:*?"<>|]', u'_', model_name)
    default_name = u'Кабельный журнал_{0}.xlsx'.format(model_name)
    chosen = forms.save_file(file_ext='xlsx', default_name=default_name,
                             files_filter='Excel Workbook (*.xlsx)|*.xlsx',
                             title=u'Сохранить копию кабельного журнала')
    if not chosen:
        return
    path = unique_destination(chosen)
    write_book(path, model_name, rk_mode, runs, segments, [])
    print(u'Необязательная копия Excel: {0}'.format(path))
    forms.alert(u'Копия Excel сохранена:\n{0}'.format(path), title=u'Кабельный журнал')


def main():
    doc = revit.doc
    output = script.get_output()
    output.set_title(u'Кабельный журнал {0}'.format(__version__))
    if doc.IsFamilyDocument:
        forms.alert(u'Откройте проект Revit, а не семейство.')
        return
    rk_mode = forms.CommandSwitchWindow.show(
        [RK_STRICT, RK_TRANSIT, RK_BREAK],
        title=u'Как трактовать РК без параметра «{0}»?'.format(BREAK_PARAM),
        message=u'Параметр РК со значением Да/Нет имеет приоритет над этим выбором.',
        recognize_access_key=False)
    if not rk_mode:
        return

    issues = []
    raw_systems = collect_electrical_systems(doc, issues)
    nodes, edges, roots = collect_network(doc, raw_systems, rk_mode, issues)
    systems = normalize_systems(raw_systems, nodes, issues)
    result = build_journal(nodes, edges, systems)
    for issue in result['issues']:
        issue['message'] = CORE_MESSAGES.get(issue['code'], u'Неизвестная ошибка маршрута.')
        if issue.get('destination_id') is not None:
            issue.setdefault('node_ids', []).append(issue['destination_id'])
        issues.append(issue)
    runs, segments = create_display_rows(result, systems, nodes, issues)
    issues = unique_issues(issues)
    plan = build_schedule_plan(doc, u'Кабельный журнал', segments, systems,
                               nodes, roots, upstream_issues=issues)

    output.print_md(u'## Кабельный журнал — предварительный расчёт')
    print(u'Цепей: {0}; кабельных путей: {1}; участков: {2}; замечаний: {3}.'.format(
        len(systems), len(runs), len(segments), len(issues)))
    print(u'Модель пока не изменена. Участки идут по пути: щит → РК → следующие узлы.')
    if runs:
        output.print_table(
            table_data=[[row['panel'], row['circuit'], row['display_id'],
                         row['source'], row['dest'], row['conductor'],
                         u'{0:.2f}'.format(row['length_m']), row['route']]
                        for row in runs],
            columns=[u'Щит', u'Цепь', u'Кабель', u'Начало', u'Конец',
                     u'Проводник', u'Длина, м', u'Полный путь'],
            title=u'Полные кабельные пути')
    if segments:
        output.print_table(
            table_data=[[row['panel'], row['circuit'], row['display_id'],
                         row['sequence'], row['number'], row['source'],
                         row['dest'], u'{0:.2f}'.format(row['length_m']), row['method']]
                        for row in segments],
            columns=[u'Щит', u'Цепь', u'Кабель', u'Шаг', u'Участок',
                     u'Начало', u'Конец', u'Длина, м', u'Способ прокладки'],
            title=u'Участки в порядке прохождения')
    for issue in issues[:50]:
        print(u'{0}, цепь ID {1}, элементы {2}: {3}'.format(
            issue.get('code', u''), issue.get('system_id', u''),
            u', '.join(unicode(i) for i in issue.get('node_ids', [])), issue.get('message', u'')))
    if len(issues) > 50:
        print(u'Ещё {0} замечаний; исправьте их перед записью.'.format(len(issues) - 50))
    preview_schedule(output, plan)
    if not plan['ready']:
        forms.alert(u'Спецификация не изменена: предварительная проверка выявила '
                    u'{0} препятствий. Список — в окне pyRevit.'.format(len(plan['issues'])),
                    title=u'Кабельный журнал: требуется проверка', warn_icon=True)
        return
    if not forms.alert(
            u'Проверьте таблицы в окне pyRevit. Будут перезаписаны параметры '
            u'{0} участков, изменена сортировка и видимость полей существующей '
            u'спецификации «Кабельный журнал». Операцию можно отменить одной '
            u'командой Revit «Отменить».\n\nЗаписать показанные данные?'.format(len(plan['preview'])),
            yes=True, no=True, title=u'Подтверждение записи в Revit'):
        print(u'Пользователь отменил запись. Модель не изменена.')
        return
    changed = apply_schedule_plan(doc, plan)
    print(u'Готово: обновлена существующая спецификация «Кабельный журнал»; '
          u'строк {0}, изменений параметров {1}.'.format(len(plan['preview']), changed))
    try:
        optional_excel(doc, rk_mode, runs, segments)
    except Exception as error:
        print(traceback.format_exc())
        forms.alert(u'Спецификация Revit обновлена, но копию Excel сохранить не удалось: {0}'.format(
                    error), title=u'Ошибка копии Excel', warn_icon=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(traceback.format_exc())
        forms.alert(u'Кабельный журнал не обновлён.\n\n{0}\n\n'
                    u'Подробности — в окне pyRevit.'.format(error),
                    title=u'Ошибка кабельного журнала', warn_icon=True)
