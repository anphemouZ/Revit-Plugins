# -*- coding: utf-8 -*-
__title__ = 'Сборная строка (Из Длины участка)'
__doc__ = 'Берет готовые значения из параметра "Длина участка", суммирует их по типам и пишет итог в "Способ прокладки".'

import collections
from pyrevit import revit, DB, forms
from System.Collections.Generic import List

doc = revit.doc

# ================= НАСТРОЙКИ ПАРАМЕТРОВ =================

# 1. ЧТО СУММИРУЕМ (Группировка)
P_GROUP_BY = "Выбор короба"

# 2. КУДА ПИШЕМ ИТОГ (Текстовая строка)
P_TARGET = "Способ прокладки" 

# 3. ОТКУДА БЕРЕМ ДЛИНУ (Теперь это источник данных!)
P_LEN_SEG = "Длина участка" 

# ================= 1. ГЕОМЕТРИЯ И СЕТЬ =================

def get_connectors(el):
    conns = []
    try:
        if hasattr(el, "ConnectorManager") and el.ConnectorManager:
            conns.extend([c for c in el.ConnectorManager.Connectors])
        elif hasattr(el, "MEPModel") and el.MEPModel and el.MEPModel.ConnectorManager:
            conns.extend([c for c in el.MEPModel.ConnectorManager.Connectors])
    except: pass
    return conns

def collect_route_elements():
    cats = [
        DB.BuiltInCategory.OST_CableTray,
        DB.BuiltInCategory.OST_CableTrayFitting,
        DB.BuiltInCategory.OST_Conduit,
        DB.BuiltInCategory.OST_ConduitFitting
    ]
    filter_cats = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](cats))
    return DB.FilteredElementCollector(doc).WhereElementIsNotElementType().WherePasses(filter_cats).ToElements()

def build_network_graph(elements):
    graph = collections.defaultdict(set)
    valid_ids = set(e.Id.IntegerValue for e in elements)
    for el in elements:
        eid = el.Id.IntegerValue
        conns = get_connectors(el)
        for c in conns:
            if c.IsConnected:
                for ref in c.AllRefs:
                    nid = ref.Owner.Id.IntegerValue
                    if nid in valid_ids and nid != eid:
                        graph[eid].add(nid)
                        graph[nid].add(eid)
    return graph, valid_ids

def find_connected_components(elements):
    graph, all_ids = build_network_graph(elements)
    visited = set()
    routes = []
    for start_id in all_ids:
        if start_id in visited: continue
        component = []
        queue = collections.deque([start_id])
        visited.add(start_id)
        while queue:
            node = queue.popleft()
            component.append(node)
            for neighbor in graph[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        routes.append(component)
    return routes

# ================= 2. ФУНКЦИИ ЧТЕНИЯ/ЗАПИСИ =================

def get_length_from_param_ft(elem):
    """
    Читает значение из параметра P_LEN_SEG ("Длина участка").
    Возвращает значение в ФУТАХ (для внутренних расчетов).
    """
    p = elem.LookupParameter(P_LEN_SEG)
    if not p or not p.HasValue:
        return 0.0

    val_ft = 0.0
    
    # А. Если параметр ЧИСЛОВОЙ (Double)
    if p.StorageType == DB.StorageType.Double:
        val_raw = p.AsDouble()
        
        # Проверяем, это Длина (футы) или Число (мм)?
        is_length_unit = False
        try:
            # По имени параметра "Длина" обычно Revit понимает Length
            if p.Definition.UnitType == DB.UnitType.UT_Length: is_length_unit = True
        except: pass
        try:
            if p.Definition.GetDataType() == DB.SpecTypeId.Length: is_length_unit = True
        except: pass
        
        if is_length_unit:
            val_ft = val_raw # Уже в футах
        else:
            val_ft = val_raw / 304.8 # Были мм -> стали футы

    # Б. Если параметр ЦЕЛОЕ (Integer) -> считаем мм
    elif p.StorageType == DB.StorageType.Integer:
        val_mm = p.AsInteger()
        val_ft = val_mm / 304.8
        
    # В. Если параметр ТЕКСТ (String)
    elif p.StorageType == DB.StorageType.String:
        try:
            txt = p.AsString().replace(',', '.').replace(u'\xa0', '').strip()
            import re
            txt = re.sub(r"[^0-9.]", "", txt)
            val = float(txt)
            # Эвристика: если < 50, это метры, иначе мм
            if val < 50: val_ft = val / 0.3048
            else: val_ft = val / 304.8
        except: pass

    return val_ft

def get_p_val_str(elem, name):
    p = elem.LookupParameter(name)
    if p and p.HasValue:
        return p.AsString() or ""
    return ""

def set_p_val_str(elem, name, value):
    p = elem.LookupParameter(name)
    if p and not p.IsReadOnly:
        p.Set(str(value))

# ================= 3. ЛОГИКА СУММИРОВАНИЯ =================

def process_route_summary(r_ids, id_map):
    route_elems = sorted([id_map[eid] for eid in r_ids], key=lambda x: x.Id.IntegerValue)
    
    sums = collections.defaultdict(float)
    
    # 1. Суммируем (читаем из "Длина участка")
    for e in route_elems:
        # <-- ВОТ ГЛАВНОЕ ИЗМЕНЕНИЕ: Читаем параметр, а не геометрию
        length = get_length_from_param_ft(e) 
        
        if length <= 0.0001: continue
        
        box_type = get_p_val_str(e, P_GROUP_BY)
        if not box_type: box_type = "Не указано"
            
        sums[box_type] += length
        
    # 2. Формируем строку
    parts = []
    for name in sorted(sums.keys()):
        len_ft = sums[name]
        len_m = len_ft * 0.3048 # Перевод в метры для текста
        
        len_str = "{:g}".format(round(len_m, 2)).replace('.', ',')
        parts.append("{}- {} м.".format(name, len_str))
        
    final_str = "; ".join(parts)
    
    # 3. Записываем результат
    count = 0
    main_id = None
    
    for idx, e in enumerate(route_elems):
        if idx == 0:
            main_id = e.Id.IntegerValue
            # Пишем итог только в первый элемент
            set_p_val_str(e, P_TARGET, final_str)
        else:
            # Остальные чистим
            set_p_val_str(e, P_TARGET, "")
        count += 1
        
    return count, final_str, main_id

# ================= ЗАПУСК =================

elems = collect_route_elements()
if not elems:
    forms.alert("Элементы не найдены.", exitscript=True)

routes = find_connected_components(elems)
id_map = {e.Id.IntegerValue: e for e in elems}

if not forms.alert("СКРИПТ: Сбор итогов из '{}'\nНайдено трасс: {}\nЗапустить?".format(P_LEN_SEG, len(routes)), yes=True, no=True):
    raise SystemExit

t = DB.Transaction(doc, "Сбор строки (из параметра)")
t.Start()

try:
    total_updated = 0
    print("--- ОТЧЕТ ---")
    
    for i, r_ids in enumerate(routes, 1):
        cnt, res_str, mid = process_route_summary(r_ids, id_map)
        total_updated += cnt
        
        if res_str:
            print("Трасса #{}: ID {} -> {}".format(i, mid, res_str))
        else:
            print("Трасса #{}: (Нет длин в параметре)".format(i))

    t.Commit()
    print("="*50)
    print("ГОТОВО! Обновлено элементов: {}".format(total_updated))
    
except Exception as e:
    t.RollBack()
    print("ОШИБКА: {}".format(e))
    import traceback
    print(traceback.format_exc())