# -*- coding: utf-8 -*-
__title__ = 'Короб к модели'
__doc__ = (
    u'Сначала выберите функцию: обычное подключение, '
    u'подключение с поочередным скрытием коробов или проверка соединителей. '
    u'Скрипт ЖЕСТКО блокирует соединение, если «Тип сети» короба и модели различается. '
    u'Перед подключением свободный конец короба доводится вдоль '
    u'его оси до проекции выбранного коннектора. '
    u'В новые участки и соединительные детали переносится «Тип системы», '
    u'«Примечание» и доступные пользовательские параметры исходного короба. '
    u'Цвет исходного короба переносится на созданные элементы активного вида. '
    u'Перед выбором модели временно скрываются короба и модели, '
    u'«Тип сети» которых не совпадает с типом сети выбранного короба.'
)
__version__ = '2.1'

import Autodesk.Revit.UI.Selection as Sel

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    ConnectorType,
    Domain,
    ElementId,
    FamilyInstance,
    FilteredElementCollector,
    Line,
    OverrideGraphicSettings,
    StorageType,
    TemporaryViewMode,
)
from Autodesk.Revit.DB.Electrical import Conduit
from Autodesk.Revit.Exceptions import OperationCanceledException
from pyrevit import revit, forms, script
from System.Collections.Generic import List


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# ============ НАСТРОЙКИ ============

METHOD_PARAM = u'Способ прокладки'
METHOD_VALUE = u'В лотке'

SYSTEM_TYPE_BUILTIN_NAME = 'RBS_CONDUIT_SYSTEM_TYPE_PARAM'
SYSTEM_TYPE_PARAMETER_NAMES = (u'Тип системы', u'System Type')

# Параметры, в которых скрипт ищет «Тип сети»
NETWORK_TYPE_PARAMETER_NAMES = (u'GLF_Тип сети', u'Тип сети',u'Тип системы')

# ВАЖНО: Жесткая защита. Если True - скрипт не даст подключить короб,
# если у модели или у самого короба не заполнен Тип сети, либо они отличаются.
BLOCK_WHEN_NETWORK_TYPE_IS_MISSING = True

# Точное имя фильтра, который временно скрывает все короба.
CONDUIT_VISIBILITY_FILTER_NAME = u'Короба'

USE_ONLY_FREE_CONNECTORS = True
POINT_TOLERANCE = 1.0 / 304.8  # 1 мм

AUTO_EXTEND_CONDUIT = True
MIN_RETAINED_CONDUIT_LENGTH = 0.15  # футы, 45.72 мм
POSITION_CHECK_TOLERANCE = 0.1 / 304.8  # 0.1 мм
TARGET_STUB_LENGTH = 150.0 / 304.8  # 150 мм
CONNECTOR_ALIGNMENT_TOLERANCE = 0.99

# Временное скрытие несовместимых элементов на время выбора модели.
# Короба и модели, «Тип сети» которых не совпадает с типом сети
# выбранного короба, скрываются на активном виде. Короб-источник остаётся.
HIDE_INCOMPATIBLE_BY_NETWORK_TYPE = True

# Скрывать ли также элементы, у которых «Тип сети» не задан вовсе
# (подключение к ним всё равно блокируется проверкой совместимости).
HIDE_ELEMENTS_WITHOUT_NETWORK_TYPE = True

ACTION_CONNECT = u'Подключить короб к модели (Обычный режим)'
ACTION_CONNECT_BLINK = u'Подключить со скрытием коробов (Видно - не видно)'
ACTION_INSPECT = u'Проверить свободные коннекторы'

PARAMS_TO_COPY = [
    u'Примечание',
    u'ADSK_Группирование',
    u'GLF_Тип сети',
    u'Источник',
    u'Потребитель',
    u'Номер электрической цепи',
    u'Кабель',
    u'Выбор короба',
    u'Выбор проводника',
    u'Имя нагрузки',
    u'ADSK_Состав_Трассы',
]

COLOR_PROPERTY_PRIORITY = (
    'ProjectionLineColor',
    'SurfaceForegroundPatternColor',
    'CutLineColor',
    'CutForegroundPatternColor',
    'SurfaceBackgroundPatternColor',
    'CutBackgroundPatternColor',
)

COLOR_OVERRIDE_SETTERS = (
    'SetProjectionLineColor',
    'SetCutLineColor',
    'SetSurfaceForegroundPatternColor',
    'SetSurfaceBackgroundPatternColor',
    'SetCutForegroundPatternColor',
    'SetCutBackgroundPatternColor',
)


# ============ 1. ПАРАМЕТРЫ ============

def copy_parameter(source, target, parameter_name):
    source_parameter = source.LookupParameter(parameter_name)
    target_parameter = target.LookupParameter(parameter_name)

    if source_parameter is None or target_parameter is None:
        return False
    if target_parameter.IsReadOnly or not source_parameter.HasValue:
        return False

    try:
        storage_type = source_parameter.StorageType

        if storage_type == StorageType.String:
            value = source_parameter.AsString()
            if value is None:
                value = u''
            target_parameter.Set(value)

        elif storage_type == StorageType.Double:
            target_parameter.Set(source_parameter.AsDouble())

        elif storage_type == StorageType.Integer:
            target_parameter.Set(source_parameter.AsInteger())

        elif storage_type == StorageType.ElementId:
            target_parameter.Set(source_parameter.AsElementId())

        else:
            return False

        return True
    except Exception:
        return False


def copy_parameters(source, target):
    for parameter_name in PARAMS_TO_COPY:
        copy_parameter(source, target, parameter_name)


def get_parameter_display_text(parameter):
    if parameter is None:
        return None

    try:
        if not parameter.HasValue:
            return None
    except Exception:
        pass

    try:
        if parameter.StorageType == StorageType.String:
            value = parameter.AsString()
            if value is not None and unicode(value).strip():
                return unicode(value).strip()
    except Exception:
        pass

    try:
        value = parameter.AsValueString()
        if value is not None and unicode(value).strip():
            return unicode(value).strip()
    except Exception:
        pass

    try:
        raw_value = get_parameter_raw_value(parameter)
        if parameter.StorageType == StorageType.ElementId:
            referenced_element = doc.GetElement(raw_value)
            if referenced_element is not None:
                try:
                    value = referenced_element.Name
                    if value is not None and unicode(value).strip():
                        return unicode(value).strip()
                except Exception:
                    pass
            return u'ID {0}'.format(raw_value.IntegerValue)

        if raw_value is not None and unicode(raw_value).strip():
            return unicode(raw_value).strip()
    except Exception:
        pass

    return None


def normalize_network_type(value):
    if value is None:
        return None
    try:
        normalized = u' '.join(unicode(value).split()).strip().lower()
    except Exception:
        return None
    return normalized if normalized else None


def get_network_type_entry(element, source_kind):
    if element is None:
        return None

    for parameter_name in NETWORK_TYPE_PARAMETER_NAMES:
        try:
            parameter = element.LookupParameter(parameter_name)
        except Exception:
            parameter = None

        value = get_parameter_display_text(parameter)
        normalized = normalize_network_type(value)
        if normalized is None:
            continue

        try:
            element_id = element.Id.IntegerValue
        except Exception:
            element_id = -1

        return {
            'value': value,
            'normalized': normalized,
            'parameter_name': parameter_name,
            'element_id': element_id,
            'source_kind': source_kind,
        }

    return None


def collect_network_type_entries(elements):
    result = []
    visited_ids = set()

    for element in elements:
        if element is None:
            continue

        candidates = [(element, u'экземпляр')]
        try:
            type_id = element.GetTypeId()
            if type_id != ElementId.InvalidElementId:
                type_element = doc.GetElement(type_id)
                if type_element is not None:
                    candidates.append((type_element, u'тип'))
        except Exception:
            pass

        for candidate, source_kind in candidates:
            try:
                candidate_key = candidate.Id.IntegerValue
            except Exception:
                candidate_key = id(candidate)

            if candidate_key in visited_ids:
                continue
            visited_ids.add(candidate_key)

            entry = get_network_type_entry(candidate, source_kind)
            if entry is not None:
                result.append(entry)

    return result


def group_network_type_entries(entries):
    grouped = {}
    for entry in entries:
        key = entry['normalized']
        grouped.setdefault(key, []).append(entry)
    return grouped


def format_network_type_entries(entries):
    if not entries:
        return u'Не задан / Отсутствует'

    result = []
    seen = set()
    for entry in entries:
        key = (
            entry['normalized'],
            entry['element_id'],
            entry['parameter_name'],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(
            u'«{0}» ({1} «{2}», ID {3})'.format(
                entry['value'],
                entry['source_kind'],
                entry['parameter_name'],
                entry['element_id'],
            )
        )
    return u'; '.join(result)


def validate_network_type_compatibility(
    source_conduit,
    target_element,
    target_connector,
):
    source_entries = collect_network_type_entries([source_conduit])

    try:
        connector_owner = target_connector.Owner
    except Exception:
        connector_owner = None

    target_root = get_family_root(target_element)
    target_entries = collect_network_type_entries([
        connector_owner,
        target_element,
        target_root,
    ])

    source_groups = group_network_type_entries(source_entries)
    target_groups = group_network_type_entries(target_entries)

    report = {
        'allowed': True,
        'verified': False,
        'source_entries': source_entries,
        'target_entries': target_entries,
        'reason': u'',
    }

    if len(source_groups) > 1:
        report.update({
            'allowed': False,
            'reason': (
                u'У выбранного короба найдены противоречащие '
                u'значения «Тип сети» на экземпляре и его типе.'
            ),
        })
        return report

    if len(target_groups) > 1:
        report.update({
            'allowed': False,
            'reason': (
                u'У выбранной модели найдены противоречащие '
                u'значения «Тип сети» у семейства, владельца '
                u'коннектора или их типов.'
            ),
        })
        return report

    # Строгая проверка на отсутствие параметра
    if not source_groups or not target_groups:
        missing_parts = []
        if not source_groups:
            missing_parts.append(u'короба')
        if not target_groups:
            missing_parts.append(u'модели')

        report['reason'] = (
            u'«Тип сети» не задан у {0}; сравнение невозможно.'
            .format(u' и '.join(missing_parts))
        )
        report['allowed'] = not BLOCK_WHEN_NETWORK_TYPE_IS_MISSING
        if not report['allowed']:
            report['reason'] += u' Соединение заблокировано настройками безопасности.'
        return report

    source_key = next(iter(source_groups))
    target_key = next(iter(target_groups))
    source_value = source_groups[source_key][0]['value']
    target_value = target_groups[target_key][0]['value']

    if source_key != target_key:
        report.update({
            'allowed': False,
            'reason': (
                u'Типы сети не совпадают: у короба — «{0}», '
                u'у модели — «{1}».'.format(source_value, target_value)
            ),
        })
        return report

    report.update({
        'verified': True,
        'reason': u'Тип сети совпадает: «{0}».'.format(source_value),
    })
    return report


def get_system_type_parameter(element):
    built_in_parameter = getattr(
        BuiltInParameter,
        SYSTEM_TYPE_BUILTIN_NAME,
        None,
    )
    if built_in_parameter is not None:
        try:
            parameter = element.get_Parameter(built_in_parameter)
            if parameter is not None:
                return parameter
        except Exception:
            pass

    for parameter_name in SYSTEM_TYPE_PARAMETER_NAMES:
        try:
            parameter = element.LookupParameter(parameter_name)
            if parameter is not None:
                return parameter
        except Exception:
            pass
    return None


def get_parameter_raw_value(parameter):
    storage_type = parameter.StorageType
    if storage_type == StorageType.String:
        value = parameter.AsString()
        return value if value is not None else u''
    if storage_type == StorageType.Double:
        return parameter.AsDouble()
    if storage_type == StorageType.Integer:
        return parameter.AsInteger()
    if storage_type == StorageType.ElementId:
        return parameter.AsElementId()
    raise Exception(u'Неподдерживаемый тип хранения параметра.')


def parameter_values_are_equal(storage_type, first, second):
    if storage_type == StorageType.Double:
        return abs(first - second) <= 1e-9
    if storage_type == StorageType.ElementId:
        if first is None or second is None:
            return first is second
        return first.IntegerValue == second.IntegerValue
    return first == second


def get_system_type_text(element):
    parameter = get_system_type_parameter(element)
    if parameter is None or not parameter.HasValue:
        return u'не задан'

    try:
        value_text = parameter.AsValueString()
        if value_text:
            return value_text
    except Exception:
        pass

    try:
        value = get_parameter_raw_value(parameter)
        if parameter.StorageType == StorageType.ElementId:
            system_type = doc.GetElement(value)
            if system_type is not None:
                return system_type.Name
            return u'ID {0}'.format(value.IntegerValue)
        if value is not None:
            return unicode(value)
    except Exception:
        pass
    return u'не задан'


def copy_system_type(source, target):
    source_parameter = get_system_type_parameter(source)
    target_parameter = get_system_type_parameter(target)

    if source_parameter is None:
        raise Exception(
            u'У исходного короба ID {0} отсутствует параметр «Тип системы».'.format(
                source.Id.IntegerValue
            )
        )
    if target_parameter is None:
        raise Exception(
            u'У нового элемента ID {0} отсутствует параметр «Тип системы».'.format(
                target.Id.IntegerValue
            )
        )
    if not source_parameter.HasValue:
        raise Exception(
            u'У исходного короба ID {0} параметр «Тип системы» не заполнен.'.format(
                source.Id.IntegerValue
            )
        )
    if source_parameter.StorageType != target_parameter.StorageType:
        raise Exception(
            u'У исходного короба и нового элемента различается тип хранения '
            u'параметра «Тип системы».'
        )

    storage_type = source_parameter.StorageType
    source_value = get_parameter_raw_value(source_parameter)
    target_value = (
        get_parameter_raw_value(target_parameter)
        if target_parameter.HasValue
        else None
    )

    if parameter_values_are_equal(storage_type, source_value, target_value):
        return True
    if target_parameter.IsReadOnly:
        raise Exception(
            u'Новый элемент ID {0} получил другой «Тип системы», а параметр '
            u'недоступен для записи.'.format(target.Id.IntegerValue)
        )

    target_parameter.Set(source_value)
    if not target_parameter.HasValue:
        raise Exception(
            u'Revit не сохранил «Тип системы» в новом элементе ID {0}.'.format(
                target.Id.IntegerValue
            )
        )

    written_value = get_parameter_raw_value(target_parameter)
    if not parameter_values_are_equal(storage_type, source_value, written_value):
        raise Exception(
            u'После записи «Тип системы» нового элемента ID {0} не совпадает '
            u'с исходным коробом.'.format(target.Id.IntegerValue)
        )
    return True


def get_conduit_owner_from_connectors(first, second):
    for connector in (first, second):
        try:
            owner = connector.Owner
            if (
                owner is not None
                and owner.Category is not None
                and owner.Category.Id.IntegerValue
                == int(BuiltInCategory.OST_Conduit)
            ):
                return owner
        except Exception:
            pass

    raise Exception(
        u'Не найден исходный короб для настройки созданной '
        u'соединительной детали.'
    )


def configure_created_conduit_fitting(
    fitting,
    template_conduit,
    color_context=None,
):
    if isinstance(fitting, ElementId):
        fitting = doc.GetElement(fitting)

    if fitting is None:
        raise Exception(
            u'Revit не вернул созданную соединительную деталь.'
        )

    if fitting.Category is None:
        raise Exception(
            u'У созданной соединительной детали отсутствует категория.'
        )

    if (
        fitting.Category.Id.IntegerValue
        != int(BuiltInCategory.OST_ConduitFitting)
    ):
        raise Exception(
            u'Созданный элемент ID {0} не относится к категории '
            u'«Соединительные детали коробов».'.format(
                fitting.Id.IntegerValue
            )
        )

    doc.Regenerate()
    copy_system_type(template_conduit, fitting)
    copy_parameters(template_conduit, fitting)
    set_method(fitting)
    apply_color_to_created_element(fitting, color_context)

    return fitting


def copy_conduit_diameter(source, target):
    try:
        source_parameter = source.get_Parameter(
            BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM
        )
        target_parameter = target.get_Parameter(
            BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM
        )
        if (
            source_parameter is not None
            and target_parameter is not None
            and not target_parameter.IsReadOnly
        ):
            target_parameter.Set(source_parameter.AsDouble())
    except Exception:
        pass


def set_method(element):
    parameter = element.LookupParameter(METHOD_PARAM)
    if parameter is None or parameter.IsReadOnly:
        return
    try:
        parameter.Set(METHOD_VALUE)
    except Exception:
        pass


# ============ 2. ЦВЕТ НА АКТИВНОМ ВИДЕ ============

def is_valid_graphic_color(color):
    if color is None:
        return False

    try:
        return bool(color.IsValid)
    except Exception:
        pass

    try:
        int(color.Red)
        int(color.Green)
        int(color.Blue)
        return True
    except Exception:
        return False


def color_to_text(color):
    if not is_valid_graphic_color(color):
        return u'не найден'
    return u'RGB({0}, {1}, {2})'.format(
        int(color.Red),
        int(color.Green),
        int(color.Blue),
    )


def get_color_from_override_settings(settings):
    if settings is None:
        return None, None

    for property_name in COLOR_PROPERTY_PRIORITY:
        try:
            color = getattr(settings, property_name)
        except Exception:
            continue

        if is_valid_graphic_color(color):
            return color, property_name

    return None, None


def get_system_type_graphic_color(element):
    system_parameter = get_system_type_parameter(element)
    if (
        system_parameter is None
        or not system_parameter.HasValue
        or system_parameter.StorageType != StorageType.ElementId
    ):
        return None, None, None

    try:
        system_type = doc.GetElement(system_parameter.AsElementId())
    except Exception:
        system_type = None

    if system_type is None:
        return None, None, None

    for property_name in ('LineColor', 'FillColor'):
        if property_name == 'FillColor':
            try:
                if not system_type.FillVisible:
                    continue
            except Exception:
                pass

        try:
            color = getattr(system_type, property_name)
        except Exception:
            continue

        if is_valid_graphic_color(color):
            try:
                system_name = system_type.Name
            except Exception:
                system_name = u'ID {0}'.format(
                    system_type.Id.IntegerValue
                )
            return color, property_name, system_name

    return None, None, None


def element_ids_are_equal(first, second):
    try:
        return first.IntegerValue == second.IntegerValue
    except Exception:
        return first == second


def filter_contains_element(filter_element, element):
    if filter_element is None or element is None:
        return False

    try:
        filter_category_ids = list(filter_element.GetCategories())
    except Exception:
        filter_category_ids = None

    if filter_category_ids is not None:
        try:
            element_category_id = element.Category.Id
        except Exception:
            return False

        category_is_allowed = any(
            element_ids_are_equal(category_id, element_category_id)
            for category_id in filter_category_ids
        )
        if not category_is_allowed:
            return False

    try:
        element_filter = filter_element.GetElementFilter()
    except Exception:
        element_filter = None

    if element_filter is not None:
        try:
            return bool(element_filter.PassesFilter(doc, element.Id))
        except Exception:
            try:
                return bool(element_filter.PassesFilter(element))
            except Exception:
                return False

    try:
        selected_ids = filter_element.GetElementIds()
    except Exception:
        selected_ids = None

    if selected_ids is not None:
        for selected_id in selected_ids:
            if element_ids_are_equal(selected_id, element.Id):
                return True

    return False


def get_ordered_filter_ids(view):
    try:
        return list(view.GetOrderedFilters())
    except Exception:
        try:
            return list(view.GetFilters())
        except Exception:
            return []


def filter_is_active_and_visible(view, filter_id):
    try:
        if not view.GetIsFilterEnabled(filter_id):
            return False
    except Exception:
        pass

    try:
        if not view.GetFilterVisibility(filter_id):
            return False
    except Exception:
        pass

    return True


def get_filter_hosts(view):
    hosts = []
    seen_ids = set()

    for candidate in (view,):
        if candidate is None:
            continue
        try:
            candidate_id = candidate.Id.IntegerValue
        except Exception:
            candidate_id = id(candidate)
        if candidate_id not in seen_ids:
            seen_ids.add(candidate_id)
            hosts.append(candidate)

    try:
        template_id = view.ViewTemplateId
        if template_id != ElementId.InvalidElementId:
            template = doc.GetElement(template_id)
        else:
            template = None
    except Exception:
        template = None

    if template is not None:
        try:
            template_key = template.Id.IntegerValue
        except Exception:
            template_key = id(template)
        if template_key not in seen_ids:
            hosts.append(template)

    return hosts


def get_named_applied_filter(view, filter_name):
    expected_name = normalize_network_type(filter_name)
    matches = []
    seen_filter_ids = set()

    for host in get_filter_hosts(view):
        for filter_id in get_ordered_filter_ids(host):
            try:
                filter_key = filter_id.IntegerValue
            except Exception:
                continue

            if filter_key in seen_filter_ids:
                continue

            filter_element = doc.GetElement(filter_id)
            if filter_element is None:
                continue

            try:
                actual_name = filter_element.Name
            except Exception:
                continue

            if normalize_network_type(actual_name) != expected_name:
                continue

            seen_filter_ids.add(filter_key)
            matches.append({
                'filter_id': filter_id,
                'filter_name': actual_name,
                'host_id': host.Id,
                'host_name': getattr(host, 'Name', u''),
                'host_is_template': bool(getattr(host, 'IsTemplate', False)),
            })

    if not matches:
        return None

    if len(matches) > 1:
        ids_text = u', '.join(
            unicode(item['filter_id'].IntegerValue)
            for item in matches
        )
        raise Exception(
            u'На виде/шаблоне найдено несколько фильтров '
            u'с именем «{0}»: ID {1}.'.format(filter_name, ids_text)
        )

    return matches[0]


def filter_is_applied_to_view(view, filter_id):
    try:
        return bool(view.IsFilterApplied(filter_id))
    except Exception:
        return any(
            element_ids_are_equal(applied_id, filter_id)
            for applied_id in get_ordered_filter_ids(view)
        )


def refresh_active_view():
    try:
        uidoc.RefreshActiveView()
    except Exception:
        pass


def begin_clean_view_mode(view, source_conduit=None, hide_conduits=True):
    """Скрывает несовместимые элементы и (по режиму) короба"""
    if view is None:
        raise Exception(u'Нет активного вида.')

    try:
        if view.IsTemplate:
            raise Exception(u'На шаблоне вида нельзя выбирать модели.')
    except AttributeError:
        pass

    filter_info = None
    if hide_conduits:
        filter_info = get_named_applied_filter(
            view,
            CONDUIT_VISIBILITY_FILTER_NAME,
        )
        if filter_info is None:
            raise Exception(
                u'На активном виде или в его шаблоне не найден '
                u'фильтр «{0}».'.format(CONDUIT_VISIBILITY_FILTER_NAME)
            )

    hide_report = collect_incompatible_network_type_ids(view, source_conduit)

    if filter_info is None and not hide_report['hidden_ids']:
        print_hide_report(hide_report)
        return None

    try:
        temporary_mode_was_active = bool(
            view.IsTemporaryViewPropertiesModeEnabled()
        )
    except Exception:
        temporary_mode_was_active = False

    state = {
        'view_id': view.Id,
        'filter_id': filter_info['filter_id'] if filter_info else None,
        'filter_name': (
            filter_info['filter_name'] if filter_info
            else CONDUIT_VISIBILITY_FILTER_NAME
        ),
        'host_name': filter_info['host_name'] if filter_info else u'',
        'host_is_template': (
            filter_info['host_is_template'] if filter_info else False
        ),
        'filter_handled': filter_info is not None,
        'temporary_mode_was_active': temporary_mode_was_active,
        'temporary_mode_enabled_by_script': False,
        'original_visibility': None,
        'original_enabled': None,
        'hidden_ids': [],
        'hide_report': hide_report,
    }

    with revit.Transaction(u'Временно скрыть несовместимые элементы'):
        if not temporary_mode_was_active:
            enabled = view.EnableTemporaryViewPropertiesMode(view.Id)
            try:
                mode_is_active = bool(
                    view.IsTemporaryViewPropertiesModeEnabled()
                )
            except Exception:
                mode_is_active = bool(enabled)

            if not enabled and not mode_is_active:
                raise Exception(
                    u'Revit не смог включить «Временные свойства вида».'
                )
            state['temporary_mode_enabled_by_script'] = True

        if filter_info is not None:
            if not filter_is_applied_to_view(view, state['filter_id']):
                raise Exception(
                    u'Фильтр «{0}» найден, но не применён '
                    u'к активному виду.'.format(state['filter_name'])
                )

            state['original_visibility'] = view.GetFilterVisibility(
                state['filter_id']
            )
            try:
                state['original_enabled'] = view.GetIsFilterEnabled(
                    state['filter_id']
                )
                view.SetIsFilterEnabled(state['filter_id'], True)
            except Exception:
                state['original_enabled'] = None

            view.SetFilterVisibility(state['filter_id'], False)
            if view.GetFilterVisibility(state['filter_id']):
                raise Exception(
                    u'Revit не принял временное скрытие фильтра.'
                )

        if hide_report['hidden_ids']:
            hide_report['errors'] = hide_elements_temporarily(
                view,
                hide_report['hidden_ids'],
            )
            if hide_report['errors']:
                # Скрыть не удалось: восстанавливать будет нечего.
                hide_report['hidden_ids'] = []
            else:
                state['hidden_ids'] = list(hide_report['hidden_ids'])

    print_hide_report(hide_report)
    refresh_active_view()
    return state


def restore_clean_view_mode(state):
    """Возвращает видимость после выбора"""
    if not state:
        return

    view = doc.GetElement(state['view_id'])
    if view is None:
        raise Exception(
            u'Не удалось получить вид для восстановления фильтра «{0}».'
            .format(state.get('filter_name', CONDUIT_VISIBILITY_FILTER_NAME))
        )

    hidden_ids = list(state.get('hidden_ids') or [])
    remaining = []

    with revit.Transaction(u'Вернуть видимость коробов'):
        if state.get('temporary_mode_enabled_by_script'):
            restored = view.EnableTemporaryViewPropertiesMode(
                ElementId.InvalidElementId
            )
            try:
                mode_is_active = bool(
                    view.IsTemporaryViewPropertiesModeEnabled()
                )
            except Exception:
                mode_is_active = not bool(restored)

            if not restored and mode_is_active:
                raise Exception(
                    u'Revit не смог выключить «Временные свойства вида».'
                )
        elif state.get('filter_handled'):
            filter_id = state['filter_id']
            if not filter_is_applied_to_view(view, filter_id):
                raise Exception(
                    u'Фильтр «{0}» был удалён с вида во время выбора.'
                    .format(state['filter_name'])
                )

            view.SetFilterVisibility(
                filter_id,
                bool(state['original_visibility']),
            )
            if state.get('original_enabled') is not None:
                view.SetIsFilterEnabled(
                    filter_id,
                    bool(state['original_enabled']),
                )

        if hidden_ids:
            doc.Regenerate()
            remaining = unhide_elements_in_view(view, hidden_ids)

    if remaining:
        print(
            u'Не удалось вернуть видимость элементов: {0}.'.format(
                u', '.join(
                    unicode(element_id.IntegerValue)
                    for element_id in remaining
                )
            )
        )

    refresh_active_view()


def create_color_context(source_element, view):
    context = {
        'view': view,
        'color': None,
        'source': u'цвет не найден',
        'channel': None,
        'applied_ids': set(),
        'failed_ids': set(),
        'errors': [],
    }

    if source_element is None or view is None:
        return context

    try:
        settings = view.GetElementOverrides(source_element.Id)
        color, channel = get_color_from_override_settings(settings)
        if color is not None:
            context.update({
                'color': color,
                'source': u'переопределение выбранного короба',
                'channel': channel,
            })
            return context
    except Exception:
        pass

    checked_filter_keys = set()
    for host in get_filter_hosts(view):
        try:
            host_key = host.Id.IntegerValue
        except Exception:
            host_key = id(host)

        for filter_id in get_ordered_filter_ids(host):
            try:
                filter_key = filter_id.IntegerValue
            except Exception:
                filter_key = unicode(filter_id)

            host_filter_key = (host_key, filter_key)
            if host_filter_key in checked_filter_keys:
                continue
            checked_filter_keys.add(host_filter_key)

            if not filter_is_active_and_visible(host, filter_id):
                continue

            filter_element = doc.GetElement(filter_id)
            if not filter_contains_element(filter_element, source_element):
                continue

            try:
                settings = host.GetFilterOverrides(filter_id)
            except Exception:
                continue

            color, channel = get_color_from_override_settings(settings)
            if color is None:
                continue

            try:
                filter_name = filter_element.Name
            except Exception:
                filter_name = u'ID {0}'.format(filter_key)

            try:
                is_template = bool(host.IsTemplate)
            except Exception:
                is_template = False

            owner_text = (
                u'шаблон вида' if is_template else u'активный вид'
            )
            context.update({
                'color': color,
                'source': u'фильтр «{0}» ({1})'.format(
                    filter_name,
                    owner_text,
                ),
                'channel': channel,
            })
            return context

    color, channel, system_name = get_system_type_graphic_color(
        source_element
    )
    if color is not None:
        context.update({
            'color': color,
            'source': u'Тип системы «{0}»'.format(system_name),
            'channel': channel,
        })
        return context

    for host in get_filter_hosts(view):
        try:
            settings = host.GetCategoryOverrides(source_element.Category.Id)
        except Exception:
            continue

        color, channel = get_color_from_override_settings(settings)
        if color is not None:
            try:
                is_template = bool(host.IsTemplate)
            except Exception:
                is_template = False
            context.update({
                'color': color,
                'source': (
                    u'переопределение категории в шаблоне вида'
                    if is_template
                    else u'переопределение категории на активном виде'
                ),
                'channel': channel,
            })
            return context

    return context


def make_color_override_settings(color):
    settings = OverrideGraphicSettings()
    applied_setters = 0

    for setter_name in COLOR_OVERRIDE_SETTERS:
        try:
            setter = getattr(settings, setter_name)
            setter(color)
            applied_setters += 1
        except Exception:
            pass

    if applied_setters == 0:
        raise Exception(
            u'Revit API не принял ни один графический канал RGB.'
        )

    return settings


def apply_color_to_created_element(element, color_context):
    if element is None or not color_context:
        return False

    color = color_context.get('color')
    view = color_context.get('view')
    if color is None or view is None:
        return False

    try:
        settings = make_color_override_settings(color)
        view.SetElementOverrides(element.Id, settings)
        color_context['applied_ids'].add(element.Id.IntegerValue)
        return True
    except Exception as error:
        try:
            element_id = element.Id.IntegerValue
        except Exception:
            element_id = -1

        color_context['failed_ids'].add(element_id)
        if len(color_context['errors']) < 10:
            color_context['errors'].append(
                u'ID {0}: {1}'.format(element_id, error)
            )
        return False


# ============ 3. СОЕДИНИТЕЛИ ============

def get_raw_connectors(element):
    if element is None:
        return []

    try:
        mep_model = getattr(element, 'MEPModel', None)
        if mep_model is not None:
            connector_manager = mep_model.ConnectorManager
            if connector_manager is not None:
                return list(connector_manager.Connectors)
    except Exception:
        pass

    try:
        connector_manager = getattr(element, 'ConnectorManager', None)
        if connector_manager is not None:
            return list(connector_manager.Connectors)
    except Exception:
        pass

    return []


def is_conduit_connector(connector):
    try:
        if connector.Domain != Domain.DomainCableTrayConduit:
            return False
    except Exception:
        return False

    try:
        if connector.ConnectorType == ConnectorType.Logical:
            return False
    except Exception:
        pass

    return True


def get_conduit_connectors(element):
    return [
        connector
        for connector in get_raw_connectors(element)
        if is_conduit_connector(connector)
    ]


def get_family_root(element, diagnostics=None):
    current = element
    visited_ids = set()

    while isinstance(current, FamilyInstance):
        current_id = current.Id.IntegerValue
        if current_id in visited_ids:
            break
        visited_ids.add(current_id)

        try:
            parent = current.SuperComponent
        except Exception as error:
            if diagnostics is not None:
                diagnostics.append(
                    u'ID {0}: ошибка чтения SuperComponent: {1}'.format(
                        current_id, error
                    )
                )
            parent = None

        if parent is None:
            break
        current = parent

    return current


def get_element_and_subcomponents(element, diagnostics=None):
    result = []
    queue = [get_family_root(element, diagnostics)]
    visited_ids = set()

    while queue:
        current = queue.pop(0)
        if current is None:
            continue

        current_id = current.Id.IntegerValue
        if current_id in visited_ids:
            continue

        visited_ids.add(current_id)
        result.append(current)

        if not isinstance(current, FamilyInstance):
            continue

        try:
            for sub_id in current.GetSubComponentIds():
                sub_element = doc.GetElement(sub_id)
                if sub_element is not None:
                    queue.append(sub_element)
        except Exception as error:
            if diagnostics is not None:
                diagnostics.append(
                    u'ID {0}: ошибка чтения GetSubComponentIds: {1}'.format(
                        current_id, error
                    )
                )

    return result


def get_model_conduit_connectors(element):
    connectors = []
    for candidate in get_element_and_subcomponents(element):
        connectors.extend(get_conduit_connectors(candidate))
    return connectors


def get_internal_owner_ids(element):
    return set(
        candidate.Id.IntegerValue
        for candidate in get_element_and_subcomponents(element)
    )


def connectors_are_same(first, second):
    try:
        if first.Owner.Id != second.Owner.Id:
            return False
    except Exception:
        return False

    try:
        return first.Id == second.Id
    except Exception:
        pass

    try:
        if first.Origin.DistanceTo(second.Origin) > POINT_TOLERANCE:
            return False
    except Exception:
        return False

    try:
        return first.ConnectorType == second.ConnectorType
    except Exception:
        return True


def get_external_physical_references(connector, internal_owner_ids=None):
    if internal_owner_ids is None:
        internal_owner_ids = set()
    else:
        internal_owner_ids = set(internal_owner_ids)

    try:
        own_id = connector.Owner.Id.IntegerValue
        internal_owner_ids.add(own_id)
    except Exception:
        pass

    try:
        references = list(connector.AllRefs)
    except Exception:
        return None

    external_references = []
    seen_keys = set()

    for reference in references:
        if reference is None:
            continue

        try:
            owner = reference.Owner
            owner_id = owner.Id.IntegerValue
        except Exception:
            continue

        if owner_id in internal_owner_ids:
            continue

        if not is_conduit_connector(reference):
            continue

        try:
            reference_id = reference.Id
        except Exception:
            reference_id = None

        key = (owner_id, unicode(reference_id))
        if key in seen_keys:
            continue

        seen_keys.add(key)
        external_references.append(reference)

    return external_references


def connector_is_free(connector, internal_owner_ids=None):
    external_references = get_external_physical_references(
        connector,
        internal_owner_ids,
    )

    if external_references is not None:
        return len(external_references) == 0

    try:
        return not connector.IsConnected
    except Exception:
        return False


def filter_available_connectors(connectors, internal_owner_ids=None):
    if not USE_ONLY_FREE_CONNECTORS:
        return list(connectors)
    return [
        connector
        for connector in connectors
        if connector_is_free(connector, internal_owner_ids)
    ]


def get_external_owner_ids(connector, internal_owner_ids=None):
    references = get_external_physical_references(
        connector,
        internal_owner_ids,
    )
    if references is None:
        return []

    result = []
    for reference in references:
        try:
            owner_id = reference.Owner.Id.IntegerValue
        except Exception:
            continue
        if owner_id not in result:
            result.append(owner_id)
    return result


def format_connector_diagnostics(connectors, internal_owner_ids=None):
    lines = []
    max_lines = 20

    for index, connector in enumerate(connectors[:max_lines]):
        try:
            raw_connected = connector.IsConnected
        except Exception:
            raw_connected = None

        owner_ids = get_external_owner_ids(
            connector,
            internal_owner_ids,
        )

        if raw_connected is True:
            raw_text = u'да'
        elif raw_connected is False:
            raw_text = u'нет'
        else:
            raw_text = u'н/д'

        if owner_ids:
            owners_text = u', '.join(unicode(value) for value in owner_ids)
        else:
            owners_text = u'нет'

        lines.append(
            u'#{0}: IsConnected={1}; внешние владельцы: {2}'.format(
                index + 1,
                raw_text,
                owners_text,
            )
        )

    if len(connectors) > max_lines:
        lines.append(
            u'… и ещё {0} соединителей'.format(
                len(connectors) - max_lines
            )
        )

    return u'\n'.join(lines)


def connectors_are_connected(first, second):
    try:
        if first.IsConnectedTo(second):
            return True
    except Exception:
        pass

    try:
        for reference in first.AllRefs:
            if connectors_are_same(reference, second):
                return True
    except Exception:
        pass

    return False


def find_nearest_connector_pair(source_connectors, target_connectors):
    best_pair = None
    best_distance = None

    for source_connector in source_connectors:
        for target_connector in target_connectors:
            try:
                distance = source_connector.Origin.DistanceTo(
                    target_connector.Origin
                )
            except Exception:
                continue

            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_pair = (source_connector, target_connector)

    return best_pair, best_distance


def get_closest_connector(element, point):
    best_connector = None
    best_distance = None

    for connector in get_conduit_connectors(element):
        try:
            distance = connector.Origin.DistanceTo(point)
        except Exception:
            continue

        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_connector = connector

    return best_connector


# ============ 3.1 СКРЫТИЕ НЕСОВМЕСТИМЫХ ПО ТИПУ СЕТИ ============

def make_element_id_collection(element_ids):
    collection = List[ElementId]()
    for element_id in element_ids:
        if element_id is None:
            continue
        collection.Add(element_id)
    return collection


def get_element_network_type_groups(element):
    if element is None:
        return {}

    try:
        members = get_element_and_subcomponents(element)
    except Exception:
        members = [element]

    return group_network_type_entries(
        collect_network_type_entries(members)
    )


def get_source_network_type_key(source_conduit):
    groups = get_element_network_type_groups(source_conduit)
    if len(groups) != 1:
        return None, groups
    return next(iter(groups)), groups


def element_exposes_conduit_connectors(element):
    try:
        if (
            getattr(element, 'MEPModel', None) is None
            and getattr(element, 'ConnectorManager', None) is None
        ):
            return False
    except Exception:
        return False

    return len(get_model_conduit_connectors(element)) > 0


def collect_visible_view_targets(view):
    conduits, models = [], []
    if view is None:
        return conduits, models

    try:
        conduits = list(
            FilteredElementCollector(doc, view.Id)
            .OfCategory(BuiltInCategory.OST_Conduit)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        conduits = []

    try:
        instances = list(
            FilteredElementCollector(doc, view.Id)
            .OfClass(FamilyInstance)
            .WhereElementIsNotElementType()
            .ToElements()
        )
    except Exception:
        instances = []

    for instance in instances:
        if element_exposes_conduit_connectors(instance):
            models.append(instance)

    return conduits, models


def classify_network_type_match(element, source_key):
    groups = get_element_network_type_groups(element)
    if not groups:
        return 'missing'
    if len(groups) > 1 or source_key not in groups:
        return 'mismatch'
    return 'match'


def collect_incompatible_network_type_ids(view, source_conduit):
    report = {
        'enabled': False,
        'value': None,
        'source_id': None,
        'hidden_ids': [],
        'hidden_conduits': 0,
        'hidden_models': 0,
        'kept_conduits': 0,
        'kept_models': 0,
        'without_type': 0,
        'errors': [],
        'reason': u'',
    }

    if not HIDE_INCOMPATIBLE_BY_NETWORK_TYPE:
        report['reason'] = (
            u'Скрытие несовместимых отключено в настройках скрипта.'
        )
        return report

    if source_conduit is None:
        report['reason'] = u'Короб для сравнения не передан.'
        return report

    source_key, source_groups = get_source_network_type_key(source_conduit)
    if source_key is None:
        report['reason'] = (
            u'У выбранного короба нет однозначного «Типа сети»; '
            u'скрытие несовместимых элементов пропущено.'
        )
        return report

    report['enabled'] = True
    report['value'] = source_groups[source_key][0]['value']
    try:
        report['source_id'] = source_conduit.Id.IntegerValue
    except Exception:
        report['source_id'] = None

    conduits, models = collect_visible_view_targets(view)

    for kind, elements in (('conduits', conduits), ('models', models)):
        for element in elements:
            try:
                element_id = element.Id.IntegerValue
            except Exception:
                continue

            if element_id == report['source_id']:
                continue

            classification = classify_network_type_match(
                element,
                source_key,
            )

            if classification == 'match':
                hide = False
            elif classification == 'missing':
                report['without_type'] += 1
                hide = bool(HIDE_ELEMENTS_WITHOUT_NETWORK_TYPE)
            else:
                hide = True

            if hide:
                report['hidden_ids'].append(element.Id)
                if kind == 'conduits':
                    report['hidden_conduits'] += 1
                else:
                    report['hidden_models'] += 1
            else:
                if kind == 'conduits':
                    report['kept_conduits'] += 1
                else:
                    report['kept_models'] += 1

    report['reason'] = u'Тип сети короба: «{0}».'.format(report['value'])
    return report


def hide_elements_temporarily(view, element_ids):
    if view is None or not element_ids:
        return []

    collection = make_element_id_collection(element_ids)
    if collection.Count == 0:
        return []

    try:
        view.HideElementsTemporary(collection)
    except Exception as error:
        return [
            u'HideElementsTemporary: {0}'.format(error),
        ]

    return []


def unhide_elements_in_view(view, element_ids):
    remaining = []
    if view is None or not element_ids:
        return remaining

    pending = []
    for element_id in element_ids:
        element = doc.GetElement(element_id)
        if element is None:
            continue
        try:
            if element.IsHidden(view):
                pending.append(element_id)
        except Exception:
            pending.append(element_id)

    if not pending:
        return remaining

    try:
        view.UnhideElements(make_element_id_collection(pending))
    except Exception:
        pass

    still_hidden = []
    for element_id in pending:
        element = doc.GetElement(element_id)
        try:
            if element is not None and element.IsHidden(view):
                still_hidden.append(element_id)
        except Exception:
            still_hidden.append(element_id)

    if still_hidden:
        # Снимает остатки временного скрытия/изоляции активного вида.
        try:
            view.DisableTemporaryViewMode(
                TemporaryViewMode.TemporaryHideIsolate
            )
        except Exception:
            pass

        for element_id in still_hidden:
            element = doc.GetElement(element_id)
            try:
                if element is not None and element.IsHidden(view):
                    remaining.append(element_id)
            except Exception:
                pass

    return remaining


def print_hide_report(report):
    if not report:
        return

    if not report.get('enabled'):
        print(
            u'Скрытие несовместимых по «Типу сети»: {0}'.format(
                report.get('reason', u'н/д')
            )
        )
        return

    print(
        u'Скрытие несовместимых по «Типу сети»: {0} '
        u'(короба: скрыто {1}, оставлено {2}; модели: скрыто {3}, '
        u'оставлено {4}; без «Типа сети»: {5}).'.format(
            report.get('reason', u'н/д'),
            report.get('hidden_conduits', 0),
            report.get('kept_conduits', 0),
            report.get('hidden_models', 0),
            report.get('kept_models', 0),
            report.get('without_type', 0),
        )
    )

    if report.get('kept_models', 0) == 0:
        print(
            u'ВНИМАНИЕ: моделей с подходящим «Типом сети» на виде '
            u'не осталось — выбор цели может быть невозможен.'
        )

    for error in report.get('errors', []):
        print(u'  - {0}'.format(error))


# ============ 4. ДОВОДКА СВОБОДНОГО КОНЦА ============

def connector_identity(connector):
    return connector.Owner.Id, connector.Id


def refresh_connector(identity, expected_point):
    owner_id, connector_id = identity
    owner = doc.GetElement(owner_id)
    if owner is not None:
        for connector in get_conduit_connectors(owner):
            if connector.Id == connector_id:
                if connector.Origin.DistanceTo(expected_point) > (
                    POSITION_CHECK_TOLERANCE
                ):
                    raise Exception(
                        u'Соединитель ID {0} элемента ID {1} сместился '
                        u'относительно ожидаемой точки.'.format(
                            connector_id, owner_id.IntegerValue
                        )
                    )
                return connector
    raise Exception(
        u'После изменения геометрии не найден прежний соединитель '
        u'ID {0} элемента ID {1}.'.format(connector_id, owner_id.IntegerValue)
    )


def physical_connection_keys(connector):
    references = get_external_physical_references(connector)
    if references is None:
        raise Exception(u'Не удалось проверить существующие соединения коннектора.')
    return set(
        (reference.Owner.Id.IntegerValue, reference.Id)
        for reference in references
    )


def remember_connector_position(connector):
    state = {
        'identity': connector_identity(connector),
        'origin': connector.Origin,
        'direction': get_connector_direction(connector),
        'connections': physical_connection_keys(connector),
    }
    root = get_family_root(connector.Owner)
    if isinstance(root, FamilyInstance):
        transform = root.GetTransform()
        state['family_id'] = root.Id
        state['family_transform'] = (
            transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ
        )
    return state


def check_connector_position(state):
    connector = refresh_connector(state['identity'], state['origin'])
    direction = get_connector_direction(connector)
    if state['direction'] is not None and (
        direction is None or direction.DistanceTo(state['direction']) > 1e-6
    ):
        raise Exception(u'Изменилось направление ранее зафиксированного коннектора.')
    if not state['connections'].issubset(physical_connection_keys(connector)):
        raise Exception(u'Revit изменил существующее соединение. Попытка отменяется.')
    if 'family_id' in state:
        root = doc.GetElement(state['family_id'])
        transform = root.GetTransform()
        values = (transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ)
        for index, (before, after) in enumerate(zip(state['family_transform'], values)):
            tolerance = POSITION_CHECK_TOLERANCE if index == 0 else 1e-6
            if before.DistanceTo(after) > tolerance:
                raise Exception(u'Revit переместил или повернул модель. Попытка отменяется.')
    return connector


def extend_conduit_along_axis(
    conduit, pt1, target_pt, norm1=None, report=None, target_direction=None
):
    if report is None:
        report = {}
    report.update({'before': pt1, 'after': pt1, 'target': target_pt, 'moved': False})
    if not AUTO_EXTEND_CONDUIT:
        report['reason'] = u'Доводка отключена в настройках.'
        return pt1

    location = conduit.Location
    curve = getattr(location, 'Curve', None)
    if not isinstance(curve, Line):
        report['reason'] = u'Доводка пропущена: исходный короб не является прямой линией.'
        return pt1

    p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
    end_index = 0 if p0.DistanceTo(pt1) < p1.DistanceTo(pt1) else 1
    active_point, fixed_point = (p0, p1) if end_index == 0 else (p1, p0)
    active_connector = get_closest_connector(conduit, pt1)
    if (
        active_point.DistanceTo(pt1) > POINT_TOLERANCE
        or active_connector is None
        or active_connector.Origin.DistanceTo(pt1) > POINT_TOLERANCE
        or active_connector.ConnectorType != ConnectorType.End
    ):
        raise Exception(u'Для доводки нужен концевой соединитель исходного короба.')
    if active_connector.IsConnected or not connector_is_free(active_connector):
        raise Exception(u'Выбранный конец короба занят. Доводка без отсоединения невозможна.')

    fixed_connector = get_closest_connector(conduit, fixed_point)
    if fixed_connector is None or fixed_connector.Origin.DistanceTo(fixed_point) > POINT_TOLERANCE:
        raise Exception(u'Не удалось проверить противоположный конец исходного короба.')
    report['fixed_state'] = remember_connector_position(fixed_connector)

    axis = (active_point - fixed_point).Normalize()
    projected_length = (target_pt - fixed_point).DotProduct(axis)
    minimum_length = max(
        MIN_RETAINED_CONDUIT_LENGTH,
        doc.Application.ShortCurveTolerance * 1.01,
    )
    if projected_length < minimum_length:
        report['reason'] = (
            u'Доводка пропущена: проекция за противоположным концом '
            u'либо остаток короба короче {0:.1f} мм.'
        ).format(minimum_length * 304.8)
        return pt1

    new_end = fixed_point + axis * projected_length
    if (
        target_direction is not None
        and new_end.DistanceTo(target_pt) <= POINT_TOLERANCE
        and axis.DotProduct(target_direction) > -CONNECTOR_ALIGNMENT_TOLERANCE
    ):
        report['reason'] = (
            u'Доводка пропущена: в точке модели оси соединителей не совпадают; '
            u'оставлено место для прежнего маршрута с отводом.'
        )
        return pt1
    if new_end.DistanceTo(active_point) <= POSITION_CHECK_TOLERANCE:
        report['reason'] = u'Свободный конец уже доведён до проекции коннектора.'
        return pt1
    if conduit.Pinned or getattr(location, 'IsReadOnly', False):
        report['reason'] = u'Доводка пропущена: короб закреплён или его геометрия недоступна для записи.'
        return pt1
    if conduit.GroupId != ElementId.InvalidElementId:
        report['reason'] = u'Доводка пропущена: короб входит в группу модели.'
        return pt1

    active_identity = connector_identity(active_connector)
    if end_index == 0:
        location.Curve = Line.CreateBound(new_end, fixed_point)
    else:
        location.Curve = Line.CreateBound(fixed_point, new_end)
    doc.Regenerate()
    actual_end = refresh_connector(active_identity, new_end).Origin
    check_connector_position(report['fixed_state'])
    report.update({
        'after': actual_end,
        'moved': True,
        'reason': u'Свободный конец доведён вдоль оси; противоположный конец сохранён.',
    })
    return actual_end


# ============ 5. СОЗДАНИЕ И СОЕДИНЕНИЕ ============

def get_conduit_level_id(conduit):
    try:
        reference_level = conduit.ReferenceLevel
        if reference_level is not None:
            return reference_level.Id
    except Exception:
        pass

    try:
        level_id = conduit.LevelId
        if level_id is not None and level_id != ElementId.InvalidElementId:
            return level_id
    except Exception:
        pass

    try:
        parameter = conduit.get_Parameter(
            BuiltInParameter.RBS_START_LEVEL_PARAM
        )
        if parameter is not None:
            level_id = parameter.AsElementId()
            if level_id != ElementId.InvalidElementId:
                return level_id
    except Exception:
        pass

    return ElementId.InvalidElementId


def create_conduit_segment(
    template,
    start_point,
    end_point,
    color_context=None,
):
    if start_point.DistanceTo(end_point) <= POINT_TOLERANCE:
        return None

    level_id = get_conduit_level_id(template)
    if level_id == ElementId.InvalidElementId:
        raise Exception(u'Не удалось определить уровень выбранного короба.')

    result = Conduit.Create(
        doc,
        template.GetTypeId(),
        start_point,
        end_point,
        level_id,
    )

    if isinstance(result, ElementId):
        new_conduit = doc.GetElement(result)
    else:
        new_conduit = result

    if new_conduit is None:
        raise Exception(u'Revit не создал новый участок короба.')

    copy_system_type(template, new_conduit)
    copy_conduit_diameter(template, new_conduit)
    copy_parameters(template, new_conduit)
    set_method(new_conduit)
    apply_color_to_created_element(new_conduit, color_context)
    return new_conduit


def get_connector_direction(connector):
    try:
        direction = connector.CoordinateSystem.BasisZ
        if direction.GetLength() <= 1e-9:
            return None
        return direction.Normalize()
    except Exception:
        return None


def get_route_connector(conduit, point):
    connector = get_closest_connector(conduit, point)
    if connector is None:
        raise Exception(u'Не найден соединитель созданного участка короба.')
    return connector


def connect_family_to_aligned_conduit(
    family_connector,
    conduit_connector,
    color_context=None,
):
    try:
        distance = family_connector.Origin.DistanceTo(
            conduit_connector.Origin
        )
    except Exception:
        return False

    if distance > POINT_TOLERANCE:
        return False

    family_direction = get_connector_direction(family_connector)
    conduit_direction = get_connector_direction(conduit_connector)
    if family_direction is None or conduit_direction is None:
        return False

    if family_direction.DotProduct(conduit_direction) > (
        -CONNECTOR_ALIGNMENT_TOLERANCE
    ):
        return False

    if connectors_are_connected(family_connector, conduit_connector):
        return True

    try:
        family_connector.ConnectTo(conduit_connector)
        if connectors_are_connected(family_connector, conduit_connector):
            return True
    except Exception:
        pass

    try:
        fitting = doc.Create.NewUnionFitting(
            family_connector,
            conduit_connector,
        )
    except Exception:
        return False

    if fitting is None:
        return False

    configure_created_conduit_fitting(
        fitting,
        conduit_connector.Owner,
        color_context,
    )
    return True


def try_create_fitting(first, second, color_context=None):
    try:
        first_direction = first.CoordinateSystem.BasisZ.Normalize()
        second_direction = second.CoordinateSystem.BasisZ.Normalize()
        dot_product = first_direction.DotProduct(second_direction)
    except Exception:
        dot_product = 0.0

    fitting_creators = []

    if dot_product < -0.99:
        fitting_creators.append(doc.Create.NewUnionFitting)
        fitting_creators.append(doc.Create.NewTransitionFitting)
        fitting_creators.append(doc.Create.NewElbowFitting)
    else:
        fitting_creators.append(doc.Create.NewElbowFitting)
        fitting_creators.append(doc.Create.NewTransitionFitting)
        fitting_creators.append(doc.Create.NewUnionFitting)

    for creator in fitting_creators:
        try:
            fitting = creator(first, second)
        except Exception:
            continue

        if fitting is None:
            continue

        template_conduit = get_conduit_owner_from_connectors(
            first,
            second,
        )
        configure_created_conduit_fitting(
            fitting,
            template_conduit,
            color_context,
        )
        return True

    return False


def connect_connectors(first, second, color_context=None):
    if connectors_are_connected(first, second):
        return True

    if try_create_fitting(first, second, color_context):
        return True

    try:
        first.ConnectTo(second)
    except Exception:
        return False

    return connectors_are_connected(first, second)


def connect_conduit_to_model(
    source_conduit,
    source_connector,
    target_connector,
    alignment_report=None,
    color_context=None,
):
    source_identity = connector_identity(source_connector)
    target_identity = connector_identity(target_connector)
    end_point = target_connector.Origin
    start_point = extend_conduit_along_axis(
        source_conduit,
        source_connector.Origin,
        end_point,
        get_connector_direction(source_connector),
        alignment_report,
        get_connector_direction(target_connector),
    )
    source_connector = refresh_connector(source_identity, start_point)
    target_connector = refresh_connector(target_identity, end_point)
    distance = start_point.DistanceTo(end_point)

    if distance <= POINT_TOLERANCE:
        if not connect_family_to_aligned_conduit(
            target_connector,
            source_connector,
            color_context,
        ):
            raise Exception(
                u'Соединители совпадают по координатам, но Revit не смог '
                u'соединить их без перемещения семейства.'
            )
        return []

    target_direction = get_connector_direction(target_connector)
    if target_direction is None:
        raise Exception(
            u'Не удалось определить направление соединителя семейства.'
        )

    route_direction = (end_point - start_point).Normalize()
    direct_route_is_aligned = (
        target_direction.DotProduct(route_direction)
        <= -CONNECTOR_ALIGNMENT_TOLERANCE
    )

    created_conduits = []

    if direct_route_is_aligned:
        route_conduit = create_conduit_segment(
            source_conduit,
            start_point,
            end_point,
            color_context,
        )
        created_conduits.append(route_conduit)
        doc.Regenerate()

        route_start_connector = get_route_connector(
            route_conduit,
            start_point,
        )
        route_target_connector = get_route_connector(
            route_conduit,
            end_point,
        )

        if not connect_family_to_aligned_conduit(
            target_connector,
            route_target_connector,
            color_context,
        ):
            raise Exception(
                u'Прямой участок не удалось соединить с моделью без '
                u'изменения её электрической сети.'
            )

        doc.Regenerate()

        if not connect_connectors(
            source_connector,
            route_start_connector,
            color_context,
        ):
            raise Exception(
                u'Не удалось подключить прямой участок к выбранному коробу.'
            )

        return created_conduits

    stub_end_point = end_point + (
        target_direction * TARGET_STUB_LENGTH
    )
    target_stub = create_conduit_segment(
        source_conduit,
        end_point,
        stub_end_point,
        color_context,
    )
    created_conduits.append(target_stub)
    doc.Regenerate()

    stub_family_connector = get_route_connector(
        target_stub,
        end_point,
    )
    stub_route_connector = get_route_connector(
        target_stub,
        stub_end_point,
    )

    if not connect_family_to_aligned_conduit(
        target_connector,
        stub_family_connector,
        color_context,
    ):
        raise Exception(
            u'Не удалось подключить соосный участок к модели. Проверьте '
            u'направление BasisZ «Соединителя коробов» в семействе.'
        )

    doc.Regenerate()

    if start_point.DistanceTo(stub_end_point) <= POINT_TOLERANCE:
        if not connect_connectors(
            source_connector,
            stub_route_connector,
            color_context,
        ):
            raise Exception(
                u'Не удалось соединить короб с соосным участком модели.'
            )
        return created_conduits

    middle_conduit = create_conduit_segment(
        source_conduit,
        start_point,
        stub_end_point,
        color_context,
    )
    created_conduits.append(middle_conduit)
    doc.Regenerate()

    middle_source_connector = get_route_connector(
        middle_conduit,
        start_point,
    )
    middle_stub_connector = get_route_connector(
        middle_conduit,
        stub_end_point,
    )

    if not connect_connectors(
        source_connector,
        middle_source_connector,
        color_context,
    ):
        raise Exception(
            u'Не удалось подключить маршрут к выбранному коробу.'
        )

    doc.Regenerate()

    if not connect_connectors(
        middle_stub_connector,
        stub_route_connector,
        color_context,
    ):
        raise Exception(
            u'Не удалось создать отвод перед соединителем модели.'
        )

    return created_conduits


# ============ 6. ФИЛЬТРЫ ВЫБОРА ============

class ConduitSelectionFilter(Sel.ISelectionFilter):
    def AllowElement(self, element):
        try:
            return (
                element.Category is not None
                and element.Category.Id.IntegerValue
                == int(BuiltInCategory.OST_Conduit)
            )
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


class ModelWithConduitConnectorFilter(Sel.ISelectionFilter):
    def __init__(self, excluded_element_id):
        self.excluded_element_id = excluded_element_id

    def AllowElement(self, element):
        try:
            if element.Id == self.excluded_element_id:
                return False
            return len(get_model_conduit_connectors(element)) > 0
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


# ============ 7. ДИАГНОСТИКА БЕЗ ИЗМЕНЕНИЯ МОДЕЛИ ============

def diagnostic_text(value):
    if value is None:
        return u'н/д'
    if isinstance(value, bool):
        return u'да' if value else u'нет'
    return unicode(value)


def diagnostic_property(element, property_name):
    try:
        return getattr(element, property_name), None
    except Exception as error:
        return None, u'{0}: {1}'.format(property_name, error)


def diagnostic_element_label(element):
    if element is None:
        return u'владелец недоступен'
    element_id, unused = diagnostic_property(element, 'Id')
    id_value, unused = diagnostic_property(element_id, 'IntegerValue')
    name, unused = diagnostic_property(element, 'Name')
    category, unused = diagnostic_property(element, 'Category')
    category_name, unused = diagnostic_property(category, 'Name')
    return u'{0}; категория «{1}»; ID {2}'.format(
        diagnostic_text(name), diagnostic_text(category_name),
        diagnostic_text(id_value),
    )


def diagnostic_connector_key(connector):
    try:
        return (connector.Owner.Id.IntegerValue, unicode(connector.Id))
    except Exception:
        return None


def read_connectors_for_diagnostics(element):
    result, notes, managers, seen_keys = [], [], [], set()
    try:
        mep_model = getattr(element, 'MEPModel', None)
        if mep_model is not None:
            manager = mep_model.ConnectorManager
            if manager is not None:
                managers.append((u'MEPModel.ConnectorManager', manager))
    except Exception as error:
        notes.append(u'Ошибка MEPModel.ConnectorManager: {0}'.format(error))

    try:
        manager = getattr(element, 'ConnectorManager', None)
        if manager is not None:
            managers.append((u'ConnectorManager', manager))
    except Exception as error:
        notes.append(u'Ошибка ConnectorManager: {0}'.format(error))

    if not managers:
        notes.append(u'Нет доступного ConnectorManager на этом экземпляре.')

    for manager_name, manager in managers:
        try:
            connectors = list(manager.Connectors)
        except Exception as error:
            notes.append(u'{0}.Connectors: {1}'.format(manager_name, error))
            continue
        notes.append(u'{0}: соединителей {1}'.format(
            manager_name, len(connectors)
        ))
        for connector in connectors:
            key = diagnostic_connector_key(connector)
            if key is not None:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            result.append(connector)
    return result, notes


def diagnose_reference(connector, reference, internal_ids):
    owner, owner_error = diagnostic_property(reference, 'Owner')
    ref_id, id_error = diagnostic_property(reference, 'Id')
    domain, domain_error = diagnostic_property(reference, 'Domain')
    kind, kind_error = diagnostic_property(reference, 'ConnectorType')
    owner_id = None
    if owner is not None:
        try:
            owner_id = owner.Id.IntegerValue
        except Exception:
            pass

    if owner_id is None:
        decision = u'пропущена: не прочитан владелец'
    elif owner_id in internal_ids:
        decision = u'пропущена текущим алгоритмом: владелец внутри семейства'
    elif domain_error or domain != Domain.DomainCableTrayConduit:
        decision = u'пропущена: другой или непрочитанный домен'
    elif kind == ConnectorType.Logical:
        decision = u'пропущена: логическая ссылка'
    else:
        decision = u'УЧТЕНА текущим алгоритмом как внешняя связь'

    try:
        linked = diagnostic_text(bool(connector.IsConnectedTo(reference)))
    except Exception as error:
        linked = u'ошибка: {0}'.format(error)

    errors = [item for item in (
        owner_error, id_error, domain_error, kind_error
    ) if item]
    line = (
        u'    → {0}; порт {1}; Domain={2}; ConnectorType={3}; '
        u'IsConnectedTo={4}; {5}'
    ).format(
        diagnostic_element_label(owner), diagnostic_text(ref_id),
        diagnostic_text(domain), diagnostic_text(kind), linked, decision,
    )
    if errors:
        line += u'; ошибки: ' + u'; '.join(errors)
    return line


def diagnose_connector(connector, internal_ids, returned_by_connection=True):
    owner, owner_error = diagnostic_property(connector, 'Owner')
    connector_id, unused = diagnostic_property(connector, 'Id')
    domain, domain_error = diagnostic_property(connector, 'Domain')
    kind, kind_error = diagnostic_property(connector, 'ConnectorType')
    shape, shape_error = diagnostic_property(connector, 'Shape')
    connected, connected_error = diagnostic_property(connector, 'IsConnected')
    if connected is not None:
        connected = bool(connected)
    try:
        references = list(connector.AllRefs)
        refs_error = None
    except Exception as error:
        references = None
        refs_error = u'AllRefs: {0}'.format(error)

    candidate = is_conduit_connector(connector)
    available = candidate and returned_by_connection and (
        not USE_ONLY_FREE_CONNECTORS
        or connector_is_free(connector, internal_ids)
    )
    if domain_error:
        reason = u'Не включён: невозможно прочитать Domain.'
    elif domain != Domain.DomainCableTrayConduit:
        reason = u'Не включён: не домен коробов/кабельных лотков.'
    elif kind == ConnectorType.Logical:
        reason = u'Не включён: ConnectorType.Logical.'
    elif not returned_by_connection:
        reason = u'Диагностика видит порт, но текущий get_raw_connectors его не возвращает.'
    elif available:
        reason = u'Текущая проверка допускает этот соединитель к подключению.'
    else:
        reason = u'Текущая проверка занятости отклоняет этот соединитель.'

    errors = [item for item in (
        owner_error, domain_error, kind_error, refs_error
    ) if item]
    if connected_error and kind != ConnectorType.Logical:
        errors.append(connected_error)
    disagreement = (
        candidate and connected is not None
        and available != (not connected)
    )

    lines = [
        u'{0}; соединитель ID {1}'.format(
            diagnostic_element_label(owner), diagnostic_text(connector_id)
        ),
        u'  Domain={0}; ConnectorType={1}; Shape={2}'.format(
            diagnostic_text(domain), diagnostic_text(kind),
            shape_error or diagnostic_text(shape),
        ),
        u'  IsConnected={0}; AllRefs={1}'.format(
            connected_error or diagnostic_text(connected),
            refs_error or diagnostic_text(len(references)),
        ),
        u'  Кандидат для коробов: {0}; допущен режимом подключения: {1}'.format(
            diagnostic_text(candidate), diagnostic_text(available)
        ),
        u'  Возвращается сборщиком режима подключения: {0}'.format(
            diagnostic_text(returned_by_connection)
        ),
        u'  ' + reason,
    ]
    try:
        point = connector.Origin
        lines.append(u'  Координаты модели, мм: X={0:.3f}; Y={1:.3f}; Z={2:.3f}'.format(
            point.X * 304.8, point.Y * 304.8, point.Z * 304.8
        ))
    except Exception as error:
        lines.append(u'  Origin не прочитан: {0}'.format(error))
    try:
        direction = connector.CoordinateSystem.BasisZ
        lines.append(u'  Ось BasisZ: X={0:.5f}; Y={1:.5f}; Z={2:.5f}'.format(
            direction.X, direction.Y, direction.Z
        ))
    except Exception as error:
        lines.append(u'  BasisZ не прочитан: {0}'.format(error))

    for reference in references or []:
        lines.append(diagnose_reference(connector, reference, internal_ids))
    if disagreement:
        lines.append(u'  ВНИМАНИЕ: IsConnected и решение алгоритма расходятся.')
    if errors:
        lines.append(u'  Неполные данные: ' + u'; '.join(errors))
    return {
        'candidate': candidate, 'available': available,
        'enumerated': candidate and returned_by_connection,
        'api_free': candidate and connected is False,
        'api_readable': candidate and connected is not None,
        'incomplete': bool(errors), 'disagreement': disagreement,
        'domain': domain_error or diagnostic_text(domain), 'lines': lines,
    }


def build_connector_report(element):
    hierarchy_notes = []
    members = get_element_and_subcomponents(element, hierarchy_notes)
    internal_ids = set(member.Id.IntegerValue for member in members)
    records, element_lines, domains = [], [], {}
    for member in members:
        connectors, notes = read_connectors_for_diagnostics(member)
        connection_connectors = get_raw_connectors(member)
        connection_keys = set(
            key for key in (
                diagnostic_connector_key(item) for item in connection_connectors
            ) if key is not None
        )
        element_lines.append(diagnostic_element_label(member))
        element_lines.extend(u'  ' + note for note in notes)
        for connector in connectors:
            key = diagnostic_connector_key(connector)
            returned = (
                key in connection_keys if key is not None
                else any(connector is item for item in connection_connectors)
            )
            record = diagnose_connector(connector, internal_ids, returned)
            records.append(record)
            domain = record['domain']
            domains[domain] = domains.get(domain, 0) + 1

    stats = {'total': len(records), 'members': len(members)}
    for field in ('candidate', 'enumerated', 'available', 'api_free', 'api_readable',
                  'incomplete', 'disagreement'):
        stats[field] = sum(1 for record in records if record[field])

    summary = (
        u'Всего соединителей всех доменов: {total}\n'
        u'Кандидатов для коробов/лотков: {candidate}\n'
        u'Из них видит режим подключения: {enumerated}\n'
        u'Свободных по текущему алгоритму: {available}\n'
        u'По Revit IsConnected=False: {api_free} '
        u'(флаг прочитан у {api_readable} кандидатов)\n'
        u'Расхождений с IsConnected: {disagreement}\n'
        u'Соединителей с неполными данными: {incomplete}'
    ).format(**stats)
    lines = [
        u'=== ПРОВЕРКА СОЕДИНИТЕЛЕЙ — версия {0} ==='.format(__version__),
        u'Выбран: ' + diagnostic_element_label(element),
        u'Проверены родительский и вложенные экземпляры: {0}'.format(len(members)),
        u'Только чтение. Транзакции не открываются, связи не изменяются.',
        summary,
        u'Распределение по доменам:',
    ]
    for domain in sorted(domains):
        lines.append(u'  {0}: {1}'.format(domain, domains[domain]))
    if not USE_ONLY_FREE_CONNECTORS:
        lines.append(u'ВНИМАНИЕ: USE_ONLY_FREE_CONNECTORS=False; фильтрация занятости отключена.')
    lines.append(u'=== ДОСТУП К СОЕДИНИТЕЛЯМ ПО ЭКЗЕМПЛЯРАМ ===')
    lines.extend(element_lines)
    lines.extend(hierarchy_notes)
    lines.append(u'=== ПОДРОБНО ПО КАЖДОМУ СОЕДИНИТЕЛЮ ===')
    for index, record in enumerate(records):
        lines.append(u'--- {0} ---'.format(index + 1))
        lines.extend(record['lines'])

    lines.extend([
        u'=== ВАЖНО ДЛЯ ВЫВОДА ===',
        u'Свободен по алгоритму — результат текущих правил, не гарантия соединения.',
        u'Внутренние ссылки показаны явно: их исключение из проверки само по себе '
        u'не доказывает, что порт физически свободен.',
        u'Выключатель может иметь электрические и другие соединители. '
        u'Категория семейства не подменяет Domain/ConnectorType.',
        u'API показывает соединители экземпляров, доступные в проекте. '
        u'Несовместимый профиль или недоступное вложение требуют отдельной проверки.',
    ])
    if stats['candidate'] == 0:
        lines.append(u'Ноль кандидатов — это не «все заняты»: '
                     u'проверьте домены, ConnectorManager и ошибки чтения выше.')
    return stats, summary, u'\n'.join(lines)


def run_connector_diagnostics():
    try:
        with forms.WarningBar(title=u'Выберите выключатель/модель для проверки (ESC — выход)'):
            reference = uidoc.Selection.PickObject(
                Sel.ObjectType.Element, u'Выберите модель для проверки соединителей'
            )
        element = doc.GetElement(reference.ElementId)
        if element is None:
            forms.alert(u'Выбранный элемент недоступен.', title=u'Проверка соединителей')
            return
        stats, summary, report = build_connector_report(element)
        try:
            output.set_title(u'Проверка соединителей — {0}'.format(__version__))
        except Exception:
            pass
        print(report)
        forms.alert(
            u'{0}\n\n{1}\n\nПолный отчёт — в окне вывода pyRevit. '
            u'Модель не изменялась.'.format(diagnostic_element_label(element), summary),
            title=u'Свободные коннекторы — {0}'.format(__version__),
            warn_icon=(stats['candidate'] == 0 or stats['incomplete'] > 0),
        )
    except OperationCanceledException:
        return
    except Exception as error:
        forms.alert(u'Ошибка диагностики (модель не изменялась):\n{0}'.format(error),
                    title=u'Проверка соединителей', warn_icon=True)


# ============ 8. ЦИКЛ ПОДКЛЮЧЕНИЯ ============

def pick_target_reference(source_conduit, clean_view):
    clean_view_state = None
    try:
        if clean_view or HIDE_INCOMPATIBLE_BY_NETWORK_TYPE:
            clean_view_state = begin_clean_view_mode(
                doc.ActiveView,
                source_conduit,
                hide_conduits=clean_view,
            )

        if clean_view:
            title = (
                u'Короба скрыты. Выберите МОДЕЛЬ '
                u'(после выбора короба вернутся)'
            )
        else:
            title = u'Выберите МОДЕЛЬ с соединителем коробов'

        if HIDE_INCOMPATIBLE_BY_NETWORK_TYPE:
            title = u'Несовместимые по «Типу сети» скрыты. ' + title

        with forms.WarningBar(title=title):
            return uidoc.Selection.PickObject(
                Sel.ObjectType.Element,
                ModelWithConduitConnectorFilter(source_conduit.Id),
                u'Выберите модель с соединителем коробов',
            )
    finally:
        if clean_view_state is not None:
            restore_clean_view_mode(clean_view_state)


def show_network_type_block_message(report):
    forms.alert(
        u'ЗАЩИТА ПОДКЛЮЧЕНИЯ СРАБОТАЛА!\n\n'
        u'{0}\n\n'
        u'Короб:\n{1}\n\n'
        u'Модель:\n{2}\n\n'
        u'Ни геометрия, ни соединения не изменены. Выберите другой короб или исправьте «Тип сети».'.format(
            report['reason'],
            format_network_type_entries(report['source_entries']),
            format_network_type_entries(report['target_entries']),
        ),
        title=u'Ошибка: Разные типы сети',
        warn_icon=True,
    )


def run_connection_loop(clean_view=False):
    while True:
        try:
            with forms.WarningBar(
                title=u'Выберите КОРОБ (ESC — выход)'
            ):
                source_reference = uidoc.Selection.PickObject(
                    Sel.ObjectType.Element,
                    ConduitSelectionFilter(),
                    u'Выберите короб',
                )

            source_conduit = doc.GetElement(source_reference.ElementId)
            if source_conduit is None:
                continue

            color_context = create_color_context(
                source_conduit,
                doc.ActiveView,
            )

            target_reference = pick_target_reference(
                source_conduit,
                clean_view,
            )

            target_element = doc.GetElement(target_reference.ElementId)
            if target_element is None:
                continue

            source_connectors = get_conduit_connectors(source_conduit)
            target_connectors = get_model_conduit_connectors(target_element)

            source_internal_ids = set([
                source_conduit.Id.IntegerValue,
            ])
            target_internal_ids = get_internal_owner_ids(target_element)

            available_source_connectors = filter_available_connectors(
                source_connectors,
                source_internal_ids,
            )
            available_target_connectors = filter_available_connectors(
                target_connectors,
                target_internal_ids,
            )

            if not available_source_connectors:
                forms.alert(
                    u'У выбранного короба нет свободного соединителя.\n\n'
                    u'Всего соединителей коробов: {0}.\n\n'
                    u'{1}\n\n'
                    u'Существующие подключения не были разорваны.'.format(
                        len(source_connectors),
                        format_connector_diagnostics(
                            source_connectors,
                            source_internal_ids,
                        ),
                    ),
                    title=u'Короб уже подключён',
                    warn_icon=True,
                )
                continue

            if not available_target_connectors:
                forms.alert(
                    u'У выбранной модели нет свободного «Соединителя коробов».\n\n'
                    u'Всего найдено соединителей: {0}.\n\n'
                    u'{1}\n\n'
                    u'Внутренние связи родительского и вложенных семейств '
                    u'свободными соединителями не считаются занятыми.'.format(
                        len(target_connectors),
                        format_connector_diagnostics(
                            target_connectors,
                            target_internal_ids,
                        ),
                    ),
                    title=u'Нет свободного соединителя',
                    warn_icon=True,
                )
                continue

            connector_pair, distance = find_nearest_connector_pair(
                available_source_connectors,
                available_target_connectors,
            )

            if connector_pair is None:
                forms.alert(
                    u'Не удалось определить ближайшую пару соединителей.',
                    title=u'Соединение не выполнено',
                    warn_icon=True,
                )
                continue

            source_connector, target_connector = connector_pair

            network_type_report = validate_network_type_compatibility(
                source_conduit,
                target_element,
                target_connector,
            )
            if not network_type_report['allowed']:
                print(
                    u'Блокировка по «Типу сети»: {0}'.format(
                        network_type_report['reason']
                    )
                )
                show_network_type_block_message(network_type_report)
                continue

            print(
                u'Проверка «Типа сети»: {0}'.format(
                    network_type_report['reason']
                )
            )

            try:
                alignment_report = {}
                target_state = remember_connector_position(target_connector)
                target_owner_id = target_connector.Owner.Id.IntegerValue
                with revit.Transaction(u'Подключить короб к модели'):
                    new_conduits = connect_conduit_to_model(
                        source_conduit,
                        source_connector,
                        target_connector,
                        alignment_report,
                        color_context,
                    )
                    doc.Regenerate()
                    check_connector_position(target_state)
                    if alignment_report.get('fixed_state') is not None:
                        check_connector_position(alignment_report['fixed_state'])

                distance_mm = distance * 304.8

                if not new_conduits:
                    result_text = u'соединены напрямую'
                else:
                    created_ids = u', '.join(
                        unicode(conduit.Id.IntegerValue)
                        for conduit in new_conduits
                    )
                    result_text = (
                        u'созданы участки коробов ID {0}'
                        .format(created_ids)
                    )

                print(
                    u'Короб ID {0} → модель ID {1}; '
                    u'соединитель принадлежит ID {2}; '
                    u'расстояние {3:.1f} мм; {4}.'.format(
                        source_conduit.Id.IntegerValue,
                        target_element.Id.IntegerValue,
                        target_owner_id,
                        distance_mm,
                        result_text,
                    )
                )
                print(u'Доводка: {0}'.format(alignment_report.get('reason', u'н/д')))
                if new_conduits:
                    print(
                        u'Тип системы: «{0}» перенесён в {1} новых участков.'.format(
                            get_system_type_text(source_conduit),
                            len(new_conduits),
                        )
                    )
                else:
                    print(u'Новых участков нет: перенос «Тип системы» не требовался.')

                if color_context.get('color') is not None:
                    print(
                        u'Цвет: {0}; источник: {1}; окрашено новых элементов: {2}.'.format(
                            color_to_text(color_context.get('color')),
                            color_context.get('source', u'н/д'),
                            len(color_context.get('applied_ids', set())),
                        )
                    )
                else:
                    print(
                        u'Явный RGB исходного короба не найден. Цвет новых '
                        u'элементов определяется скопированными параметрами '
                        u'и фильтрами вида.'
                    )

                failed_color_ids = color_context.get('failed_ids', set())
                if failed_color_ids:
                    print(
                        u'Не удалось назначить явный RGB элементам: {0}.'.format(
                            u', '.join(
                                unicode(element_id)
                                for element_id in sorted(failed_color_ids)
                            )
                        )
                    )
                    for color_error in color_context.get('errors', []):
                        print(u'  - {0}'.format(color_error))

                for label, key in ((u'До', 'before'), (u'После', 'after'),
                                   (u'Коннектор модели', 'target')):
                    point = alignment_report.get(key)
                    if point is not None:
                        print(u'{0}: X={1:.1f}; Y={2:.1f}; Z={3:.1f} мм'.format(
                            label, point.X * 304.8, point.Y * 304.8, point.Z * 304.8
                        ))

            except Exception as error:
                forms.alert(
                    u'Соединение не создано. Все изменения текущей попытки '
                    u'отменены.\n\n{0}'.format(error),
                    title=u'Ошибка соединения',
                    warn_icon=True,
                )

        except OperationCanceledException:
            break
        except Exception as error:
            forms.alert(
                u'Ошибка выполнения:\n\n{0}'.format(error),
                title=u'Соединить короб с моделью',
                warn_icon=True,
            )
            break


# ============ 9. СТАРТОВОЕ МЕНЮ ============

def main():
    action = forms.CommandSwitchWindow.show(
        [ACTION_CONNECT, ACTION_CONNECT_BLINK, ACTION_INSPECT],
        title=u'Короб к модели — {0}'.format(__version__),
        message=(
            u'Выберите функцию.\nРежим скрытия временно убирает фильтр '
            u'«{0}» между кликом на короб и кликом на модель.'.format(
                CONDUIT_VISIBILITY_FILTER_NAME
            )
        ),
        recognize_access_key=False,
    )
    if action == ACTION_INSPECT:
        run_connector_diagnostics()
    elif action == ACTION_CONNECT_BLINK:
        run_connection_loop(clean_view=True)
    elif action == ACTION_CONNECT:
        run_connection_loop(clean_view=False)
    # Закрытие меню/ESC: ничего не делаем.


if __name__ == '__main__':
    main()