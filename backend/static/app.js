// --- Session expiry (issue #27) ---
// A 401 from any /api call means the server-side session is gone - idle
// timeout, or a PVE ticket that could no longer be refreshed
// (auth.get_session). The backend answers fetch() callers with a plain
// 401 (and htmx with an HX-Redirect it handles itself). Without this,
// every fetch() call site just bails on the non-OK status - the poll
// loop especially keeps failing silently every tick, forever, with no
// hint to the user. Mirror what a fresh page load already does: go to
// /login, with a reason so that page can explain why.
let sessionExpiredHandled = false;
function redirectToLogin() {
  if (sessionExpiredHandled) return; // a burst of failing polls => one redirect
  sessionExpiredHandled = true;
  window.location.href = '/login?reason=expired';
}

// Drop-in for fetch() on authenticated endpoints. Returns the Response
// for the caller's own !resp.ok handling; on 401 it triggers the
// redirect and returns a promise that never settles, so the caller does
// not also flash its generic error in the moment before navigation.
async function apiFetch(input, init) {
  const resp = await fetch(input, init);
  if (resp.status === 401) {
    redirectToLogin();
    return new Promise(() => {});
  }
  return resp;
}

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

function restoreJobsWidget() {
  const ACTIVE_STATUSES = ['queued', 'running', 'verifying'];
  const POLL_INTERVAL_MS = 4000;
  return {
    open: false,
    jobs: [],
    selectedId: null,
    // Drag offset for the modal (docs/plan.md §7.5's UI section) - an
    // inline transform on the modal box, reset only on a fresh page
    // load so the position persists across repeated opens/closes.
    dragX: 0,
    dragY: 0,
    // Same idea, independent offset, for the log viewer modal below -
    // it's a separate modal_box that can be open at the same time as
    // (well, actually instead of, but positioned independently from)
    // the jobs list modal.
    logDragX: 0,
    logDragY: 0,
    // Tracks whether the *backdrop itself* (not a descendant) was where
    // the current mouse-down/up gesture started, so a click-outside
    // close only fires when both ends of the gesture targeted the
    // backdrop - not just @click.self, which resolves its target from
    // wherever the mouseup happens to land. That distinction matters
    // because dragging the native resize handle to grow a modal (see
    // .modal-box--resizable) can end with the mouseup landing back on
    // the backdrop even though the gesture began on the resize handle,
    // which made resizing larger read as a click-outside and close the
    // modal - not a regression from any one change, present since
    // resizing was first added. One shared flag is safe even with two
    // stacked overlays open at once (job list + log) since the topmost
    // one's backdrop covers the full viewport, so a mousedown can never
    // land on the other one's backdrop underneath it.
    backdropDown: false,
    // Log viewer - a second modal, live-updated by piggybacking on the
    // same poll tick as the job list (below) rather than running its own
    // separate timer.
    logOpen: false,
    logJobId: null,
    logDetail: null,
    logLoading: false,
    logError: null,
    get activeCount() {
      return this.jobs.filter((j) => ACTIVE_STATUSES.includes(j.status)).length;
    },
    get selectedCancellable() {
      const job = this.jobs.find((j) => j.id === this.selectedId);
      return !!job && job.cancellable;
    },
    init() {
      this.refresh();
      setInterval(() => this.refresh(), POLL_INTERVAL_MS);
    },
    async refresh() {
      try {
        const resp = await apiFetch('/api/restore-jobs');
        if (!resp.ok) return;
        this.jobs = await resp.json();
      } catch (e) {
        // Transient failure - keep showing the last known list rather
        // than flashing it empty on every hiccup.
      }
      if (this.logOpen) await this.refreshLog();
    },
    async cancelSelected() {
      if (!this.selectedId || !this.selectedCancellable) return;
      try {
        await apiFetch('/api/restore-jobs/' + encodeURIComponent(this.selectedId) + '/cancel', { method: 'POST' });
      } finally {
        await this.refresh();
      }
    },
    openLog() {
      if (!this.selectedId) return;
      this.logJobId = this.selectedId;
      this.logDetail = null;
      this.logError = null;
      this.logOpen = true;
      this.refreshLog();
    },
    async refreshLog() {
      if (!this.logJobId) return;
      this.logLoading = true;
      try {
        const resp = await apiFetch('/api/restore-jobs/' + encodeURIComponent(this.logJobId));
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          this.logError = data.detail || 'Could not load this job’s log.';
          return;
        }
        this.logDetail = data;
        this.logError = null;
        this.$nextTick(() => {
          const el = this.$refs.logBody;
          if (el) el.scrollTop = el.scrollHeight;
        });
      } catch (e) {
        this.logError = 'Could not load this job’s log: ' + e;
      } finally {
        this.logLoading = false;
      }
    },
    formatElapsed(seconds) {
      const total = Math.max(0, Math.floor(seconds || 0));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      const pad = (n) => String(n).padStart(2, '0');
      return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
    },
    formatStatus(job) {
      const pct = job.progress_percent;
      return pct === null || pct === undefined ? job.status : `${job.status} (${pct}%)`;
    },

    // Dragging a modal by its header. Same window-pointermove/pointerup
    // pattern as the timeline's drag-to-pan (_bindTimelineDrag) -
    // deliberately no setPointerCapture(), which retargets click/pointerup
    // to the capturing element and would break the header's own Close
    // button and the table's row-selection clicks (see that function's
    // comment for the full story). xProp/yProp let the jobs-list modal
    // and the log modal share this logic while tracking independent
    // offsets (dragX/dragY vs logDragX/logDragY).
    startDrag(e, xProp = 'dragX', yProp = 'dragY') {
      if (e.target.closest('.modal-close')) return; // let Close still work
      const startX = e.clientX;
      const startY = e.clientY;
      const originX = this[xProp];
      const originY = this[yProp];

      const onMove = (ev) => {
        this[xProp] = originX + (ev.clientX - startX);
        this[yProp] = originY + (ev.clientY - startY);
      };
      const endDrag = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', endDrag);
        window.removeEventListener('pointercancel', endDrag);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', endDrag);
      window.addEventListener('pointercancel', endDrag);
    },
  };
}

// Theme (issue #29). The choice is persisted client-side only (no
// backend, no session) in localStorage under `pfr_theme`, one of
// 'auto' | 'light' | 'dark'. 'auto' follows the OS, but with no
// explicit OS preference it resolves to dark. base.html / login.html
// carry a tiny inline copy of this resolution in <head> so the initial
// paint is already themed; this module keeps it in sync afterwards.
const THEME_CHOICES = ['auto', 'light', 'dark', 'proxmox-dark'];

// The admin-configured default (DEFAULT_THEME in .env), injected into
// <head> by base.html / login.html before this script runs. Used only
// when the visitor has made no explicit choice of their own.
function serverDefaultTheme() {
  try {
    const d = window.__PFR_DEFAULT_THEME__;
    if (THEME_CHOICES.includes(d)) return d;
  } catch (e) {
    // no window global - fall through
  }
  return 'auto';
}

function readStoredTheme() {
  try {
    const t = window.localStorage.getItem('pfr_theme');
    if (THEME_CHOICES.includes(t)) return t;
  } catch (e) {
    // storage unavailable - fall through to the server default
  }
  return serverDefaultTheme();
}

function resolveTheme(choice) {
  if (THEME_CHOICES.includes(choice) && choice !== 'auto') return choice;
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  } catch (e) {
    return 'dark';
  }
}

function applyTheme(choice) {
  const resolved = resolveTheme(choice);
  try {
    document.documentElement.setAttribute('data-theme', resolved);
  } catch (e) {
    // No document (tests) - the caller still gets the resolved value.
  }
  return resolved;
}

function userMenu(identity) {
  return {
    identity,
    open: false,
    aboutOpen: false,
    themeOpen: false,
    theme: 'auto', // the persisted choice
    themeChoice: 'auto', // the dropdown's working value while the modal is open
    init() {
      this.theme = readStoredTheme();
      this.themeChoice = this.theme;
      applyTheme(this.theme);
      // Re-resolve 'auto' if the OS flips its light/dark preference
      // while the page is open.
      try {
        const mq = window.matchMedia('(prefers-color-scheme: light)');
        const onChange = () => {
          if (this.theme === 'auto') applyTheme('auto');
        };
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
      } catch (e) {
        // matchMedia unavailable - 'auto' just stays at its load-time value.
      }
    },
    openThemeModal() {
      this.open = false;
      this.themeChoice = this.theme;
      this.themeOpen = true;
    },
    saveTheme() {
      this.theme = THEME_CHOICES.includes(this.themeChoice) ? this.themeChoice : 'auto';
      try {
        window.localStorage.setItem('pfr_theme', this.theme);
      } catch (e) {
        // Private mode / storage disabled - the choice still applies for this page.
      }
      applyTheme(this.theme);
      this.themeOpen = false;
    },
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

    // --- PH.5 restore modal (docs/plan.md §7.5) ---
    restoreOpen: false,
    restoreDestDir: '',
    restoreOverwrite: false,
    // Both need VM.GuestAgent.Unrestricted (same gate as browsing, hence
    // reusing restoreBrowseAvailable below rather than a separate flag) -
    // restoreMetadata only restores mtime, the one piece of metadata
    // file-restore/list actually exposes (no owner/mode available on any
    // PVE version).
    restoreMetadata: false,
    restoreVerify: false,
    restoreSubmitting: false,
    restoreError: null,
    restoreSubmitted: false,
    // Browsing the guest filesystem needs VM.GuestAgent.Unrestricted
    // (guest-exec, no dedicated QGA listing command - docs/plan.md §7.5),
    // so it's only offered when that's available; otherwise restoreDestDir
    // stays a plain typed field.
    restoreBrowseAvailable: false,
    restoreBrowsing: false,
    restoreBrowsePath: null,
    restoreBrowseParent: null,
    restoreBrowseEntries: [],
    restoreBrowseLoading: false,
    restoreBrowseError: null,
    // Status messages (loading/error/empty) used to render inside the
    // folder-list box itself, above the entries - moved into the
    // toolbar's path display instead so the path/status line stays in
    // one consistent place rather than the list area's content shifting
    // around depending on state. null means "show the real path" (the
    // toolbar falls back to that itself).
    get restoreBrowseStatusText() {
      if (this.restoreBrowseLoading) return 'Loading…';
      if (this.restoreBrowseError) return this.restoreBrowseError;
      if (this.restoreBrowseEntries.length === 0) return 'No subfolders here.';
      return null;
    },

    init() {
      this.applySort();
    },

    // guest/snapshotTime/browseAvailable come from the caller's Alpine
    // expression (see file_grid.html), which - unlike this method body
    // itself - is evaluated in the ancestor portalApp() scope, so it can
    // read `guest`/`selectedSnapshotTime`/`restoreCaps` directly. A plain
    // JS method here has no such scope-chaining, hence passing them in as
    // arguments rather than trying `this.guest` from inside fileGridState().
    openRestore(guestType, guestVmid, guestLabel, snapshotTime, browseAvailable) {
      this._guestType = guestType;
      this._guestVmid = guestVmid;
      this._guestLabel = guestLabel;
      this._snapshotTime = snapshotTime;
      this.restoreDestDir = '';
      this.restoreOverwrite = false;
      this.restoreMetadata = false;
      this.restoreVerify = false;
      this.restoreError = null;
      this.restoreSubmitted = false;
      this.restoreBrowseAvailable = browseAvailable;
      this.restoreBrowsing = browseAvailable;
      this.restoreBrowsePath = null;
      this.restoreBrowseParent = null;
      this.restoreBrowseEntries = [];
      this.restoreBrowseError = null;
      this.restoreOpen = true;
      if (browseAvailable) this.browseInto(null);
    },

    // mode is 'browse' or 'manual' - the segmented toggle above the
    // destination picker calls this directly rather than a plain flip, so
    // clicking the already-active side is a no-op instead of re-fetching.
    setDestMode(mode) {
      const wantBrowsing = mode === 'browse';
      if (wantBrowsing === this.restoreBrowsing) return;
      this.restoreBrowsing = wantBrowsing;
      if (wantBrowsing) this.browseInto(this.restoreBrowsePath);
    },

    async browseInto(path) {
      this.restoreBrowseLoading = true;
      this.restoreBrowseError = null;
      try {
        const params = new URLSearchParams({ type: this._guestType, vmid: this._guestVmid });
        if (path) params.set('path', path);
        const resp = await apiFetch('/api/restore-browse?' + params.toString());
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          this.restoreBrowseError = data.detail || 'Could not list that folder.';
          return;
        }
        this.restoreBrowsePath = data.path;
        this.restoreBrowseParent = data.parent;
        this.restoreBrowseEntries = data.entries || [];
        if (data.path) this.restoreDestDir = data.path;
      } catch (e) {
        this.restoreBrowseError = 'Could not list that folder: ' + e;
      } finally {
        this.restoreBrowseLoading = false;
      }
    },

    browseUp() {
      // restoreBrowseParent is null both at an actual filesystem root
      // (POSIX "/") and at a Windows drive root - the template disables
      // the Up button in both cases; a separate "Drives" shortcut
      // (browseInto(null)) is what gets a Windows user from a drive root
      // back to the drive list, rather than overloading this button.
      this.browseInto(this.restoreBrowseParent);
    },

    async startRestore() {
      void this.count; // see singleDownloadHref - keeps selectedItems reactive
      const sel = this.selectedItems;
      if (sel.length < 1 || !this.restoreDestDir || !this.restoreOverwrite) return;
      this.restoreSubmitting = true;
      this.restoreError = null;
      try {
        const body = new URLSearchParams();
        body.set('volume', this.$refs.form.dataset.volume);
        body.set('guest_type', this._guestType);
        body.set('vmid', this._guestVmid);
        body.set('guest_label', this._guestLabel);
        body.set('snapshot_time', this._snapshotTime);
        body.set('dest_dir', this.restoreDestDir);
        body.set('overwrite', this.restoreOverwrite ? 'true' : 'false');
        if (this.isSingleFile) {
          // Single-leaf-file restore - unchanged from before multi-file
          // restore existed (docs/plan.md §7.7, issue #24).
          body.set('filepath', sel[0].filepath);
          body.set('name', sel[0].name);
          body.set('restore_metadata', this.restoreMetadata ? 'true' : 'false');
          body.set('verify', this.restoreVerify ? 'true' : 'false');
          if (sel[0].mtime !== null && sel[0].mtime !== undefined) {
            body.set('source_mtime', String(sel[0].mtime));
          }
        } else {
          // Multi-file/directory bundle restore - restore_metadata/
          // verify don't apply here (mtime is automatic via the bundle
          // format; verify always runs, not optional - see the modal's
          // own copy) so they're deliberately not sent.
          sel.forEach((it) => body.append('item', JSON.stringify(it)));
        }
        const resp = await apiFetch('/api/restore', { method: 'POST', body });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          this.restoreError = data.detail || 'Restore failed to start.';
          return;
        }
        this.restoreSubmitted = true;
      } catch (e) {
        this.restoreError = 'Restore failed to start: ' + e;
      } finally {
        this.restoreSubmitting = false;
      }
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
//
// Every level shows ~60 minor ticks, so at a typical panel width they land
// ~18px apart and the every-other-one labels ~36px apart -- enough that a
// 10px font's 2-4 character labels clear each other. _thinLabels() is the
// backstop for narrower panels.
const TIMELINE_MINUTE_MS = 60 * 1000;
const TIMELINE_HOUR_MS = 60 * TIMELINE_MINUTE_MS;
const TIMELINE_DAY_MS = 24 * TIMELINE_HOUR_MS;
const ZOOM_LEVELS = {
  1: { span: 60 * TIMELINE_MINUTE_MS }, // 1 hour    -- 60 minute minors
  2: { span: 60 * TIMELINE_HOUR_MS }, // 2.5 days  -- 60 hour minors
  3: { span: 60 * TIMELINE_DAY_MS }, // 60 days   -- 60 day minors
  4: { span: 365 * TIMELINE_DAY_MS }, // ~1 year   -- 60 month-fifth minors
  5: { span: 5 * 365 * TIMELINE_DAY_MS }, // ~5 years  -- 60 month minors
};
const DEFAULT_ZOOM_LEVEL = 3;

function portalApp(rawSnapshots, guest) {
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

    // --- PH.5 restore state (docs/plan.md §7.5) ---
    // {type, vmid, label} for the currently-viewed guest - static for the
    // whole page load (switching guest is a full navigation, see taskPicker's
    // confirm()), so capabilities are fetched once here rather than on every
    // folder browse/htmx swap.
    guest,
    restoreCaps: null,

    async _loadRestoreCapabilities() {
      if (!this.guest || !this.guest.vmid) return;
      try {
        const params = new URLSearchParams({ type: this.guest.type, vmid: this.guest.vmid });
        const resp = await apiFetch('/api/restore-capabilities?' + params.toString());
        if (!resp.ok) return;
        this.restoreCaps = await resp.json();
      } catch (e) {
        this.restoreCaps = null;
      }
    },

    init() {
      this._loadRestoreCapabilities();
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

    get selectedSnapshotTime() {
      const match = this.snapshots.find((s) => s.volume === this.selectedVolume);
      return match ? match.time : '';
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
          return this._thinLabels(this._ticksMinute());
        case 2:
          return this._thinLabels(this._ticksHour());
        case 4:
          return this._thinLabels(this._ticksMonthFifth());
        case 5:
          return this._thinLabels(this._ticksMonth());
        default:
          return this._thinLabels(this._ticksDay());
      }
    },

    // Tick labels are plain SVG text with no collision handling of their
    // own, so on a narrow panel (or right beside a wide two-line major)
    // neighbouring labels run into each other. Drop the ones that don't
    // fit: majors are always kept, then minors are taken left-to-right and
    // any whose estimated box would touch one already kept is unlabeled.
    // The tick mark itself always stays -- only its text goes.
    //
    // Two gaps, because a minor next to a major reads fine much tighter
    // than two minors next to each other. Without the looser major gap the
    // last labelled day of a month (28/29/30) -- one day before the next
    // month's wide two-line major -- gets dropped, which is what "Feb had
    // no 28" was.
    _thinLabels(ticks) {
      const GAP_MINOR = 4; // clear px required between two minor labels
      const GAP_MAJOR = 0; // minor-vs-major: touching is ok, overlap is not
      const CHAR_W = 5; // ~10px Open Sans advance per digit/letter
      const halfWidth = (t) => (Math.max(...t.lines.map((l) => l.length)) * CHAR_W) / 2;
      const kept = [];
      for (const t of ticks) {
        if (t.major && t.lines.length) kept.push({ x: t.x, half: halfWidth(t), major: true });
      }
      for (const t of ticks) {
        if (t.major || !t.lines.length) continue;
        const half = halfWidth(t);
        const collides = kept.some((k) => {
          const gap = k.major ? GAP_MAJOR : GAP_MINOR;
          return Math.abs(k.x - t.x) < k.half + half + gap;
        });
        if (collides) t.lines = [];
        else kept.push({ x: t.x, half, major: false });
      }
      return ticks;
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
          // Grows upward from the tail at y=-8 so the tail stays attached
          // to the dot; the hit area (-26..10) still covers the taller pill.
          const bubble = document.createElementNS(NS, 'rect');
          bubble.setAttribute('x', '-9');
          bubble.setAttribute('y', '-25');
          bubble.setAttribute('width', '18');
          bubble.setAttribute('height', '17');
          bubble.setAttribute('rx', '3');
          bubble.setAttribute('class', 'timeline-bubble');
          g.appendChild(bubble);

          const tail = document.createElementNS(NS, 'polygon');
          tail.setAttribute('points', '-3,-8 3,-8 0,-4');
          tail.setAttribute('class', 'timeline-bubble');
          g.appendChild(tail);

          const count = document.createElementNS(NS, 'text');
          count.setAttribute('x', '0');
          count.setAttribute('y', '-13');
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
