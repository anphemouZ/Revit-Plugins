# -*- coding: utf-8 -*-
__title__ = u'Кабельный журнал\n(На чертежном виде)'
__doc__ = u'''Анализ трасс, интерактивный редактор.
ГОСТ-ТАБЛИЦА С АВТОПЕРЕНОСОМ ТЕКСТА.
Разделка и фасонина скрыты и прибавлены внутрь Способа прокладки.
Version = 9.6 (Слияние разделки со способом прокладки)'''
__author__ = 'pyRevit Assistant'

import os
import re
import math
import collections
import traceback
import json
import codecs

import clr
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')

from System.Drawing import Size, Point, Color, Font, FontStyle
from System.Windows.Forms import (Form, DataGridView, DataGridViewTextBoxColumn, 
                                  DataGridViewComboBoxColumn, Button, DialogResult, 
                                  DockStyle, FormStartPosition, DataGridViewAutoSizeColumnsMode,
                                  DataGridViewSelectionMode, Panel, Padding, DataGridViewTextBoxCell,
                                  DataGridViewColumnSortMode, Label, TextBox, DataGridViewTriState, DataGridViewAutoSizeRowsMode)

from pyrevit import revit, DB, script, forms
from System.Collections.Generic import List

doc = revit.doc
output = script.get_output()

# ============ НАСТРОЙКИ ПАРАМЕТРОВ ============
P_CIRCUIT    = u"Номер цепи"
P_GROUP_BY   = u"Выбор короба"
P_CONDUCTOR  = u"Выбор проводника"
P_ROUTE_NUM  = u"Номер участка трассы"

P_START      = u"Начало"
P_END        = u"Дальний потребитель"

P_LENGTH     = u"Длина трассы"      
P_LEN_SEG    = u"Длина участка"     
P_METHOD     = u"Способ прокладки"  

P_RES_PCT    = u"Процент запаса"
P_RES_TERM   = u"Запас на разделку"
P_RES_FIT    = u"Запас на фитинг"

FT_TO_M = 0.3048

# ============ 1. РАБОТА С JSON КАТАЛОГОМ ============

def ensure_and_load_catalog():
    dir_path = os.path.dirname(__file__)
    json_path = os.path.join(dir_path, 'cables.json')
    
    default_cables = [
        u"Не указано",
        u"ВВГнг(А)-LS 3x1.5",
        u"ВВГнг(А)-LS 3x2.5",
        u"ВВГнг(А)-LS 5x4",
        u"ВБШвнг(А)-LS 5x6",
        u"FRLS 3x1.5"
    ]
    
    if not os.path.exists(json_path):
        with codecs.open(json_path, 'w', 'utf-8') as f:
            json.dump(default_cables, f, ensure_ascii=False, indent=4)
            
    try:
        with codecs.open(json_path, 'r', 'utf-8') as f:
            return json.load(f)
    except:
        return default_cables

# ============ 2. ИНТЕРФЕЙС РЕДАКТОРА ============

class JournalEditorForm(Form):
    def __init__(self, journal_data, catalog):
        self.Text = u"Редактор Кабельного журнала (с расчетом запасов)"
        self.Size = Size(1250, 650)
        self.StartPosition = FormStartPosition.CenterScreen
        
        self.i_circ = 0; self.i_rout = 1; self.i_src = 2; self.i_dst = 3
        self.i_cond = 4; self.i_base = 5; self.i_fcount = 6; self.i_fmargin = 7
        self.i_len  = 8; self.i_pct  = 9; self.i_term = 10; self.i_meth = 11
        
        self.grid = DataGridView()
        self.grid.Dock = DockStyle.Fill
        self.grid.AllowUserToAddRows = False
        self.grid.RowHeadersVisible = False
        self.grid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill
        self.grid.SelectionMode = DataGridViewSelectionMode.CellSelect
        self.grid.BackgroundColor = Color.White
        
        self.grid.DefaultCellStyle.WrapMode = DataGridViewTriState.True
        self.grid.AutoSizeRowsMode = DataGridViewAutoSizeRowsMode.AllCells
        
        cols = [DataGridViewTextBoxColumn() for _ in range(12)]
        cols[self.i_cond] = DataGridViewComboBoxColumn()
        
        cols[self.i_circ].HeaderText = u"Цепь"; cols[self.i_circ].Visible = False
        cols[self.i_rout].HeaderText = u"Номер участка"
        cols[self.i_src].HeaderText = u"Начало"
        cols[self.i_dst].HeaderText = u"Конец (Дальний потр.)"
        
        for c in catalog: cols[self.i_cond].Items.Add(c)
        cols[self.i_cond].HeaderText = u"Выбор проводника"
        
        cols[self.i_base].HeaderText = u"База"; cols[self.i_base].Visible = False
        cols[self.i_fcount].HeaderText = u"Шт. фасонины"; cols[self.i_fcount].Visible = False
        cols[self.i_fmargin].HeaderText = u"На 1 фитинг, м"; cols[self.i_fmargin].Visible = False
        
        cols[self.i_len].HeaderText = u"Длина трассы, м"; cols[self.i_len].ReadOnly = True; cols[self.i_len].DefaultCellStyle.BackColor = Color.LightYellow
        cols[self.i_pct].HeaderText = u"Запас, %"; cols[self.i_pct].Visible = False
        cols[self.i_term].HeaderText = u"Разделка, м"; cols[self.i_term].Visible = False
        cols[self.i_meth].HeaderText = u"Способ прокладки"; cols[self.i_meth].ReadOnly = True; cols[self.i_meth].DefaultCellStyle.BackColor = Color.LightYellow
        
        self.grid.Columns.AddRange(*cols)
        for col in self.grid.Columns: col.SortMode = DataGridViewColumnSortMode.NotSortable
            
        bold_font = Font(self.grid.Font, FontStyle.Bold)
        current_circuit = None
        
        for row_data in journal_data:
            if row_data['circuit'] != current_circuit:
                if current_circuit is not None:
                    e_idx = self.grid.Rows.Add()
                    e_row = self.grid.Rows[e_idx]
                    e_row.Tag = "EMPTY"
                    e_row.ReadOnly = True; e_row.Height = 15
                    e_row.DefaultCellStyle.BackColor = Color.White
                    for i in range(1, 12):
                        e_row.Cells[i] = DataGridViewTextBoxCell()
                        e_row.Cells[i].Value = u""

                current_circuit = row_data['circuit']
                h_idx = self.grid.Rows.Add()
                h_row = self.grid.Rows[h_idx]
                h_row.Tag = "HEADER"
                h_row.ReadOnly = True
                h_row.DefaultCellStyle.BackColor = Color.FromArgb(230, 230, 230)
                h_row.DefaultCellStyle.Font = bold_font
                for i in range(1, 12):
                    h_row.Cells[i] = DataGridViewTextBoxCell()
                    h_row.Cells[i].Value = u""
                h_row.Cells[self.i_rout].Value = current_circuit
            
            d_idx = self.grid.Rows.Add()
            d_row = self.grid.Rows[d_idx]
            
            d_row.Tag = {
                'elements': row_data['elements'],
                'method_sums': row_data['method_sums'] 
            }
            
            d_row.Cells[self.i_circ].Value = row_data['circuit']
            d_row.Cells[self.i_rout].Value = row_data['route']
            d_row.Cells[self.i_src].Value  = row_data['source']
            d_row.Cells[self.i_dst].Value  = row_data['dest']
            
            cond_val = row_data.get('conductor', u"Не указано")
            if not cols[self.i_cond].Items.Contains(cond_val): cols[self.i_cond].Items.Add(cond_val)
            d_row.Cells[self.i_cond].Value = cond_val
            
            d_row.Cells[self.i_base].Value = row_data['base_length']
            d_row.Cells[self.i_fcount].Value = row_data['fitting_count']
            d_row.Cells[self.i_fmargin].Value = row_data['fit_margin']
            d_row.Cells[self.i_pct].Value  = row_data['pct']
            d_row.Cells[self.i_term].Value = row_data['term']
            
            self.recalc_row(d_row) 
            
        self.grid.CellValueChanged += self.on_cell_changed

        self.panel_bulk = Panel()
        self.panel_bulk.Dock = DockStyle.Top; self.panel_bulk.Height = 40; self.panel_bulk.Visible = False
        self.panel_bulk.BackColor = Color.AliceBlue
        
        l1 = Label(); l1.Text = u"Запас (%):"; l1.AutoSize = True; l1.Location = Point(10, 12)
        self.tb_bulk_pct = TextBox(); self.tb_bulk_pct.Location = Point(70, 10); self.tb_bulk_pct.Width = 40
        l2 = Label(); l2.Text = u"Разделка (м):"; l2.AutoSize = True; l2.Location = Point(130, 12)
        self.tb_bulk_term = TextBox(); self.tb_bulk_term.Location = Point(210, 10); self.tb_bulk_term.Width = 40
        l3 = Label(); l3.Text = u"На 1 фитинг (м):"; l3.AutoSize = True; l3.Location = Point(270, 12)
        self.tb_bulk_fit = TextBox(); self.tb_bulk_fit.Location = Point(370, 10); self.tb_bulk_fit.Width = 40; self.tb_bulk_fit.Text = u"0.1"
        
        btn_apply_bulk = Button(); btn_apply_bulk.Text = u"Применить ко всем"; btn_apply_bulk.Location = Point(430, 8); btn_apply_bulk.Width = 130
        btn_apply_bulk.Click += self.on_bulk_apply
        
        self.panel_bulk.Controls.AddRange((l1, self.tb_bulk_pct, l2, self.tb_bulk_term, l3, self.tb_bulk_fit, btn_apply_bulk))

        panel_bottom = Panel()
        panel_bottom.Dock = DockStyle.Bottom; panel_bottom.Height = 50; panel_bottom.Padding = Padding(10)
        
        self.btn_toggle = Button(); self.btn_toggle.Text = u"Настроить запас..."
        self.btn_toggle.Dock = DockStyle.Left; self.btn_toggle.Width = 140
        self.btn_toggle.Click += self.on_toggle_reserves
        
        self.btn_ok = Button(); self.btn_ok.Text = u"Обновить и Отрисовать"
        self.btn_ok.DialogResult = DialogResult.OK
        self.btn_ok.Dock = DockStyle.Right; self.btn_ok.Width = 180
        
        self.btn_cancel = Button(); self.btn_cancel.Text = u"Отмена"
        self.btn_cancel.DialogResult = DialogResult.Cancel
        self.btn_cancel.Dock = DockStyle.Right; self.btn_cancel.Width = 100
        
        panel_bottom.Controls.Add(self.btn_toggle)
        panel_bottom.Controls.Add(self.btn_cancel)
        panel_bottom.Controls.Add(self.btn_ok)
        
        self.Controls.Add(self.grid)
        self.Controls.Add(self.panel_bulk)
        self.Controls.Add(panel_bottom)

    def parse_float(self, val):
        try:
            v = unicode(val).replace(u',', u'.').strip()
            f = float(v) if v else 0.0
            if f > 100000: return 0.0
            return f
        except: return 0.0

    def recalc_row(self, row):
        """Пересчитывает длины. Теперь Разделка и Фитинги размазываются внутри 'Способа прокладки'"""
        if str(row.Tag) in ["HEADER", "EMPTY"]: return
        mem_data = row.Tag
        base_sums = mem_data['method_sums']
        
        pct      = self.parse_float(row.Cells[self.i_pct].Value)
        term     = self.parse_float(row.Cells[self.i_term].Value)
        f_count  = self.parse_float(row.Cells[self.i_fcount].Value)
        f_margin = self.parse_float(row.Cells[self.i_fmargin].Value)
        
        final_len = 0.0
        parts = []
        
        l_fit = f_count * f_margin
        l_fit_pct = l_fit * (1.0 + pct / 100.0)
        total_base = sum(base_sums.values())
        
        # Распределяем фасонину И разделку пропорционально
        for m_name in sorted(base_sums.keys()):
            l_base = base_sums[m_name]
            l_pct = l_base * (1.0 + pct / 100.0)
            
            if total_base > 0:
                l_pct += l_fit_pct * (l_base / total_base)  # Размазываем фитинги
                l_pct += term * (l_base / total_base)       # Размазываем разделку
                
            final_len += l_pct
            if l_pct > 0.01:
                parts.append(u"{}- {:g} м.".format(m_name, round(l_pct, 2)).replace(u'.', u','))
                
        # Страховка: если трасса вообще без прямых участков (только фитинги/точки),
        # то скидываем всё в общий котел, чтобы длина не потерялась.
        if total_base <= 0.0001:
            leftover = l_fit_pct + term
            if leftover > 0.01:
                final_len += leftover
                parts.append(u"Узлы/Разделка- {:g} м.".format(round(leftover, 2)).replace(u'.', u','))
            
        row.Cells[self.i_len].Value = u"{:g}".format(round(final_len, 2)).replace(u'.', u',')
        row.Cells[self.i_meth].Value = u"\n".join(parts)

    def on_cell_changed(self, sender, args):
        if args.RowIndex >= 0 and args.ColumnIndex in (self.i_pct, self.i_term, self.i_fmargin):
            self.recalc_row(self.grid.Rows[args.RowIndex])

    def on_toggle_reserves(self, sender, args):
        vis = not self.grid.Columns[self.i_pct].Visible
        self.grid.Columns[self.i_pct].Visible = vis
        self.grid.Columns[self.i_term].Visible = vis
        self.grid.Columns[self.i_fmargin].Visible = vis
        self.panel_bulk.Visible = vis

    def on_bulk_apply(self, sender, args):
        p_val = self.tb_bulk_pct.Text.strip()
        t_val = self.tb_bulk_term.Text.strip()
        f_val = self.tb_bulk_fit.Text.strip()
        for row in self.grid.Rows:
            if str(row.Tag) not in ["HEADER", "EMPTY"]:
                if p_val: row.Cells[self.i_pct].Value = p_val
                if t_val: row.Cells[self.i_term].Value = t_val
                if f_val: row.Cells[self.i_fmargin].Value = f_val
                self.recalc_row(row)

    def get_updated_data(self):
        updated = []
        for row in self.grid.Rows:
            if str(row.Tag) not in ["HEADER", "EMPTY"]:
                mem_data = row.Tag
                updated.append({
                    'circuit': row.Cells[self.i_circ].Value,
                    'route':   row.Cells[self.i_rout].Value,
                    'source':  row.Cells[self.i_src].Value,
                    'dest':    row.Cells[self.i_dst].Value,
                    'conductor': row.Cells[self.i_cond].Value,
                    'length':  self.parse_float(row.Cells[self.i_len].Value),
                    'pct':     row.Cells[self.i_pct].Value, 
                    'term':    row.Cells[self.i_term].Value,
                    'f_margin':row.Cells[self.i_fmargin].Value,
                    'method':  row.Cells[self.i_meth].Value, 
                    'elements': mem_data['elements']
                })
        return updated

# ============ 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ТОПОЛОГИЯ ============

def set_param_smart(elem, name, value, is_length_m=False):
    try:
        p = elem.LookupParameter(name)
        if not p or p.IsReadOnly: return
        
        if p.StorageType == DB.StorageType.String:
            if isinstance(value, float):
                p.Set(unicode(u"{:g}".format(round(value, 3)).replace(u'.', u',')))
            else:
                p.Set(unicode(value))
                
        elif p.StorageType == DB.StorageType.Double:
            val_f = float(value) if value else 0.0
            if is_length_m:
                is_unit_length = False
                try:
                    if p.Definition.UnitType == DB.UnitType.UT_Length: is_unit_length = True
                except: pass
                try:
                    if p.Definition.GetDataType() == DB.SpecTypeId.Length: is_unit_length = True
                except: pass
                
                if is_unit_length: 
                    p.Set(val_f / FT_TO_M)
                else: 
                    p.Set(val_f)
            else:
                p.Set(val_f)
    except: pass

def get_param_smart_read(elem, name, is_length_m=False):
    try:
        p = elem.LookupParameter(name)
        if not p or not p.HasValue: return u""
        
        if p.StorageType == DB.StorageType.String:
            return p.AsString()
        elif p.StorageType == DB.StorageType.Integer:
            return unicode(p.AsInteger())
        elif p.StorageType == DB.StorageType.Double:
            val = p.AsDouble()
            if is_length_m:
                is_unit_length = False
                try:
                    if p.Definition.UnitType == DB.UnitType.UT_Length: is_unit_length = True
                except: pass
                try:
                    if p.Definition.GetDataType() == DB.SpecTypeId.Length: is_unit_length = True
                except: pass
                
                if is_unit_length: 
                    return unicode(round(val * FT_TO_M, 3))
                else:
                    return unicode(round(val, 3))
            else:
                return unicode(round(val, 3))
    except: pass
    return u""

def get_mark(elem):
    if not elem: return u"?"
    try:
        for p_name in [DB.BuiltInParameter.ALL_MODEL_MARK, u"Марка", u"Mark"]:
            p = elem.get_Parameter(p_name) if isinstance(p_name, DB.BuiltInParameter) else elem.LookupParameter(p_name)
            if p and p.HasValue:
                val = p.AsString()
                if val and val.strip(): return val.strip()
        if isinstance(elem, DB.FamilyInstance) and elem.Symbol and elem.Symbol.Family:
            return elem.Symbol.Family.Name
        return elem.Name
    except: return u"Без имени"

def get_connectors(elem):
    conns = []
    try:
        if hasattr(elem, "ConnectorManager") and elem.ConnectorManager:
            conns.extend([c for c in elem.ConnectorManager.Connectors])
        elif hasattr(elem, "MEPModel") and elem.MEPModel and elem.MEPModel.ConnectorManager:
            conns.extend([c for c in elem.MEPModel.ConnectorManager.Connectors])
    except: pass
    return conns

def classify_role(elem):
    mark = get_mark(elem).upper()
    cat = elem.Category.Id.IntegerValue if elem.Category else 0
    if u"ЩР" in mark or u"ЩИТ" in mark or u"ВРУ" in mark: return 1 
    if u"РК" in mark or u"RK" in mark or cat == int(DB.BuiltInCategory.OST_ElectricalEquipment): return 2 
    return 3 

def get_circuit_number(elem):
    if not elem: return None
    try:
        if hasattr(elem, "MEPModel") and elem.MEPModel:
            systems = elem.MEPModel.GetElectricalSystems()
            if systems:
                for s in systems:
                    if s.CircuitNumber: return s.CircuitNumber
    except: pass
    try:
        p = elem.get_Parameter(DB.BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER)
        if p and p.HasValue: return p.AsString()
    except: pass
    try:
        p = elem.LookupParameter(P_CIRCUIT)
        if p and p.HasValue: return p.AsString() or p.AsValueString()
    except: pass
    return None

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', unicode(s))]

def generate_journal_data():
    cats = [DB.BuiltInCategory.OST_CableTray, DB.BuiltInCategory.OST_CableTrayFitting,
            DB.BuiltInCategory.OST_Conduit, DB.BuiltInCategory.OST_ConduitFitting]
    filter_cats = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](cats))
    elems = DB.FilteredElementCollector(doc).WhereElementIsNotElementType().WherePasses(filter_cats).ToElements()
    
    if not elems: return []
    id_map = {e.Id.IntegerValue: e for e in elems}
    
    graph = collections.defaultdict(set)
    for el in elems:
        eid = el.Id.IntegerValue
        for c in get_connectors(el):
            if c.ConnectorType == DB.ConnectorType.Logical: continue
            if c.IsConnected:
                for ref in c.AllRefs:
                    nid = ref.Owner.Id.IntegerValue
                    if nid in id_map and nid != eid:
                        graph[eid].add(nid)
                        graph[nid].add(eid)
                        
    visited = set()
    routes = []
    for start_id in id_map.keys():
        if start_id in visited: continue
        comp = []
        queue = collections.deque([start_id])
        visited.add(start_id)
        while queue:
            node = queue.popleft()
            comp.append(node)
            for neighbor in graph[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        routes.append(comp)
        
    raw_segments = []
    fitting_cats = [int(DB.BuiltInCategory.OST_CableTrayFitting), int(DB.BuiltInCategory.OST_ConduitFitting)]
    
    for r_ids in routes:
        endpoints = []
        for eid in r_ids:
            for c in get_connectors(id_map[eid]):
                if c.IsConnected:
                    for ref in c.AllRefs:
                        owner = ref.Owner
                        if owner.Id.IntegerValue not in id_map:
                            if not isinstance(owner, DB.MEPCurve):
                                if not any(x.Id == owner.Id for x in endpoints):
                                    endpoints.append(owner)
                                    
        if len(endpoints) == 0: source = dest = None
        elif len(endpoints) == 1: source = endpoints[0]; dest = None
        else:
            endpoints.sort(key=lambda e: (classify_role(e), natural_sort_key(get_mark(e))))
            source = endpoints[0]; dest = endpoints[-1]
            
        source_mark = get_mark(source) if source else u"?"
        dest_mark = get_mark(dest) if dest else u"?"
        
        c_num = u"Без цепи"
        for el in [dest, source]:
            if el:
                val = get_circuit_number(el)
                if val: 
                    c_num = val; break
                    
        saved_cond = u"Не указано"
        saved_pct  = u""
        saved_term = u""
        saved_fit  = u"0.1" 
        
        for eid in r_ids:
            el = id_map[eid]
            val_c = get_param_smart_read(el, P_CONDUCTOR, is_length_m=False)
            if val_c and val_c != u"Не указано": saved_cond = val_c
            
            val_p = get_param_smart_read(el, P_RES_PCT, is_length_m=False)
            if val_p: saved_pct = val_p
            
            val_t = get_param_smart_read(el, P_RES_TERM, is_length_m=True)
            if val_t: saved_term = val_t
            
            val_f = get_param_smart_read(el, P_RES_FIT, is_length_m=True)
            if val_f: saved_fit = val_f
            
            if val_c or val_p or val_t: break 
                
        method_sums = collections.defaultdict(float)
        base_length = 0.0
        fitting_count = 0
        
        for eid in r_ids:
            el = id_map[eid]
            if el.Category and el.Category.Id.IntegerValue in fitting_cats:
                fitting_count += 1
                
            if isinstance(el, DB.MEPCurve):
                try: 
                    length = el.get_Parameter(DB.BuiltInParameter.CURVE_ELEM_LENGTH).AsDouble() * FT_TO_M
                    base_length += length
                    method = get_param_smart_read(el, P_GROUP_BY, is_length_m=False)
                    if not method: method = u"Не указано"
                    method_sums[method] += length
                except: pass
            
        raw_segments.append({
            'circuit': c_num,
            'source_mark': source_mark,
            'dest_mark': dest_mark,
            'source_role': classify_role(source) if source else 99,
            'dest_role': classify_role(dest) if dest else 99,
            'base_length': base_length,
            'fitting_count': fitting_count,
            'conductor': saved_cond,
            'pct': saved_pct,
            'term': saved_term,
            'fit_margin': saved_fit,
            'method_sums': method_sums,
            'elements': r_ids 
        })

    final_data = []
    by_circuit = collections.defaultdict(list)
    for seg in raw_segments:
        by_circuit[seg['circuit']].append(seg)
        
    for c_num in sorted(by_circuit.keys(), key=natural_sort_key):
        segs = by_circuit[c_num]
        segs.sort(key=lambda s: (s['source_role'], natural_sort_key(s['source_mark']), s['dest_role'], natural_sort_key(s['dest_mark'])))
        for idx, seg in enumerate(segs, 1):
            route_num = u"ТР{}-{}".format(c_num, idx) if c_num != u"Без цепи" else u"ТР-{}".format(idx)
            final_data.append({
                'circuit': c_num,
                'route': route_num,
                'source': seg['source_mark'],
                'dest': seg['dest_mark'],
                'conductor': seg['conductor'],
                'base_length': seg['base_length'],
                'fitting_count': seg['fitting_count'],
                'pct': seg['pct'],
                'term': seg['term'],
                'fit_margin': seg['fit_margin'],
                'method_sums': seg['method_sums'],
                'elements': seg['elements']
            })
            
    return final_data

# ============ 4. ОТРИСОВКА НА ЧЕРТЕЖНОМ ВИДЕ ============

def get_or_create_drafting_view(view_name):
    views = DB.FilteredElementCollector(doc).OfClass(DB.ViewDrafting).ToElements()
    for v in views:
        if v.Name == view_name: return v
    view_family_types = DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType).ToElements()
    drafting_type = next((vft for vft in view_family_types if vft.ViewFamily == DB.ViewFamily.Drafting), None)
    if not drafting_type: raise Exception(u"В проекте нет типа для Чертежных видов!")
    new_view = DB.ViewDrafting.Create(doc, drafting_type.Id)
    new_view.Name = view_name
    new_view.Scale = 1
    return new_view

def clear_view(view):
    ids_to_delete = List[DB.ElementId]()
    curves = DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement).ToElements()
    for c in curves:
        if c.Pinned: c.Pinned = False
        ids_to_delete.Add(c.Id)
    texts = DB.FilteredElementCollector(doc, view.Id).OfClass(DB.TextNote).ToElements()
    for t in texts:
        if t.Pinned: t.Pinned = False
        ids_to_delete.Add(t.Id)
    if ids_to_delete.Count > 0:
        doc.Delete(ids_to_delete)

def draw_line(view, x1_mm, y1_mm, x2_mm, y2_mm):
    pt1 = DB.XYZ(x1_mm / 304.8, y1_mm / 304.8, 0)
    pt2 = DB.XYZ(x2_mm / 304.8, y2_mm / 304.8, 0)
    doc.Create.NewDetailCurve(view, DB.Line.CreateBound(pt1, pt2))

def write_text(view, x_mm, y_mm, width_mm, text, text_type_id, align="Center"):
    pt = DB.XYZ(x_mm / 304.8, y_mm / 304.8, 0)
    opts = DB.TextNoteOptions()
    opts.TypeId = text_type_id
    if align == "Left": opts.HorizontalAlignment = DB.HorizontalTextAlignment.Left
    else: opts.HorizontalAlignment = DB.HorizontalTextAlignment.Center
    opts.VerticalAlignment = DB.VerticalTextAlignment.Middle
    
    if isinstance(text, float): safe_text = unicode(u"{:g}".format(round(text, 2)).replace(u'.', u','))
    else: safe_text = unicode(text).strip()
    if not safe_text: safe_text = u"\u00A0" 
    DB.TextNote.Create(doc, view.Id, pt, width_mm / 304.8, safe_text, opts)

def draw_table_on_view(journal_data):
    view_name = u"Кабельный журнал (Авто)"
    text_types = DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType).ToElements()
    if not text_types: raise Exception(u"В проекте нет стилей текста!")
    default_text_type = text_types[0].Id
    
    view = get_or_create_drafting_view(view_name)
    clear_view(view)
    
    cols = [25, 35, 35, 45, 40, 20] 
    MIN_ROW_H = 8 
    header_h = 16 
    total_w = sum(cols)
    
    start_x = 0
    current_y = 0
    
    draw_line(view, start_x, current_y, start_x + total_w, current_y)
    line_mid_y = current_y - MIN_ROW_H
    
    c1_x = start_x + cols[0]
    c3_x = c1_x + cols[1] + cols[2]
    draw_line(view, c1_x, line_mid_y, c3_x, line_mid_y)
    
    c4_x = start_x + sum(cols[:4])
    end_x = start_x + total_w
    draw_line(view, c4_x, line_mid_y, end_x, line_mid_y)
    
    bottom_h_y = current_y - header_h
    draw_line(view, start_x, bottom_h_y, start_x + total_w, bottom_h_y)
    
    cx = start_x
    draw_line(view, cx, current_y, cx, bottom_h_y); cx += cols[0]
    draw_line(view, cx, current_y, cx, bottom_h_y); 
    draw_line(view, cx + cols[1], line_mid_y, cx + cols[1], bottom_h_y); cx += cols[1] + cols[2]
    draw_line(view, cx, current_y, cx, bottom_h_y); cx += cols[3]
    draw_line(view, cx, current_y, cx, bottom_h_y); 
    draw_line(view, cx + cols[4], line_mid_y, cx + cols[4], bottom_h_y); cx += cols[4] + cols[5]
    draw_line(view, cx, current_y, cx, bottom_h_y) 
    
    write_text(view, start_x + (cols[0]/2), current_y - 8, cols[0]-1, u"Обозначение\nучастка", default_text_type)
    write_text(view, start_x + cols[0] + ((cols[1]+cols[2])/2), current_y - 4, cols[1]+cols[2]-1, u"Трасса", default_text_type)
    write_text(view, start_x + cols[0] + (cols[1]/2), line_mid_y - 4, cols[1]-1, u"Начало", default_text_type)
    write_text(view, start_x + cols[0] + cols[1] + (cols[2]/2), line_mid_y - 4, cols[2]-1, u"Конец", default_text_type)
    write_text(view, start_x + sum(cols[:3]) + (cols[3]/2), current_y - 8, cols[3]-1, u"Способ\nпрокладки", default_text_type)
    write_text(view, start_x + sum(cols[:4]) + ((cols[4]+cols[5])/2), current_y - 4, cols[4]+cols[5]-1, u"Кабель (По проекту)", default_text_type)
    write_text(view, start_x + sum(cols[:4]) + (cols[4]/2), line_mid_y - 4, cols[4]-1, u"Марка", default_text_type)
    write_text(view, start_x + sum(cols[:5]) + (cols[5]/2), line_mid_y - 4, cols[5]-1, u"Длина, м", default_text_type)

    current_y -= header_h
    current_circuit = None
    
    for row_data in journal_data:
        if row_data['circuit'] != current_circuit:
            if current_circuit is not None:
                draw_line(view, start_x, current_y, start_x, current_y - MIN_ROW_H)
                draw_line(view, start_x + total_w, current_y, start_x + total_w, current_y - MIN_ROW_H)
                current_y -= MIN_ROW_H
                draw_line(view, start_x, current_y, start_x + total_w, current_y)
                
            current_circuit = row_data['circuit']
            draw_line(view, start_x, current_y, start_x, current_y - MIN_ROW_H)
            draw_line(view, start_x + total_w, current_y, start_x + total_w, current_y - MIN_ROW_H)
            write_text(view, start_x + 2, current_y - (MIN_ROW_H / 2.0), total_w - 4, current_circuit, default_text_type, "Left")
            current_y -= MIN_ROW_H
            draw_line(view, start_x, current_y, start_x + total_w, current_y)
            
        data = [
            row_data['route'], 
            row_data['source'], 
            row_data['dest'], 
            row_data['method'], 
            row_data['conductor'], 
            row_data['length']
        ]
        
        max_lines = 1
        for i, val in enumerate(data):
            s_val = unicode(val)
            lines_in_cell = 0
            for part in s_val.split('\n'):
                chars_per_line = max(1, (cols[i] - 2) / 1.8) 
                lines_in_cell += max(1, int(math.ceil(len(part) / chars_per_line)))
            if lines_in_cell > max_lines: max_lines = lines_in_cell
                
        row_h = max(MIN_ROW_H, max_lines * 4 + 2)
                
        current_x = start_x
        for i, w in enumerate(cols):
            center_x = current_x + (w / 2.0)
            center_y = current_y - (row_h / 2.0)
            text_w = w - 2 
            
            if i in [0, 1, 2, 3]: write_text(view, current_x + 1, center_y, text_w, data[i], default_text_type, "Left")
            else: write_text(view, center_x, center_y, text_w, data[i], default_text_type, "Center")
            
            draw_line(view, current_x, current_y, current_x, current_y - row_h)
            current_x += w
            
        draw_line(view, current_x, current_y, current_x, current_y - row_h)
        current_y -= row_h
        draw_line(view, start_x, current_y, start_x + total_w, current_y)

    return view

# ============ 5. ГЛАВНЫЙ ПРОЦЕСС ============

def main():
    try:
        catalog = ensure_and_load_catalog()
        with forms.ProgressBar(title=u"Анализ цепей...", cancellable=False):
            journal_data = generate_journal_data()
            
        if not journal_data:
            forms.alert(u"Не найдено ни одной связной трассы в проекте.", exitscript=True)
            
        form = JournalEditorForm(journal_data, catalog)
        if form.ShowDialog() != DialogResult.OK:
            return
            
        updated_data = form.get_updated_data()
        
        t = DB.Transaction(doc, u"КЖ: Запись и чертеж (ГОСТ)")
        t.Start()
        try:
            for row in updated_data:
                pct = parse_float_val(row['pct'])
                
                for idx, eid in enumerate(row['elements']):
                    el = doc.GetElement(DB.ElementId(eid))
                    if not el: continue
                    
                    set_param_smart(el, P_ROUTE_NUM, row['route'])
                    set_param_smart(el, P_CIRCUIT, row['circuit'])
                    set_param_smart(el, P_CONDUCTOR, row['conductor'])
                    
                    set_param_smart(el, P_RES_PCT, row['pct'], is_length_m=False)
                    set_param_smart(el, P_RES_TERM, row['term'], is_length_m=True)
                    set_param_smart(el, P_RES_FIT, row['f_margin'], is_length_m=True)
                    
                    geom_len_ft = 0.0
                    if isinstance(el, DB.MEPCurve):
                        try: geom_len_ft = el.get_Parameter(DB.BuiltInParameter.CURVE_ELEM_LENGTH).AsDouble()
                        except: pass
                    seg_len_with_pct = (geom_len_ft * FT_TO_M) * (1.0 + pct / 100.0)
                    set_param_smart(el, P_LEN_SEG, seg_len_with_pct, is_length_m=True)
                    
                    if idx == 0:
                        set_param_smart(el, P_START, row['source'])
                        set_param_smart(el, P_END, row['dest'])
                        set_param_smart(el, P_LENGTH, row['length'], is_length_m=True)
                        set_param_smart(el, P_METHOD, row['method']) 
                    else:
                        set_param_smart(el, P_START, u"")
                        set_param_smart(el, P_END, u"")
                        set_param_smart(el, P_LENGTH, 0.0, is_length_m=True)
                        set_param_smart(el, P_METHOD, u"")

            created_view = draw_table_on_view(updated_data)
            t.Commit()
            
            output.print_md(u'### ✅ Готово! Журнал начерчен. Фасонина спрятана.')
            try: revit.uidoc.ActiveView = created_view
            except: pass
            
        except Exception as draw_error:
            t.RollBack()
            raise draw_error
            
    except Exception as e:
        print(traceback.format_exc())

if __name__ == '__main__':
    main()