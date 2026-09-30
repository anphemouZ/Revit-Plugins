# -*- coding: utf-8 -*-
__title__ = 'Записать маршруты'
__doc__ = '1. Строгая логика: Щит всегда источник. Меньшая РК - источник. 2. Длина +5см за стык. 3. Цепь из Потребителя.'

import re
import collections
from pyrevit import revit, DB, forms
from System.Collections.Generic import List

doc = revit.doc

# ============ НАСТРОЙКИ ПАРАМЕТРОВ ============
P_SOURCE = "Источник"
P_DEST = "Потребитель"
P_METHOD = "Способ прокладки"

# Параметры для записи
P_CIRCUIT_NUM = "Номер электрической цепи" 
P_LOAD_NAME = "Имя нагрузки"               

# Параметры длин
P_LEN_SEG = "Длина участка"     
P_LEN_TOT = "Длина трассы"      

# Запас (50 мм)
MARGIN_PER_CONN_FT = 50.0 / 304.8

# ============ 1. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def set_p_val(elem, name, value):
    p = elem.LookupParameter(name)
    if p and not p.IsReadOnly:
        try:
            if p.StorageType == DB.StorageType.String:
                p.Set(str(value))
            else:
                p.Set(value)
            return True
        except: pass
    return False

def get_mark(elem):
    if not elem: return "?"
    try:
        for param_name in ["Mark", "Марка", DB.BuiltInParameter.ALL_MODEL_MARK]:
            p = elem.LookupParameter(param_name) if isinstance(param_name, str) else elem.get_Parameter(param_name)
            if p and p.HasValue:
                val = p.AsString()
                if val and val.strip():
                    return val.strip()
        
        if isinstance(elem, DB.FamilyInstance) and elem.Symbol:
            if elem.Symbol.Family:
                return elem.Symbol.Family.Name
            return elem.Name
        return elem.Name
    except: 
        return "Без имени"

def get_connectors(element):
    conns = []
    try:
        if hasattr(element, "ConnectorManager") and element.ConnectorManager:
            conns.extend([c for c in element.ConnectorManager.Connectors])
        elif hasattr(element, "MEPModel") and element.MEPModel and element.MEPModel.ConnectorManager:
            conns.extend([c for c in element.MEPModel.ConnectorManager.Connectors])
    except: pass
    return conns

# ============ 2. СТРОГОЕ ОПРЕДЕЛЕНИЕ РОЛЕЙ ============

def classify_element(el):
    try:
        cat_id = el.Category.Id.IntegerValue
        if isinstance(el, DB.MEPCurve) or cat_id in [int(DB.BuiltInCategory.OST_ConduitFitting), int(DB.BuiltInCategory.OST_CableTrayFitting)]:
            return "PIPE"
        
        mark = get_mark(el).upper()
        if "ЩР" in mark or "ЩИТ" in mark: return "PANEL"
        if "РК" in mark or "RK" in mark or cat_id == int(DB.BuiltInCategory.OST_ElectricalEquipment): return "BOX"
        return "DEVICE"
    except: return "UNKNOWN"

def get_route_ends(route_ids, id_map):
    route_set = set(route_ids)
    connections = [] 
    for eid in route_ids:
        el = id_map[eid]
        conns = get_connectors(el)
        for c in conns:
            try:
                if c.ConnectorType == DB.ConnectorType.Logical: continue
                if c.IsConnected:
                    for ref in c.AllRefs:
                        owner = ref.Owner
                        if owner.Id.IntegerValue not in route_set:
                            if isinstance(owner, DB.MEPCurve): continue
                            if not any(x.Id == owner.Id for x in connections):
                                connections.append(owner)
            except: pass
    return connections

def identify_endpoints_universal(connections):
    if not connections: 
        return None, None, False, "Нет подключений"
    if len(connections) == 1:
        return connections[0], None, False, "1 конец"
        
    # --- НАТУРАЛЬНАЯ СОРТИРОВКА (РК-1 < РК-2 < РК-10) ---
    def sort_key(el):
        mark = get_mark(el)
        # Разбиваем строку на текст и числа. Например, "РК3-2" -> ["РК", 3, "-", 2]
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', mark)]
        
    sorted_conns = sorted(connections, key=sort_key)
    
    panels = []
    boxes = []
    devices = []
    
    for c in sorted_conns:
        role = classify_element(c)
        if role == "PANEL": panels.append(c)
        elif role == "BOX": boxes.append(c)
        else: devices.append(c)
        
    # === 1. ИЩЕМ ИСТОЧНИК ===
    source = None
    if panels:
        source = panels[0] # Щит всегда в приоритете. Если их два, возьмет ЩР-1 вместо ЩР-2.
    elif boxes:
        source = boxes[0]  # Коробка - источник, если нет щита. Возьмет РК3-1 вместо РК3-2.
    elif devices:
        source = devices[0]
        
    # === 2. ИЩЕМ ПОТРЕБИТЕЛЯ ===
    candidates = [c for c in sorted_conns if c.Id != source.Id]
    
    if not candidates:
        return source, None, False, "Замкнуто на себя"
        
    dest_devices = [c for c in candidates if classify_element(c) == "DEVICE"]
    dest_boxes = [c for c in candidates if classify_element(c) == "BOX"]
    dest_panels = [c for c in candidates if classify_element(c) == "PANEL"]
    
    if dest_devices:
        dest = dest_devices[-1] # Устройства в приоритете на потребителя
    elif dest_boxes:
        dest = dest_boxes[-1]   # Берет самую большую коробку (РК3-2)
    elif dest_panels:
        dest = dest_panels[-1]
        
    return source, dest, True, "OK"

# ============ 3. ПОИСК ЦЕПИ (В ПОТРЕБИТЕЛЕ) ============

def get_circuit_data_from_elem(elem):
    data = {'num': None, 'load': None}
    if not elem: return data
    try:
        if hasattr(elem, "MEPModel") and elem.MEPModel:
            systems = elem.MEPModel.GetElectricalSystems()
            if systems:
                target = None
                for s in systems:
                    if s.SystemType == DB.Electrical.ElectricalSystemType.PowerCircuit:
                        target = s; break
                if not target: target = list(systems)[0]
                if target:
                    data['num'] = target.CircuitNumber
                    data['load'] = target.LoadName
                    return data
        conns = get_connectors(elem)
        for c in conns:
            if c.MEPSystem and isinstance(c.MEPSystem, DB.Electrical.ElectricalSystem):
                data['num'] = c.MEPSystem.CircuitNumber
                data['load'] = c.MEPSystem.LoadName
                return data
    except: pass
    return data

# ============ 4. ГЕОМЕТРИЯ ТРАССЫ И ДЛИНА (+5см) ============

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
            try:
                if c.ConnectorType == DB.ConnectorType.Logical: continue
                if c.IsConnected:
                    for ref in c.AllRefs:
                        nid = ref.Owner.Id.IntegerValue
                        if nid in valid_ids and nid != eid:
                            graph[eid].add(nid)
                            graph[nid].add(eid)
            except: pass
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

def get_mounting_margin(elem):
    cat_id = elem.Category.Id.IntegerValue
    if cat_id in [int(DB.BuiltInCategory.OST_CableTrayFitting), int(DB.BuiltInCategory.OST_ConduitFitting)]:
        return 0.0
        
    margin = 0.0
    conns = get_connectors(elem)
    fit_cats = [int(DB.BuiltInCategory.OST_CableTrayFitting), int(DB.BuiltInCategory.OST_ConduitFitting)]
    
    for c in conns:
        try:
            if c.ConnectorType == DB.ConnectorType.Logical: continue
            if c.IsConnected:
                for ref in c.AllRefs:
                    neighbor = ref.Owner
                    if neighbor.Id == elem.Id: continue
                    if neighbor.Category.Id.IntegerValue in fit_cats:
                        margin += MARGIN_PER_CONN_FT
                        break 
        except: pass
    return margin

def set_len_param(elem, name, val_ft):
    p = elem.LookupParameter(name)
    if not p or p.IsReadOnly: return
    is_len = False
    try: 
        if p.Definition.UnitType == DB.UnitType.UT_Length: is_len = True
    except: pass
    try:
        if p.Definition.GetDataType() == DB.SpecTypeId.Length: is_len = True
    except: pass

    if is_len: p.Set(val_ft)
    else:
        val_mm = int(round(val_ft * 304.8))
        if p.StorageType == DB.StorageType.String: p.Set(str(val_mm))
        else: p.Set(val_mm)

# ============ 5. ГЛАВНЫЙ ПРОЦЕСС ============

def process_route(i, r_ids, id_map):
    try:
        conns = get_route_ends(r_ids, id_map)
        src_obj, cons_obj, is_valid, msg = identify_endpoints_universal(conns)

        src_txt = get_mark(src_obj) if src_obj else "?"
        cons_txt = get_mark(cons_obj) if cons_obj else "?"
        
        c_info = get_circuit_data_from_elem(cons_obj)
        if not c_info['num'] and src_obj:
             c_info = get_circuit_data_from_elem(src_obj)
        
        route_elems = sorted([id_map[eid] for eid in r_ids], key=lambda x: x.Id.IntegerValue)
        GRAND_TOTAL = 0.0
        
        for e in route_elems:
            set_p_val(e, P_SOURCE, src_txt)
            set_p_val(e, P_DEST, cons_txt)
            
            if c_info['num']:
                set_p_val(e, P_CIRCUIT_NUM, c_info['num'])
            if c_info['load']:
                set_p_val(e, P_LOAD_NAME, c_info['load'])
            
            geom_len = 0.0
            if isinstance(e, DB.MEPCurve):
                geom_len = e.get_Parameter(DB.BuiltInParameter.CURVE_ELEM_LENGTH).AsDouble()
            
            margin = get_mounting_margin(e)
            seg_len = geom_len + margin
            
            set_len_param(e, P_LEN_SEG, seg_len)
            GRAND_TOTAL += seg_len
            
        for idx, e in enumerate(route_elems):
            if idx == 0:
                set_len_param(e, P_LEN_TOT, GRAND_TOTAL)
            else:
                set_len_param(e, P_LEN_TOT, 0.0)
        
        res = c_info['num'] if c_info['num'] else "--"
        return "{} -> {} [Цепь: {}]".format(src_txt, cons_txt, res)
        
    except Exception as e:
        return "ERR: " + str(e)

# ============ ЗАПУСК ============

elems = collect_route_elements()
routes = find_connected_components(elems)
id_map = {e.Id.IntegerValue: e for e in elems}

if forms.alert("СТАРТ: Логика Источник/Потребитель + Нат.сортировка + 5см + Цепь\nТрасс: {}\nЗапустить?".format(len(routes)), yes=True, no=True):
    t = DB.Transaction(doc, "Расчет трасс (All-in-One +5cm)")
    t.Start()
    
    print("--- ОТЧЕТ ---")
    cnt = 0
    for i, r_ids in enumerate(routes, 1):
        msg = process_route(i, r_ids, id_map)
        print("Трасса #{}: {}".format(i, msg))
        if "ERR" not in msg: cnt += 1
        
    t.Commit()
    print("="*40)
    print("Готово. Успешно: {}".format(cnt))