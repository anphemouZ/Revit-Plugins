# -*- coding: utf-8 -*-
__title__ = 'Массовый\nОпуск'
__doc__ = 'Выделите группу коробов, нажмите Готово, затем кликните на щит. Скрипт массово опустит короба и создаст коннекторы.'

import math
import clr
import Autodesk.Revit.UI.Selection as Sel
from Autodesk.Revit.DB import *
from Autodesk.Revit.Exceptions import OperationCanceledException
from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ============ НАСТРОЙКИ ============
METHOD_PARAM = "Способ прокладки" 
METHOD_VALUE = "В лотке" 

PARAMS_TO_COPY = [
    "ADSK_Группирование", "GLF_Тип сети", "Источник",
    "Потребитель", "Номер электрической цепи", "Кабель",
    "Выбор короба", "Выбор проводника", "Имя нагрузки", "ADSK_Состав_Трассы"
]

# ============ УТИЛИТЫ И ПАРАМЕТРЫ ============

class FamilyOption(IFamilyLoadOptions):
    def OnFamilyFound(self, familyInUse, overwriteParameterValues): return True, True
    def OnSharedFamilyFound(self, sharedFamily, familyInUse, source, overwriteParameterValues): return True, True

def get_param_value(elem, param_name):
    p = elem.LookupParameter(param_name)
    if not p: p = elem.get_Parameter(getattr(BuiltInParameter, param_name, BuiltInParameter.INVALID))
    if p and p.HasValue:
        val = p.AsString()
        if not val: val = p.AsValueString()
        if val: return val.strip()
    return None

def copy_params(src, tgt):
    for p_name in PARAMS_TO_COPY:
        val = get_param_value(src, p_name)
        if val: 
            p = tgt.LookupParameter(p_name)
            if p and not p.IsReadOnly:
                try: p.Set(val)
                except: pass

def set_method(elem, val):
    p = elem.LookupParameter(METHOD_PARAM)
    if p and not p.IsReadOnly: 
        try: p.Set(val)
        except: pass

def get_connectors(elem):
    try:
        if hasattr(elem, "MEPModel") and elem.MEPModel: return list(elem.MEPModel.ConnectorManager.Connectors)
        elif hasattr(elem, "ConnectorManager"): return list(elem.ConnectorManager.Connectors)
    except: pass
    return []

def is_physical_connected(conn):
    try:
        if conn.ConnectorType == DB.ConnectorType.Logical: return False
        return conn.IsConnected
    except: return False

# ============ ФИЛЬТРЫ ВЫБОРА ============

class ConduitFilter(Sel.ISelectionFilter):
    def AllowElement(self, e): 
        return e.Category and e.Category.Id.IntegerValue == int(BuiltInCategory.OST_Conduit)
    def AllowReference(self, r, p): return True 

class EquipFilter(Sel.ISelectionFilter):
    def AllowElement(self, e): 
        return e.Category and e.Category.Id.IntegerValue == int(BuiltInCategory.OST_ElectricalEquipment)
    def AllowReference(self, r, p): return True 

# ============ ВЗЛОМ СЕМЕЙСТВА И КООРДИНАТЫ (МАССОВЫЙ) ============

def prepare_system_connectors_batch(doc, equip, connection_data):
    """Открывает семейство щита ОДИН РАЗ и создает все нужные коннекторы"""
    equip_conns = get_connectors(equip)
    pts_to_create = []
    
    # 1. Проверяем, какие коннекторы уже существуют
    for data in connection_data:
        ideal_pt = data['ideal_pt']
        found = False
        for c in equip_conns:
            if c.ConnectorType == DB.ConnectorType.End and not is_physical_connected(c) and c.Domain == DB.Domain.DomainCableTrayConduit:
                if c.Origin.DistanceTo(ideal_pt) < (1.0 / 12.0): # ~2.5 см
                    if c.CoordinateSystem.BasisZ.DotProduct(DB.XYZ.BasisZ) > 0.9:
                        data['conn_pt'] = c.Origin
                        found = True
                        break
        if not found:
            pts_to_create.append(data)

    if not pts_to_create:
        return # Все коннекторы уже есть на своих местах
        
    output.print_md("⚠️ Создаем новые коннекторы на щите ({} шт.)...".format(len(pts_to_create)))
    
    # 2. Массово внедряем коннекторы
    fam_doc = doc.EditFamily(equip.Symbol.Family)
    fam_trans = DB.Transaction(fam_doc, "Add Connectors")
    fam_trans.Start()
    
    equip_transform = equip.GetTransform()
    
    for data in pts_to_create:
        ideal_pt = data['ideal_pt']
        diam_val = data['diam']
        
        fam_pt = equip_transform.Inverse.OfPoint(ideal_pt)
        fam_normal = equip_transform.Inverse.OfVector(DB.XYZ.BasisZ)

        plane = DB.Plane.CreateByNormalAndOrigin(fam_normal, fam_pt)
        sp = DB.SketchPlane.Create(fam_doc, plane)
        arc = DB.Arc.Create(plane, 5.0 / 304.8, 0.0, 2 * math.pi)
        ca = DB.CurveArray(); ca.Append(arc)
        caa = DB.CurveArrArray(); caa.Append(ca)

        ext = fam_doc.FamilyCreate.NewExtrusion(True, caa, sp, 2.0 / 304.8)
        fam_doc.Regenerate()

        opt = DB.Options(); opt.ComputeReferences = True
        geom = ext.get_Geometry(opt)
        target_face_ref = None
        
        for obj in geom:
            if isinstance(obj, DB.Solid) and obj.Volume > 0:
                for face in obj.Faces:
                    if isinstance(face, DB.PlanarFace) and face.FaceNormal.IsAlmostEqualTo(fam_normal):
                        target_face_ref = face.Reference
                        break
                if target_face_ref: break

        if target_face_ref:
            conn = DB.ConnectorElement.CreateConduitConnector(fam_doc, target_face_ref)
            p_diam = conn.get_Parameter(BuiltInParameter.CONNECTOR_DIAMETER)
            if p_diam and not p_diam.IsReadOnly: p_diam.Set(diam_val)
            
        data['conn_pt'] = ideal_pt + DB.XYZ.BasisZ * (2.0 / 304.8)

    fam_trans.Commit()
    fam_doc.LoadFamily(doc, FamilyOption())
    fam_doc.Close(False)

# ============ ИДЕАЛЬНАЯ СТЯЖКА (AUTO-CONNECT) ============

def bridge_connect(fixed_elem, new_elem):
    cons1 = get_connectors(fixed_elem)
    cons2 = get_connectors(new_elem)
    best_pair = None
    min_dist = 1e9
    
    for x in cons1:
        if x.ConnectorType == DB.ConnectorType.Logical: continue
        if x.ConnectorType != DB.ConnectorType.End: continue 
        for y in cons2:
            if y.ConnectorType == DB.ConnectorType.Logical: continue
            if y.ConnectorType != DB.ConnectorType.End: continue 
            try:
                d = x.Origin.DistanceTo(y.Origin)
                if d < min_dist:
                    min_dist = d
                    best_pair = (x, y)
            except: pass
    
    if best_pair and min_dist < 5.0: 
        connA, connB = best_pair
        try:
            if is_physical_connected(connA): 
                for ref in connA.AllRefs:
                    if ref.Owner.Id == new_elem.Id: return True 
                connA.DisconnectFrom(connA.AllRefs[0])
            if is_physical_connected(connB): 
                for ref in connB.AllRefs:
                    if ref.Owner.Id == fixed_elem.Id: return True 
                connB.DisconnectFrom(connB.AllRefs[0])
            
            is_equip = (fixed_elem.Category.Id.IntegerValue == int(BuiltInCategory.OST_ElectricalEquipment) or 
                        new_elem.Category.Id.IntegerValue == int(BuiltInCategory.OST_ElectricalEquipment))
            if is_equip:
                try: 
                    connA.ConnectTo(connB)
                    return True
                except: pass

            vA = connA.CoordinateSystem.BasisZ
            vB = connB.CoordinateSystem.BasisZ
            dot = vA.DotProduct(vB)
            
            if dot < -0.99:
                try: doc.Create.NewUnionFitting(connA, connB)
                except: connA.ConnectTo(connB)
            else:
                try: doc.Create.NewElbowFitting(connA, connB)
                except: connA.ConnectTo(connB)
            return True
        except: pass
    return False

# ============ ГЛАВНЫЙ ЦИКЛ ============

def main():
    # 1. Получаем короба (из текущего выделения или просим выбрать)
    selection = revit.get_selection()
    conduits = [el for el in selection.elements if el.Category and el.Category.Id.IntegerValue == int(BuiltInCategory.OST_Conduit)]
    
    if not conduits:
        try:
            with forms.WarningBar(title="1. Выделите КОРОБА рамкой или кликом и нажмите 'Готово'"):
                refs = uidoc.Selection.PickObjects(Sel.ObjectType.Element, ConduitFilter(), "Выберите короба")
                conduits = [doc.GetElement(ref) for ref in refs]
        except OperationCanceledException:
            return

    if not conduits:
        return

    # 2. Получаем щит
    try:
        with forms.WarningBar(title="2. Кликните на ЩИТ"):
            equip_ref = uidoc.Selection.PickObject(Sel.ObjectType.Element, EquipFilter(), "Выберите щит")
            equip = doc.GetElement(equip_ref)
    except OperationCanceledException:
        return

    bbox = equip.get_BoundingBox(None)
    if not bbox:
        forms.alert("Не удалось определить габариты щита.", warn_icon=True)
        return
        
    target_z = bbox.Max.Z
    equip_xy = DB.XYZ(bbox.Min.X + (bbox.Max.X - bbox.Min.X)/2, bbox.Min.Y + (bbox.Max.Y - bbox.Min.Y)/2, 0)
    
    connection_data = []
    
    # 3. Собираем данные по каждому коробу
    for cond in conduits:
        free_ends = [c for c in get_connectors(cond) if c.ConnectorType == DB.ConnectorType.End and not is_physical_connected(c) and c.Domain == DB.Domain.DomainCableTrayConduit]
        
        if not free_ends:
            output.print_md("⚠️ Короб ID `{}` не имеет свободных концов. Пропуск.".format(cond.Id))
            continue
            
        # Выбираем тот конец, который физически ближе к центру щита в плане
        best_end = min(free_ends, key=lambda c: DB.XYZ(c.Origin.X, c.Origin.Y, 0).DistanceTo(equip_xy))
        
        pt_top = best_end.Origin
        pt_bottom = DB.XYZ(pt_top.X, pt_top.Y, target_z)
        
        d_p = cond.get_Parameter(BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM)
        diam = d_p.AsDouble() if d_p else (25.0 / 304.8)
        
        connection_data.append({
            'cond': cond,
            'pt_top': pt_top,
            'ideal_pt': pt_bottom,
            'diam': diam,
            'conn_pt': pt_bottom # Дефолтная точка, обновится если создастся коннектор
        })

    if not connection_data:
        return

    output.print_md("---")
    
    # 4. Массово создаем коннекторы на щите
    prepare_system_connectors_batch(doc, equip, connection_data)
    
    equip_live = doc.GetElement(equip.Id)
    
    # 5. Строим опуски и спаиваем
    with revit.Transaction("Массовый вертикальный опуск"):
        for data in connection_data:
            cond = doc.GetElement(data['cond'].Id)
            pt_top = data['pt_top']
            pt_bot = data['conn_pt']
            diam = data['diam']
            
            if pt_top.DistanceTo(pt_bot) > 0.15: # Больше 4.5 см
                seg = DB.Electrical.Conduit.Create(doc, cond.GetTypeId(), pt_top, pt_bot, cond.ReferenceLevel.Id)
                new_cond = doc.GetElement(seg) if isinstance(seg, DB.ElementId) else seg
                
                if new_cond:
                    new_cond.get_Parameter(BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM).Set(diam)
                    copy_params(cond, new_cond)
                    set_method(new_cond, METHOD_VALUE)
                    
                    doc.Regenerate()
                    
                    bridge_connect(cond, new_cond)
                    bridge_connect(new_cond, equip_live)
            else:
                bridge_connect(cond, equip_live)
                
    output.print_md("### 🎯 Успешно подключено коробов: **{}**".format(len(connection_data)))

if __name__ == '__main__':
    main()