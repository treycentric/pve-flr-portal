function taskPicker(groups, current) {
  return {
    groups,
    open: false,
    filter: '',
    selected: current,
    current,
    get currentLabel() {
      const [type, vmid] = this.current.split(':');
      const match = this.groups.find((g) => g.type === type && g.vmid === vmid);
      return match && match.name ? `${vmid} (${match.name})` : `${type.toUpperCase()} ${vmid}`;
    },
    get filtered() {
      const f = this.filter.trim().toLowerCase();
      if (!f) return this.groups;
      return this.groups.filter(
        (g) =>
          g.vmid.toLowerCase().includes(f) ||
          g.type.toLowerCase().includes(f) ||
          (g.name && g.name.toLowerCase().includes(f))
      );
    },
    formatLastBackup(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    },
    confirm() {
      window.location = '/?task=' + encodeURIComponent(this.selected);
    },
  };
}

function userMenu(identity) {
  return {
    identity,
    open: false,
    aboutOpen: false,
    logout() {
      this.open = false;
      // Placeholder: no real session exists yet (single shared service
      // token, see docs/plan.md PH.4). Wire this to POST /logout once
      // per-user PVE ticket auth lands.
    },
  };
}

function fileGridState() {
  return {
    count: 0,
    allChecked: false,
    sortKey: 'name',
    sortDir: 'asc',
    init() {
      this.applySort();
    },
    toggleAll() {
      const boxes = this.$refs.tbody.querySelectorAll('input[type=checkbox]');
      boxes.forEach((box) => {
        box.checked = this.allChecked;
      });
      this.count = this.allChecked ? boxes.length : 0;
    },
    syncAllChecked() {
      const boxes = this.$refs.tbody.querySelectorAll('input[type=checkbox]');
      this.count = Array.from(boxes).filter((b) => b.checked).length;
      this.allChecked = boxes.length > 0 && this.count === boxes.length;
    },
    setSort(key) {
      if (this.sortKey === key) {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.sortKey = key;
        this.sortDir = 'asc';
      }
      this.applySort();
    },
    applySort() {
      const tbody = this.$refs.tbody;
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const dir = this.sortDir === 'asc' ? 1 : -1;
      const numeric = this.sortKey === 'size' || this.sortKey === 'modified';
      rows.sort((a, b) => {
        const av = a.dataset[this.sortKey];
        const bv = b.dataset[this.sortKey];
        if (numeric) return (Number(av) - Number(bv)) * dir;
        return av.localeCompare(bv) * dir;
      });
      rows.forEach((r) => tbody.appendChild(r));
    },
  };
}

function portalApp(rawSnapshots) {
  const snapshots = rawSnapshots
    .map((s) => ({ ...s, date: new Date(s.time) }))
    .sort((a, b) => a.date - b.date);

  return {
    // --- timeline state ---
    snapshots,
    selectedVolume: null,
    viewStart: null,
    viewEnd: null,
    activeDayKey: null,
    activeDayItems: [],
    activeDayX: 0,
    _dragMoved: false,

    // --- browse state ---
    volume: null,
    crumbs: [],
    fileFilter: '',
    history: [],
    historyIndex: -1,

    init() {
      this.$nextTick(() => {
        this._bindTimelineDrag();
        if (this.snapshots.length) {
          const span = 1000 * 60 * 60 * 24 * 75; // ~2.5 months, default zoom level
          const last = this.snapshots[this.snapshots.length - 1].date.getTime();
          this.viewStart = new Date(last - span / 2);
          this.viewEnd = new Date(last + span / 2);
          this.selectSnapshot(this.snapshots[this.snapshots.length - 1]);
        }
      });
    },

    xFor(date) {
      const total = this.viewEnd - this.viewStart;
      if (total <= 0) return 0;
      return ((date - this.viewStart) / total) * 1000;
    },

    groupsInView() {
      // Group by local calendar day (matching ticksInView) and position each
      // group's dot at that day's midnight, so it lands exactly on the day's
      // tick mark rather than at the snapshot's exact time-of-day.
      const map = new Map();
      for (const s of this.snapshots) {
        if (s.date < this.viewStart || s.date > this.viewEnd) continue;
        const dayStart = new Date(s.date.getFullYear(), s.date.getMonth(), s.date.getDate());
        const key = dayStart.getTime();
        if (!map.has(key)) map.set(key, { dayStart, items: [] });
        map.get(key).items.push(s);
      }
      return Array.from(map.values()).map(({ dayStart, items }) => ({
        key: dayStart.toISOString().slice(0, 10),
        items,
        x: this.xFor(dayStart),
      }));
    },

    ticksInView() {
      const msPerDay = 24 * 60 * 60 * 1000;
      const totalDays = Math.ceil((this.viewEnd - this.viewStart) / msPerDay);

      // Zoomed way out: per-day ticks would be too dense to render usefully.
      if (totalDays > 120) {
        const days = [];
        const count = 6;
        const total = this.viewEnd - this.viewStart;
        for (let i = 0; i <= count; i++) {
          const d = new Date(this.viewStart.getTime() + (total * i) / count);
          days.push({ x: (i / count) * 1000, label: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), isMonth: false });
        }
        return days;
      }

      const days = [];
      const cursor = new Date(this.viewStart.getFullYear(), this.viewStart.getMonth(), this.viewStart.getDate());
      while (cursor <= this.viewEnd) {
        if (cursor >= this.viewStart) {
          const isMonthStart = cursor.getDate() === 1;
          days.push({
            x: this.xFor(cursor),
            isMonth: isMonthStart,
            year: isMonthStart ? cursor.getFullYear() : null,
            monthAbbr: isMonthStart ? cursor.toLocaleDateString(undefined, { month: 'short' }) : null,
            label: isMonthStart ? '' : (cursor.getDate() % 2 === 0 ? String(cursor.getDate()) : ''),
          });
        }
        cursor.setDate(cursor.getDate() + 1);
      }
      return days;
    },

    async selectSnapshot(snapshot) {
      const preservedSelection = this._captureSelection();
      this.selectedVolume = snapshot.volume;
      this.activeDayKey = null;
      this.activeDayItems = [];
      this.volume = snapshot.volume;
      await this.loadTree();
      if (this.crumbs.length === 0) {
        this.crumbs = [{ label: 'Root', filepath: '/' }];
      }
      // Center the view on the selected date's midnight (matching where its
      // dot is actually drawn, per groupsInView) under the fixed center
      // line, preserving whatever zoom level is currently active.
      const span = this.viewEnd - this.viewStart;
      const dayStart = new Date(snapshot.date.getFullYear(), snapshot.date.getMonth(), snapshot.date.getDate());
      const target = dayStart.getTime();
      this.viewStart = new Date(target - span / 2);
      this.viewEnd = new Date(target + span / 2);
      this.renderTimeline();
      // Try to land on the same path in the new snapshot; if a level along
      // the current breadcrumb trail doesn't exist there (browse_error.html
      // came back), fall back one level at a time until one resolves.
      while (this.crumbs.length > 1) {
        if (await this.load()) {
          this._pushHistory();
          this._applySelection(preservedSelection);
          return;
        }
        this.crumbs.pop();
      }
      await this.load();
      this._pushHistory();
      this._applySelection(preservedSelection);
    },

    toggleDay(key, items, screenX) {
      if (this.activeDayKey === key) {
        this.activeDayKey = null;
        this.activeDayItems = [];
      } else {
        this.activeDayKey = key;
        this.activeDayItems = items;
        this.activeDayX = screenX;
      }
    },

    zoom(factor) {
      const mid = (this.viewStart.getTime() + this.viewEnd.getTime()) / 2;
      const half = ((this.viewEnd - this.viewStart) / 2) * factor;
      this.viewStart = new Date(mid - half);
      this.viewEnd = new Date(mid + half);
      this.renderTimeline();
    },

    get selectedIndex() {
      return this.snapshots.findIndex((s) => s.volume === this.selectedVolume);
    },

    get hasOlder() {
      return this.selectedIndex > 0;
    },

    get hasNewer() {
      const idx = this.selectedIndex;
      return idx >= 0 && idx < this.snapshots.length - 1;
    },

    olderSnapshot() {
      const idx = this.selectedIndex;
      if (idx > 0) this.selectSnapshot(this.snapshots[idx - 1]);
    },

    newerSnapshot() {
      const idx = this.selectedIndex;
      if (idx >= 0 && idx < this.snapshots.length - 1) this.selectSnapshot(this.snapshots[idx + 1]);
    },

    resetView() {
      if (!this.snapshots.length) return;
      this.selectSnapshot(this.snapshots[this.snapshots.length - 1]);
    },

    jumpToNow() {
      const now = new Date();
      const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const span = this.viewEnd - this.viewStart;
      const target = todayStart.getTime();
      this.viewStart = new Date(target - span / 2);
      this.viewEnd = new Date(target + span / 2);
      this.renderTimeline();
    },

    openDatePicker() {
      const el = this.$refs.datePicker;
      if (!el) return;
      if (el.showPicker) el.showPicker();
      else el.click();
    },

    jumpToDate(dateStr) {
      if (!dateStr) return;
      const [y, m, d] = dateStr.split('-').map(Number);
      const target = new Date(y, m - 1, d).getTime();
      const span = this.viewEnd - this.viewStart;
      this.viewStart = new Date(target - span / 2);
      this.viewEnd = new Date(target + span / 2);
      this.renderTimeline();
    },

    refreshSnapshots() {
      window.location.reload();
    },

    _formatTimestamp(iso) {
      const d = new Date(iso);
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    },

    _bindTimelineDrag() {
      const svg = this.$refs.timelineSvg;
      if (!svg) return;

      let dragging = false;
      let startX = 0;
      let dragViewStart = null;
      let dragViewEnd = null;

      const unitsPerPixel = () => {
        const rect = svg.getBoundingClientRect();
        return rect.width ? 1000 / rect.width : 0;
      };

      // IMPORTANT: do NOT call svg.setPointerCapture() here.
      //
      // Pointer capture retargets every subsequent event for that pointer --
      // including pointerup and the compatibility mouse events -- to the
      // capture element. The `click` event's target is then resolved against
      // the (retargeted) pointerdown/pointerup targets, so it lands on the
      // <svg> root itself instead of the marker shape actually under the
      // cursor. That made every per-marker click listener dead: e.target was
      // always the bare <svg>, regardless of hit-area size or where the user
      // clicked. It reproduces identically in Gecko and Blink because both
      // implement the same "click follows pointer capture" retargeting.
      //
      // Listening on window while a drag is in progress gives the same
      // "keep panning even if the cursor leaves the widget" behaviour with
      // no retargeting side effects.
      const onMove = (e) => {
        if (!dragging) return;
        if (Math.abs(e.clientX - startX) > 3) this._dragMoved = true;
        const dxUnits = (e.clientX - startX) * unitsPerPixel();
        const totalMs = dragViewEnd - dragViewStart;
        const dxMs = (dxUnits / 1000) * totalMs;
        this.viewStart = new Date(dragViewStart.getTime() - dxMs);
        this.viewEnd = new Date(dragViewEnd.getTime() - dxMs);
        this.renderTimeline();
      };

      const endDrag = () => {
        if (!dragging) return;
        dragging = false;
        svg.classList.remove('dragging');
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', endDrag);
        window.removeEventListener('pointercancel', endDrag);
      };

      svg.addEventListener('pointerdown', (e) => {
        if (e.button !== 0 && e.pointerType === 'mouse') return;
        // Suppresses native text/image drag selection. Cancelling pointerdown
        // does not suppress the later `click`, so marker clicks still fire.
        e.preventDefault();
        dragging = true;
        this._dragMoved = false;
        startX = e.clientX;
        dragViewStart = this.viewStart;
        dragViewEnd = this.viewEnd;
        svg.classList.add('dragging');
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', endDrag);
        window.addEventListener('pointercancel', endDrag);
      });

      // Safety net: if for any reason the click's target resolves to the <svg>
      // root rather than a marker group (extension-injected overlays, future
      // pointer-capture regressions, synthetic clicks), fall back to picking
      // the nearest marker by horizontal distance. Per-group listeners call
      // stopPropagation(), so this never double-fires for a normal hit.
      svg.addEventListener('click', (e) => {
        if (this._dragMoved) return;
        const rect = svg.getBoundingClientRect();
        if (!rect.width) return;
        const scaleX = rect.width / 1000;
        const ux = (e.clientX - rect.left) / scaleX;
        let best = null;
        let bestDist = Infinity;
        for (const group of this.groupsInView()) {
          const dist = Math.abs(group.x - ux);
          if (dist < bestDist) {
            bestDist = dist;
            best = group;
          }
        }
        if (!best || bestDist * scaleX > 14) return;
        e.stopPropagation();
        const trackRect = this.$refs.timelineTrack.getBoundingClientRect();
        this.toggleDay(best.key, best.items, rect.left - trackRect.left + best.x * scaleX);
      });
    },

    renderTimeline() {
      const svg = this.$refs.timelineSvg;
      if (!svg) return;
      const NS = 'http://www.w3.org/2000/svg';
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      // Shared vertical layout. AXIS_Y leaves enough room above for the tall
      // selected-snapshot bubble to fit natively near the top of the widget
      // and below for tick/day/month labels.
      const TOTAL_HEIGHT = 90;
      const AXIS_Y = 64;
      const BUBBLE_TOP = -58;
      const BUBBLE_HEIGHT = 17;

      const axis = document.createElementNS(NS, 'line');
      axis.setAttribute('x1', '0');
      axis.setAttribute('x2', '1000');
      axis.setAttribute('y1', String(AXIS_Y));
      axis.setAttribute('y2', String(AXIS_Y));
      axis.setAttribute('class', 'timeline-axis');
      svg.appendChild(axis);

      // Fixed reference line at the horizontal center of the view. Its top
      // end is pinned to the top of the selected bubble, its bottom end to
      // the bottom border of the widget. Selecting a snapshot pans the
      // timeline so its date lands here; it does not track any particular
      // date itself.
      const centerLine = document.createElementNS(NS, 'line');
      centerLine.setAttribute('x1', '500');
      centerLine.setAttribute('x2', '500');
      centerLine.setAttribute('y1', String(TOTAL_HEIGHT));
      centerLine.setAttribute('y2', String(AXIS_Y + BUBBLE_TOP));
      centerLine.setAttribute('class', 'timeline-center-line');
      svg.appendChild(centerLine);

      for (const tick of this.ticksInView()) {
        const g = document.createElementNS(NS, 'g');
        g.setAttribute('transform', `translate(${tick.x},${AXIS_Y})`);
        const line = document.createElementNS(NS, 'line');
        line.setAttribute('y1', '0');
        line.setAttribute('y2', '-5');
        line.setAttribute('class', 'timeline-tick');
        g.appendChild(line);
        if (tick.isMonth) {
          const text = document.createElementNS(NS, 'text');
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('class', 'timeline-tick-label--month');
          const yearSpan = document.createElementNS(NS, 'tspan');
          yearSpan.setAttribute('x', '0');
          yearSpan.setAttribute('y', '13');
          yearSpan.textContent = String(tick.year);
          const monthSpan = document.createElementNS(NS, 'tspan');
          monthSpan.setAttribute('x', '0');
          monthSpan.setAttribute('dy', '7');
          monthSpan.textContent = tick.monthAbbr;
          text.appendChild(yearSpan);
          text.appendChild(monthSpan);
          g.appendChild(text);
        } else if (tick.label) {
          const text = document.createElementNS(NS, 'text');
          text.setAttribute('y', '14');
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('class', 'timeline-tick-label');
          text.textContent = tick.label;
          g.appendChild(text);
        }
        svg.appendChild(g);
      }

      const svgRect = svg.getBoundingClientRect();
      const trackRect = this.$refs.timelineTrack.getBoundingClientRect();
      const offsetX = svgRect.left - trackRect.left;
      const scaleX = svgRect.width / 1000;

      for (const group of this.groupsInView()) {
        const g = document.createElementNS(NS, 'g');
        g.setAttribute('transform', `translate(${group.x},${AXIS_Y})`);
        g.setAttribute('class', 'timeline-group');

        const isSelected = group.items.some((s) => s.volume === this.selectedVolume);

        const shadow = document.createElementNS(NS, 'circle');
        shadow.setAttribute('cx', '0.5');
        shadow.setAttribute('cy', '1.3');
        shadow.setAttribute('r', '5');
        shadow.setAttribute('class', 'timeline-shadow');
        g.appendChild(shadow);

        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('r', '5');
        dot.setAttribute('class', 'timeline-dot' + (isSelected ? ' timeline-dot--selected' : ''));
        g.appendChild(dot);

        if (isSelected) {
          const selected = group.items.find((s) => s.volume === this.selectedVolume);
          const countStr = String(group.items.length);
          const tsStr = this._formatTimestamp(selected.time);
          const width = 16 + (countStr.length + tsStr.length) * 5.2 + 6;
          const bubbleBottom = BUBBLE_TOP + BUBBLE_HEIGHT;
          const tailApex = bubbleBottom + 6;

          const connector = document.createElementNS(NS, 'line');
          connector.setAttribute('x1', '0');
          connector.setAttribute('x2', '0');
          connector.setAttribute('y1', String(tailApex));
          connector.setAttribute('y2', '-4');
          connector.setAttribute('class', 'timeline-bubble-connector');
          g.appendChild(connector);

          const bubble = document.createElementNS(NS, 'rect');
          bubble.setAttribute('x', String(-width / 2));
          bubble.setAttribute('y', String(BUBBLE_TOP));
          bubble.setAttribute('width', String(width));
          bubble.setAttribute('height', String(BUBBLE_HEIGHT));
          bubble.setAttribute('rx', '3');
          bubble.setAttribute('class', 'timeline-bubble timeline-bubble--selected');
          g.appendChild(bubble);

          const tail = document.createElementNS(NS, 'polygon');
          tail.setAttribute('points', `-5,${bubbleBottom} 5,${bubbleBottom} 0,${tailApex}`);
          tail.setAttribute('class', 'timeline-bubble timeline-bubble--selected');
          g.appendChild(tail);

          const text = document.createElementNS(NS, 'text');
          text.setAttribute('x', '0');
          text.setAttribute('y', String(BUBBLE_TOP + 12));
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('class', 'timeline-bubble-label');
          const countSpan = document.createElementNS(NS, 'tspan');
          countSpan.textContent = countStr;
          const tsSpan = document.createElementNS(NS, 'tspan');
          tsSpan.setAttribute('dx', '6');
          tsSpan.textContent = tsStr;
          text.appendChild(countSpan);
          text.appendChild(tsSpan);
          g.appendChild(text);

          // Invisible click target spanning the full SVG height (from its
          // very top down to a comfortable margin past the dot), so there is
          // no vertical boundary near the visible shapes where a real mouse
          // click can miss. Inserted first so it sits beneath the visible
          // shapes but still catches clicks in the padding between them.
          const hit = document.createElementNS(NS, 'rect');
          hit.setAttribute('x', String(-width / 2 - 10));
          hit.setAttribute('y', String(-AXIS_Y));
          hit.setAttribute('width', String(width + 20));
          hit.setAttribute('height', String(AXIS_Y + 25));
          hit.setAttribute('fill', 'transparent');
          hit.setAttribute('class', 'timeline-hit-area');
          g.insertBefore(hit, g.firstChild);
        } else {
          const bubble = document.createElementNS(NS, 'rect');
          bubble.setAttribute('x', '-9');
          bubble.setAttribute('y', '-20');
          bubble.setAttribute('width', '18');
          bubble.setAttribute('height', '12');
          bubble.setAttribute('rx', '3');
          bubble.setAttribute('class', 'timeline-bubble');
          g.appendChild(bubble);

          const tail = document.createElementNS(NS, 'polygon');
          tail.setAttribute('points', '-3,-8 3,-8 0,-4');
          tail.setAttribute('class', 'timeline-bubble');
          g.appendChild(tail);

          const count = document.createElementNS(NS, 'text');
          count.setAttribute('x', '0');
          count.setAttribute('y', '-11.5');
          count.setAttribute('text-anchor', 'middle');
          count.setAttribute('class', 'timeline-bubble-label');
          count.textContent = String(group.items.length);
          g.appendChild(count);

          const hit = document.createElementNS(NS, 'rect');
          hit.setAttribute('x', '-14');
          hit.setAttribute('y', '-26');
          hit.setAttribute('width', '28');
          hit.setAttribute('height', '36');
          hit.setAttribute('fill', 'transparent');
          hit.setAttribute('class', 'timeline-hit-area');
          g.insertBefore(hit, g.firstChild);
        }

        g.addEventListener('click', (e) => {
          // Without this, Alpine's @click.outside on the day-picker popup
          // sees this same click (the popup isn't open yet, so the marker
          // "is outside" it) and immediately closes what toggleDay just
          // opened, in the same bubble phase.
          e.stopPropagation();
          if (this._dragMoved) return;
          const screenX = offsetX + group.x * scaleX;
          this.toggleDay(group.key, group.items, screenX);
        });

        svg.appendChild(g);
      }
    },

    // --- folder browsing (drives the file grid via htmx) ---
    loading: false,
    goInto(filepath, label) {
      if (this.loading) return;
      this.crumbs.push({ label, filepath });
      this._pushHistory();
      this.load();
    },
    goTo(idx) {
      if (this.loading) return;
      this.crumbs = this.crumbs.slice(0, idx + 1);
      this._pushHistory();
      this.load();
    },
    setPath(crumbs) {
      if (this.loading) return;
      this.crumbs = crumbs;
      this._pushHistory();
      this.load();
    },
    treeWidth: 200,
    _treeResizing: false,
    startTreeResize(e) {
      this._treeResizing = true;
      const startX = e.clientX;
      const startWidth = this.treeWidth;
      const onMove = (ev) => {
        if (!this._treeResizing) return;
        this.treeWidth = Math.min(500, Math.max(120, startWidth + (ev.clientX - startX)));
      };
      const onUp = () => {
        this._treeResizing = false;
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    _treeVolume: null,
    expandedTreePaths: [],
    trackTreeToggle(filepath, isOpen) {
      if (isOpen) {
        if (!this.expandedTreePaths.includes(filepath)) this.expandedTreePaths.push(filepath);
      } else {
        this.expandedTreePaths = this.expandedTreePaths.filter((p) => p !== filepath);
      }
    },
    async _restoreTreeExpansion() {
      for (const path of this.expandedTreePaths) {
        const btn = document.querySelector('.tree-toggle[data-filepath="' + path + '"]');
        if (!btn) continue;
        const node = btn.closest('.tree-node');
        const ul = node && node.nextElementSibling;
        if (!ul || ul.tagName !== 'UL') continue;
        const url = btn.getAttribute('hx-get');
        try {
          await htmx.ajax('GET', url, { target: ul, swap: 'innerHTML' });
        } catch (e) {
          continue;
        }
        if (window.Alpine) window.Alpine.$data(node).open = true;
      }
    },
    async loadTree() {
      if (this._treeVolume === this.volume) return;
      this._treeVolume = this.volume;
      const rootCrumbs = encodeURIComponent(JSON.stringify([{ label: 'Root', filepath: '/' }]));
      const url = '/api/tree?volume=' + encodeURIComponent(this.volume) + '&filepath=' + encodeURIComponent('/') + '&crumbs=' + rootCrumbs;
      await htmx.ajax('GET', url, { target: '#tree-root', swap: 'innerHTML' });
      await this._restoreTreeExpansion();
    },
    _captureSelection() {
      const boxes = document.querySelectorAll('#file-grid input[type=checkbox][name=item]');
      return Array.from(boxes)
        .filter((b) => b.checked)
        .map((b) => JSON.parse(b.value).filepath);
    },
    _applySelection(filepaths) {
      if (!filepaths || !filepaths.length) return;
      const rows = document.querySelectorAll('#file-grid tbody tr');
      rows.forEach((row) => {
        const box = row.querySelector('input[type=checkbox][name=item]');
        if (box && filepaths.includes(row.dataset.filepath)) box.checked = true;
      });
      const grid = document.querySelector('#file-grid');
      if (grid) grid.dispatchEvent(new Event('change', { bubbles: true }));
    },
    _pushHistory() {
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push({ volume: this.volume, crumbs: this.crumbs.map((c) => ({ ...c })) });
      this.historyIndex = this.history.length - 1;
    },
    get canGoBack() {
      return this.historyIndex > 0;
    },
    get canGoForward() {
      return this.historyIndex < this.history.length - 1;
    },
    async goBack() {
      if (this.loading || !this.canGoBack) return;
      this.historyIndex--;
      const entry = this.history[this.historyIndex];
      this.volume = entry.volume;
      await this.loadTree();
      this.crumbs = entry.crumbs.map((c) => ({ ...c }));
      this.load();
    },
    async goForward() {
      if (this.loading || !this.canGoForward) return;
      this.historyIndex++;
      const entry = this.history[this.historyIndex];
      this.volume = entry.volume;
      await this.loadTree();
      this.crumbs = entry.crumbs.map((c) => ({ ...c }));
      this.load();
    },
    async load() {
      const current = this.crumbs[this.crumbs.length - 1];
      const url =
        '/api/browse?volume=' + encodeURIComponent(this.volume) +
        '&filepath=' + encodeURIComponent(current.filepath);
      this.loading = true;
      await htmx.ajax('GET', url, { target: '#file-grid', indicator: '#loading' });
      this.loading = false;
      await this._syncTreeToCrumbs();
      return !document.querySelector('#file-grid .browse-error');
    },
    // Expands whatever ancestor tree nodes are needed so the node matching
    // the current crumb trail is visible; highlighting itself is handled
    // reactively by the is-selected binding in tree_nodes.html comparing
    // against `crumbs`, since both share this same Alpine scope.
    async _syncTreeToCrumbs() {
      for (let i = 1; i < this.crumbs.length; i++) {
        const c = this.crumbs[i];
        const btn = document.querySelector('.tree-toggle[data-filepath="' + c.filepath + '"]');
        if (!btn) break;
        if (i === this.crumbs.length - 1) break;
        const node = btn.closest('.tree-node');
        const isOpen = window.Alpine ? window.Alpine.$data(node).open : false;
        if (isOpen) continue;
        const ul = node.nextElementSibling;
        if (!ul || ul.tagName !== 'UL') break;
        const fetchUrl = btn.getAttribute('hx-get');
        try {
          await htmx.ajax('GET', fetchUrl, { target: ul, swap: 'innerHTML' });
        } catch (e) {
          break;
        }
        if (window.Alpine) window.Alpine.$data(node).open = true;
        this.trackTreeToggle(c.filepath, true);
      }
    },
  };
}
