# -*- coding: utf-8 -*-
"""Synchronize *section* rows with an existing Revit ViewSchedule.

pyRevit / Revit 2023 / IronPython 2.7.  No transaction is opened by
``build_schedule_plan``.  One scheduled element can carry only one row, so
shared route elements and rows without a unique straight carrier are rejected.
The caller must show ``plan['preview']`` and ``plan['schedule_changes']`` and
obtain user confirmation before calling ``apply_schedule_plan``.
"""

import math
import re
from collections import defaultdict

from pyrevit import DB
from System.Collections.Generic import List


FT_TO_M = 0.3048
P_CIRCUIT = u'Номер электрической цепи'
P_NUMBER = u'Номер участка трассы'
P_SOURCE = u'Источник'
P_DEST = u'Потребитель'
P_CONDUCTOR = u'Выбор проводника'
P_TOTAL = u'Длина трассы'
P_METHOD = u'Способ прокладки'

# The six visible columns requested for the existing journal.  Conductor is
# still validated and stored on the row carrier, but hidden in this schedule.
VISIBLE_ROLES = ('circuit', 'number', 'source', 'dest', 'total', 'method')
ROLE_NAMES = {
    'circuit': P_CIRCUIT, 'number': P_NUMBER, 'source': P_SOURCE,
    'dest': P_DEST, 'conductor': P_CONDUCTOR, 'total': P_TOTAL,
    'method': P_METHOD,
}
TEXT_ROLES = ('circuit', 'number', 'source', 'dest', 'conductor', 'method')
LINE_CATS = set([int(DB.BuiltInCategory.OST_Conduit)])
ROUTE_CATS = set(int(x) for x in (
    DB.BuiltInCategory.OST_Conduit, DB.BuiltInCategory.OST_ConduitFitting,
    DB.BuiltInCategory.OST_CableTray, DB.BuiltInCategory.OST_CableTrayFitting,
))


def _text(value):
    return u'' if value is None else unicode(value).strip()


def _eid(element):
    return element.Id.IntegerValue


def _natural(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.lower())
                 for part in re.split(r'(\d+)', _text(value)))


def _category(element):
    return element.Category.Id.IntegerValue if element.Category else None


def _parameter(element, name):
    values = list(element.GetParameters(name))
    if len(values) != 1:
        raise ValueError(u'ID {0}: требуется ровно один параметр «{1}», найдено {2}.'.format(
            _eid(element), name, len(values)))
    return values[0]


def _raw(parameter):
    if parameter.StorageType == DB.StorageType.String:
        return parameter.AsString() or u''
    if parameter.StorageType == DB.StorageType.Double:
        return parameter.AsDouble()
    if parameter.StorageType == DB.StorageType.Integer:
        return parameter.AsInteger()
    raise ValueError(u'Неподдерживаемый тип хранения параметра.')


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(a - b) < 1e-8
        except Exception:
            return False
    return a == b


def _length_value(parameter, length_m):
    if not isinstance(length_m, (int, float)) or math.isnan(length_m) or math.isinf(length_m):
        raise ValueError(u'Некорректная длина участка.')
    if length_m <= 0:
        raise ValueError(u'Длина участка должна быть положительной.')
    if parameter.StorageType == DB.StorageType.Double:
        spec = parameter.Definition.GetDataType()
        if spec == DB.SpecTypeId.Length:
            return length_m / FT_TO_M
        if spec == DB.SpecTypeId.Number:
            return length_m * 1000.0
        raise ValueError(u'«{0}» должен иметь тип Длина либо Число.'.format(P_TOTAL))
    if parameter.StorageType == DB.StorageType.Integer:
        return int(math.floor(length_m * 1000.0 + 0.5))
    if parameter.StorageType == DB.StorageType.String:
        # Original route scripts store non-Length values in millimetres.
        return u'{0:.3f}'.format(length_m * 1000.0).rstrip(u'0').rstrip(u'.')
    raise ValueError(u'«{0}»: неподдерживаемый тип.'.format(P_TOTAL))


def _find_schedule(doc, exact_name):
    found = [view for view in DB.FilteredElementCollector(doc).OfClass(DB.ViewSchedule)
             if not view.IsTemplate and _text(view.Name) == exact_name]
    if len(found) != 1:
        raise ValueError(u'Спецификаций с точным именем «{0}»: {1}; нужна ровно одна.'.format(
            exact_name, len(found)))
    schedule = found[0]
    if schedule.Definition.IsKeySchedule:
        raise ValueError(u'«{0}» — ключевая спецификация, не журнал элементов.'.format(exact_name))
    return schedule


def _template_issue(doc, schedule):
    template_id = schedule.ViewTemplateId
    if template_id == DB.ElementId.InvalidElementId:
        return None
    template = doc.GetElement(template_id)
    if template is None:
        return u'Назначенный шаблон вида не найден.'
    controlled = set(x.IntegerValue for x in template.GetTemplateParameterIds())
    free = set(x.IntegerValue for x in template.GetNonControlledTemplateParameterIds())
    for enum_name in ('SCHEDULE_FIELDS_PARAM', 'SCHEDULE_FILTER_PARAM',
                      'SCHEDULE_GROUP_PARAM', 'SCHEDULE_FORMAT_PARAM'):
        enum_value = getattr(DB.BuiltInParameter, enum_name, None)
        if enum_value is None:
            continue
        parameter_id = int(enum_value)
        if parameter_id in controlled and parameter_id not in free:
            return u'Шаблон вида управляет полями, фильтром, сортировкой или форматированием.'
    return None


def _schedule_field_map(definition, parameter_map):
    fields = [definition.GetField(field_id) for field_id in definition.GetFieldOrder()]
    result = {}
    for role, parameter in parameter_map.items():
        matched = [field for field in fields
                   if field.ParameterId.IntegerValue == parameter.Id.IntegerValue]
        if len(matched) != 1:
            raise ValueError(u'Поле «{0}» (ID параметра {1}) найдено в спецификации {2} раз(а).'.format(
                ROLE_NAMES[role], parameter.Id.IntegerValue, len(matched)))
        if matched[0].FieldType != DB.ScheduleFieldType.Instance:
            raise ValueError(u'Поле «{0}» должно показывать параметр экземпляра.'.format(
                ROLE_NAMES[role]))
        result[role] = matched[0]
    return result, fields


def _candidate_parameters(element, definition):
    parameters = {}
    for role, name in ROLE_NAMES.items():
        parameter = _parameter(element, name)
        if parameter.IsReadOnly:
            raise ValueError(u'ID {0}: «{1}» доступен только для чтения.'.format(_eid(element), name))
        if role in TEXT_ROLES and parameter.StorageType != DB.StorageType.String:
            raise ValueError(u'ID {0}: «{1}» должен быть текстовым.'.format(_eid(element), name))
        parameters[role] = parameter
    fields, all_fields = _schedule_field_map(definition, parameters)
    return parameters, fields, all_fields


def _category_eligible(schedule, element):
    schedule_cat = schedule.Definition.CategoryId.IntegerValue
    multi = getattr(DB.BuiltInCategory, 'OST_MultiCategory', None)
    return schedule_cat == _category(element) or (multi is not None and schedule_cat == int(multi))


def _make_operation(element, parameter, new_value, role):
    return {'element_id': _eid(element), 'parameter': parameter,
            'parameter_name': ROLE_NAMES[role], 'role': role,
            'old': _raw(parameter), 'had_value': bool(parameter.HasValue),
            'new': new_value}


def _segment_key(segment):
    route_index = segment.get('route_index')
    if route_index is None:
        match = re.search(r'-(\d+)$', _text(segment.get('number')))
        route_index = int(match.group(1)) if match else int(segment.get('sequence', 0))
    return (_natural(segment.get('circuit')), _text(segment.get('system_id')),
            int(route_index), int(segment.get('sequence', 0)),
            _text(segment.get('source')), _text(segment.get('dest')))


def _segment_conductor(segment, roots):
    """The cable mark comes from straight OST_Conduit, never from RK/tray/system."""
    values = set()
    conduits = []
    for element_id in sorted(set(segment.get('element_ids') or [])):
        element = roots.get(element_id)
        if element is None or _category(element) != int(DB.BuiltInCategory.OST_Conduit):
            continue
        conduits.append(element_id)
        parameter = _parameter(element, P_CONDUCTOR)
        if parameter.StorageType != DB.StorageType.String:
            raise ValueError(u'ID {0}: «{1}» должен быть текстовым.'.format(
                element_id, P_CONDUCTOR))
        value = _text(parameter.AsString())
        if not value or value.lower() in (u'?', u'не указано'):
            raise ValueError(u'ID {0}: не заполнен «{1}».'.format(element_id, P_CONDUCTOR))
        values.add(value)
    if not conduits:
        raise ValueError(u'Нет прямого элемента OST_Conduit для «{0}».'.format(P_CONDUCTOR))
    if len(values) != 1:
        raise ValueError(u'Разные значения «{0}» на элементах OST_Conduit участка: {1}.'.format(
            P_CONDUCTOR, u', '.join(unicode(x) for x in conduits)))
    return next(iter(values))


def _filter_description(schedule_filter):
    try:
        value = schedule_filter.GetStringValue() if schedule_filter.IsStringValue else u''
    except Exception:
        value = u''
    return u'{0} / поле {1} / {2}'.format(
        schedule_filter.FilterType, schedule_filter.FieldId, value)


def _sort_description(definition):
    return u', '.join(u'{0} ({1})'.format(
        definition.GetSortGroupField(index).FieldId,
        definition.GetSortGroupField(index).SortOrder)
        for index in range(definition.GetSortGroupFieldCount())) or u'нет'


def build_schedule_plan(doc, schedule_name, segments, systems, nodes, roots,
                        upstream_issues=None):
    """Read-only preflight. Returns a plan; ``ready`` gates all writes.

    ``segments`` is the validated display-row list from ``create_display_rows``.
    ``roots`` maps Revit ElementId integers to physical route elements.  The
    ``nodes`` argument is retained for contract symmetry and future diagnostics.
    Any upstream topology issue blocks a model update (a partial clean schedule
    must not be presented as authoritative).
    """
    plan = {'ready': False, 'issues': [], 'preview': [], 'stale_preview': [],
            'changes_preview': [],
            'schedule_changes': [],
            'operations': [], 'schedule': None, 'field_ids': {},
            'carrier_ids': [], 'filter_required': True, 'existing_filters': []}
    if upstream_issues:
        plan['issues'].append(u'Есть {0} ошибок/предупреждений маршрута; запись всей спецификации запрещена.'.format(
            len(upstream_issues)))
    try:
        schedule = _find_schedule(doc, schedule_name)
        plan['schedule'] = schedule
        problem = _template_issue(doc, schedule)
        if problem:
            plan['issues'].append(problem)
    except Exception as error:
        plan['issues'].append(_text(error))
        return plan
    if not segments:
        plan['issues'].append(u'Нет участков для записи.')
        return plan

    definition = schedule.Definition
    systems_by_id = dict((item['id'], item) for item in systems)
    circuit_owners = defaultdict(set)
    for segment in segments:
        circuit_owners[_text(segment.get('circuit'))].add(segment.get('system_id'))
    for circuit, owners in circuit_owners.items():
        if not circuit:
            plan['issues'].append(u'Есть участок без номера цепи.')
        if len(owners) > 1:
            plan['issues'].append(u'Номер цепи «{0}» принадлежит нескольким ElectricalSystem; '
                                  u'нумерация ТР будет неоднозначной.'.format(circuit))

    # One physical straight element in two journal sections is a shared route.
    # Its single set of instance parameters cannot express two cable rows.
    appearances = defaultdict(set)
    for index, segment in enumerate(segments):
        for element_id in set(segment.get('element_ids') or []):
            appearances[element_id].add(index)
    for element_id, occurrences in appearances.items():
        if len(occurrences) > 1:
            plan['issues'].append(u'Общий элемент трассы ID {0} входит в {1} строк(и); '
                                  u'назначение кабеля на одном носителе неоднозначно.'.format(
                                      element_id, len(occurrences)))

    ordered = sorted(segments, key=_segment_key)
    sequence_by_circuit = defaultdict(int)
    selected = set()
    field_ids = None
    all_fields = None
    for segment in ordered:
        system_id = segment.get('system_id')
        system = systems_by_id.get(system_id)
        if system is None:
            plan['issues'].append(u'Не найдена цепь ID {0} для участка.'.format(system_id))
            continue
        try:
            conductor = _segment_conductor(segment, roots)
        except Exception as error:
            plan['issues'].append(u'Цепь ID {0}: {1}'.format(system_id, error))
            continue
        for key in ('circuit', 'source', 'dest', 'method'):
            if not _text(segment.get(key)):
                plan['issues'].append(u'Цепь ID {0}: пустое поле «{1}» участка.'.format(system_id, key))
        circuit = _text(segment.get('circuit'))
        sequence_by_circuit[circuit] += 1
        number = u'ТР{0}-{1:03d}'.format(circuit, sequence_by_circuit[circuit])
        length = float(segment.get('length_m', 0))
        if math.isnan(length) or math.isinf(length):
            plan['issues'].append(u'Участок {0}: некорректная длина.'.format(number))
            continue
        candidate_errors = []
        selected_candidate = None
        for element_id in sorted(set(segment.get('element_ids') or [])):
            element = roots.get(element_id)
            if element is None or _category(element) not in LINE_CATS:
                continue
            if not _category_eligible(schedule, element):
                candidate_errors.append(u'ID {0} не относится к категории спецификации.'.format(element_id))
                continue
            if element_id in selected:
                candidate_errors.append(u'ID {0} уже назначен другому участку.'.format(element_id))
                continue
            try:
                parameters, fields, ordered_fields = _candidate_parameters(element, definition)
                if field_ids is not None and any(
                        fields[role].FieldId != field_ids[role] for role in ROLE_NAMES):
                    raise ValueError(u'Поля разных категорий не совпадают по ID параметров.')
                new_values = {
                    'circuit': circuit, 'number': number,
                    'source': _text(segment.get('source')),
                    'dest': _text(segment.get('dest')),
                    'conductor': conductor,
                    'total': _length_value(parameters['total'], float(segment['length_m'])),
                    'method': _text(segment.get('method')),
                }
                selected_candidate = (element, parameters, fields, ordered_fields, new_values)
                break
            except Exception as error:
                candidate_errors.append(_text(error))
        if selected_candidate is None:
            plan['issues'].append(u'Участок {0}: нет уникального прямого носителя с нужными '
                                  u'параметрами и полями. {1}'.format(
                                      number, u'; '.join(candidate_errors[:3])))
            continue
        element, parameters, fields, ordered_fields, new_values = selected_candidate
        selected.add(_eid(element))
        if field_ids is None:
            field_ids = dict((role, fields[role].FieldId) for role in ROLE_NAMES)
            all_fields = ordered_fields
        for role in ROLE_NAMES:
            plan['operations'].append(_make_operation(element, parameters[role], new_values[role], role))
        plan['preview'].append({
            'carrier_id': _eid(element), 'circuit': circuit, 'number': number,
            'source': new_values['source'], 'dest': new_values['dest'],
            'conductor': conductor, 'length_m': float(segment['length_m']),
            'method': new_values['method'],
        })

    if field_ids is None:
        return plan
    plan['field_ids'] = field_ids
    plan['carrier_ids'] = sorted(selected)
    for role in ('circuit', 'number'):
        try:
            if not definition.CanSortByField(field_ids[role]):
                plan['issues'].append(u'Поле «{0}» нельзя использовать для сортировки.'.format(
                    ROLE_NAMES[role]))
        except Exception as error:
            plan['issues'].append(u'Не удалось проверить сортировку «{0}»: {1}'.format(
                ROLE_NAMES[role], error))
    number_parameter_id = definition.GetField(field_ids['number']).ParameterId.IntegerValue

    # Only clear old journal numbers on elements *currently present* in this
    # schedule.  Values elsewhere in the project belong to other views/users.
    # Remaining TR-prefixed rows, if any, are caught by post-write verification.
    try:
        current_schedule_ids = set(_eid(element) for element in
            DB.FilteredElementCollector(doc, schedule.Id).WhereElementIsNotElementType())
    except Exception as error:
        plan['issues'].append(u'Не удалось прочитать текущие строки спецификации: {0}'.format(error))
        current_schedule_ids = set()
    for element_id in sorted(current_schedule_ids):
        if element_id in selected:
            continue
        element = roots.get(element_id)
        if element is None:
            continue
        if _category(element) not in ROUTE_CATS:
            continue
        values = list(element.GetParameters(P_NUMBER))
        if not values:
            continue
        if len(values) != 1:
            plan['issues'].append(u'ID {0}: несколько параметров «{1}»; старую строку нельзя очистить.'.format(
                element_id, P_NUMBER))
            continue
        parameter = values[0]
        if parameter.Id.IntegerValue != number_parameter_id:
            plan['issues'].append(u'ID {0}: одноимённый номер участка имеет другой ID параметра.'.format(element_id))
            continue
        if parameter.StorageType != DB.StorageType.String:
            plan['issues'].append(u'ID {0}: номер участка не текстовый.'.format(element_id))
            continue
        old_number = _text(parameter.AsString())
        if not old_number.startswith(u'ТР'):
            continue
        if parameter.IsReadOnly:
            plan['issues'].append(u'ID {0}: нельзя очистить старый номер участка.'.format(element_id))
            continue
        plan['operations'].append(_make_operation(element, parameter, u'', 'number'))
        plan['stale_preview'].append({
            'element_id': element_id, 'old_number': old_number,
            'new_number': u'', 'action': u'Очистить старую строку журнала',
        })

    existing_filters = [definition.GetFilter(index)
                        for index in range(definition.GetFilterCount())]
    plan['existing_filters'] = existing_filters
    number_id = field_ids['number']
    for existing in existing_filters:
        if (existing.FieldId == number_id and
                existing.FilterType == DB.ScheduleFilterType.BeginsWith and
                existing.IsStringValue and existing.GetStringValue() == u'ТР'):
            plan['filter_required'] = False
    if plan['filter_required'] and len(existing_filters) >= 8:
        plan['issues'].append(u'В спецификации уже 8 фильтров; добавить фильтр журнала невозможно.')
    if plan['filter_required']:
        try:
            if not definition.CanFilterByValue(number_id):
                plan['issues'].append(u'Поле «{0}» нельзя фильтровать по значению.'.format(P_NUMBER))
        except Exception as error:
            plan['issues'].append(u'Не удалось проверить фильтр «{0}»: {1}'.format(P_NUMBER, error))
    desired_ids = [field_ids[role] for role in VISIBLE_ROLES]
    current_ids = list(definition.GetFieldOrder())
    desired_ids.extend(field_id for field_id in current_ids if field_id not in desired_ids)
    visible_before = [_text(field.ColumnHeading) for field in all_fields if not field.IsHidden]
    plan['schedule_changes'] = [
        u'Видимые поля: {0} → {1}.'.format(
            u', '.join(visible_before),
            u', '.join(ROLE_NAMES[role] for role in VISIBLE_ROLES)),
        u'Порядок полей: шесть столбцов журнала первыми; остальные поля сохранены скрытыми.',
        u'Сортировка: {0} → цепь ↑, номер участка ↑.'.format(_sort_description(definition)),
        u'Для каждого экземпляра: {0} → Да.'.format(definition.IsItemized),
        u'Фильтры сохранены: {0}; {1}.'.format(
            u'; '.join(_filter_description(item) for item in existing_filters) or u'нет',
            u'добавить «номер начинается с ТР»' if plan['filter_required']
            else u'фильтр «номер начинается с ТР» уже есть'),
    ]
    plan['field_order'] = desired_ids
    plan['all_field_ids'] = current_ids
    plan['changes_preview'] = [
        {'element_id': operation['element_id'],
         'parameter_name': operation['parameter_name'],
         'old': operation['old'] if operation['had_value'] else u'<не задано>',
         'new': operation['new']}
        for operation in plan['operations']
        if not operation['had_value'] or not _same(operation['old'], operation['new'])]
    plan['ready'] = not plan['issues'] and len(plan['preview']) == len(segments)
    return plan


def _verify_schedule_rows(doc, schedule, expected_ids):
    # A view-scoped collector proves that retained filters and category rules
    # have not hidden planned carriers or left stale journal rows in the view.
    visible = set(_eid(element) for element in
                  DB.FilteredElementCollector(doc, schedule.Id).WhereElementIsNotElementType())
    missing = set(expected_ids) - visible
    if missing:
        raise ValueError(u'Сохранённый фильтр/категория скрывает носители ID: {0}.'.format(
            u', '.join(unicode(x) for x in sorted(missing))))
    extra = visible - set(expected_ids)
    if extra:
        raise ValueError(u'В спецификации остались посторонние строки ID: {0}.'.format(
            u', '.join(unicode(x) for x in sorted(extra)[:30])))


def apply_schedule_plan(doc, plan):
    """Apply a confirmed plan atomically; return changed-parameter count.

    Revit API access must stay on its UI thread.  This routine intentionally
    refuses plans with unresolved issues, without a schedule, or with no rows.
    """
    if not plan.get('ready') or plan.get('issues') or not plan.get('carrier_ids'):
        raise ValueError(u'Предварительная проверка не завершена; модель не изменена.')
    schedule = plan['schedule']
    if schedule is None or schedule.Document != doc:
        raise ValueError(u'Спецификация не относится к текущему документу.')
    transaction = DB.Transaction(doc, u'Обновить кабельный журнал')
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError(u'Не удалось начать транзакцию.')
        # No caller may silently change source values after preview.
        for operation in plan['operations']:
            parameter = operation['parameter']
            if parameter.HasValue != operation['had_value'] or not _same(
                    _raw(parameter), operation['old']):
                raise ValueError(u'Параметр «{0}» ID {1} изменился после просмотра.'.format(
                    operation['parameter_name'], operation['element_id']))
        changed = 0
        for operation in plan['operations']:
            parameter = operation['parameter']
            if not operation['had_value'] or not _same(operation['old'], operation['new']):
                parameter.Set(operation['new'])
                changed += 1
            if not _same(_raw(parameter), operation['new']):
                raise ValueError(u'Не удалось записать «{0}» ID {1}.'.format(
                    operation['parameter_name'], operation['element_id']))

        definition = schedule.Definition
        # Retain each existing field and its format; only visibility/order is
        # changed to produce the explicitly requested six-column journal.
        visible_ids = set(plan['field_ids'][role] for role in VISIBLE_ROLES)
        for field_id in plan['all_field_ids']:
            field = definition.GetField(field_id)
            field.IsHidden = field_id not in visible_ids
        ordered = List[DB.ScheduleFieldId]()
        for field_id in plan['field_order']:
            ordered.Add(field_id)
        definition.SetFieldOrder(ordered)
        while definition.GetSortGroupFieldCount():
            definition.RemoveSortGroupField(definition.GetSortGroupFieldCount() - 1)
        circuit_sort = DB.ScheduleSortGroupField(
            plan['field_ids']['circuit'], DB.ScheduleSortOrder.Ascending)
        circuit_sort.ShowHeader = True
        circuit_sort.ShowBlankLine = True
        definition.AddSortGroupField(circuit_sort)
        definition.AddSortGroupField(DB.ScheduleSortGroupField(
            plan['field_ids']['number'], DB.ScheduleSortOrder.Ascending))
        definition.IsItemized = True
        if plan['filter_required']:
            definition.AddFilter(DB.ScheduleFilter(
                plan['field_ids']['number'], DB.ScheduleFilterType.BeginsWith, u'ТР'))
        doc.Regenerate()
        for operation in plan['operations']:
            if not _same(_raw(operation['parameter']), operation['new']):
                raise ValueError(u'Revit изменил «{0}» ID {1} после пересчёта.'.format(
                    operation['parameter_name'], operation['element_id']))
        _verify_schedule_rows(doc, schedule, plan['carrier_ids'])
        status = transaction.Commit()
        if status != DB.TransactionStatus.Committed:
            raise ValueError(u'Revit не подтвердил обновление спецификации: {0}.'.format(status))
        return changed
    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        raise
    finally:
        if transaction.GetStatus() != DB.TransactionStatus.Pending:
            transaction.Dispose()
