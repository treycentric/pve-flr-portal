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

    selectSnapshot(snapshot) {
      this.selectedVolume = snapshot.volume;
      this.activeDayKey = null;
      this.activeDayItems = [];
      this.volume = snapshot.volume;
      this.crumbs = [{ label: 'Root', filepath: '/' }];
      // Center the view on the selected date, under the fixed center line,
      // preserving whatever zoom level is currently active.
      const span = this.viewEnd - this.viewStart;
      const target = snapshot.date.getTime();
      this.viewStart = new Date(target - span / 2);
      this.viewEnd = new Date(target + span / 2);
      this.load();
      this.renderTimeline();
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
      const now = Date.now();
      const span = this.viewEnd - this.viewStart;
      this.viewStart = new Date(now - span / 2);
      this.viewEnd = new Date(now + span / 2);
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

      const endDrag = () => {
        dragging = false;
        svg.classList.remove('dragging');
      };

      svg.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        dragging = true;
        this._dragMoved = false;
        startX = e.clientX;
        dragViewStart = this.viewStart;
        dragViewEnd = this.viewEnd;
        svg.setPointerCapture(e.pointerId);
        svg.classList.add('dragging');
      });

      svg.addEventListener('pointermove', (e) => {
        if (!dragging) return;
        if (Math.abs(e.clientX - startX) > 3) this._dragMoved = true;
        const dxUnits = (e.clientX - startX) * unitsPerPixel();
        const totalMs = dragViewEnd - dragViewStart;
        const dxMs = (dxUnits / 1000) * totalMs;
        this.viewStart = new Date(dragViewStart.getTime() - dxMs);
        this.viewEnd = new Date(dragViewEnd.getTime() - dxMs);
        this.renderTimeline();
      });

      svg.addEventListener('pointerup', endDrag);
      svg.addEventListener('pointerleave', endDrag);
      svg.addEventListener('pointercancel', endDrag);
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
        }

        g.addEventListener('click', () => {
          if (this._dragMoved) return;
          const screenX = offsetX + group.x * scaleX;
          this.toggleDay(group.key, group.items, screenX);
        });

        svg.appendChild(g);
      }
    },

    // --- folder browsing (drives the file grid via htmx) ---
    goInto(filepath, label) {
      this.crumbs.push({ label, filepath });
      this.load();
    },
    goTo(idx) {
      this.crumbs = this.crumbs.slice(0, idx + 1);
      this.load();
    },
    up() {
      if (this.crumbs.length > 1) {
        this.crumbs.pop();
        this.load();
      }
    },
    load() {
      const current = this.crumbs[this.crumbs.length - 1];
      const url =
        '/api/browse?volume=' + encodeURIComponent(this.volume) +
        '&filepath=' + encodeURIComponent(current.filepath);
      htmx.ajax('GET', url, { target: '#file-grid', indicator: '#loading' });
    },
  };
}
