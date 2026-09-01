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
      window.location = '/logout';
    },
  };
}

function fileGridState() {
  return {
    count: 0,
    allChecked: false,
    downloadMenuOpen: false,
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
    get selectedItems() {
      const boxes = this.$refs.tbody.querySelectorAll('input[type=checkbox]:checked');
      return Array.from(boxes).map((b) => JSON.parse(b.value));
    },
    get isSingleFile() {
      const sel = this.selectedItems;
      return sel.length === 1 && sel[0].leaf !== false;
    },
    get archiveBaseName() {
      const sel = this.selectedItems;
      if (sel.length === 1 && sel[0].leaf === false) return sel[0].name;
      return this.crumbs && this.crumbs.length ? this.crumbs[this.crumbs.length - 1].label : 'download';
    },
    singleDownloadHref() {
      // Reading this.count (even unused) gives Alpine's dependency tracker
      // something reactive to subscribe to - selectedItems itself only
      // reads raw checkbox DOM state via $refs, which Alpine can't see,
      // so without this the href would freeze at whatever it was on the
      // first render and never update as checkboxes are (un)checked.
      void this.count;
      const sel = this.selectedItems;
      if (!sel.length) return '#';
      const params = new URLSearchParams();
      params.set('volume', this.$refs.form.dataset.volume);
      params.set('filepath', sel[0].filepath);
      params.set('name', sel[0].name);
      return '/api/download?' + params.toString();
    },
    bundleHref(format) {
      void this.count; // see singleDownloadHref
      const params = new URLSearchParams();
      params.set('volume', this.$refs.form.dataset.volume);
      params.set('name', this.archiveBaseName);
      params.set('format', format);
      this.selectedItems.forEach((it) => params.append('item', JSON.stringify(it)));
      return '/api/download-bundle?' + params.toString();
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

// Five discrete zoom levels (issue #18). Each level fixes the view span
// and the tick/label scheme (see _ticks* below); it does NOT bound
// panning -- the user can keep dragging left/right past any snapshot at
// every level. Spans are approximate (real months/years vary in length)
// and picked so a readable number of major ticks fills a ~1000px panel.
// Level 1 is farthest in (minutes), level 5 farthest out (years).
const TIMELINE_DAY_MS = 24 * 60 * 60 * 1000;
const ZOOM_LEVELS = {
  1: { span: 2 * 60 * 60 * 1000 }, // ~2 hours   -- minute ticks, 10-min majors
  2: { span: 4 * TIMELINE_DAY_MS }, // ~4 days    -- hourly minors, daily majors
  3: { span: 75 * TIMELINE_DAY_MS }, // ~2.5 months -- daily minors, monthly majors
  4: { span: 365 * TIMELINE_DAY_MS }, // ~1 year   -- month/5 minors, monthly majors
  5: { span: 6 * 365 * TIMELINE_DAY_MS }, // ~6 years -- monthly minors, Jan majors
};
const DEFAULT_ZOOM_LEVEL = 3;

function portalApp(rawSnapshots) {
  const snapshots = rawSnapshots
    .map((s) => ({ ...s, date: new Date(s.time) }))
    .sort((a, b) => a.date - b.date);

  return {
    // --- timeline state ---
    snapshots,
    selectedVolume: null,
    zoomLevel: DEFAULT_ZOOM_LEVEL,
    viewStart: null,
    viewEnd: null,
    activeDayKey: null,
    activeDayItems: [],
    activeDayX: 0,
    activeDayTop: 0,
    _dragMoved: false,
    // Live SVG geometry, refreshed by renderTimeline(). One SVG user unit
    // is one CSS pixel (see renderTimeline), so _w doubles as both the
    // viewBox width and the element's pixel width.
    _w: 1000,
    _axisY: 60,
    _bubbleY: 12,
    _bubbleHeight: 22,

    // --- browse state ---
    volume: null,
    crumbs: [],
    fileFilter: '',
    history: [],
    historyIndex: -1,

    init() {
      this.$nextTick(() => {
        this._bindTimelineDrag();
        // The viewBox is derived from the element's measured pixel width,
        // so a window resize invalidates the whole drawing.
        window.addEventListener('resize', () => this.renderTimeline());
        if (this.snapshots.length) {
          const span = ZOOM_LEVELS[this.zoomLevel].span;
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
      return ((date - this.viewStart) / total) * this._w;
    },

    // The tick a given instant collapses onto at the current zoom level.
    // groupsInView() groups snapshots by this and draws each group's marker
    // at xFor(bucketStart), so it lands exactly on a tick; zooming out
    // merges snapshots whose buckets coincide, zooming in spreads them
    // back apart (issue #18).
    _bucketStart(date) {
      switch (this.zoomLevel) {
        case 1: {
          const d = new Date(date);
          d.setSeconds(0, 0);
          return d;
        }
        case 2: {
          const d = new Date(date);
          d.setMinutes(0, 0, 0);
          return d;
        }
        case 3:
          return new Date(date.getFullYear(), date.getMonth(), date.getDate());
        case 4: {
          const monthStart = new Date(date.getFullYear(), date.getMonth(), 1);
          const nextMonth = new Date(date.getFullYear(), date.getMonth() + 1, 1);
          const step = (nextMonth - monthStart) / 5;
          const k = Math.floor((date - monthStart) / step);
          return new Date(monthStart.getTime() + k * step);
        }
        case 5:
          return new Date(date.getFullYear(), date.getMonth(), 1);
        default:
          return new Date(date.getFullYear(), date.getMonth(), date.getDate());
      }
    },

    // Human-readable heading for a bucket, shown as the list-picker title.
    _bucketLabel(bucketStart) {
      const d = bucketStart;
      const pad = (n) => String(n).padStart(2, '0');
      switch (this.zoomLevel) {
        case 1:
        case 2:
          return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        case 4:
        case 5:
          return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short' });
        default:
          return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      }
    },

    groupsInView() {
      // Group by the current zoom level's tick bucket and position each
      // group's marker at that bucket's start, so it lands exactly on the
      // tick rather than at the snapshot's exact instant.
      const map = new Map();
      for (const s of this.snapshots) {
        if (s.date < this.viewStart || s.date > this.viewEnd) continue;
        const bucketStart = this._bucketStart(s.date);
        const key = bucketStart.getTime();
        if (!map.has(key)) map.set(key, { bucketStart, items: [] });
        map.get(key).items.push(s);
      }
      return Array.from(map.values()).map(({ bucketStart, items }) => ({
        key: this._bucketLabel(bucketStart),
        items,
        x: this.xFor(bucketStart),
      }));
    },

    // Each tick: { x, major, lines }. lines is [] (unlabeled), [str]
    // (single-line minor label) or [top, bottom] (two-line major label).
    ticksInView() {
      switch (this.zoomLevel) {
        case 1:
          return this._ticksMinute();
        case 2:
          return this._ticksHour();
        case 4:
          return this._ticksMonthFifth();
        case 5:
          return this._ticksMonth();
        default:
          return this._ticksDay();
      }
    },

    // Level 1: every tick a minute, majors every 10 min (HH:MM), even
    // minors labeled with the 2-digit minute.
    _ticksMinute() {
      const ticks = [];
      const c = new Date(this.viewStart);
      c.setSeconds(0, 0);
      while (c <= this.viewEnd) {
        if (c >= this.viewStart) {
          const m = c.getMinutes();
          const major = m % 10 === 0;
          let lines = [];
          if (major) lines = [`${c.getHours()}:${String(m).padStart(2, '0')}`];
          else if (m % 2 === 0) lines = [String(m).padStart(2, '0')];
          ticks.push({ x: this.xFor(new Date(c)), major, lines });
        }
        c.setMinutes(c.getMinutes() + 1);
      }
      return ticks;
    },

    // Level 2: majors at the start of a day (month abbr / day number),
    // hourly minors with even hours labeled.
    _ticksHour() {
      const ticks = [];
      const c = new Date(this.viewStart);
      c.setMinutes(0, 0, 0);
      while (c <= this.viewEnd) {
        if (c >= this.viewStart) {
          const h = c.getHours();
          const major = h === 0;
          let lines = [];
          if (major) lines = [c.toLocaleDateString(undefined, { month: 'short' }), String(c.getDate())];
          else if (h % 2 === 0) lines = [String(h)];
          ticks.push({ x: this.xFor(new Date(c)), major, lines });
        }
        c.setHours(c.getHours() + 1);
      }
      return ticks;
    },

    // Level 3: majors at the start of a month (year / month abbr), daily
    // minors with even days labeled.
    _ticksDay() {
      const ticks = [];
      const c = new Date(this.viewStart.getFullYear(), this.viewStart.getMonth(), this.viewStart.getDate());
      while (c <= this.viewEnd) {
        if (c >= this.viewStart) {
          const day = c.getDate();
          const major = day === 1;
          let lines = [];
          if (major) lines = [String(c.getFullYear()), c.toLocaleDateString(undefined, { month: 'short' })];
          else if (day % 2 === 0) lines = [String(day)];
          ticks.push({ x: this.xFor(new Date(c)), major, lines });
        }
        c.setDate(c.getDate() + 1);
      }
      return ticks;
    },

    // Level 4: majors at the start of a month (year / month abbr), one
    // major every 5 unlabeled minor ticks (each month split into fifths).
    _ticksMonthFifth() {
      const ticks = [];
      const c = new Date(this.viewStart.getFullYear(), this.viewStart.getMonth(), 1);
      while (c <= this.viewEnd) {
        const monthStart = new Date(c);
        const nextMonth = new Date(c.getFullYear(), c.getMonth() + 1, 1);
        const step = (nextMonth - monthStart) / 5;
        for (let k = 0; k < 5; k++) {
          const t = new Date(monthStart.getTime() + k * step);
          if (t < this.viewStart || t > this.viewEnd) continue;
          ticks.push({
            x: this.xFor(t),
            major: k === 0,
            lines: k === 0 ? [String(t.getFullYear()), t.toLocaleDateString(undefined, { month: 'short' })] : [],
          });
        }
        c.setMonth(c.getMonth() + 1);
      }
      return ticks;
    },

    // Level 5: every minor tick a month, majors at January (year / "Jan"),
    // even months labeled with the 3-char month name.
    _ticksMonth() {
      const ticks = [];
      const c = new Date(this.viewStart.getFullYear(), this.viewStart.getMonth(), 1);
      while (c <= this.viewEnd) {
        if (c >= this.viewStart) {
          const m = c.getMonth();
          const major = m === 0;
          let lines = [];
          if (major) lines = [String(c.getFullYear()), 'Jan'];
          else if ((m + 1) % 2 === 0) lines = [c.toLocaleDateString(undefined, { month: 'short' })];
          ticks.push({ x: this.xFor(new Date(c)), major, lines });
        }
        c.setMonth(c.getMonth() + 1);
      }
      return ticks;
    },

    async selectSnapshot(snapshot) {
      const preservedSelection = this._captureSelection();
      this.selectedVolume = snapshot.volume;
      this.activeDayKey = null;
      this.activeDayItems = [];
      this.volume = snapshot.volume;
      if (this.crumbs.length === 0) {
        this.crumbs = [{ label: 'Root', filepath: '/' }];
      }
      // Center the view on the selected snapshot's tick bucket (matching
      // where its marker is actually drawn, per groupsInView) under the
      // fixed red center line, preserving the active zoom level. "Anytime
      // an actual snapshot is selected it centers on the thick red line"
      // (issue #18).
      //
      // This pan + repaint happens BEFORE any of the awaits below, so the
      // callout moves to the clicked snapshot the instant it is clicked.
      // The loads that follow hit the PVE file-restore API against a
      // possibly-cold snapshot and can take many seconds (and loadTree()
      // issues one request per expanded tree node); doing them first made
      // a click look like it had simply done nothing.
      const span = this.viewEnd - this.viewStart;
      const target = this._bucketStart(snapshot.date).getTime();
      this.viewStart = new Date(target - span / 2);
      this.viewEnd = new Date(target + span / 2);
      this.renderTimeline();

      // A tree failure must not abort the browse below.
      try {
        await this.loadTree();
      } catch (e) {
        this._treeVolume = null; // let the next selection retry it
      }
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

    // The number in the dark-blue callout: WHICH of this tick's snapshots
    // is currently selected, 1-based, matching the row numbers in the list
    // picker. It is not a count of how many are grouped here -- a lone
    // snapshot always reads "1", and it only differs from 1 when a
    // later member of a multi-snapshot group is picked (issue #18).
    _calloutPosition(group) {
      return group.items.findIndex((s) => s.volume === this.selectedVolume) + 1;
    },

    // What a click on a marker does (issue #18 callout semantics):
    //  - a lone snapshot selects immediately (and centers on the red line);
    //  - a multi-snapshot callout (the pale-blue numbered circle) opens the
    //    tall list box anchored over the callout for the user to pick one --
    //    it does NOT auto-select anything.
    async activateGroup(group) {
      if (group.items.length === 1) {
        await this.selectSnapshot(group.items[0]);
        return;
      }
      this.toggleDay(group.key, group.items, group.x);
    },

    toggleDay(key, items, screenX) {
      if (this.activeDayKey === key) {
        this.activeDayKey = null;
        this.activeDayItems = [];
      } else {
        this.activeDayKey = key;
        this.activeDayItems = items;
        this.activeDayX = screenX;
        // Anchor the popup's BOTTOM edge to the BOTTOM of the callout band,
        // so the picker covers the dark-blue callout it was opened from
        // rather than floating just above it; it grows upward from there
        // (translateY(-100%) in CSS). Anchoring it near the axis instead
        // would make it expand back down over the ruler as the list gets
        // longer. The picker's z-index (10) beats the toolbar's (2), so the
        // part that overlaps the toolbar band still paints on top.
        this.activeDayTop = this._bubbleY + this._bubbleHeight;
      }
    },

    // Step between the five discrete zoom levels (delta -1 = zoom in,
    // +1 = zoom out), clamped at the ends. Only the view span changes;
    // the center instant is held fixed and panning stays unbounded.
    stepZoom(delta) {
      const next = Math.min(5, Math.max(1, this.zoomLevel + delta));
      if (next === this.zoomLevel) return;
      this.zoomLevel = next;
      const mid = (this.viewStart.getTime() + this.viewEnd.getTime()) / 2;
      const half = ZOOM_LEVELS[next].span / 2;
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

      // No units-per-pixel conversion any more: the viewBox is sized to the
      // element's pixel box, so one SVG unit is one pixel.

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
        // Movement past this many pixels counts as a pan, which suppresses
        // the click so panning doesn't also select. Keep it forgiving: a
        // real mouse drifts a few pixels during a deliberate click on a
        // 10px dot, and too tight a threshold silently swallows the click.
        if (Math.abs(e.clientX - startX) > 5) this._dragMoved = true;
        const width = svg.getBoundingClientRect().width;
        if (!width) return;
        const totalMs = dragViewEnd - dragViewStart;
        const dxMs = ((e.clientX - startX) / width) * totalMs;
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
        // 1 unit == 1 px, so client offsets are already SVG coordinates.
        const ux = e.clientX - rect.left;
        let best = null;
        let bestDist = Infinity;
        for (const group of this.groupsInView()) {
          const dist = Math.abs(group.x - ux);
          if (dist < bestDist) {
            bestDist = dist;
            best = group;
          }
        }
        if (!best || bestDist > 14) return;
        e.stopPropagation();
        this.activateGroup(best);
      });
    },

    renderTimeline() {
      const svg = this.$refs.timelineSvg;
      if (!svg) return;
      const NS = 'http://www.w3.org/2000/svg';
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      // ONE SVG USER UNIT == ONE CSS PIXEL.
      //
      // This viewBox is re-derived from the element's measured pixel box on
      // every render (and on window resize) instead of being a fixed
      // 1000-wide box stretched to fit with preserveAspectRatio="none".
      // That fixed box scaled X by ~1.9 and Y by 1 on a full-width panel,
      // which is what turned every <circle> into an oval and stretched the
      // label glyphs horizontally (reading as "vertically squished" text).
      // With the box matched to the pixel size there is no distortion, and
      // shape/font sizes below are plain pixel values.
      const rect = svg.getBoundingClientRect();
      const W = Math.round(rect.width);
      const H = Math.round(rect.height);
      if (!W || !H) return;
      svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
      this._w = W;

      // Soft drop shadow for the snapshot dots. A real feDropShadow rather
      // than the offset black disc this used to draw underneath each dot --
      // that only ever looked like a shadow because the old non-uniform
      // scale smeared it sideways.
      const defs = document.createElementNS(NS, 'defs');
      const filter = document.createElementNS(NS, 'filter');
      filter.setAttribute('id', 'timeline-dot-shadow');
      // Room for the blur so it isn't clipped by the filter region.
      filter.setAttribute('x', '-50%');
      filter.setAttribute('y', '-50%');
      filter.setAttribute('width', '200%');
      filter.setAttribute('height', '200%');
      const drop = document.createElementNS(NS, 'feDropShadow');
      drop.setAttribute('dx', '0');
      drop.setAttribute('dy', '1');
      drop.setAttribute('stdDeviation', '1');
      drop.setAttribute('flood-color', '#000000');
      drop.setAttribute('flood-opacity', '0.45');
      filter.appendChild(drop);
      defs.appendChild(filter);
      svg.appendChild(defs);

      // Vertical layout, absolute within the SVG. The track spans the whole
      // panel (including the toolbar band), so BUBBLE_Y can sit level with
      // the toolbar buttons -- the toolbar paints over the track, which is
      // what makes a dragged bubble slide behind the buttons.
      const AXIS_Y = H - 30; // room below for day + month/year labels
      const BUBBLE_Y = 12;
      const BUBBLE_HEIGHT = 22;
      // Marker groups are translated to the axis, so bubble offsets inside
      // a group are axis-relative.
      const BUBBLE_TOP = BUBBLE_Y - AXIS_Y;
      this._axisY = AXIS_Y;
      this._bubbleY = BUBBLE_Y;
      this._bubbleHeight = BUBBLE_HEIGHT;

      const axis = document.createElementNS(NS, 'line');
      axis.setAttribute('x1', '0');
      axis.setAttribute('x2', String(W));
      axis.setAttribute('y1', String(AXIS_Y));
      axis.setAttribute('y2', String(AXIS_Y));
      axis.setAttribute('class', 'timeline-axis');
      svg.appendChild(axis);

      // Fixed reference line at the horizontal center of the view, spanning
      // the full height of the sunken panel. Selecting a snapshot pans the
      // timeline so its date lands here; it does not track any particular
      // date itself.
      const centerLine = document.createElementNS(NS, 'line');
      centerLine.setAttribute('x1', String(W / 2));
      centerLine.setAttribute('x2', String(W / 2));
      centerLine.setAttribute('y1', '0');
      centerLine.setAttribute('y2', String(H));
      centerLine.setAttribute('class', 'timeline-center-line');
      svg.appendChild(centerLine);

      for (const tick of this.ticksInView()) {
        const g = document.createElementNS(NS, 'g');
        g.setAttribute('transform', `translate(${tick.x},${AXIS_Y})`);
        const line = document.createElementNS(NS, 'line');
        line.setAttribute('y1', '0');
        line.setAttribute('y2', tick.major ? '-8' : '-5');
        line.setAttribute('class', 'timeline-tick');
        g.appendChild(line);
        // The class follows tick.major (majors are bold), the layout follows
        // how many lines the level asked for. The two are independent: a
        // level-1 major is a single bold "11:00", a level-3 major is a
        // two-line bold year/month.
        if (tick.lines.length) {
          const cls = tick.major ? 'timeline-tick-label--major' : 'timeline-tick-label';
          const text = document.createElementNS(NS, 'text');
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('class', cls);
          if (tick.lines.length === 2) {
            const topSpan = document.createElementNS(NS, 'tspan');
            topSpan.setAttribute('x', '0');
            topSpan.setAttribute('y', '14');
            topSpan.textContent = tick.lines[0];
            const botSpan = document.createElementNS(NS, 'tspan');
            botSpan.setAttribute('x', '0');
            botSpan.setAttribute('dy', '11');
            botSpan.textContent = tick.lines[1];
            text.appendChild(topSpan);
            text.appendChild(botSpan);
          } else {
            text.setAttribute('y', '14');
            text.textContent = tick.lines[0];
          }
          g.appendChild(text);
        }
        svg.appendChild(g);
      }


      for (const group of this.groupsInView()) {
        const g = document.createElementNS(NS, 'g');
        g.setAttribute('transform', `translate(${group.x},${AXIS_Y})`);
        g.setAttribute('class', 'timeline-group');
        // Appended up front so getBBox() below can measure the label text
        // (an element must be in the document to have a box).
        svg.appendChild(g);

        // Click target around the dot, kept deliberately narrow and added
        // first so it sits beneath the visible shapes.
        //
        // It must NOT be widened to cover the callout: adjacent days can be
        // ~27px apart at the default zoom (e.g. a guest with two daily
        // snapshots), and marker groups are painted in date order, so an
        // oversized rect on the newer/selected marker lands on top of its
        // older neighbour and swallows every click on it. The callout's own
        // bubble/tail/label are filled shapes and stay clickable on their
        // own, so no extra hit area is needed up there.
        const hit = document.createElementNS(NS, 'rect');
        hit.setAttribute('x', '-12');
        hit.setAttribute('y', '-26');
        hit.setAttribute('width', '24');
        hit.setAttribute('height', '36');
        hit.setAttribute('fill', 'transparent');
        hit.setAttribute('class', 'timeline-hit-area');
        g.appendChild(hit);

        const isSelected = group.items.some((s) => s.volume === this.selectedVolume);

        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('r', '5');
        dot.setAttribute('filter', 'url(#timeline-dot-shadow)');
        dot.setAttribute('class', 'timeline-dot' + (isSelected ? ' timeline-dot--selected' : ''));
        g.appendChild(dot);

        if (isSelected) {
          const selected = group.items.find((s) => s.volume === this.selectedVolume);
          const posStr = String(this._calloutPosition(group));
          const tsStr = this._formatTimestamp(selected.time);
          const bubbleBottom = BUBBLE_TOP + BUBBLE_HEIGHT;
          const tailApex = bubbleBottom + 6;

          const text = document.createElementNS(NS, 'text');
          text.setAttribute('x', '0');
          text.setAttribute('y', String(BUBBLE_TOP + 15));
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('class', 'timeline-bubble-label');
          const countSpan = document.createElementNS(NS, 'tspan');
          countSpan.textContent = posStr;
          const tsSpan = document.createElementNS(NS, 'tspan');
          tsSpan.setAttribute('dx', '8');
          tsSpan.textContent = tsStr;
          text.appendChild(countSpan);
          text.appendChild(tsSpan);
          g.appendChild(text);

          // Size the pill from the text's real rendered box rather than a
          // characters-times-magic-number estimate, which drifts with the
          // font and only ever worked at one particular scale.
          const width = Math.ceil(text.getBBox().width) + 24;

          const connector = document.createElementNS(NS, 'line');
          connector.setAttribute('x1', '0');
          connector.setAttribute('x2', '0');
          connector.setAttribute('y1', String(tailApex));
          connector.setAttribute('y2', '-4');
          connector.setAttribute('class', 'timeline-bubble-connector');
          g.insertBefore(connector, text);

          const bubble = document.createElementNS(NS, 'rect');
          bubble.setAttribute('x', String(-width / 2));
          bubble.setAttribute('y', String(BUBBLE_TOP));
          bubble.setAttribute('width', String(width));
          bubble.setAttribute('height', String(BUBBLE_HEIGHT));
          bubble.setAttribute('rx', '3');
          bubble.setAttribute('class', 'timeline-bubble timeline-bubble--selected');
          g.insertBefore(bubble, text);

          const tail = document.createElementNS(NS, 'polygon');
          tail.setAttribute('points', `-5,${bubbleBottom} 5,${bubbleBottom} 0,${tailApex}`);
          tail.setAttribute('class', 'timeline-bubble timeline-bubble--selected');
          g.insertBefore(tail, text);

        } else {
          // Unselected marker: the small pale-blue numbered pill + tail. A
          // lone snapshot still shows its "1"; several collapsed onto one
          // tick show the count. Clicking a count > 1 opens the list picker
          // (activateGroup); it never auto-selects (issue #18).
          const bubble = document.createElementNS(NS, 'rect');
          bubble.setAttribute('x', '-9');
          bubble.setAttribute('y', '-22');
          bubble.setAttribute('width', '18');
          bubble.setAttribute('height', '14');
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
          count.setAttribute('class', 'timeline-bubble-label timeline-bubble-label--count');
          count.textContent = String(group.items.length);
          g.appendChild(count);
        }

        g.addEventListener('click', (e) => {
          // Without this, Alpine's @click.outside on the day-picker popup
          // sees this same click (the popup isn't open yet, so the marker
          // "is outside" it) and immediately closes what toggleDay just
          // opened, in the same bubble phase.
          e.stopPropagation();
          if (this._dragMoved) return;
          this.activateGroup(group);
        });
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
