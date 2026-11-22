// HydroPlon /app — logika kreatora (4 kroki). Wszystkie dane z API backendu; słowniki na górze to tylko etykiety.
(() => {
'use strict';
// ---------- słowniki prezentacji (tylko etykiety; wartości zawsze z API) ----------
const CROP_UI = {
  maize_grain: { label: 'Kukurydza', icon: 'crop-corn' }, winter_wheat: { label: 'Pszenica ozima', icon: 'crop-wheat' },
  winter_rapeseed: { label: 'Rzepak ozimy', icon: 'crop-rapeseed' }, potato: { label: 'Ziemniaki', icon: 'crop-potato' },
  sugar_beet: { label: 'Buraki cukrowe', icon: 'crop-beet' }, spring_barley: { label: 'Jęczmień jary', icon: 'crop-barley' },
  winter_triticale_rye: { label: 'Żyto / pszenżyto', icon: 'crop-rye' }, grassland: { label: 'Łąki i pastwiska', icon: 'crop-grass' },
};
// Ikony metod: symbole SVG #m-<method_id> w app.html.
const methodIcon = (id) => `<svg class="icon"><use href="#m-${id}"/></svg>`;
const MIN_CLICK_ZOOM = 14; // poniżej tego przybliżenia kliknięcie nie wskazuje konkretnej działki
const EXAMPLE_PARCEL = { id: '120404_2.0006.131', label: 'Wójcina, gm. Mędrzechów (małopolskie)' };
const PROBLEM_PL = { periodic_water_deficit: 'Okresowe niedobory wody', surface_runoff_erosion: 'Spływ powierzchniowy i erozja',
  low_soil_retention: 'Niska retencja wodna gleby', low_organic_matter: 'Niska zawartość materii organicznej' };
const LEVEL_PL = { high: 'wysokie', medium: 'średnie', low: 'niskie', unknown: 'nieustalone' };
const CONF_PL = { high: 'wysoka', medium: 'średnia', low: 'niska', unknown: 'nieustalona' };
const MODULE_PL = { soil: 'Gleba', terrain: 'Teren (NMT)', hydrology: 'Hydrologia', weather: 'Pogoda (IMGW)', satellite: 'Satelity (Sentinel-2)' };
const MSTATUS_PL = { ok: 'dane dostępne', partial: 'dane częściowe', unavailable: 'dane niedostępne', unknown: 'brak oceny', error: 'błąd źródła',
  parameters_pending_validation: 'Brak parametrów uprawy do bilansu', no_data: 'Brak danych działki do obliczenia bilansu' };
const MATCH_PL = { possible: 'Możliwe', eligible: 'Kwalifikuje się', likely_eligible: 'Prawdopodobnie kwalifikuje się', needs_verification: 'Do weryfikacji', not_eligible: 'Nie kwalifikuje się' };
const ID_PL = { wind_exposure: 'narażenie na wiatr', low_soil_retention: 'niska retencja gleby', low_organic_matter: 'niska zawartość materii organicznej',
  surface_runoff_erosion: 'spływ powierzchniowy i erozja', periodic_water_deficit: 'okresowe niedobory wody', drainage_context: 'kontekst drenażu', flood_hazard: 'zagrożenie powodziowe' };
// Teksty z silnika reguł zawierają identyfikatory problemów — na ekranie pokazujemy polskie nazwy.
const human = (t) => String(t ?? '').replace(/[a-z]+(?:_[a-z0-9]+)+/g, (id) => ID_PL[id] || id);
const CALL_PL = { open: 'nabór otwarty', closed: 'nabór zakończony', announced: 'nabór zapowiedziany', upcoming: 'nabór zapowiedziany', planned: 'nabór planowany', unknown: 'termin naboru nieznany' };

// ---------- narzędzia ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 0) => (v == null || Number.isNaN(Number(v))) ? null : Number(v).toLocaleString('pl-PL', { maximumFractionDigits: d });
const zl = (v) => v == null ? null : `${num(v, 0)} zł`;
const or = (v, fallback = 'brak danych') => (v == null || v === '') ? `<span class="na">${esc(fallback)}</span>` : esc(v);
const fmtDate = (iso) => (iso ? String(iso).slice(0, 10).split('-').reverse().join('.') : '');
const todayStr = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const plural = (n, a, b, c) => n === 1 ? a : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) ? b : c;
let toastTimer;
function toast(msg) { const el = $('toast'); el.textContent = msg; el.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 3500); }

async function api(method, url, body) {
  let r;
  try { r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined }); }
  catch (e) { throw new Error('Brak połączenia z serwerem HydroPlon.'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const err = new Error(data.message_pl || `Błąd serwera (${r.status}).`); err.code = data.error_code; err.status = r.status; throw err; }
  return data;
}

// ---------- stan kreatora ----------
const state = {
  step: 1, health: null, parcel: null, date: todayStr(),
  analysis: null, analysisErr: null, analysisPromise: null, analysisKey: null, analysisT0: 0,
  crops: [], cropId: null, fc: { drainage: 'unknown', retention: 'unknown', types: [] },
  assessment: null, ranking: null, selected: new Set(), selectedFor: null,
  selection: null, plan: null, busy: false,
};
let parcelSeq = 0, analysisSeq = 0;

// Zmiana danych nadrzędnych unieważnia wyniki zależne (spec. §53).
function invalidate(level) {
  if (level <= 1) { ++analysisSeq; state.analysis = null; state.analysisErr = null; state.analysisPromise = null; state.analysisKey = null; }
  if (level <= 2) { state.assessment = null; state.ranking = null; }
  if (level <= 3) { state.selection = null; state.plan = null; }
}
function reachable(n) { return n === 1 || (n === 2 && !!state.parcel && !!state.date) || (n === 3 && !!state.ranking) || (n === 4 && !!state.plan); }

// ---------- mapa ----------
const map = L.map('map', { zoomControl: false }).setView([52.1, 19.4], 6);
L.control.zoom({ position: 'bottomright' }).addTo(map);
const baseMap = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap' }).addTo(map);
const baseOrto = L.tileLayer.wms('https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution',
  { layers: 'Raster', format: 'image/jpeg', maxZoom: 19, attribution: 'Ortofotomapa &copy; GUGiK' });
const addressLayer = L.layerGroup().addTo(map);
const parcelLayer = L.geoJSON(null, { style: { color: '#0E6548', weight: 3, fillColor: '#18865E', fillOpacity: 0.2 } }).addTo(map);
const fixtureLayer = L.geoJSON(null, { style: { color: '#BA830D', weight: 1.5, dashArray: '4', fillOpacity: 0.06 },
  onEachFeature: (f, l) => l.bindTooltip(`${esc(f.properties.label)}: ${esc(f.properties.scenario_pl)}`) }).addTo(map);
window.hpMap = map;
// Mapa może powstać zanim zewnętrzny CSS nada panelowi wymiary (albo gdy krok 1 jest ukryty) — przeliczamy rozmiar przy każdej zmianie.
if ('ResizeObserver' in window) new ResizeObserver(() => map.invalidateSize()).observe($('map-panel'));
document.querySelectorAll('[data-map-mode]').forEach((btn) => btn.addEventListener('click', () => {
  document.querySelectorAll('[data-map-mode]').forEach((b) => b.classList.toggle('active', b === btn));
  if (btn.dataset.mapMode === 'sat') { map.removeLayer(baseMap); baseOrto.addTo(map); } else { map.removeLayer(baseOrto); baseMap.addTo(map); }
  parcelLayer.bringToFront();
}));
map.on('click', (e) => {
  // Przy widoku kraju kliknięcie trafiłoby w przypadkową działkę — najpierw przybliżamy w kliknięte miejsce.
  if (map.getZoom() < MIN_CLICK_ZOOM) { map.setView(e.latlng, Math.min(map.getZoom() + 4, MIN_CLICK_ZOOM + 1)); return; }
  loadParcel(`/api/parcels/by-point?lat=${e.latlng.lat.toFixed(7)}&lon=${e.latlng.lng.toFixed(7)}`, 'Szukam działki w klikniętym miejscu…');
});
function updateMapChip() {
  if (addressLayer.getLayers().length) {
    $('map-chip-text').textContent = 'Kliknij działkę w okolicy adresu';
  } else if (state.parcel) {
    const p = state.parcel;
    $('map-chip-text').textContent = `Działka ${p.administrative_location.parcel_number || p.parcel_id}`;
  } else {
    $('map-chip-text').textContent = map.getZoom() < MIN_CLICK_ZOOM ? 'Przybliż mapę do swojej działki' : 'Kliknij działkę, aby ją wybrać';
  }
}
map.on('zoomend', updateMapChip);

// ---------- krok 1: działka ----------
const addressInput = $('address-input');
const addressButton = $('address-search');
const addressStatus = $('address-status');
const addressResults = $('address-results');

function showAddress(place) {
  if ($('parcel-search').disabled) {
    ++parcelSeq;
    $('parcel-search').disabled = false;
    renderParcelCard();
  }
  const point = [place.lat, place.lon];
  const zoom = { address: 17, street: 16, city: MIN_CLICK_ZOOM }[place.kind];
  addressLayer.clearLayers();
  L.circleMarker(point, { radius: 8, color: '#805A0B', fillColor: '#F4CF65', fillOpacity: 1, interactive: false }).addTo(addressLayer);
  map.invalidateSize({ pan: false });
  map.setView(point, zoom, { animate: false });
  updateMapChip();
  addressStatus.textContent = `${place.label}. Kliknij działkę na mapie.`;
  $('map-panel').scrollIntoView({ block: 'nearest' });
}

$('address-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (addressButton.disabled) return;
  const query = addressInput.value.trim();
  if (query.length < 3) {
    addressStatus.textContent = 'Wpisz co najmniej 3 znaki adresu lub nazwy miejscowości.';
    addressInput.focus();
    return;
  }
  const parcelRequest = parcelSeq;
  addressButton.disabled = true;
  addressLayer.clearLayers();
  updateMapChip();
  addressResults.replaceChildren();
  addressResults.hidden = true;
  addressStatus.textContent = 'Szukam adresu w Geoportalu…';
  try {
    const results = await api('GET', `/api/parcels/address-search?q=${encodeURIComponent(query)}`);
    if (parcelRequest !== parcelSeq) { addressStatus.textContent = ''; return; }
    if (results.length === 1) {
      showAddress(results[0]);
    } else if (results.length > 1) {
      addressStatus.textContent = 'Wybierz wynik, aby przybliżyć mapę.';
      for (const place of results) {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'link-btn';
        button.textContent = place.label;
        button.addEventListener('click', () => showAddress(place));
        item.append(button);
        addressResults.append(item);
      }
      addressResults.hidden = false;
    } else {
      addressStatus.textContent = 'Brak wyników. Sprawdź nazwę miejscowości, ulicę i numer.';
    }
  } catch (error) {
    addressStatus.textContent = parcelRequest === parcelSeq ? error.message : '';
  } finally {
    addressButton.disabled = addressInput.disabled;
  }
});
async function loadParcel(url, msg) {
  const my = ++parcelSeq;
  hideError();
  $('parcel-detail-slot').innerHTML = `<div class="status-box"><span class="spinner"></span><span>${esc(msg || 'Pobieram działkę z GUGiK…')}</span></div>`;
  $('parcel-search').disabled = true;
  try {
    const p = await api('GET', url);
    if (my !== parcelSeq) return;
    setParcel(p);
  } catch (e) {
    if (my !== parcelSeq) return;
    $('parcel-detail-slot').innerHTML = `<div class="status-box err"><svg class="icon"><use href="#icon-info"/></svg><span><b>Nie udało się pobrać działki.</b><br>${esc(e.message)}</span></div>`;
  } finally { if (my === parcelSeq) $('parcel-search').disabled = false; }
}
function setParcel(p) {
  const changed = !state.parcel || state.parcel.parcel_id !== p.parcel_id;
  state.parcel = p;
  $('parcel-input').value = p.parcel_id;
  parcelLayer.clearLayers(); parcelLayer.addData(p.geojson_feature);
  map.invalidateSize();
  map.fitBounds(parcelLayer.getBounds(), { maxZoom: 17, padding: [40, 40] });
  if (addressLayer.getLayers().length) {
    addressLayer.clearLayers();
    addressStatus.textContent = '';
  }
  updateMapChip();
  renderParcelCard();
  if (changed) { invalidate(1); }
  startAnalysis();
  updateStep();
}
function renderParcelCard() {
  const p = state.parcel;
  if (!p) {
    const live = state.health?.data_mode === 'live';
    $('parcel-detail-slot').innerHTML = live ? `<div class="parcel-empty"><button type="button" class="link-btn" data-example>Zobacz przykład: działka ${esc(EXAMPLE_PARCEL.id)} — ${esc(EXAMPLE_PARCEL.label)}</button></div>` : '';
    return;
  }
  const m = p.metrics, s = p.source, a = p.administrative_location;
  const loc = [a.region && `obręb ${a.region}`, a.commune && `gm. ${a.commune}`, a.county && `pow. ${a.county}`, a.voivodeship && `woj. ${a.voivodeship}`].filter(Boolean).join(', ');
  $('parcel-detail-slot').innerHTML = `<div class="parcel-detail">
      <div class="parcel-detail-top"><svg class="icon"><use href="#icon-check"/></svg>DZIAŁKA WYBRANA${s.is_demonstration ? ' <span class="tag-demo">DANE TESTOWE</span>' : ''}</div>
      <div class="parcel-id">${esc(p.parcel_id)}</div>
      <div class="parcel-location">${esc(loc || a.full_label || '')}</div>
      <div class="parcel-stats"><div><small>Powierzchnia (z geometrii)</small><b>${esc(num(m.area_ha, 2))} ha</b>${m.is_multipart ? `<small>${esc(m.part_count)} części</small>` : ''}</div>
      <div style="text-align:right"><small>Źródło granic</small><b style="font-size:13px">${esc(s.service)}</b><small>${esc(s.datasource || '')}</small></div></div>
    </div>
    <details class="notes"><summary>Uwagi do danych ewidencyjnych (${p.notices.length})</summary><ul>${p.notices.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></details>`;
}
$('parcel-search').addEventListener('click', () => {
  const v = $('parcel-input').value.trim();
  if (!v) { toast('Wpisz identyfikator działki.'); $('parcel-input').focus(); return; }
  loadParcel(`/api/parcels/by-id?id=${encodeURIComponent(v)}`);
});
$('parcel-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('parcel-search').click(); } });
const dateInput = $('date-input');
const dateError = $('date-error');
function updateAnalysisDate() {
  const date = dateInput.validity.valid && /^\d{4}-\d{2}-\d{2}$/.test(dateInput.value) ? dateInput.value : null;
  dateInput.setAttribute('aria-invalid', String(!date));
  dateError.hidden = !!date;
  if (!date) {
    dateError.textContent = dateInput.validity.rangeUnderflow
      ? `Najwcześniejsza dostępna data analizy to ${fmtDate(dateInput.min)}.`
      : 'Wpisz poprawną datę analizy.';
  } else {
    dateError.textContent = '';
  }
  if (date === state.date) return;
  state.date = date;
  if (date && date > todayStr()) toast('Dla daty przyszłej pokażemy analizę scenariuszową.');
  invalidate(1);
  if (state.parcel && date) startAnalysis();
  else renderAnalysisStatus();
  updateStep();
}
dateInput.addEventListener('change', updateAnalysisDate);

// ---------- analiza w tle (etap 2 backendu, bez osobnego ekranu) ----------
function startAnalysis(force) {
  if (!state.parcel || !state.date) return null;
  const key = `${state.parcel.parcel_id}|${state.date}`;
  if (!force && state.analysisKey === key && (state.analysisPromise || state.analysis)) return state.analysisPromise;
  const my = ++analysisSeq;
  state.analysisKey = key; state.analysis = null; state.analysisErr = null; state.analysisT0 = Date.now();
  const p = api('POST', '/api/analysis', { parcel_id: state.parcel.parcel_id, as_of_date: state.date })
    .then((an) => { if (my === analysisSeq) { state.analysis = an; state.analysisPromise = null; renderAnalysisStatus(); } return an; },
          (e) => { if (my === analysisSeq) { state.analysisErr = e; state.analysisPromise = null; renderAnalysisStatus(); } throw e; });
  p.catch(() => {});
  state.analysisPromise = p;
  renderAnalysisStatus();
  return p;
}
function unavailableModules(an) { return Object.entries(an.module_statuses || {}).filter(([, v]) => v !== 'ok').map(([k, v]) => ({ k, v })); }
function analysisStatusHtml() {
  if (!state.parcel) return '';
  if (state.analysisPromise) {
    const s = Math.round((Date.now() - state.analysisT0) / 1000);
    return `<div class="status-box"><span class="spinner"></span><span><b>Analiza działki w toku · ${s} s</b><br>Gleba, teren, hydrologia, pogoda i zdjęcia satelitarne z publicznych źródeł. Możesz w tym czasie przejść dalej i wybrać uprawę.</span></div>`;
  }
  if (state.analysisErr) {
    return `<div class="status-box err"><svg class="icon"><use href="#icon-info"/></svg><span><b>Analiza nie powiodła się.</b> ${esc(state.analysisErr.message)}<br><button type="button" class="link-btn" data-retry-analysis>Ponów analizę</button></span></div>`;
  }
  const an = state.analysis; if (!an) return '';
  const missing = unavailableModules(an);
  const n = 5 - missing.length;
  if (!missing.length) return `<div class="status-box ok"><svg class="icon"><use href="#icon-check"/></svg><span><b>Analiza działki gotowa</b> · ${n}/5 źródeł danych · stan na ${esc(an.time_context.as_of_date)}</span></div>`;
  return `<div class="status-box warn"><svg class="icon"><use href="#icon-info"/></svg><span><b>Analiza częściowa (${n}/5 źródeł)</b> · stan na ${esc(an.time_context.as_of_date)} · brak: ${missing.map((m) => `${esc(MODULE_PL[m.k] || m.k)} — ${esc(MSTATUS_PL[m.v] || m.v)}`).join(', ')}. Braki są oznaczone, a zależne metody dostaną status warunkowy.<br><button type="button" class="link-btn" data-retry-analysis>Ponów pobranie brakujących danych</button></span></div>`;
}
function renderAnalysisStatus() {
  const html = analysisStatusHtml();
  $('an-status-1').innerHTML = html; $('an-status-2').innerHTML = html;
  if (state.step === 3) renderPartialNote();
}
setInterval(() => { if (state.analysisPromise) renderAnalysisStatus(); }, 1000);
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-example]')) { loadParcel(`/api/parcels/by-id?id=${encodeURIComponent(EXAMPLE_PARCEL.id)}`); return; }
  if (!e.target.closest('[data-retry-analysis]')) return;
  invalidate(1); startAnalysis(true); updateStep();
});

// ---------- krok 2: uprawa i deklaracje ----------
function renderCrops() {
  $('crop-balance-hint').hidden = !state.crops.some((c) => !c.available_for_assessment);
  $('crop-grid').innerHTML = state.crops.map((c) => {
    const ui = CROP_UI[c.crop_id] || { label: c.name_pl, icon: 'crop-grass' };
    const sel = state.cropId === c.crop_id;
    const missingParams = !c.available_for_assessment;
    return `<button type="button" class="crop-button ${sel ? 'selected' : ''}" data-crop="${esc(c.crop_id)}" aria-pressed="${sel}" title="${esc(c.name_pl)}"${missingParams ? ' aria-describedby="crop-balance-hint"' : ''}>
      <span class="crop-circle"><svg class="crop-svg" viewBox="0 0 100 100"><use href="#${ui.icon}"/></svg><span class="crop-check"><svg class="icon"><use href="#icon-check"/></svg></span></span>
      <span class="crop-name">${esc(ui.label)}</span>${missingParams ? '<span class="crop-note">Brak parametrów do bilansu</span>' : ''}</button>`;
  }).join('');
}
$('crop-grid').addEventListener('click', (e) => {
  const b = e.target.closest('[data-crop]'); if (!b || state.busy) return;
  if (state.cropId !== b.dataset.crop) { state.cropId = b.dataset.crop; invalidate(2); }
  renderCrops(); updateStep();
});
function readFieldContext() {
  const drainage = document.querySelector('input[name="drainage"]:checked').value;
  const retention = document.querySelector('input[name="retention"]:checked').value;
  if (retention !== 'yes') document.querySelectorAll('#retention-types input').forEach((i) => { i.checked = false; });
  $('retention-types').classList.toggle('visible', retention === 'yes');
  const types = [...document.querySelectorAll('#retention-types input:checked')].map((i) => i.value);
  state.fc = { drainage, retention, types };
  invalidate(2); updateStep();
}
document.querySelectorAll('input[name="drainage"], input[name="retention"], #retention-types input').forEach((i) => i.addEventListener('change', readFieldContext));

async function computeRanking() {
  const key = state.analysisKey;
  let an = state.analysis;
  if (!an) {
    if (!state.analysisPromise) startAnalysis(true);
    setNavNote('Czekam na zakończenie analizy działki…', true);
    an = await state.analysisPromise;
  }
  if (key !== state.analysisKey) throw new Error('Działka lub data zmieniły się w trakcie obliczeń — spróbuj ponownie.');
  setNavNote('Oceniam potrzeby wodne uprawy i dobieram metody…', true);
  const ca = await api('POST', '/api/crops/assess', { analysis_id: an.analysis_id, crop_id: state.cropId });
  const fc = { subsurface_drainage_status: state.fc.drainage, existing_retention_status: state.fc.retention,
    existing_retention_types: state.fc.retention === 'yes' ? state.fc.types : [] };
  const rk = await api('POST', '/api/recommendations', { analysis_id: an.analysis_id, crop_assessment_id: ca.crop_assessment_id, field_context: fc });
  state.assessment = ca; state.ranking = rk;
  if (state.selectedFor !== rk.ranking_run_id) {   // domyślny zestaw tylko dla nowego rankingu (spec. §59)
    state.selected = new Set([...rk.recommended, ...rk.conditional].filter((m) => m.default_selected).map((m) => m.method_id));
    state.selectedFor = rk.ranking_run_id;
    state.selection = null; state.plan = null;
  }
}

// ---------- krok 3: metody ----------
function allMethods() {
  const rk = state.ranking; if (!rk) return [];
  const scr = Object.fromEntries((rk.screening?.methods || []).map((s) => [s.method_id, s]));
  return [...rk.recommended, ...rk.conditional, ...rk.not_recommended].map((m) => ({ ...m, scr: scr[m.method_id] || {} }));
}
function renderPartialNote() {
  const an = state.analysis; const missing = an ? unavailableModules(an) : [];
  $('methods-partial').innerHTML = missing.length ? `<div class="status-box warn" style="margin-bottom:16px"><svg class="icon"><use href="#icon-info"/></svg><span><b>Ranking na niepełnych danych:</b> brak — ${missing.map((m) => esc(MODULE_PL[m.k] || m.k)).join(', ')}. Metody, których potrzeby nie da się ocenić, mają status warunkowy.</span></div>` : '';
}
function renderMethods() {
  const list = allMethods();
  const ca = state.assessment;
  $('methods-context').textContent = `Dla działki ${state.parcel?.parcel_id || ''} · ${CROP_UI[state.cropId]?.label || ca?.crop?.name_pl || ''} · stan na ${state.analysis?.time_context?.as_of_date || state.date}`;
  renderPartialNote();
  $('method-list').innerHTML = list.map((m) => {
    const st = m.screening_status, sel = state.selected.has(m.method_id), blocked = st === 'not_recommended';
    const cls = st === 'recommended' ? 'recommended' : st === 'conditional' ? 'conditional' : 'not-recommended';
    const badge = st === 'recommended' ? '<span class="method-status rec">✓ Rekomendowana</span>' : st === 'conditional' ? '<span class="method-status cond">Wymaga weryfikacji</span>' : '<span class="method-status not">Niezalecana</span>';
    const expl = human((m.scr.explanation_pl || m.rationale_pl || []).slice(0, 2).join(' '));
    const verif = (m.scr.required_verifications || []).length;
    return `<label class="method-row ${cls} ${sel ? 'selected' : ''}">
      <input type="checkbox" class="method-check" data-method="${esc(m.method_id)}" ${sel ? 'checked' : ''} ${blocked ? 'disabled' : ''} aria-label="${esc(m.name_pl)}"/>
      <span class="method-icon" aria-hidden="true">${methodIcon(m.method_id)}</span>
      <span><span class="method-heading">${esc(m.name_pl)}</span><span class="method-description" style="display:block">${esc(expl)}${verif ? ` <i>Weryfikacje do planu: ${verif}.</i>` : ''}</span></span>
      ${badge}</label>`;
  }).join('');
  const n = state.selected.size;
  $('selected-counter').textContent = `Wybrano ${n} ${plural(n, 'metodę', 'metody', 'metod')}`;
}
$('method-list').addEventListener('change', (e) => {
  const ch = e.target.closest('[data-method]'); if (!ch || state.busy) return;
  ch.checked ? state.selected.add(ch.dataset.method) : state.selected.delete(ch.dataset.method);
  state.selection = null; state.plan = null;
  renderMethods(); updateStep();
});

async function computePlan() {
  setNavNote('Liczę kosztorys, opłacalność, dotacje i harmonogram…', true);
  const ids = allMethods().filter((m) => state.selected.has(m.method_id)).map((m) => m.method_id);
  const sel = await api('POST', '/api/selections', { analysis_id: state.analysis.analysis_id, ranking_run_id: state.ranking.ranking_run_id, selected_method_ids: ids });
  const fp = await api('POST', '/api/plans', { selection_id: sel.selection_id });
  state.selection = sel; state.plan = fp;
}

// ---------- krok 4: raport ----------
function costPerMethod(costs, methodId) {
  let capex = 0, opex = 0, unpriced = 0;
  for (const it of costs.items) {
    if (it.shared || !(it.method_ids || []).includes(methodId)) continue;
    if (it.total_net_pln == null) { unpriced++; continue; }
    if (it.cost_type === 'CAPEX') capex += it.total_net_pln; else opex += it.total_net_pln;
  }
  return { capex, opex, unpriced };
}
function costLabel({ capex, opex, unpriced }) {
  const parts = [];
  if (capex > 0) parts.push(zl(capex));
  if (opex > 0) parts.push(`${zl(opex)}/rok`);
  if (unpriced) parts.push('do wyceny');
  return parts.length ? parts.join(' + ') : 'w pozycjach wspólnych';
}
function renderImplementation(impl) {
  const phases = new Map();
  for (const task of impl.tasks) {
    if (!phases.has(task.phase)) phases.set(task.phase, { label: task.phase_label_pl, tasks: [] });
    phases.get(task.phase).tasks.push(task);
  }

  const taskCount = (n) => `${n} ${plural(n, 'zadanie', 'zadania', 'zadań')}`;
  const time = (date) => `<time datetime="${esc(date)}">${esc(fmtDate(date))}</time>`;
  const dateRange = (task) => task.window_to
    ? `${time(task.window_from)} – ${time(task.window_to)}`
    : `od ${time(task.window_from)}`;
  const chevron = '<svg class="icon" aria-hidden="true"><use href="#icon-chevron"/></svg>';

  const taskHtml = (task) => {
    const codes = (task.program_ids || []).map((label) => label.split(' ')[0]);
    return `<li class="implementation-task" id="implementation-${esc(task.task_id)}" tabindex="-1">
      <div class="implementation-task-heading">
        <strong>${esc(task.title_pl)}</strong>
        ${task.status === 'requires_verification' ? '<span class="implementation-check">Wymaga weryfikacji</span>' : ''}
      </div>
      <div class="implementation-task-meta">
        ${task.window_from ? `<span class="implementation-date">${dateRange(task)}</span>` : `<span>${esc(task.timing_pl)}</span>`}
        ${codes.length ? `<span class="implementation-program">${esc(codes.join(', '))}</span>` : ''}
      </div>
      ${task.completion_check_pl ? `<p class="implementation-completion">Sprawdzenie wykonania · ${esc(task.completion_check_pl)}</p>` : ''}
    </li>`;
  };

  const phaseHtml = ([, phase], index) => `<li class="implementation-stage">
    <span class="implementation-step" aria-hidden="true">${index + 1}</span>
    <details class="implementation-phase"${index === 0 ? ' open' : ''}>
      <summary>
        <span><span class="implementation-phase-title">${esc(phase.label)}</span>
          <span class="implementation-phase-count">${taskCount(phase.tasks.length)}</span></span>
        ${chevron}
      </summary>
      <ul class="implementation-tasks" role="list">${phase.tasks.map(taskHtml).join('')}</ul>
    </details>
  </li>`;

  const dated = impl.tasks.filter((task) => task.window_from)
    .sort((a, b) => a.window_from.localeCompare(b.window_from));
  const deadlineList = (tasks) => `<ul class="implementation-deadlines" role="list">${tasks.map((task) => `<li>
    <span class="implementation-date">${dateRange(task)}</span>
    <a href="#implementation-${encodeURIComponent(task.task_id)}" data-implementation-task="implementation-${esc(task.task_id)}">${esc(task.title_pl)}</a>
    ${task.window_note_pl ? `<p>${esc(task.window_note_pl)}</p>` : ''}
  </li>`).join('')}</ul>`;
  const upcomingCount = 3;
  const laterCount = dated.length - upcomingCount;
  const checks = impl.stop_go_conditions || [];
  const warnings = impl.warnings || [];
  const phaseCount = phases.size;
  return `<section class="report-section implementation-plan" aria-labelledby="implementation-title">
    <header class="implementation-header">
      <span class="implementation-icon"><svg class="icon" aria-hidden="true"><use href="#icon-plan"/></svg></span>
      <div>
        <h3 id="implementation-title">Plan wdrożenia</h3>
        <p>${phaseCount} ${plural(phaseCount, 'etap', 'etapy', 'etapów')} · ${taskCount(impl.tasks.length)}</p>
      </div>
    </header>
    ${phaseCount ? `<div class="implementation-layout">
      <div>
        <h4>Etapy prac</h4>
        <ol class="implementation-stages" role="list">${[...phases].sort(([a], [b]) => a - b).map(phaseHtml).join('')}</ol>
      </div>
      <aside class="implementation-sidebar" aria-label="Terminy i warunki wdrożenia">
        <div class="implementation-agenda">
          <h4><svg class="icon" aria-hidden="true"><use href="#icon-calendar"/></svg>Najbliższe terminy</h4>
          ${dated.length ? deadlineList(dated.slice(0, upcomingCount)) : '<p>Szczegóły dotyczące czasu realizacji znajdziesz przy zadaniach.</p>'}
          ${laterCount > 0 ? `<details class="implementation-more"><summary>Kolejne terminy (${laterCount})</summary>${deadlineList(dated.slice(upcomingCount))}</details>` : ''}
        </div>
        ${checks.length ? `<details class="implementation-conditions">
          <summary>Warunki rozpoczęcia (${checks.length})</summary>
          <ul>${checks.map((condition) => `<li>${esc(condition)}</li>`).join('')}</ul>
        </details>` : ''}
      </aside>
    </div>` : '<p class="implementation-empty">Brak zadań w planie wdrożenia.</p>'}
    ${warnings.length ? `<ul class="fine-list">${warnings.map((warning) => `<li>${esc(warning)}</li>`).join('')}</ul>` : ''}
  </section>`;
}

function renderReport() {
  const fp = state.plan; if (!fp) { $('report').innerHTML = ''; return; }
  const pr = fp.section_1_profitability, costs = fp.section_2_costs, t = costs.totals, fund = fp.section_3_funding, impl = fp.section_4_implementation;
  const an = state.analysis, ca = state.assessment;
  const methods = allMethods().filter((m) => state.selected.has(m.method_id));
  const demoPrices = costs.price_catalog_is_demonstration || pr.baseline?.is_demonstration;
  $('report-context').innerHTML = `Działka <b>${esc(state.parcel.parcel_id)}</b> · ${esc(num(state.parcel.metrics.area_ha, 2))} ha · ${esc(CROP_UI[state.cropId]?.label || '')} · stan na ${esc(an.time_context.as_of_date)} · plan <code>${esc(fp.plan_id)}</code>`;

  // 1. Opłacalność
  const be = pr.break_even || {};
  const benefit = pr.annual_net_benefit_pln;
  const hero = benefit != null ? `${zl(benefit)} / rok` : 'Do oszacowania';
  const benefitHtml = `<div class="benefit-card"><div class="report-heading"><svg class="icon"><use href="#icon-chart"/></svg>OPŁACALNOŚĆ I POTENCJALNE KORZYŚCI${demoPrices ? '<span class="tag-demo">CENY DEMONSTRACYJNE</span>' : ''}</div>
    <div class="benefit-main">${esc(hero)}</div><p>${esc(pr.headline_pl)}</p>
    <div class="benefit-metrics">
      <div class="benefit-metric"><span>Roczna korzyść netto</span><b>${benefit != null ? esc(zl(benefit)) : 'Do oszacowania'}</b></div>
      <div class="benefit-metric"><span>Okres zwrotu</span><b>${pr.simple_payback_years != null ? `${esc(num(pr.simple_payback_years, 1))} lat` : 'Do oszacowania'}</b></div>
      <div class="benefit-metric"><span>Próg opłacalności (${esc(be.horizon_years ?? '—')} lat)</span><b>${be.required_annual_benefit_pln != null ? `min. ${esc(zl(be.required_annual_benefit_pln))}/rok${be.is_lower_bound ? '*' : ''}` : 'brak danych'}</b></div>
    </div>
    ${pr.yield_effect?.reason_pl ? `<p class="fine">${esc(pr.yield_effect.reason_pl)}</p>` : ''}
    ${be.is_lower_bound ? '<p class="fine">* Dolna granica — część kosztów wymaga wyceny.</p>' : ''}</div>`;

  // 2. Koszty
  const shared = costs.items.filter((i) => i.shared);
  const sharedCapex = shared.filter((i) => i.cost_type === 'CAPEX' && i.total_net_pln != null).reduce((a, i) => a + i.total_net_pln, 0);
  const sharedOpex = shared.filter((i) => i.cost_type !== 'CAPEX' && i.total_net_pln != null).reduce((a, i) => a + i.total_net_pln, 0);
  const sharedUnpriced = shared.filter((i) => i.total_net_pln == null).length;
  const capexMain = t.capex_known_net_pln != null ? `${zl(t.capex_known_net_pln)}${t.capex_is_complete ? '' : ' +'}` : (t.capex_is_complete ? '0 zł' : 'Do wyceny');
  const costHtml = `<div class="cost-card"><div class="report-heading"><svg class="icon"><use href="#icon-wallet"/></svg>KOSZTY WDROŻENIA</div>
    <div class="cost-main">${esc(capexMain)}</div>
    <p class="cost-sub">Nakłady inwestycyjne netto (znane pozycje)${t.opex_annual_known_net_pln != null ? ` · utrzymanie ${esc(zl(t.opex_annual_known_net_pln))}/rok${t.opex_is_complete ? '' : ' +'}` : ''}${t.unpriced_item_ids.length ? ` · <b>${t.unpriced_item_ids.length} ${plural(t.unpriced_item_ids.length, 'pozycja', 'pozycje', 'pozycji')} do wyceny</b>` : ''}</p>
    ${methods.map((m) => `<div class="cost-line"><span>${esc(m.name_pl)}</span><strong>${esc(costLabel(costPerMethod(costs, m.method_id)))}</strong></div>`).join('')}
    ${shared.length ? `<div class="cost-line"><span>Prace wspólne dla kilku metod (liczone raz)</span><strong>${esc(costLabel({ capex: sharedCapex, opex: sharedOpex, unpriced: sharedUnpriced }))}</strong></div>` : ''}
    <p class="fine">${esc(t.explanation_pl || '')}${demoPrices ? ' Stawki z cennika demonstracyjnego — nie są cenami rynkowymi.' : ''}</p></div>`;

  // 3. Dotacje — programy z katalogu (w trybie live: interwencje PS WPR z dokumentów ARiMR/CDR/MRiRW)
  const matches = fund.matches || [];
  const methodName = (id) => (allMethods().find((m) => m.method_id === id) || {}).name_pl || id;
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return u; } };
  const callText = (m) => {
    const w = m.call_window, st = m.call_status_now;
    if (!w) return CALL_PL[st] || 'nabór nieogłoszony';
    const range = w.valid_to ? `${fmtDate(w.valid_from)} – ${fmtDate(w.valid_to)}` : `od ${fmtDate(w.valid_from)}, nabór ciągły`;
    return `${CALL_PL[st] || st}: ${range}`;
  };
  const grantHtml = (m) => `<div class="grant-inner"><div class="grant-main">
      <strong>${m.code ? `<span class="grant-code">${esc(m.code)}</span>` : ''}${esc(m.program_name_pl)}</strong>
      <p>${esc(m.support_type || '')}${m.institution ? ` · ${esc(m.institution)}` : ''}</p>
      <ul class="grant-facts">
        <li><b>Dla metod:</b> ${m.method_ids.map((id) => esc(m.method_practices_pl?.[id] ? `${methodName(id)} — ${m.method_practices_pl[id]}` : methodName(id))).join('; ')}</li>
        ${(m.rates_pl || []).map((r) => `<li><b>Stawka:</b> ${esc(r)}</li>`).join('')}
        ${(m.estimate_pl || []).map((r) => `<li class="grant-estimate">${esc(r)}</li>`).join('')}
        <li><b>Nabór:</b> ${esc(callText(m))}${m.call_basis_pl ? ` <span class="grant-note">(${esc(m.call_basis_pl)})</span>` : ''}</li>
        ${m.how_to_apply_pl ? `<li><b>Jak złożyć wniosek:</b> ${esc(m.how_to_apply_pl)}</li>` : ''}
      </ul>
      ${(m.conditions_pl || []).length ? `<details class="more"><summary>Warunki i uwagi (${m.conditions_pl.length + m.reasons_pl.length})</summary><ul class="fine-list">${[...m.conditions_pl, ...m.reasons_pl].map((c) => `<li>${esc(c)}</li>`).join('')}</ul></details>` : ''}
      ${(m.source_urls || []).length ? `<p class="grant-src">Źródło: ${m.source_urls.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(host(u))}</a>`).join(', ')}${m.verified_at ? ` · stan na ${esc(fmtDate(m.verified_at))}` : ''}</p>` : ''}
    </div><span class="grant-status">${esc(MATCH_PL[m.match_status] || m.match_status)}</span></div>`;
  const unmatched = (fund.unmatched_method_ids || []).map(methodName);
  const fundHtml = `<div class="report-section grant-box"><div class="report-heading"><svg class="icon"><use href="#icon-bank"/></svg>MOŻLIWE DOFINANSOWANIA${fund.catalog_is_demonstration ? '<span class="tag-demo">KATALOG DEMONSTRACYJNY</span>' : ''}</div>
    ${matches.length ? matches.map(grantHtml).join('') : '<div class="grant-inner"><div><strong>Brak dopasowanych programów</strong><p>Dla wybranych metod nie znaleziono programu w katalogu.</p></div></div>'}
    ${unmatched.length ? `<p class="fine">Bez programu wsparcia w katalogu: ${esc(unmatched.join(', '))}.</p>` : ''}
    ${[...(fund.double_funding_warnings || []), ...(fund.warnings || [])].length ? `<ul class="fine-list">${[...(fund.double_funding_warnings || []), ...(fund.warnings || [])].map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    <p class="fine">Dotacji nie odejmujemy od kosztów — wskazanie programu nie oznacza przyznania wsparcia.</p></div>`;

  const implHtml = renderImplementation(impl);

  // 5. Rozwijana analiza (zawsze na końcu)
  $('report').innerHTML = `<div class="report-top">${benefitHtml}${costHtml}</div>${fundHtml}${implHtml}${analysisDetailsHtml(an, ca, fp)}
    <div class="report-print"><a class="action blue" href="/api/plans/${encodeURIComponent(fp.plan_id)}/pdf" target="_blank" rel="noopener"><svg class="icon"><use href="#icon-printer"/></svg>Pobierz raport PDF</a></div>`;
}
$('report').addEventListener('click', (event) => {
  const link = event.target.closest('[data-implementation-task]');
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const task = $(link.dataset.implementationTask);
  if (!task) return;
  event.preventDefault();
  task.closest('details').open = true;
  task.focus({ preventScroll: true });
  task.scrollIntoView({ block: 'nearest' });
});
function chip(title, status, lines) {
  const ok = status === 'ok';
  return `<div class="analysis-chip ${ok ? '' : 'missing'}"><b>${esc(title)}</b>${ok || status === 'partial' ? lines.filter(Boolean).map((l) => `<span>${l}</span>`).join('') : `<span class="na">${esc(MSTATUS_PL[status] || status || 'brak danych')}</span>`}</div>`;
}
function analysisDetailsHtml(an, ca, fp) {
  const m = an.modules, d = an.diagnosis;
  const so = m.soil || {}, te = m.terrain || {}, hy = m.hydrology || {}, we = m.weather || {}, sa = m.satellite || {};
  const nw = hy.nearby_water_features || {}, fl = hy.flood_and_basin_context || {};
  const ds = we.decade_summary_up_to_target || {}, ss = we.target_date_season_snapshot || {}, st = we.stations_used?.precipitation_station || {};
  const ob = sa.latest_valid_observation || {}, bl = sa.baseline_comparison || {};
  const wb = ca?.crop_water_balance || {};
  const chips = [
    chip('Gleba (SoilGrids, 0–30 cm)', so.status, [
      `Retencja: ${or(so.retention_class)}`, `Woda dostępna (AWC): ${or(num(so.water_holding_capacity_0_30cm?.awc_mm, 1))} mm`,
      `Materia organiczna: ${or(num(so.organic_matter?.organic_matter_pct, 2))} %`,
      `Piasek / pył / ił: ${or(num(so.granulometry?.sand_pct, 0))} / ${or(num(so.granulometry?.silt_pct, 0))} / ${or(num(so.granulometry?.clay_pct, 0))} %`]),
    chip('Teren (NMT GUGiK)', te.status, [
      `Ryzyko spływu i erozji: ${or(te.runoff_erosion_risk)}`, `Nachylenie średnie / maks.: ${or(num(te.slope?.mean_deg, 1))}° / ${or(num(te.slope?.max_deg, 1))}°`,
      `Powierzchnia > 3°: ${or(num(te.slope?.area_over_3deg_pct, 0))} %`, `Wysokość: ${or(num(te.elevation_m?.min, 1))}–${or(num(te.elevation_m?.max, 1))} m n.p.m.`]),
    chip('Hydrologia (BDOT10k, mapy powodziowe)', hy.status, [
      `Najbliższy rów: ${or(num(nw.nearest_ditch_m, 0))} m · ciek: ${or(num(nw.nearest_watercourse_m, 0))} m`,
      `Zbiornik wodny: ${or(num(nw.nearest_reservoir_m, 0))} m`,
      `Strefa zagrożenia powodziowego: ${fl.in_flood_hazard_zone == null ? or(null) : fl.in_flood_hazard_zone ? '<b>tak</b>' : 'nie'}`,
      fl.flood_hazard_class ? esc(fl.flood_hazard_class) : null]),
    chip('Pogoda (IMGW-PIB)', we.status, [
      `Stacja: ${or(st.name)}${st.distance_km != null ? ` (${esc(num(st.distance_km, 1))} km)` : ''}`,
      `Śr. bilans P−ET0 sezonu: ${or(num(ds.mean_veg_season_kbw_mm, 0))} mm (${or(ds.completed_veg_seasons_count)} sezonów)`,
      `Bieżący sezon do dnia analizy: ${or(num(ss.kbw_to_date_mm, 0))} mm`, `Najdłuższy okres bez opadu: ${or(ds.max_consecutive_dry_days)} dni`]),
    chip('Satelity (Sentinel-2)', sa.status, [
      `Ostatnie czyste zdjęcie: ${or(ob.acquisition_date)}`, `NDVI: ${or(num(ob.ndvi_median, 2))} · NDMI: ${or(num(ob.ndmi_median, 2))}`,
      `Odchylenie od lat poprzednich (NDVI / NDMI): ${or(num(bl.ndvi_anomaly, 2))} / ${or(num(bl.ndmi_anomaly, 2))}`,
      bl.canopy_rule?.interpretation_pl ? esc(bl.canopy_rule.interpretation_pl) : null]),
    chip(`Uprawa: ${CROP_UI[state.cropId]?.label || ca?.crop?.name_pl || ''}`, ca?.status_raw, [
      `Orientacyjne P−ETc w oknie krytycznym: ${or(num(wb.indicative_critical_window_p_minus_etc_mm, 0))} mm`,
      `P−ETc całego sezonu: ${or(num(wb.indicative_season_p_minus_etc_mm, 0))} mm`,
      ca?.crop?.critical_stage_label_pl ? `Okres krytyczny: ${esc(ca.crop.critical_stage_label_pl)}` : null,
      ca?.crop?.parameter_basis_pl ? esc(ca.crop.parameter_basis_pl) : null,
      'Wskaźnik własny — nie jest oficjalnym KBW IUNG.']),
  ].join('');
  const problems = d.problems || [], constraints = d.constraints || [];
  const sources = Object.entries(an.source_versions || {}).filter(([k]) => k !== 'data_mode').map(([k, v]) => `${esc(k)}: <code>${esc(v)}</code>`).join(' · ');
  const warnings = [...new Set([...(an.warnings || []), ...(fp.warnings || [])])];
  return `<details class="analysis-section" id="analysis-details"><summary><span>Szczegóły analizy działki i uprawy</span><svg class="icon"><use href="#icon-chevron"/></svg></summary><div class="analysis-body">
    <h4>Diagnoza działki</h4>
    ${problems.length ? `<ul class="problem-list">${problems.map((p) => `<li><b>${esc(PROBLEM_PL[p.problem_id] || p.problem_id)}</b> — nasilenie ${esc(LEVEL_PL[p.severity] || p.severity)}, pewność ${esc(CONF_PL[p.confidence] || p.confidence)}<br><span>${esc(human((p.reasons_pl || p.reasons || []).join(' ')))}</span></li>`).join('')}</ul>` : '<p>Na podstawie dostępnych danych nie stwierdzono problemów — nie oznacza to braku ryzyka.</p>'}
    ${constraints.length ? `<h4>Ograniczenia do sprawdzenia</h4><ul class="problem-list">${constraints.map((c) => `<li>${esc(c.reason_pl || c.constraint_id)}</li>`).join('')}</ul>` : ''}
    <h4>Dane źródłowe</h4><div class="analysis-chips">${chips}</div>
    ${warnings.length ? `<h4>Ograniczenia interpretacji</h4><ul class="fine-list">${warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    ${(fp.disclaimers_pl || []).length ? `<ul class="fine-list">${fp.disclaimers_pl.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    <p class="fine">Wersje źródeł: ${sources}. Analiza <code>${esc(an.analysis_id)}</code> (${esc(an.time_context.mode)}), wygenerowano ${esc(an.generated_at)}.
    <a href="/api/plans/${encodeURIComponent(fp.plan_id)}" target="_blank" rel="noopener">Pełne dane (JSON)</a></p>
  </div></details>`;
}

// ---------- nawigacja ----------
function setNavNote(text, spinning) {
  $('nav-note').innerHTML = `${spinning ? '<span class="spinner"></span>' : '<svg class="icon"><use href="#icon-info"/></svg>'}<span>${esc(text)}</span>`;
}
function showError(msg) { const el = $('error-box'); el.innerHTML = `<b>Coś poszło nie tak.</b> ${esc(msg)}`; el.hidden = false; }
function hideError() { $('error-box').hidden = true; }
const NAV_NOTES = {
  1: 'Granice działki z GUGiK; analiza rusza w tle zaraz po wyborze.',
  2: 'Uprawa jest wymagana; pytania możesz pominąć.',
  3: 'Zaznacz co najmniej jedną metodę.',
  4: 'Raport i PDF pochodzą z tego samego planu.',
};
function updateStep() {
  document.querySelectorAll('.step-screen').forEach((el) => el.classList.toggle('active', Number(el.id.split('-')[1]) === state.step));
  document.querySelectorAll('.step-indicator').forEach((el) => {
    const n = Number(el.dataset.step);
    el.dataset.state = n === state.step ? 'active' : (n < state.step && reachable(n)) ? 'done' : 'future';
    el.setAttribute('aria-current', n === state.step ? 'step' : 'false');
    el.disabled = state.busy || (n !== state.step && !reachable(n));
  });
  document.querySelectorAll('.step-line').forEach((el) => el.classList.toggle('done', Number(el.dataset.after) < state.step));
  const next = $('nav-next');
  $('nav-back').disabled = state.step === 1 || state.busy;
  next.disabled = state.busy || (state.step === 1 && (!state.parcel || !state.date)) || (state.step === 2 && !state.cropId) || (state.step === 3 && state.selected.size === 0);
  if (state.busy) next.innerHTML = '<span class="spinner light"></span>Liczę…';
  else if (state.step === 3) next.innerHTML = 'Utwórz plan<svg class="icon"><use href="#icon-right"/></svg>';
  else if (state.step === 4) next.innerHTML = '<svg class="icon"><use href="#icon-printer"/></svg>Pobierz PDF';
  else next.innerHTML = 'Dalej<svg class="icon"><use href="#icon-right"/></svg>';
  if (!state.busy) setNavNote(NAV_NOTES[state.step]);
  document.title = `HydroPlon — ${['Działka', 'Uprawa', 'Metody', 'Raport'][state.step - 1]}`;
}
function go(step) {
  state.step = Math.max(1, Math.min(4, step));
  if (state.step === 3) renderMethods();
  if (state.step === 4) renderReport();
  if (state.step === 1) setTimeout(() => map.invalidateSize(), 50);
  updateStep();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  const h = $(`title-${state.step}`); if (h) h.focus({ preventScroll: true });
}
async function runBusy(fn) {
  state.busy = true; hideError(); updateStep();
  try { await fn(); return true; }
  catch (e) { showError(e.message); return false; }
  finally { state.busy = false; updateStep(); }
}
$('nav-back').addEventListener('click', () => { hideError(); go(state.step - 1); });
$('nav-next').addEventListener('click', async () => {
  if (state.busy) return;
  if (state.step === 1) {
    if (!state.parcel || !state.date) return;
    startAnalysis(); go(2); return;
  }
  if (state.step === 2) {
    if (!state.cropId) { toast('Wybierz jedną uprawę, aby kontynuować.'); return; }
    if (!state.ranking && !(await runBusy(computeRanking))) return;
    go(3); return;
  }
  if (state.step === 3) {
    if (!state.selected.size) { toast('Zaznacz przynajmniej jedną metodę.'); return; }
    if (!state.plan && !(await runBusy(computePlan))) return;
    go(4); return;
  }
  if (state.step === 4 && state.plan) window.open(`/api/plans/${encodeURIComponent(state.plan.plan_id)}/pdf`, '_blank', 'noopener');
});
document.querySelectorAll('.step-indicator').forEach((b) => b.addEventListener('click', () => { const n = Number(b.dataset.step); if (reachable(n) && !state.busy) go(n); }));

// ---------- start ----------
(async () => {
  const params = new URLSearchParams(location.search);
  const suppliedDate = params.get('date');
  dateInput.value = suppliedDate === null ? state.date : suppliedDate.trim().replace(
    /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/,
    (_, day, month, year) => `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')}`,
  );
  updateAnalysisDate();
  updateStep();
  try {
    const h = await api('GET', '/api/health'); state.health = h;
    const live = h.data_mode === 'live';
    if (!live) {
      addressInput.disabled = true;
      addressButton.disabled = true;
      addressStatus.textContent = 'Wyszukiwanie adresów jest dostępne w trybie z danymi rzeczywistymi.';
    }
    $('mode-pill').textContent = live ? 'DANE RZECZYWISTE' : 'DANE TESTOWE';
    $('context-text').textContent = live ? 'Źródła: GUGiK, SoilGrids, IMGW-PIB, Wody Polskie, Sentinel-2' : 'Tryb testowy — dane syntetyczne';
    const parcelId = params.get('parcel');
    if (parcelSeq === 0) {
      renderParcelCard();
      if (parcelId) {
        $('parcel-input').value = parcelId;
        loadParcel(`/api/parcels/by-id?id=${encodeURIComponent(parcelId)}`);
      }
    }
    const cr = await api('GET', '/api/crops'); state.crops = cr.crops; renderCrops();
    const fx = await api('GET', '/api/system/fixture-parcels');
    if (fx.features?.length) {
      fixtureLayer.addData(fx);
      $('fixture-links').innerHTML = '<span class="hint">Działki testowe:</span> ' + fx.features.map((f) => `<button type="button" class="link-btn" data-id="${esc(f.properties.parcel_id)}" title="${esc(f.properties.scenario_pl)}">${esc(f.properties.label)}</button>`).join(' ');
      $('fixture-links').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => loadParcel(`/api/parcels/by-id?id=${encodeURIComponent(b.dataset.id)}`)));
    }
  } catch (e) { showError(e.message); $('mode-pill').textContent = 'BRAK POŁĄCZENIA'; }
})();
})();
