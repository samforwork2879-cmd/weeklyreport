let markdownEditor = null;

function getMarkdownInputElement() {
  return document.getElementById("content-md-input");
}

function getMarkdownValue() {
  const textarea = getMarkdownInputElement();
  if (!textarea) {
    return "";
  }
  if (markdownEditor) {
    return markdownEditor.value();
  }
  return textarea.value;
}

function setMarkdownValue(value) {
  const textarea = getMarkdownInputElement();
  if (!textarea) {
    return;
  }
  if (markdownEditor) {
    markdownEditor.value(value || "");
    textarea.value = markdownEditor.value();
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  textarea.value = value || "";
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function loadStyleOnce(id, href) {
  if (document.getElementById(id)) {
    return;
  }
  const link = document.createElement("link");
  link.id = id;
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}

function loadScriptOnce(id, src, onReady) {
  const existing = document.getElementById(id);
  if (existing) {
    if (window.EasyMDE) {
      onReady();
    } else {
      existing.addEventListener("load", onReady, { once: true });
    }
    return;
  }
  const script = document.createElement("script");
  script.id = id;
  script.src = src;
  script.defer = true;
  script.addEventListener("load", onReady, { once: true });
  document.head.appendChild(script);
}

function initRichMarkdownEditor() {
  const textarea = getMarkdownInputElement();
  if (!textarea || markdownEditor) {
    return;
  }

  const boot = () => {
    if (!window.EasyMDE || markdownEditor) {
      return;
    }
    markdownEditor = new window.EasyMDE({
      element: textarea,
      spellChecker: false,
      autoDownloadFontAwesome: false,
      status: ["lines", "words", "cursor"],
      sideBySideFullscreen: false,
      minHeight: "340px",
      previewClass: ["markdown-body"],
      placeholder: textarea.placeholder || "請輸入 Markdown 內容",
      toolbar: [
        "bold",
        "italic",
        "strikethrough",
        "|",
        "heading",
        "quote",
        "unordered-list",
        "ordered-list",
        "checked-list",
        "|",
        "link",
        "image",
        "table",
        "code",
        "horizontal-rule",
        "|",
        "fullscreen"
      ],
    });

    markdownEditor.codemirror.on("change", () => {
      textarea.value = markdownEditor.value();
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    document.dispatchEvent(new CustomEvent("markdown:ready"));
  };

  loadStyleOnce(
    "easymde-css",
    "https://cdn.jsdelivr.net/npm/easymde/dist/easymde.min.css"
  );
  loadStyleOnce(
    "font-awesome-v4-css",
    "https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/css/font-awesome.min.css"
  );
  loadScriptOnce(
    "easymde-js",
    "https://cdn.jsdelivr.net/npm/easymde/dist/easymde.min.js",
    boot
  );
}

function initMarkdownLayout() {
  const shell = document.querySelector(".editor-shell");
  if (!shell) {
    return;
  }
  const buttons = Array.from(shell.querySelectorAll(".editor-tab[data-mode]"));
  const textarea = getMarkdownInputElement();
  const storageKey = "weekly_report_editor_mode";

  function setMode(mode) {
    const resolved = mode || "both";
    shell.classList.add("is-switching");
    requestAnimationFrame(() => {
      shell.dataset.mode = resolved;
      buttons.forEach((btn) => {
        btn.classList.toggle("is-active", btn.dataset.mode === resolved);
      });
      try {
        localStorage.setItem(storageKey, resolved);
      } catch (_err) {
        // ignore storage errors
      }
      if (resolved !== "edit" && textarea) {
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      }
      if (markdownEditor && markdownEditor.codemirror) {
        requestAnimationFrame(() => {
          markdownEditor.codemirror.refresh();
        });
      }
    });
    window.setTimeout(() => {
      shell.classList.remove("is-switching");
    }, 420);
  }

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  let initialMode = "both";
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved) {
      initialMode = saved;
    }
  } catch (_err) {
    // ignore storage errors
  }
  setMode(initialMode);
}

function initThemeToggle() {
  const button = document.getElementById("theme-toggle");
  const body = document.body;
  if (!button || !body) {
    return;
  }

  const storageKey = "weekly_report_theme";

  function applyTheme(mode) {
    if (mode === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
      body.dataset.theme = "dark";
      button.textContent = "淺色模式";
      button.setAttribute("aria-pressed", "true");
    } else {
      document.documentElement.setAttribute("data-theme", "light");
      body.dataset.theme = "light";
      button.textContent = "深色模式";
      button.setAttribute("aria-pressed", "false");
    }
  }

  function resolveInitialTheme() {
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved === "dark" || saved === "light") {
        return saved;
      }
    } catch (_err) {
      // ignore storage errors
    }
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function persist(mode) {
    try {
      localStorage.setItem(storageKey, mode);
    } catch (_err) {
      // ignore storage errors
    }
  }

  let current = resolveInitialTheme();
  applyTheme(current);

  button.addEventListener("click", () => {
    current = current === "dark" ? "light" : "dark";
    applyTheme(current);
    persist(current);
  });
}

function initCalendar() {
  const wrap = document.querySelector(".calendar-wrap");
  if (!wrap) {
    return;
  }

  const reports = JSON.parse(wrap.dataset.reports || "[]");
  const reportMap = new Map();
  reports.forEach((item) => {
    const key = item.week_start;
    if (!reportMap.has(key)) {
      reportMap.set(key, []);
    }
    reportMap.get(key).push(item);
  });

  const titleEl = document.getElementById("calendar-title");
  const gridEl = document.getElementById("calendar-grid");
  const prevBtn = document.getElementById("prev-month");
  const nextBtn = document.getElementById("next-month");
  let cursor = new Date();

  const dayNames = ["日", "一", "二", "三", "四", "五", "六"];

  function render() {
    const year = cursor.getFullYear();
    const month = cursor.getMonth();
    titleEl.textContent = `${year} 年 ${month + 1} 月`;
    gridEl.innerHTML = "";

    dayNames.forEach((d) => {
      const dayHeader = document.createElement("div");
      dayHeader.className = "calendar-cell";
      dayHeader.innerHTML = `<strong>${d}</strong>`;
      gridEl.appendChild(dayHeader);
    });

    const first = new Date(year, month, 1);
    const startWeekday = first.getDay();
    const totalDays = new Date(year, month + 1, 0).getDate();
    const prevMonthDays = new Date(year, month, 0).getDate();

    for (let i = 0; i < startWeekday; i++) {
      const day = prevMonthDays - startWeekday + i + 1;
      gridEl.appendChild(buildCell(new Date(year, month - 1, day), true));
    }

    for (let d = 1; d <= totalDays; d++) {
      gridEl.appendChild(buildCell(new Date(year, month, d), false));
    }

    const cellCount = gridEl.children.length - 7;
    const remainder = cellCount % 7;
    if (remainder > 0) {
      const needed = 7 - remainder;
      for (let i = 1; i <= needed; i++) {
        gridEl.appendChild(buildCell(new Date(year, month + 1, i), true));
      }
    }
  }

  function buildCell(date, otherMonth) {
    const el = document.createElement("div");
    el.className = `calendar-cell${otherMonth ? " other" : ""}`;
    const iso = date.toISOString().slice(0, 10);
    el.innerHTML = `<div class="cell-date">${date.getDate()}</div>`;

    const matches = reportMap.get(iso) || [];
    matches.forEach((report) => {
      const item = document.createElement("a");
      item.className = "cell-item";
      item.href = `/report/${report.id}`;
      item.textContent = report.title;
      el.appendChild(item);
    });

    return el;
  }

  prevBtn.addEventListener("click", () => {
    cursor = new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1);
    render();
  });

  nextBtn.addEventListener("click", () => {
    cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
    render();
  });

  render();
}

function initMarkdownPreview() {
  const form = document.querySelector(".report-form[data-preview-url]");
  if (!form) {
    return;
  }

  const textarea = document.getElementById("content-md-input");
  const preview = document.getElementById("markdown-preview");
  if (!textarea || !preview) {
    return;
  }

  const previewUrl = form.dataset.previewUrl;
  let timer = null;
  let currentController = null;

  function showEmptyText() {
    preview.classList.add("is-empty");
    preview.textContent = "尚未輸入內容，這裡會顯示 Markdown 預覽。";
  }

  function renderPreview() {
    const markdownText = getMarkdownValue().trim();
    if (!markdownText) {
      showEmptyText();
      return;
    }

    if (currentController) {
      currentController.abort();
    }
    currentController = new AbortController();
    const body = new URLSearchParams({ content_md: getMarkdownValue() });

    fetch(previewUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
      signal: currentController.signal,
    })
      .then((resp) => {
        if (!resp.ok) {
          throw new Error("預覽失敗");
        }
        return resp.json();
      })
      .then((data) => {
        preview.classList.remove("is-empty");
        preview.innerHTML = data.html || "";
      })
      .catch((err) => {
        if (err.name === "AbortError") {
          return;
        }
        preview.classList.add("is-empty");
        preview.textContent = "Markdown 預覽發生錯誤。";
      });
  }

  textarea.addEventListener("input", () => {
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(renderPreview, 200);
  });

  renderPreview();

  function attachScrollSync() {
    if (!markdownEditor || !markdownEditor.codemirror) {
      return;
    }
    const editor = markdownEditor.codemirror;
    let lockEditor = false;
    let lockPreview = false;

    editor.on("scroll", () => {
      if (lockPreview) {
        return;
      }
      const info = editor.getScrollInfo();
      const ratio = info.top / Math.max(1, info.height - info.clientHeight);
      lockEditor = true;
      preview.scrollTop = ratio * (preview.scrollHeight - preview.clientHeight);
      requestAnimationFrame(() => {
        lockEditor = false;
      });
    });

    preview.addEventListener("scroll", () => {
      if (lockEditor) {
        return;
      }
      const ratio = preview.scrollTop / Math.max(1, preview.scrollHeight - preview.clientHeight);
      const info = editor.getScrollInfo();
      lockPreview = true;
      editor.scrollTo(null, ratio * (info.height - info.clientHeight));
      requestAnimationFrame(() => {
        lockPreview = false;
      });
    });
  }

  if (markdownEditor) {
    attachScrollSync();
  } else {
    document.addEventListener("markdown:ready", attachScrollSync, { once: true });
  }
}

function initTemplateApplier() {
  const selectEl = document.getElementById("template-select");
  const applyBtn = document.getElementById("apply-template-btn");
  const textarea = document.getElementById("content-md-input");
  const rawData = document.getElementById("report-templates-data");

  if (!selectEl || !applyBtn || !textarea || !rawData) {
    return;
  }

  let templates = [];
  try {
    templates = JSON.parse(rawData.textContent || "[]");
  } catch (_err) {
    templates = [];
  }

  const templateMap = new Map();
  templates.forEach((tpl) => {
    templateMap.set(String(tpl.id), tpl.content_md || "");
  });

  applyBtn.addEventListener("click", () => {
    const selectedId = selectEl.value;
    if (!selectedId) {
      return;
    }
    setMarkdownValue(templateMap.get(selectedId) || "");
  });

  if (selectEl.value && !getMarkdownValue().trim()) {
    setMarkdownValue(templateMap.get(selectEl.value) || "");
  }
}

function initDraftAutosave() {
  const form = document.querySelector(".report-form[data-draft-save-url]");
  if (!form) {
    return;
  }

  const saveUrl = form.dataset.draftSaveUrl;
  const clearUrl = form.dataset.draftClearUrl;
  const reportId = form.dataset.reportId || "new";
  const titleEl = document.getElementById("field-title");
  const weekStartEl = document.getElementById("field-week-start");
  const tagsHiddenEl = document.getElementById("field-tags");
  const selectedTagsWrapEl = document.getElementById("field-selected-tags");
  const customTagsEl = document.getElementById("field-custom-tags");
  const contentEl = document.getElementById("content-md-input");
  const clearBtn = document.getElementById("clear-draft-btn");
  const meta = document.querySelector(".draft-meta");
  const storageKey = `weekly_report_draft_${reportId}`;

  if (!titleEl || !weekStartEl || !contentEl) {
    return;
  }

  let timer = null;

  function parseTags(raw) {
    return String(raw || "")
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item);
  }

  function normalizeTagList(items) {
    const result = [];
    const seen = new Set();
    items.forEach((item) => {
      const tag = item.trim();
      if (!tag) {
        return;
      }
      const key = tag.toLowerCase();
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      result.push(tag);
    });
    return result;
  }

  function selectedTagsFromUI() {
    if (!selectedTagsWrapEl) {
      return [];
    }
    return Array.from(selectedTagsWrapEl.querySelectorAll("input[name='selected_tags']:checked"))
      .map((el) => el.value.trim())
      .filter((v) => v);
  }

  function composeTagsFromUI() {
    const selected = selectedTagsFromUI();
    const custom = customTagsEl ? parseTags(customTagsEl.value) : [];
    return normalizeTagList([...selected, ...custom]).join(", ");
  }

  function syncHiddenTags() {
    if (tagsHiddenEl) {
      tagsHiddenEl.value = composeTagsFromUI();
    }
  }

  function applyTagTextToUI(tagText) {
    const tags = normalizeTagList(parseTags(tagText));

    if (selectedTagsWrapEl) {
      const checkboxes = Array.from(selectedTagsWrapEl.querySelectorAll("input[name='selected_tags']"));
      const checkboxValues = checkboxes.map((el) => el.value.toLowerCase());
      checkboxes.forEach((el) => {
        const matched = tags.some((tag) => tag.toLowerCase() === el.value.toLowerCase());
        el.checked = matched;
      });
      const custom = tags.filter((tag) => !checkboxValues.includes(tag.toLowerCase()));
      if (customTagsEl) {
        customTagsEl.value = custom.join(", ");
      }
    } else if (customTagsEl) {
      customTagsEl.value = tags.join(", ");
    }

    syncHiddenTags();
  }

  function currentPayload() {
    syncHiddenTags();
    return {
      title: titleEl.value,
      week_start: weekStartEl.value,
      tags: tagsHiddenEl ? tagsHiddenEl.value : composeTagsFromUI(),
      content_md: getMarkdownValue(),
    };
  }

  function applyPayload(data) {
    titleEl.value = data.title || "";
    weekStartEl.value = data.week_start || "";
    applyTagTextToUI(data.tags || "");
    setMarkdownValue(data.content_md || "");
  }

  function isFormEmpty() {
    return !titleEl.value && !weekStartEl.value && !composeTagsFromUI() && !getMarkdownValue();
  }

  const localRaw = localStorage.getItem(storageKey);
  if (localRaw && isFormEmpty()) {
    try {
      applyPayload(JSON.parse(localRaw));
      if (meta) {
        meta.textContent = "已從瀏覽器恢復草稿";
      }
    } catch (_err) {
      localStorage.removeItem(storageKey);
    }
  }

  function persistDraft() {
    const payload = currentPayload();
    localStorage.setItem(storageKey, JSON.stringify(payload));
    const body = new URLSearchParams(payload);

    fetch(saveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    })
      .then((resp) => {
        if (!resp.ok) {
          throw new Error("save failed");
        }
        return resp.json();
      })
      .then((data) => {
        if (meta && data.updated_at) {
          meta.textContent = `草稿已儲存：${data.updated_at}`;
        }
      })
      .catch(() => {
        if (meta) {
          meta.textContent = "草稿儲存失敗（已保留在瀏覽器）";
        }
      });
  }

  function schedulePersist() {
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(persistDraft, 500);
  }

  ["input", "change"].forEach((evtName) => {
    [titleEl, weekStartEl, contentEl].forEach((el) => {
      el.addEventListener(evtName, schedulePersist);
    });
  });

  if (selectedTagsWrapEl) {
    Array.from(selectedTagsWrapEl.querySelectorAll("input[name='selected_tags']")).forEach((el) => {
      el.addEventListener("change", () => {
        syncHiddenTags();
        schedulePersist();
      });
    });
  }

  if (customTagsEl) {
    customTagsEl.addEventListener("input", () => {
      syncHiddenTags();
      schedulePersist();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      titleEl.value = "";
      weekStartEl.value = "";
      if (selectedTagsWrapEl) {
        Array.from(selectedTagsWrapEl.querySelectorAll("input[name='selected_tags']")).forEach((el) => {
          el.checked = false;
        });
      }
      if (customTagsEl) {
        customTagsEl.value = "";
      }
      syncHiddenTags();
      setMarkdownValue("");
      localStorage.removeItem(storageKey);
      fetch(clearUrl, { method: "POST" }).catch(() => {});
      if (meta) {
        meta.textContent = "草稿已清除";
      }
    });
  }

  syncHiddenTags();
}

initRichMarkdownEditor();
initMarkdownLayout();
initThemeToggle();
initCalendar();
initMarkdownPreview();
initTemplateApplier();
initDraftAutosave();
