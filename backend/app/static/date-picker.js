(() => {
  const picker = document.getElementById('date-picker');
  if (typeof picker.showModal !== 'function') return;

  const field = document.getElementById('date-input');
  const toggle = document.getElementById('date-picker-toggle');
  const grid = document.getElementById('calendar-grid');
  const caption = document.getElementById('calendar-caption');
  const month = document.getElementById('calendar-month');
  const year = document.getElementById('calendar-year');
  const previous = document.getElementById('calendar-prev');
  const next = document.getElementById('calendar-next');
  const todayButton = document.getElementById('calendar-today');
  const start = new Date(field.min);
  const end = new Date(field.max || '9999-12-31');
  const fullDate = new Intl.DateTimeFormat('pl-PL', { dateStyle: 'full', timeZone: 'UTC' });
  const monthName = new Intl.DateTimeFormat('pl-PL', { month: 'long', timeZone: 'UTC' });
  const monthCaption = new Intl.DateTimeFormat('pl-PL', { month: 'long', year: 'numeric', timeZone: 'UTC' });
  const toIso = (date) => date.toISOString().slice(0, 10);
  const dayIndex = (date) => (date.getUTCDay() + 6) % 7;
  const makeDate = (year, month, day) => new Date(Date.UTC(year, month, day));
  const withinBounds = (date) => date >= start && date <= end;
  const clamp = (date) => date < start ? start : date > end ? end : date;
  let active, today, invoker;

  for (let index = 0; index < 12; index++) {
    month.add(new Option(monthName.format(makeDate(2024, index, 1)), index));
  }
  year.min = start.getUTCFullYear();
  year.max = end.getUTCFullYear();

  function updateToggleLabel() {
    const selected = field.valueAsDate;
    toggle.setAttribute('aria-label', selected && withinBounds(selected)
      ? `Otwórz kalendarz. Wybrana data: ${fullDate.format(selected)}`
      : 'Otwórz kalendarz');
  }

  function render(focusDay = false) {
    const y = active.getUTCFullYear();
    const m = active.getUTCMonth();
    const first = makeDate(y, m, 1);
    const last = makeDate(y, m + 1, 0);
    const leading = dayIndex(first);
    const days = last.getUTCDate();
    const selected = field.value;
    const todayIso = toIso(today);
    const displayedMonth = monthCaption.format(active);
    if (caption.textContent !== displayedMonth) caption.textContent = displayedMonth;
    month.value = m;
    year.value = y;
    const previousFocused = document.activeElement === previous;
    const nextFocused = document.activeElement === next;
    previous.disabled = first <= start;
    next.disabled = last >= end;

    let rows = '';
    for (let row = 0; row < Math.ceil((leading + days) / 7); row++) {
      rows += '<tr>';
      for (let col = 0; col < 7; col++) {
        const day = row * 7 + col - leading + 1;
        if (day < 1 || day > days) {
          rows += '<td></td>';
          continue;
        }
        const date = makeDate(y, m, day);
        const iso = toIso(date);
        rows += `<td aria-selected="${iso === selected}"><button type="button" data-date="${iso}" aria-label="${fullDate.format(date)}"${iso === todayIso ? ' aria-current="date"' : ''} tabindex="${day === active.getUTCDate() ? 0 : -1}"${withinBounds(date) ? '' : ' disabled'}>${day}</button></td>`;
      }
      rows += '</tr>';
    }
    grid.tBodies[0].innerHTML = rows;
    if (focusDay || (previousFocused && previous.disabled) || (nextFocused && next.disabled)) {
      grid.querySelector('[tabindex="0"]').focus();
    }
  }

  function changeMonth(y, m, focusDay = false) {
    const last = makeDate(y, m + 1, 0).getUTCDate();
    active = clamp(makeDate(y, m, Math.min(active.getUTCDate(), last)));
    render(focusDay);
  }

  function open(from) {
    const now = new Date();
    today = makeDate(now.getFullYear(), now.getMonth(), now.getDate());
    invoker = from;
    const selected = field.valueAsDate;
    active = clamp(selected && withinBounds(selected) ? selected : today);
    todayButton.disabled = !withinBounds(today);
    render();
    picker.showModal();
    grid.querySelector('[tabindex="0"]').focus();
  }

  function selectDate(iso) {
    field.value = iso;
    field.dispatchEvent(new Event('change', { bubbles: true }));
    picker.close();
  }

  toggle.hidden = false;
  updateToggleLabel();
  field.addEventListener('change', updateToggleLabel);
  toggle.addEventListener('click', () => open(toggle));
  field.addEventListener('keydown', (event) => {
    if (!(event.altKey && event.key === 'ArrowDown')) return;
    event.preventDefault();
    open(field);
  });
  picker.addEventListener('close', () => invoker.focus());
  picker.addEventListener('click', (event) => {
    if (event.target !== picker) return;
    const { left, right, top, bottom } = picker.getBoundingClientRect();
    if (event.clientX < left || event.clientX > right || event.clientY < top || event.clientY > bottom) picker.close();
  });

  previous.addEventListener('click', () => changeMonth(active.getUTCFullYear(), active.getUTCMonth() - 1));
  next.addEventListener('click', () => changeMonth(active.getUTCFullYear(), active.getUTCMonth() + 1));
  month.addEventListener('change', () => changeMonth(active.getUTCFullYear(), Number(month.value)));
  year.addEventListener('change', () => {
    if (year.validity.valid) changeMonth(year.valueAsNumber, active.getUTCMonth());
  });
  todayButton.addEventListener('click', () => selectDate(toIso(today)));
  grid.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-date]');
    if (button && !button.disabled) selectDate(button.dataset.date);
  });
  grid.addEventListener('keydown', (event) => {
    if (!event.target.matches('button[data-date]') || event.altKey || event.ctrlKey || event.metaKey) return;
    const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -dayIndex(active), End: 6 - dayIndex(active) };
    if (event.key === 'PageUp' || event.key === 'PageDown') {
      event.preventDefault();
      const offset = (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1);
      changeMonth(active.getUTCFullYear(), active.getUTCMonth() + offset, true);
    } else if (Object.hasOwn(offsets, event.key)) {
      event.preventDefault();
      active = clamp(makeDate(active.getUTCFullYear(), active.getUTCMonth(), active.getUTCDate() + offsets[event.key]));
      render(true);
    }
  });
})();
