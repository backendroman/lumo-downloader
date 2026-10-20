// alpine csp build can't evaluate expressions in html, so all logic lives here
(() => {
  "use strict";

  const STORAGE_KEY = "lumo:job";
  const MAX_TOASTS = 4;
  const RING_LENGTH = 2 * Math.PI * 24; // r=24 in the template

  const nf1 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });

  function formatBytes(bytes) {
    if (!bytes || bytes < 0) return "";
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${(i >= 2 ? nf1 : nf0).format(n)} ${units[i]}`;
  }

  const pad = (v) => (v < 10 ? `0${v}` : String(v));

  function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return "";
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  }

  function formatEta(seconds) {
    if (seconds == null || !isFinite(seconds) || seconds < 0) return "";
    const s = Math.round(seconds);
    if (s < 60) return `≈ ${Math.max(1, s)} с`;
    const m = Math.floor(s / 60);
    if (m < 60) return s % 60 ? `≈ ${m} мин ${s % 60} с` : `≈ ${m} мин`;
    return `≈ ${Math.floor(m / 60)} ч ${m % 60} мин`;
  }

  class ApiError {
    constructor(kind, code, message, status) {
      this.kind = kind; // network | server
      this.code = code;
      this.message = message;
      this.status = status || 0;
    }
  }

  function api(method, path, body) {
    const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(
      (res) => res.json().catch(() => null).then((data) => {
        if (res.ok && data && data.ok) return data;
        const err = (data && data.error) || {};
        throw new ApiError("server", err.code || "internal",
          err.message || `Сервер вернул ошибку (${res.status}). Попробуйте ещё раз.`, res.status);
      }),
      () => {
        throw new ApiError("network", "network", "Нет соединения с сервером. Проверьте интернет и повторите.", 0);
      }
    );
  }

  const EXTRACTION_CODES = new Set([
    "unsupported", "playlist", "live", "no_formats", "auth_required", "drm", "geo_blocked",
    "unavailable", "too_long", "source_blocked", "rate_limited_source", "timeout", "network_error",
  ]);
  const INPUT_CODES = new Set(["invalid_url", "unsupported_scheme", "forbidden_target", "dns_error", "invalid_request"]);
  const RETRYABLE = new Set([
    "network", "timeout", "busy", "rate_limited", "network_error", "source_blocked", "rate_limited_source", "internal",
  ]);

  const HINTS = {
    unsupported: "Проверьте, что ссылка ведёт на страницу конкретного видео или трека.",
    playlist: "Откройте нужное видео из плейлиста и скопируйте ссылку на него.",
    auth_required: "Сервис не скачивает приватный и платный контент.",
    no_formats: "Источник не отдал ни одного подходящего потока.",
    busy: "Все слоты заняты, попробуйте через минуту.",
    rate_limited: "Лимит запросов скоро обновится.",
    network: "Похоже, пропало соединение с сервером.",
  };

  const STAGES = {
    created: "Создаём задание",
    queued: "Ожидаем свободный слот",
    starting: "Подключаемся к источнику",
    downloading: "Скачиваем",
    merging: "Объединяем видео и аудио",
    converting: "Преобразуем формат",
    finalizing: "Подготавливаем файл",
    completed: "Файл готов",
    cancelled: "Загрузка отменена",
    failed: "Не удалось скачать",
    expired: "Срок хранения файла истёк",
  };
  const STAGE_ORDER = { created: 0, queued: 0, starting: 1, downloading: 1, merging: 2, converting: 2, finalizing: 2, completed: 3 };
  const ACTIVE_STATUSES = new Set(["created", "queued", "running", "processing"]);
  const FAILED_STATUSES = new Set(["failed", "cancelled", "expired"]);

  function looksLikeUrl(text) {
    if (!text) return false;
    const t = text.trim();
    return t.length <= 2048 && /^(https?:\/\/)?[^\s\/?#]+\.[^\s\/?#]{2,}([\/?#]\S*)?$/i.test(t);
  }

  const jobPath = (id, suffix = "") => `/api/jobs/${encodeURIComponent(id)}${suffix}`;

  function component() {
    return {
      view: "idle", // idle | analyzing | ready | job | error
      url: "",
      urlInvalid: false,
      media: {},
      options: [],
      analysisId: null,
      tab: "video",
      selectedId: null,
      thumbFailed: false,
      thumbLoaded: false,
      starting: false,
      job: null,
      jobId: null,
      cancelling: false,
      connection: "idle", // idle | open | reconnecting | lost
      error: null,
      toasts: [],
      theme: "dark",
      hasFfmpeg: true,
      _es: null,
      _reconnectTimer: null,
      _reconnectAttempts: 0,
      _toastSeq: 0,
      _lastAnalyzedUrl: "",

      init() {
        const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
        this.theme = document.documentElement.getAttribute("data-theme") || (dark ? "dark" : "light");
        this.hasFfmpeg = this.$el.getAttribute("data-has-ffmpeg") === "true";
        window.addEventListener("online", () => this.onOnline());
        window.addEventListener("offline", () => this.onOffline());
        document.addEventListener("keydown", (e) => {
          const tag = document.activeElement ? document.activeElement.tagName : "";
          if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA") {
            e.preventDefault();
            this.$refs.url.focus();
          }
        });
        this.resumeJob();
      },

      toggleTheme() {
        this.theme = this.theme === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", this.theme);
        try { localStorage.setItem("theme", this.theme); } catch (e) {}
      },
      get themeLabel() { return this.theme === "dark" ? "Светлая тема" : "Тёмная тема"; },

      get hasUrl() { return this.url.length > 0; },
      get canPaste() { return !this.hasUrl && !!(navigator.clipboard && navigator.clipboard.readText); },
      get isAnalyzing() { return this.view === "analyzing"; },
      get isBusy() { return this.view === "analyzing" || this.starting || (this.view === "job" && this.jobActive); },
      get submitDisabled() { return this.isBusy || this.url.trim().length === 0; },
      get submitLabel() { return this.isAnalyzing ? "Ищем…" : "Найти"; },
      get linkbarClass() {
        return { "is-busy": this.isAnalyzing, "is-invalid": this.urlInvalid, "has-value": this.hasUrl };
      },
      get hintText() {
        return this.urlInvalid ? "Это не похоже на ссылку." : "";
      },
      get hintClass() { return { "is-error": this.urlInvalid }; },
      get heroClass() { return { "is-compact": this.view !== "idle" }; },

      onUrlInput() { this.urlInvalid = false; },
      onPaste() {
        // x-model updates on the next tick
        setTimeout(() => {
          if (!this.isBusy && looksLikeUrl(this.url)) this.analyze();
        }, 30);
      },
      clearUrl() {
        this.url = "";
        this.urlInvalid = false;
        this.$refs.url.focus();
      },
      pasteFromClipboard() {
        navigator.clipboard.readText().then((text) => {
          text = (text || "").trim();
          if (!text) { this.toast("Буфер обмена пуст.", "info"); return; }
          this.url = text.slice(0, 2048);
          if (looksLikeUrl(this.url)) this.analyze();
          else { this.urlInvalid = true; this.$refs.url.focus(); }
        }, () => {
          this.toast("Нет доступа к буферу, вставьте вручную.", "info");
          this.$refs.url.focus();
        });
      },

      analyze() {
        const url = this.url.trim();
        if (!url) { this.$refs.url.focus(); return; }
        if (!looksLikeUrl(url)) { this.urlInvalid = true; this.$refs.url.focus(); return; }
        if (this.isBusy) return;
        this.closeStream();
        this.clearStoredJob();
        this.job = null; this.jobId = null; this.error = null;
        this.media = {}; this.options = []; this.analysisId = null; this.selectedId = null;
        this.thumbFailed = false; this.thumbLoaded = false;
        this.view = "analyzing";
        this._lastAnalyzedUrl = url;
        // ignore the response if the user has moved on since
        const stale = () => this._lastAnalyzedUrl !== url || this.view !== "analyzing";
        api("POST", "/api/analyze", { url }).then((data) => {
          if (stale()) return;
          this.media = data.media || {};
          this.options = data.options || [];
          this.analysisId = data.analysis_id;
          this.tab = this.options.some((o) => o.kind === "video") ? "video" : "audio";
          this.selectedId = this.defaultOptionId(this.tab);
          this.view = "ready";
        }, (err) => {
          if (stale()) return;
          this.showError(err, "analysis");
        });
      },

      showError(err, phase) {
        const code = err.code || "internal";
        let kind;
        if (err.kind === "network") kind = "Ошибка сети";
        else if (INPUT_CODES.has(code)) kind = "Проверьте ссылку";
        else if (code === "rate_limited" || code === "busy" || code === "too_many_jobs") kind = "Сервер занят";
        else if (phase === "job") kind = "Ошибка загрузки";
        else if (EXTRACTION_CODES.has(code)) kind = "Не удалось получить медиа";
        else kind = "Ошибка";
        this.error = { code, kind, message: err.message, hint: HINTS[code] || "", network: err.kind === "network" };
        this.urlInvalid = code === "invalid_url";
        this.view = "error";
      },
      get errorKind() { return this.error ? this.error.kind : ""; },
      get errorMessage() { return this.error ? this.error.message : ""; },
      get errorHint() { return this.error ? this.error.hint : ""; },
      get canRetry() { return !!this.error && (this.error.network || RETRYABLE.has(this.error.code)); },

      get hasMedia() { return this.view === "ready" || this.view === "job"; },
      get showThumb() { return !!this.media.thumbnail && !this.thumbFailed; },
      get thumbClass() { return { "is-loaded": this.thumbLoaded && !this.thumbFailed, "is-empty": !this.showThumb }; },
      get durationText() { return formatDuration(this.media.duration); },
      get uploaderInitial() { return (this.media.uploader || "").trim().charAt(0).toUpperCase(); },
      onThumbError() { this.thumbFailed = true; },
      onThumbLoad() { this.thumbLoaded = true; },

      get videoOptions() { return this.options.filter((o) => o.kind === "video"); },
      get audioOptions() { return this.options.filter((o) => o.kind === "audio"); },
      get showTabs() { return this.videoOptions.length > 0 && this.audioOptions.length > 0; },
      get visibleOptions() { return this.tab === "video" ? this.videoOptions : this.audioOptions; },
      get optionsLabel() { return this.tab === "video" ? "Качество видео" : "Формат аудио"; },
      get gliderClass() { return { "is-audio": this.tab === "audio" }; },
      get selectedOption() {
        return this.options.find((o) => o.id === this.selectedId) || null;
      },
      defaultOptionId(tab) {
        const list = this.options.filter((o) => o.kind === tab);
        if (!list.length) return null;
        const preferred = tab === "video"
          ? list.find((o) => o.quality === "best" && o.container === "mp4")
          : list.find((o) => o.container === "mp3");
        return (preferred || list[0]).id;
      },
      setTab(tab) {
        if (this.tab === tab) return;
        this.tab = tab;
        const current = this.selectedOption;
        if (!current || current.kind !== tab) this.selectedId = this.defaultOptionId(tab);
      },
      focusTab(tab) {
        this.setTab(tab);
        const el = document.getElementById(`tab-${tab}`);
        if (el) el.focus();
      },
      selectOption(id) { this.selectedId = id; },
      isSelected(opt) { return opt.id === this.selectedId; },
      optionClass(opt) { return { "is-selected": opt.id === this.selectedId }; },
      onOptionsKey(e) {
        const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
        if (e.key === "Enter") { e.preventDefault(); this.startDownload(); return; }
        if (!step) return;
        e.preventDefault();
        const list = this.visibleOptions;
        const idx = list.findIndex((o) => o.id === this.selectedId);
        const next = list[(idx + step + list.length) % list.length];
        if (!next) return;
        this.selectedId = next.id;
        this.$nextTick(() => {
          const el = document.querySelector(`[data-option="${next.id}"]`);
          if (el) el.focus();
        });
      },
      badgeFor(opt) {
        return { mp4: "MP4", mp3: "MP3", original: "Исходный" }[opt.container] || "";
      },
      sizeLabel(opt) {
        if (!opt.size) return "";
        return (opt.size_is_estimate ? "≈ " : "") + formatBytes(opt.size);
      },
      optionIcon(opt) {
        // "best" has no height in quality, but detail starts with it ("1080p · MP4 · ...")
        const h = parseInt(opt.quality === "best" ? opt.detail : opt.quality, 10);
        if (!h) return "HD";
        if (h >= 4320) return "8K";
        if (h >= 2160) return "4K";
        if (h >= 1440) return "2K";
        return h >= 720 ? "HD" : "SD";
      },
      optionIconClass(opt) {
        return {
          "app-icon--indigo": opt.kind === "video" && opt.quality === "best",
          "app-icon--blue": opt.kind === "video" && opt.quality !== "best",
          "app-icon--pink": opt.container === "mp3",
          "app-icon--orange": opt.kind === "audio" && opt.container !== "mp3",
        };
      },
      get dockTitle() {
        const o = this.selectedOption;
        if (!o) return "Ничего не выбрано";
        const badge = this.badgeFor(o);
        return badge ? `${o.label} · ${badge}` : o.label;
      },
      get dockSub() {
        const o = this.selectedOption;
        if (!o) return "";
        return this.sizeLabel(o) || "Размер неизвестен";
      },
      get selectedNote() {
        const o = this.selectedOption;
        if (!o) return "Выберите вариант";
        if (o.container === "mp3") return "Аудио будет преобразовано в MP3 через FFmpeg.";
        if (o.kind === "audio") return o.needs_ffmpeg ? "Звуковая дорожка будет извлечена без перекодирования." : "Аудио сохранится без преобразования.";
        if (o.container === "mp4" && o.needs_ffmpeg) return "Потоки будут собраны в MP4 без перекодирования.";
        if (o.needs_ffmpeg) return "Видео и аудио будут объединены без перекодирования.";
        return "Файл сохранится в исходном виде.";
      },
      get startDisabled() { return !this.selectedId || this.starting; },

      startDownload() {
        if (this.startDisabled || this.view !== "ready") return;
        this.starting = true;
        api("POST", "/api/jobs", { analysis_id: this.analysisId, option_id: this.selectedId }).then((data) => {
          this.starting = false;
          this.applyJob(data.job);
          this.view = "job";
          this.storeJob();
          this.openStream();
        }, (err) => {
          this.starting = false;
          if (err.code === "analysis_expired") {
            this.toast(err.message, "error");
            this.view = "idle";
            this.analyze();
            return;
          }
          if (err.code === "too_many_jobs" || err.code === "busy" || err.code === "rate_limited" || err.kind === "network") {
            this.toast(err.message, "error");
            return;
          }
          this.showError(err, "job");
        });
      },

      applyJob(job) {
        if (!job) return;
        const same = this.job && this.job.id === job.id;
        if (same && job.version < this.job.version) return;
        const prev = same ? this.job.status : null;
        this.job = job;
        this.jobId = job.id;
        if (prev !== job.status) this.onStatusChange(prev, job.status);
      },

      onStatusChange(prev, status) {
        if (status === "completed") {
          this.closeStream();
          if (prev) this.toast("Готово! Файл можно скачать.", "success");
        } else if (FAILED_STATUSES.has(status)) {
          this.closeStream();
          this.clearStoredJob();
          if (status === "failed" && prev) this.toast(this.job.error ? this.job.error.message : "Загрузка не удалась.", "error");
        }
      },

      openStream() {
        this.closeStream();
        if (!this.jobId) return;
        if (!window.EventSource) { this.scheduleReconnect(); return; }
        const es = new EventSource(jobPath(this.jobId, "/events"));
        this._es = es;
        es.addEventListener("open", () => {
          if (this.connection === "reconnecting" || this.connection === "lost") this.toast("Соединение восстановлено.", "success");
          this.connection = "open";
          this._reconnectAttempts = 0;
        });
        es.addEventListener("state", (e) => {
          try { this.applyJob(JSON.parse(e.data)); } catch (err) {}
        });
        es.addEventListener("end", () => { if (this._es === es) this.closeStream(); });
        es.addEventListener("error", () => {
          if (this._es !== es) return;
          // no built-in EventSource retry: fetch the status over http first, then back off
          es.close();
          this._es = null;
          if (this.jobDone) return;
          this.scheduleReconnect();
        });
      },

      scheduleReconnect() {
        this._reconnectAttempts += 1;
        this.connection = this._reconnectAttempts > 3 ? "lost" : "reconnecting";
        const delay = Math.min(15000, 800 * Math.pow(1.7, this._reconnectAttempts - 1));
        clearTimeout(this._reconnectTimer);
        this._reconnectTimer = setTimeout(() => {
          this._reconnectTimer = null;
          if (navigator.onLine === false) { this.scheduleReconnect(); return; }
          this.pollOnce().then((alive) => {
            if (alive && !this.jobDone && !this._es) this.openStream();
          });
        }, delay);
      },

      pollOnce() {
        if (!this.jobId) return Promise.resolve(false);
        return api("GET", jobPath(this.jobId)).then((data) => {
          this.applyJob(data.job);
          return true;
        }, (err) => {
          if (err.code === "not_found") {
            this.closeStream();
            this.clearStoredJob();
            this.showError(err, "job");
          } else {
            this.scheduleReconnect();
          }
          return false;
        });
      },

      closeStream() {
        if (this._es) { this._es.close(); this._es = null; }
        if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
        this.connection = "idle";
      },

      onOnline() {
        if (this.view === "job" && !this.jobDone && !this._es) {
          this._reconnectAttempts = 0;
          this.pollOnce().then((alive) => { if (alive && !this.jobDone && !this._es) this.openStream(); });
        }
      },
      onOffline() {
        this.toast("Нет подключения к интернету.", "error");
        if (this.view === "job" && !this.jobDone) this.connection = "lost";
      },

      cancelJob() {
        if (!this.jobId || this.cancelling) return;
        this.cancelling = true;
        api("POST", jobPath(this.jobId, "/cancel")).then((data) => {
          this.cancelling = false;
          this.applyJob(data.job);
          if (data.job.status === "cancelled") this.toast("Загрузка отменена.", "info");
        }, (err) => {
          this.cancelling = false;
          this.toast(err.message, "error");
        });
      },

      // restore the job after a tab reload
      storeJob() {
        try {
          sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
            id: this.jobId, media: this.media, url: this.url, analysisId: this.analysisId, options: this.options,
          }));
        } catch (e) {}
      },
      clearStoredJob() {
        try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
      },
      resumeJob() {
        let saved = null;
        try { saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null"); } catch (e) { saved = null; }
        if (!saved || typeof saved.id !== "string") return;
        api("GET", jobPath(saved.id)).then((data) => {
          this.media = saved.media || {};
          this.options = saved.options || [];
          this.analysisId = saved.analysisId || null;
          this.url = saved.url || "";
          this.applyJob(data.job);
          this.view = "job";
          if (!this.jobDone) this.openStream();
        }, () => this.clearStoredJob());
      },

      backToOptions() {
        this.closeStream();
        this.clearStoredJob();
        this.job = null; this.jobId = null;
        this.view = this.analysisId && this.options.length ? "ready" : "idle";
      },

      reset() {
        this.closeStream();
        this.clearStoredJob();
        this.view = "idle";
        this.url = ""; this.urlInvalid = false;
        this.media = {}; this.options = []; this.analysisId = null; this.selectedId = null;
        this.job = null; this.jobId = null; this.error = null; this.cancelling = false; this.starting = false;
        this._lastAnalyzedUrl = "";
        this.$nextTick(() => {
          window.scrollTo({ top: 0, behavior: "smooth" });
          this.$refs.url.focus({ preventScroll: true });
        });
      },

      get jobStatus() { return this.job ? this.job.status : ""; },
      get jobStage() { return this.job ? this.job.stage : ""; },
      get jobActive() { return ACTIVE_STATUSES.has(this.jobStatus); },
      get jobCompleted() { return this.jobStatus === "completed"; },
      get jobFailed() { return FAILED_STATUSES.has(this.jobStatus); },
      get jobDone() { return !!this.job && !this.jobActive; },
      get jobClass() {
        return { "is-active": this.jobActive, "is-done": this.jobCompleted, "is-failed": this.jobFailed,
                 "is-cancelled": this.jobStatus === "cancelled" };
      },
      get progress() { return (this.job && this.job.progress) || {}; },
      get hasPercent() { return this.jobActive && this.jobStage === "downloading" && this.progress.fraction != null; },
      get percentValue() { return this.hasPercent ? Math.floor(this.progress.fraction * 1000) / 10 : 0; },
      get percentText() { return this.hasPercent ? `${nf1.format(this.percentValue)}%` : ""; },
      get ariaPercent() { return this.hasPercent ? String(Math.round(this.percentValue)) : null; },
      get ringClass() {
        return {
          "is-spinning": this.jobActive && !this.hasPercent,
          "is-done": this.jobCompleted,
          "is-failed": this.jobFailed,
        };
      },
      get ringStyle() {
        let f = 0.25; // spinning arc
        if (this.jobCompleted) f = 1;
        else if (this.jobFailed) f = 0;
        else if (this.hasPercent) f = Math.max(0.01, this.progress.fraction);
        return { strokeDashoffset: String(RING_LENGTH * (1 - f)) };
      },
      get ringText() { return this.hasPercent ? `${Math.floor(this.percentValue)}%` : ""; },
      get stageTitle() {
        if (!this.job) return "";
        if (this.jobStatus === "failed" && this.job.error) return this.job.error.message;
        let title = STAGES[this.jobStage] || STAGES[this.jobStatus] || "Обработка";
        const p = this.progress;
        if (this.jobStage === "downloading" && p.parts > 1) {
          title += `${p.part === 1 ? " видео" : " аудио"} · поток ${p.part} из ${p.parts}`;
        }
        return title;
      },
      get stageSubtitle() {
        const j = this.job;
        if (!j) return "";
        const optionText = j.option ? `${j.option.label} · ${j.option.detail}` : "";
        switch (this.jobStatus) {
          case "queued": return "Начнётся, как только освободится слот.";
          case "processing": return "Обработка в FFmpeg";
          case "completed": return "";
          case "cancelled": return "";
          case "expired": return "Файл удалён с сервера. Скачайте медиа заново.";
          case "failed": return optionText;
        }
        const p = this.progress;
        const parts = [];
        if (p.downloaded) parts.push(formatBytes(p.downloaded) + (p.total ? ` из ${formatBytes(p.total)}` : ""));
        if (p.speed) parts.push(`${formatBytes(p.speed)}/с`);
        if (p.eta != null && this.hasPercent) parts.push(`осталось ${formatEta(p.eta)}`);
        return parts.length ? parts.join(" · ") : optionText;
      },
      get steps() {
        const o = this.job ? this.job.option : null;
        const list = [{ id: "queue", label: "Очередь", rank: 0 }, { id: "download", label: "Скачивание", rank: 1 }];
        if (o && (o.container !== "original" || this.hasFfmpeg)) list.push({ id: "process", label: "Обработка", rank: 2 });
        list.push({ id: "ready", label: "Готово", rank: 3 });
        return list;
      },
      stepClass(step) {
        const current = STAGE_ORDER[this.jobStage] ?? 0;
        return { "is-done": step.rank < current, "is-current": step.rank === current };
      },
      get connectionNote() {
        if (!this.jobActive) return "";
        if (this.connection === "reconnecting") return "Переподключаемся…";
        if (this.connection === "lost") return "Нет связи с сервером, загрузка продолжается.";
        return "";
      },
      get cancelLabel() { return this.cancelling ? "Отменяем…" : "Отменить загрузку"; },
      get canPickAnother() { return this.jobStatus !== "expired" && !!this.analysisId && this.options.length > 0; },

      get fileInfo() { return (this.job && this.job.file) || {}; },
      get fileName() { return this.fileInfo.name || ""; },
      get fileExt() { return (this.fileInfo.ext || "").toUpperCase(); },
      get fileUrl() { return this.fileInfo.url || "#"; },
      get fileIconClass() {
        const audio = !!this.job && !!this.job.option && this.job.option.kind === "audio";
        return { "app-icon--orange": audio, "app-icon--blue": !audio };
      },
      get fileMeta() {
        const f = this.fileInfo;
        const parts = [];
        if (f.size) parts.push(formatBytes(f.size));
        if (f.ext) parts.push(f.ext.toUpperCase());
        if (f.expires_in) parts.push(`ещё ${Math.max(1, Math.round(f.expires_in / 60))} мин`);
        return parts.join(" · ");
      },

      toast(text, type = "info") {
        const id = ++this._toastSeq;
        this.toasts.push({ id, text, type });
        if (this.toasts.length > MAX_TOASTS) this.toasts.shift();
        setTimeout(() => this.dismissToast(id), type === "error" ? 6500 : 4000);
      },
      dismissToast(id) {
        this.toasts = this.toasts.filter((t) => t.id !== id);
      },
      toastClass(t) { return `toast--${t.type}`; },
    };
  }

  document.addEventListener("alpine:init", () => {
    window.Alpine.data("downloader", component);
  });
})();
