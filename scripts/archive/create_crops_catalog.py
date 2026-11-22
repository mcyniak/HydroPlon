from pathlib import Path
import json, re
root=Path('/mnt/data')
s=(root/'modu_3_1_katalog_upraw_i_parametry_agronomiczne_fao_56_iung_crops_json_crop_catalog.md').read_text()
block=re.findall(r'```json\s*(.*?)\s*```',s,re.S)[0]
source=json.loads(block)
existing={c['crop_id']:c for c in source['crops']}
entries=[
 ('maize_grain','Kukurydza (ziarno / kiszonka)','uprawa jara'),
 ('winter_wheat','Pszenica ozima','zboze ozime'),
 ('winter_rapeseed','Rzepak ozimy','oleista ozima'),
 ('potato','Ziemniak','okopowa'),
 ('sugar_beet','Burak cukrowy','okopowa'),
 ('spring_barley','Jeczmien jary','zboze jare'),
 ('winter_triticale_rye','Pszenzyto ozime / zyto','zboze ozime'),
 ('grassland','Trwale uzytki zielone','uzytek zielony'),
]
crops=[]
for cid,name,category in entries:
    if cid in existing:
        item=existing[cid].copy()
        item.pop('iung_critical_kbw_mm',None)  # Nieporownywalne z P-ETc.
        item.pop('root_awc_multiplier',None)  # Nie wolno ekstrapolowac 0-30 cm na caly profil.
        item['parameter_status']='provisional'
        item['field_model_version']='2026.11-draft'
        item['limitations']=['Parametry faz i Kc pochodza z materialu zespolu i wymagaja agronomicznej walidacji lokalnej.', 'Nie sa to dane ani progi oficjalnego SMSR IUNG.']
    else:
        item={'crop_id':cid,'name_pl':name,'category':category,
              'parameter_status':'pending_validation',
              'water_sensitivity_class':'nieustalona',
              'root_depth_m':None, 'depletion_fraction_p':None,
              'critical_months':[], 'critical_stage_label_pl':'wymaga walidacji',
              'phenology_calendar_pl':None, 'kc_values':None,
              'agronomic_notes_pl':'Uprawa w katalogu; analiza liczbowa wymaga zweryfikowanych parametrow.',
              'field_model_version':'2026.11-draft',
              'limitations':['Brak potwierdzonych parametrow dla obliczen; nie nalezy imputowac z innych gatunkow.']}
    crops.append(item)
result={'version':'2.0.0','parameter_status_legend':{'provisional':'dane demonstracyjne do walidacji','pending_validation':'brak danych do obliczen','validated':'przyjete i zweryfikowane parametry'},
 'methodology_reference':['https://www.fao.org/4/x0490e/x0490e0b.htm','https://www.fao.org/4/x0490e/x0490e0e.htm','https://susza.iung.pulawy.pl/system/'],
 'note':'Osiem pozycji katalogowych, trzy z parametrami roboczymi z materialow zespolu, piec wymaga uzupelnienia. Uzycie progu IUNG do wlasnego P-ETc jest zabronione.',
 'crops':crops}
p=root/'HydroPlon_Etap3_Diagnosis_v4/backend/app/data/crops.json'
p.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(p, len(crops),sum(x['parameter_status']=='provisional' for x in crops))
