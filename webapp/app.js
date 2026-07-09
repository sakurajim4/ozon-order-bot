const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const initData = tg.initData || "";

const state = {
  shops: [],   // [{shop_key, shop_name, postings: [{pn, name, qty, photo_url}]}]
  selected: new Set(),
  filterText: "",
};

const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const counterEl = document.getElementById("counter");
const filterEl = document.getElementById("filter");

function applyTheme() {
  const p = tg.themeParams || {};
  const root = document.documentElement.style;
  if (p.bg_color) root.setProperty("--bg", p.bg_color);
  if (p.text_color) root.setProperty("--text", p.text_color);
  if (p.hint_color) root.setProperty("--hint", p.hint_color);
  if (p.link_color) root.setProperty("--link", p.link_color);
  if (p.button_color) root.setProperty("--accent", p.button_color);
  if (p.secondary_bg_color) root.setProperty("--card", p.secondary_bg_color);
}
applyTheme();
if (tg.onEvent) tg.onEvent("themeChanged", applyTheme);

function matchesFilter(posting, text) {
  if (!text) return true;
  const t = text.toLowerCase();
  return posting.pn.toLowerCase().includes(t) || posting.name.toLowerCase().includes(t);
}

function visiblePostingsFlat() {
  const result = [];
  for (const shop of state.shops) {
    for (const p of shop.postings) {
      if (matchesFilter(p, state.filterText)) result.push(p);
    }
  }
  return result;
}

function toggle(pn) {
  if (state.selected.has(pn)) state.selected.delete(pn);
  else state.selected.add(pn);
  render();
}

function updateCounterAndButton() {
  const n = state.selected.size;
  counterEl.textContent = `Выбрано: ${n}`;
  if (n > 0) {
    tg.MainButton.setText(`Собрать PDF (${n})`);
    tg.MainButton.show();
    tg.MainButton.enable();
  } else {
    tg.MainButton.hide();
  }
}

function render() {
  listEl.innerHTML = "";
  let anyPostings = false;

  for (const shop of state.shops) {
    const visiblePostings = shop.postings.filter(p => matchesFilter(p, state.filterText));
    if (shop.postings.length) anyPostings = true;
    if (!visiblePostings.length) continue;

    const section = document.createElement("div");
    section.className = "shop-section";

    const header = document.createElement("div");
    header.className = "shop-header";
    header.textContent = `${shop.shop_name} (${visiblePostings.length})`;
    section.appendChild(header);

    for (const posting of visiblePostings) {
      const row = document.createElement("div");
      row.className = "posting-row" + (state.selected.has(posting.pn) ? " selected" : "");

      const thumb = document.createElement("div");
      thumb.className = "thumb";
      if (posting.photo_url) {
        const img = document.createElement("img");
        img.src = posting.photo_url;
        img.loading = "lazy";
        img.alt = "";
        thumb.appendChild(img);
      } else {
        thumb.textContent = "—";
      }
      row.appendChild(thumb);

      const info = document.createElement("div");
      info.className = "info";
      const nameEl = document.createElement("div");
      nameEl.className = "name";
      nameEl.textContent = posting.name + (posting.qty > 1 ? ` ×${posting.qty}` : "");
      const pnEl = document.createElement("div");
      pnEl.className = "pn";
      pnEl.textContent = posting.pn;
      info.appendChild(nameEl);
      info.appendChild(pnEl);
      row.appendChild(info);

      const check = document.createElement("div");
      check.className = "check";
      check.textContent = state.selected.has(posting.pn) ? "✅" : "⬜";
      row.appendChild(check);

      row.addEventListener("click", () => toggle(posting.pn));
      section.appendChild(row);
    }
    listEl.appendChild(section);
  }

  emptyEl.style.display = anyPostings ? "none" : "block";
  updateCounterAndButton();
}

document.getElementById("select-all").addEventListener("click", () => {
  for (const p of visiblePostingsFlat()) state.selected.add(p.pn);
  render();
});
document.getElementById("select-none").addEventListener("click", () => {
  for (const p of visiblePostingsFlat()) state.selected.delete(p.pn);
  render();
});
filterEl.addEventListener("input", (e) => {
  state.filterText = e.target.value;
  render();
});

async function loadPending() {
  try {
    const resp = await fetch("/api/pending", {
      headers: { "Authorization": `tma ${initData}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.shops = data.shops || [];
    render();
  } catch (e) {
    listEl.innerHTML = `<div class="error">Не удалось загрузить список: ${e.message}</div>`;
  }
}

tg.MainButton.onClick(async () => {
  const pns = Array.from(state.selected);
  if (!pns.length) return;
  tg.MainButton.showProgress(true);
  tg.MainButton.disable();
  try {
    const resp = await fetch("/api/picking-list", {
      method: "POST",
      headers: {
        "Authorization": `tma ${initData}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ posting_numbers: pns }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || `HTTP ${resp.status}`);
    }
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    if (tg.showPopup) {
      tg.showPopup({ message: "PDF отправлен в чат ✅" }, () => tg.close());
    } else {
      tg.close();
    }
  } catch (e) {
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("error");
    alert("Ошибка: " + e.message);
  } finally {
    tg.MainButton.hideProgress();
    tg.MainButton.enable();
  }
});

loadPending();
