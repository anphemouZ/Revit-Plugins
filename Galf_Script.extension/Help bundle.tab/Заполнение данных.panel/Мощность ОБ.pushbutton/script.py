# -*- coding: utf-8 -*-
__title__ = u"Мощность\nОБ"
__doc__ = u"Подбирает потребителей розеток ОБ по словам в марке и заполняет мощность и напряжение после проверки таблицы."

import os
import sys

import clr

clr.AddReference("RevitAPI")
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from Autodesk.Revit.DB import *
from System.Drawing import Size
from System.Windows.Forms import (
    Button, DataGridView, DataGridViewAutoSizeColumnsMode,
    DataGridViewComboBoxColumn, DataGridViewColumnSortMode,
    DataGridViewTextBoxColumn, DialogResult, DockStyle, FlowLayoutPanel,
    FlowDirection, Form, FormStartPosition, Label, Padding,
)
from pyrevit import forms, revit, script


BUNDLE_DIR = os.path.dirname(__file__)
if BUNDLE_DIR not in sys.path:
    sys.path.append(BUNDLE_DIR)

# Импорт вашей локальной библиотеки для сопоставления
from consumer_rules import load_catalog, match_consumer, normalize_text


TARGET_FAMILY_KEYWORD = u"розетк"
TARGET_NETWORK_TYPE = u"ОБ"

PARAM_MARK = u"Марка"
PARAM_POWER = u"ADSK_Номинальная мощность"
PARAM_NETWORK = u"GLF_Тип сети"

# ИСПРАВЛЕНО: Добавлен "ADSK_Напряжение" на первое место (приоритетный поиск)
VOLTAGE_PARAMETER_NAMES = (
    u"ADSK_Напряжение",
    u"ADSK_Номинальное напряжение",
    u"Напряжение",
)

CATALOG_PATH = os.path.join(BUNDLE_DIR, "consumers.json")
WATT_TO_INTERNAL = 10.763910416709722


doc = revit.doc
output = script.get_output()


def parameter_text(parameter):
    if parameter is None:
        return u""
    try:
        value = parameter.AsString()
        if value:
            return value
    except Exception:
        pass
    try:
        value = parameter.AsValueString()
        if value:
            return value
    except Exception:
        pass
    return u""


def family_name(element):
    try:
        return element.Symbol.Family.Name or u""
    except Exception:
        return u""


def model_name(element):
    family = family_name(element)
    try:
        type_name = element.Symbol.Name or u""
    except Exception:
        type_name = u""
    return u" / ".join(part for part in (family, type_name) if part)


def element_caption(element, mark=None):
    parts = [u"ID {0}".format(element.Id.IntegerValue)]
    if mark:
        parts.append(u'Марка: "{0}"'.format(mark))
    family = family_name(element)
    if family:
        parts.append(u'Семейство: "{0}"'.format(family))
    return u", ".join(parts)


def collect_targets():
    targets = []
    elements = FilteredElementCollector(doc).OfClass(FamilyInstance).WhereElementIsNotElementType()
    for element in elements:
        if TARGET_FAMILY_KEYWORD not in family_name(element).lower():
            continue
        network = parameter_text(element.LookupParameter(PARAM_NETWORK))
        if normalize_text(network) == normalize_text(TARGET_NETWORK_TYPE):
            targets.append(element)
    return targets


def convert_to_internal(value, quantity):
    numeric = float(value)
    try:
        unit_type_id = getattr(UnitTypeId, quantity)
        return UnitUtils.ConvertToInternalUnits(numeric, unit_type_id)
    except Exception:
        pass

    legacy_name = "DUT_WATTS" if quantity == "Watts" else "DUT_VOLTS"
    try:
        display_unit = getattr(DisplayUnitType, legacy_name)
        return UnitUtils.ConvertToInternalUnits(numeric, display_unit)
    except Exception:
        pass

    if quantity == "Watts":
        return numeric * WATT_TO_INTERNAL
    return numeric


def set_quantity(parameter, value, quantity):
    if parameter is None:
        raise ValueError(u"параметр отсутствует")
    if parameter.IsReadOnly:
        raise ValueError(u"параметр доступен только для чтения")

    if parameter.StorageType == StorageType.Double:
        result = parameter.Set(convert_to_internal(value, quantity))
    elif parameter.StorageType == StorageType.Integer:
        result = parameter.Set(int(round(float(value))))
    elif parameter.StorageType == StorageType.String:
        numeric = float(value)
        text_value = u"{0:g}".format(numeric)
        result = parameter.Set(text_value)
    else:
        raise ValueError(u"неподдерживаемый тип хранения")

    if result is False:
        raise ValueError(u"Revit отклонил новое значение")


def find_voltage_parameter(element):
    read_only_candidate = None
    read_only_name = None
    
    # Ищем параметр из нашего обновленного списка
    for parameter_name in VOLTAGE_PARAMETER_NAMES:
        parameter = element.LookupParameter(parameter_name)
        if parameter is None:
            continue
        if not parameter.IsReadOnly:
            return parameter, parameter_name
        if read_only_candidate is None:
            read_only_candidate = parameter
            read_only_name = parameter_name

    # Резервный поиск встроенного параметра (RBS_ELEC_VOLTAGE)
    try:
        parameter = element.get_Parameter(BuiltInParameter.RBS_ELEC_VOLTAGE)
        if parameter is not None:
            if not parameter.IsReadOnly:
                return parameter, u"Встроенный параметр напряжения"
            if read_only_candidate is None:
                read_only_candidate = parameter
                read_only_name = u"Встроенный параметр напряжения"
    except Exception:
        pass

    return read_only_candidate, read_only_name


def print_items(title, items):
    if not items:
        return
    output.print_md(u"#### {0} — {1}".format(title, len(items)))
    for item in items:
        print(u"- " + item)


def prepare_selection_rows(targets, catalog):
    rows = []
    for element in targets:
        mark = parameter_text(element.LookupParameter(PARAM_MARK)).strip()
        status, consumer, aliases = match_consumer(mark, catalog)
        if status == "matched":
            hint = u"Совпало: {0}".format(u", ".join(aliases))
        elif status == "ambiguous":
            if aliases:
                hint = u"Несколько вариантов: {0}".format(u", ".join(aliases))
            else:
                hint = u"Уточните марку для подбора"
        else:
            hint = u"Совпадений нет"
        rows.append({
            "element": element,
            "mark": mark,
            "consumer": consumer,
            "hint": hint,
        })
    rows.sort(key=lambda row: (normalize_text(row["mark"]), row["element"].Id.IntegerValue))
    return rows


def add_text_column(grid, title, weight):
    column = DataGridViewTextBoxColumn()
    column.HeaderText = title
    column.FillWeight = weight
    column.ReadOnly = True
    column.SortMode = DataGridViewColumnSortMode.NotSortable
    grid.Columns.Add(column)


def make_selection_grid(rows, catalog):
    grid = DataGridView()
    grid.Dock = DockStyle.Fill
    grid.AllowUserToAddRows = False
    grid.AllowUserToDeleteRows = False
    grid.RowHeadersVisible = False
    grid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill
    add_text_column(grid, u"ID", 10)
    add_text_column(grid, u"Модель (семейство / тип)", 29)
    add_text_column(grid, u"Марка", 25)
    add_text_column(grid, u"Совпадение", 20)

    choice_column = DataGridViewComboBoxColumn()
    choice_column.HeaderText = u"Потребитель"
    choice_column.FillWeight = 25
    choice_column.SortMode = DataGridViewColumnSortMode.NotSortable
    skip_label = u"Не заполнять"
    choice_column.Items.Add(skip_label)
    choices = {skip_label: None}
    labels = []
    for index, consumer in enumerate(catalog, 1):
        label = u"{0}. {1} ({2} Вт, {3} В)".format(
            index, consumer["name"], consumer["power_w"], consumer["voltage_v"]
        )
        choice_column.Items.Add(label)
        choices[label] = consumer
        labels.append(label)
    grid.Columns.Add(choice_column)

    for entry in rows:
        selected_label = skip_label
        if entry["consumer"] is not None:
            for index, consumer in enumerate(catalog):
                if consumer is entry["consumer"]:
                    selected_label = labels[index]
                    break
        row_index = grid.Rows.Add(
            str(entry["element"].Id.IntegerValue),
            model_name(entry["element"]),
            entry["mark"] or u"<пусто>",
            entry["hint"],
            selected_label,
        )
        grid.Rows[row_index].Tag = entry
    return grid, choices


def make_selection_form(rows, catalog):
    form = Form()
    form.Text = u"Подбор потребителей розеток ОБ"
    form.Size = Size(1200, 650)
    form.MinimumSize = Size(850, 400)
    form.StartPosition = FormStartPosition.CenterScreen
    form.ShowIcon = False

    instruction = Label()
    instruction.Dock = DockStyle.Top
    instruction.Height = 42
    instruction.Text = (
        u"Розеток ОБ: {0}. Проверьте столбец «Потребитель». "
        u"Строки с «Не заполнять» будут пропущены."
    ).format(len(rows))

    grid, choices = make_selection_grid(rows, catalog)

    buttons = FlowLayoutPanel()
    buttons.Dock = DockStyle.Bottom
    buttons.Height = 50
    buttons.FlowDirection = FlowDirection.RightToLeft
    buttons.Padding = Padding(8)
    apply_button = Button()
    apply_button.Text = u"Записать выбранное"
    apply_button.Width = 170
    apply_button.DialogResult = DialogResult.OK
    cancel_button = Button()
    cancel_button.Text = u"Отмена"
    cancel_button.Width = 100
    cancel_button.DialogResult = DialogResult.Cancel
    buttons.Controls.Add(apply_button)
    buttons.Controls.Add(cancel_button)
    form.AcceptButton = apply_button
    form.CancelButton = cancel_button
    form.Controls.Add(grid)
    form.Controls.Add(buttons)
    form.Controls.Add(instruction)
    return form, grid, choices


def show_selection_table(rows, catalog):
    form, grid, choices = make_selection_form(rows, catalog)
    try:
        if form.ShowDialog() != DialogResult.OK:
            return None
        grid.EndEdit()
        selected = []
        skipped = []
        for grid_row in grid.Rows:
            entry = grid_row.Tag
            consumer = choices[grid_row.Cells[4].Value]
            if consumer is None:
                skipped.append(element_caption(entry["element"], entry["mark"] or u"<пусто>"))
            else:
                selected.append((entry["element"], entry["mark"], consumer))
        return selected, skipped
    finally:
        form.Dispose()


def main():
    try:
        catalog = load_catalog(CATALOG_PATH)
    except Exception as error:
        forms.alert(
            u"Не удалось прочитать справочник потребителей:\n{0}".format(error),
            title=__title__.replace(u"\n", u" "),
            warn_icon=True,
        )
        return

    targets = collect_targets()
    if not targets:
        forms.alert(
            u'Не найдены семейства с "розетк" в имени и значением "ОБ" в параметре "GLF_Тип сети".',
            title=__title__.replace(u"\n", u" "),
        )
        return

    selection = show_selection_table(prepare_selection_rows(targets, catalog), catalog)
    if selection is None:
        return
    matched, skipped = selection

    power_written = 0
    voltage_written = 0
    power_errors = []
    voltage_errors = []

    if matched:
        with revit.Transaction(u"Заполнить мощность и напряжение ОБ"):
            for element, mark, consumer in matched:
                caption = element_caption(element, mark)

                # Запись мощности
                power_parameter = element.LookupParameter(PARAM_POWER)
                try:
                    set_quantity(power_parameter, consumer["power_w"], "Watts")
                    power_written += 1
                except Exception as error:
                    power_errors.append(
                        u"{0}: {1} — {2}".format(caption, PARAM_POWER, error)
                    )

                # Запись напряжения
                voltage_parameter, voltage_name = find_voltage_parameter(element)
                try:
                    if voltage_parameter is None:
                        # ИСПРАВЛЕНО: Теперь ошибка динамически выводит список параметров, которые искал скрипт
                        raise ValueError(
                            u'не найдены параметры: {0}'.format(
                                u", ".join(VOLTAGE_PARAMETER_NAMES)
                            )
                        )
                    set_quantity(voltage_parameter, consumer["voltage_v"], "Volts")
                    voltage_written += 1
                except Exception as error:
                    voltage_errors.append(
                        u"{0}: {1} — {2}".format(
                            caption, voltage_name or u"Напряжение", error
                        )
                    )

    output.print_md(u"### Результат: Мощность ОБ")
    print(u"Найдено элементов сети ОБ: {0}".format(len(targets)))
    print(u"Выбрано для записи: {0}".format(len(matched)))
    print(u"Записана мощность: {0}".format(power_written))
    print(u"Записано напряжение: {0}".format(voltage_written))
    print_items(u"Пропущено в таблице", skipped)
    print_items(u"Не удалось записать мощность", power_errors)
    print_items(u"Не удалось записать напряжение", voltage_errors)

    issue_count = len(skipped) + len(power_errors) + len(voltage_errors)
    message = (
        u"Элементов сети ОБ: {0}\n"
        u"Выбрано для записи: {1}\n"
        u"Мощность записана: {2}\n"
        u"Напряжение записано: {3}"
    ).format(len(targets), len(matched), power_written, voltage_written)
    
    if issue_count:
        message += u"\n\nЗамечаний: {0}. Подробности показаны в окне вывода pyRevit.".format(issue_count)

    forms.alert(
        message,
        title=__title__.replace(u"\n", u" "),
        warn_icon=bool(issue_count),
    )


if __name__ == "__main__":
    main()