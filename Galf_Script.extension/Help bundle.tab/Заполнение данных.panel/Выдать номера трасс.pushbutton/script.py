# -*- coding: utf-8 -*-
__title__ = 'Нумерация Участков (Групповая)'
__doc__ = 'Присваивает ОДИН номер (ТР1-1) всем элементам на участке между двумя узлами.'

import collections
from pyrevit import revit, DB, forms
from System.Collections.Generic import List

doc = revit.doc

# ============ НАСТРОЙКИ ============
P_CIRCUIT_NUM = "Номер электрической цепи"
P_SOURCE = "Источник"
P_DEST = "Потребитель"
P_ROUTE_NUM = "Номер участка трассы"  # Куда писать результат

# ============ 1. СБОР ЭЛЕМЕНТОВ ============

def get_p_val(elem, name):
    p = elem.LookupParameter(name)
    if p and p.HasValue:
        val = p.AsString()
        if val: return val.strip()
    return None

def collect_elements():
    cats = [
        DB.BuiltInCategory.OST_CableTray,
        DB.BuiltInCategory.OST_CableTrayFitting,
        DB.BuiltInCategory.OST_Conduit,
        DB.BuiltInCategory.OST_ConduitFitting
    ]
    filter_cats = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](cats))
    return DB.FilteredElementCollector(doc).WhereElementIsNotElementType().WherePasses(filter_cats).ToElements()

# ============ 2. ЛОГИКА НУМЕРАЦИИ СЕГМЕНТОВ ============

def renumber_circuit_segments(circuit_num, elements):
    """
    Группирует элементы по уникальным парам (Источник -> Потребитель).
    Выстраивает логическую цепочку и нумерует группы.
    """
    
    # 1. Группируем элементы в СЕГМЕНТЫ
    # segment_map: (ИмяИсточника, ИмяПотребителя) -> [Список элементов]
    segment_map = collections.defaultdict(list)
    
    # Граф для построения порядка: Кто -> Кого
    # graph: ИмяИсточника -> [ИмяПотребителя1, ИмяПотребителя2...]
    graph = collections.defaultdict(set)
    
    # Множества для поиска корня
    all_src = set()
    all_dst = set()
    
    for e in elements:
        src = get_p_val(e, P_SOURCE)
        dest = get_p_val(e, P_DEST)
        
        if not src: continue
        if not dest: dest = "Конец" # Для тупиков
        
        key = (src, dest)
        segment_map[key].append(e)
        
        # Строим топологию только если есть реальный потребитель
        if dest != "Конец":
            graph[src].add(dest)
            all_src.add(src)
            all_dst.add(dest)
        else:
            # Если это тупик (розетка), просто запоминаем, что src куда-то ведет
            graph[src].add(None)
            all_src.add(src)

    # 2. Находим КОРЕНЬ (Щит)
    # Это узел, который есть в Источниках, но нет в Потребителях
    roots = list(all_src - all_dst)
    roots.sort() # Стабильность
    
    # Если корни не найдены (ошибки в именах или кольцо), ищем по ключевым словам
    if not roots and all_src:
        keywords = ["ЩР", "ЩИТ", "РК", "ВРУ", "ПАНЕЛЬ"]
        for s in all_src:
            if any(k in s.upper() for k in keywords):
                roots.append(s)
        if not roots: roots = [sorted(list(all_src))[0]]

    # 3. Обход в ширину (BFS) по СЕГМЕНТАМ
    counter = 1
    queue = list(roots) # Очередь имен узлов (напр. "ЩР-1")
    processed_keys = set() # Чтобы не нумеровать сегмент дважды
    count_updated = 0
    
    while queue:
        curr_node = queue.pop(0)
        
        # Получаем всех потребителей, запитанных от текущего узла
        destinations = sorted(list(graph[curr_node]), key=lambda x: str(x))
        
        for dest in destinations:
            # Ключ сегмента "Откуда -> Куда"
            # (dest может быть None или "Конец", нужно учесть как мы сохраняли в segment_map)
            # В segment_map мы писали "Конец" для пустых dest
            lookup_dest = dest if dest else "Конец"
            
            seg_key = (curr_node, lookup_dest)
            
            if seg_key in processed_keys: continue
            
            # Получаем все элементы этого сегмента (их может быть 15 штук)
            elems_in_segment = segment_map.get(seg_key, [])
            
            # === ГЛАВНОЕ: ПИШЕМ ОДИН НОМЕР ВСЕМ ЭЛЕМЕНТАМ ===
            route_val = "ТР{}-{}".format(circuit_num, counter)
            
            for el in elems_in_segment:
                p = el.LookupParameter(P_ROUTE_NUM)
                if p and not p.IsReadOnly:
                    p.Set(route_val)
                    count_updated += 1
            
            # Фиксируем обработку
            processed_keys.add(seg_key)
            counter += 1
            
            # Добавляем потребителя в очередь (если это не тупик)
            if dest and dest != "Конец":
                queue.append(dest)
                
    return count_updated

# ============ ЗАПУСК ============

all_elems = collect_elements()

# Группировка по Цепям
circuit_groups = collections.defaultdict(list)
for e in all_elems:
    c_num = get_p_val(e, P_CIRCUIT_NUM)
    if c_num and get_p_val(e, P_SOURCE):
        circuit_groups[c_num].append(e)

if not circuit_groups:
    forms.alert("Нет данных! Сначала выполните расчет трасс.", exitscript=True)

if forms.alert("Проставить номера участков (Групповая)?\nПример: Все трубы от Щита до РК получат ТР1-1.", yes=True, no=True):
    t = DB.Transaction(doc, "Нумерация Участков")
    t.Start()
    
    total = 0
    print("--- ОТЧЕТ ---")
    
    # Сортировка
    def sort_key(k):
        try: return int(re.findall(r'\d+', k)[0])
        except: return k
        
    for c_num in sorted(circuit_groups.keys(), key=sort_key):
        cnt = renumber_circuit_segments(c_num, circuit_groups[c_num])
        total += cnt
        print("Цепь {}: обновлено {} элементов".format(c_num, cnt))
        
    t.Commit()
    print("="*40)
    print("ГОТОВО! Всего: {}".format(total))