
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand(); tg.setHeaderColor("secondary_bg_color");

const API_URL = "https://cash-512.onrender.com";   // ← ЗАМЕНИ
const BOT_USERNAME = "@free_stars_infbot";           // ← ЗАМЕНИ

const initData = tg.initData;
let currentUser = null;
let currentGame = null;
let ws = null;
let canvas, ctx;
let spinAnimation = null;

async function api(path, options = {}) {
    const resp = await fetch(API_URL + path, {
        ...options,
        headers: { "X-Init-Data": initData, "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Ошибка" }));
        throw new Error(err.detail || "Ошибка");
    }
    return resp.json();
}

// ========== Профиль ==========
async function loadMe() {
    try {
        const d = await api("/api/me");
        currentUser = d;
        document.getElementById("balanceValue").textContent = d.balance;
        document.getElementById("pUsername").textContent = d.username ? "@" + d.username : d.first_name;
        document.getElementById("pUserId").textContent = d.user_id;
        document.getElementById("pBalance").textContent = d.balance + " ⭐";
        document.getElementById("pTopup").textContent = d.total_topup + " ⭐";
        document.getElementById("pWon").textContent = d.total_won + " ⭐";
        document.getElementById("pRefCount").textContent = d.ref_count;
        document.getElementById("pRefEarned").textContent = d.ref_earned + " ⭐";
        document.getElementById("refLink").textContent = getRefLink();
        if (d.is_admin) {
            document.getElementById("adminTabBtn").classList.remove("hidden");
        }
    } catch (e) { console.error(e); }
}

function getRefLink() {
    return `https://t.me/${BOT_USERNAME}?start=ref_${currentUser?.user_id || ""}`;
}
function copyRef() { navigator.clipboard.writeText(getRefLink()); tg.showAlert("Скопировано!"); }
function shareRef() {
    const link = getRefLink();
    const text = encodeURIComponent("🎰 Заходи в казино!");
    tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(link)}&text=${text}`);
}

async function claimDaily() {
    try {
        const res = await api("/api/daily", { method: "POST" });
        if (res.ok) { tg.showAlert(`🎁 +${res.amount}⭐`); loadMe(); }
        else tg.showAlert(res.msg || "Недоступно");
    } catch (e) { tg.showAlert(e.message); }
}

// ========== История и топ ==========
async function loadHistory() {
    try {
        const rounds = await api("/api/history");
        const el = document.getElementById("history");
        el.innerHTML = rounds.length ? rounds.map(r => `
            <div class="history-item">
                <span>${r.game === "wheel" ? "🎡" : "🟦"} @${r.winner_name || r.winner_id}</span>
                <span class="win">+${r.payout}⭐</span>
            </div>
        `).join("") : '<div class="muted">Пока нет игр</div>';
    } catch (e) { console.error(e); }
}

async function loadTop() {
    try {
        const top = await api("/api/top");
        const el = document.getElementById("topList");
        el.innerHTML = top.length ? top.map((u, i) => `
            <div class="top-row">
                <span class="rank">#${i + 1}</span>
                <span class="name">@${u.username || u.first_name || u.user_id}</span>
                <span class="won">${u.total_won}⭐</span>
            </div>
        `).join("") : '<div class="muted">Пусто</div>';
    } catch (e) { console.error(e); }
}

// ========== Навигация ==========
document.querySelectorAll(".nav-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.querySelectorAll(".tab").forEach(t => t.classList.add("hidden"));
        document.getElementById("tab-" + btn.dataset.tab).classList.remove("hidden");
        if (btn.dataset.tab === "games") { loadHistory(); loadTop(); }
        if (btn.dataset.tab === "profile") loadMe();
        if (btn.dataset.tab === "admin") loadAdmin();
    });
});
document.querySelectorAll(".game-btn").forEach(btn => btn.addEventListener("click", () => openGame(btn.dataset.game)));

function openGame(game) {
    currentGame = game;
    document.querySelectorAll(".tab").forEach(t => t.classList.add("hidden"));
    document.getElementById("tab-play").classList.remove("hidden");
    document.getElementById("gameTitle").textContent = game === "wheel" ? "🎡 Колесо" : "🟦 Квадрат";
    document.getElementById("betError").textContent = "";
    document.getElementById("topWinner").classList.add("hidden");
    initCanvas();
    connectWS();
}

function backToGames() {
    currentGame = null;
    if (spinAnimation) cancelAnimationFrame(spinAnimation);
    spinAnimation = null;
    document.querySelectorAll(".tab").forEach(t => t.classList.add("hidden"));
    document.getElementById("tab-games").classList.remove("hidden");
    loadHistory(); loadTop();
}

// ========== Canvas (то же, что раньше) ==========
function initCanvas() {
    canvas = document.getElementById("gameCanvas");
    ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#232e3c"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#7d8b99"; ctx.font = "16px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("Ожидание игроков…", canvas.width / 2, canvas.height / 2);
}

function drawWheel(players, rotation, winnerIndex, highlight) {
    const cx = canvas.width/2, cy = canvas.height/2, radius = Math.min(cx, cy) - 10;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const total = players.reduce((s, p) => s + p.stake, 0);
    if (!total) return;
    let start = rotation;
    players.forEach((p, i) => {
        const slice = (p.stake / total) * Math.PI * 2;
        ctx.beginPath(); ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, radius, start, start + slice); ctx.closePath();
        ctx.fillStyle = p.color; ctx.fill();
        if (highlight && i === winnerIndex) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 4; ctx.stroke(); }
        const mid = start + slice / 2;
        const tx = cx + Math.cos(mid) * radius * 0.65;
        const ty = cy + Math.sin(mid) * radius * 0.65;
        ctx.save(); ctx.translate(tx, ty); ctx.rotate(mid + Math.PI/2);
        ctx.fillStyle = "#000"; ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center";
        const label = p.username ? "@" + p.username : p.first_name;
        ctx.fillText(label.length > 10 ? label.slice(0,10)+"…" : label, 0, 0);
        ctx.fillText(p.stake + "⭐", 0, 14);
        ctx.restore();
        start += slice;
    });
    ctx.beginPath();
    ctx.moveTo(cx, cy - radius - 8);
    ctx.lineTo(cx - 10, cy - radius + 12);
    ctx.lineTo(cx + 10, cy - radius + 12);
    ctx.closePath(); ctx.fillStyle = "#fff"; ctx.fill();
}

function drawSquare(players, dotPos, winnerIndex) {
    const pad = 20, W = canvas.width - pad*2, H = canvas.height - pad*2;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const total = players.reduce((s, p) => s + p.stake, 0);
    if (!total) return;
    const perRow = Math.ceil(Math.sqrt(players.length));
    const cellW = W / perRow, cellH = H / Math.ceil(players.length / perRow);
    players.forEach((p, i) => {
        const col = i % perRow, row = Math.floor(i / perRow);
        const cx = pad + col * cellW, cy = pad + row * cellH;
        ctx.fillStyle = p.color; ctx.fillRect(cx, cy, cellW - 2, cellH - 2);
        if (winnerIndex !== null && i === winnerIndex) {
            ctx.strokeStyle = "#fff"; ctx.lineWidth = 4;
            ctx.strokeRect(cx, cy, cellW - 2, cellH - 2);
        }
        ctx.fillStyle = "#000"; ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center";
        const label = p.username ? "@" + p.username : p.first_name;
        ctx.fillText(label.length > 10 ? label.slice(0,10)+"…" : label, cx + cellW/2, cy + cellH/2 - 6);
        ctx.fillText(p.stake + "⭐", cx + cellW/2, cy + cellH/2 + 12);
    });
    if (dotPos) {
        ctx.beginPath(); ctx.arc(dotPos.x, dotPos.y, 8, 0, Math.PI*2);
        ctx.fillStyle = "#fff"; ctx.fill();
        ctx.strokeStyle = "#000"; ctx.lineWidth = 2; ctx.stroke();
    }
}

// ========== WebSocket ==========
function connectWS() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    const url = API_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws/" + currentUser.user_id;
    ws = new WebSocket(url);
    ws.onmessage = (ev) => handleWSEvent(JSON.parse(ev.data));
    ws.onerror = (e) => console.error("WS error", e);
    ws.onclose = () => { setTimeout(() => { if (currentGame) connectWS(); }, 2000); };
}

function handleWSEvent({ event, data }) {
    if (!currentGame) return;
    if (data.game && data.game !== currentGame) return;
    if (event === "state") renderState(data);
    else if (event === "spin") startSpin(data);
    else if (event === "result") showResult(data);
    else if (event === "refunded") { tg.showAlert("Ставки возвращены"); loadMe(); }
}

function renderState(s) {
    document.getElementById("pool").textContent = s.pool;
    document.getElementById("timer").textContent = s.time_left > 0 ? s.time_left : "—";
    const el = document.getElementById("players");
    el.innerHTML = s.players.length ? s.players.map(p => `
        <div class="player-row" style="border-left-color: ${p.color};">
            <div class="name">${p.username ? "@" + p.username : p.first_name}</div>
            <div><span class="stake">${p.stake}⭐</span><span class="share">${p.share}%</span></div>
        </div>
    `).join("") : '<div class="muted">Пока никого. Сделай первую ставку!</div>';
    if (currentGame === "wheel") drawWheel(s.players, 0, null, false);
    else drawSquare(s.players, null, null);
}

function startSpin(data) {
    const duration = (data.duration || 6) * 1000;
    const startTime = performance.now();
    const players = data.players;
    const total = players.reduce((s, p) => s + p.stake, 0);
    const winnerIdx = data.winner_index;
    let acc = 0;
    for (let i = 0; i < winnerIdx; i++) acc += players[i].stake;
    const winMid = ((acc + players[winnerIdx].stake / 2) / total) * Math.PI * 2;

    const animate = (now) => {
        const t = Math.min(1, (now - startTime) / duration);
        const ease = 1 - Math.pow(1 - t, 3);
        if (currentGame === "wheel") {
            const target = -winMid - Math.PI / 2;
            const rot = target + (1 - ease) * Math.PI * 12;
            drawWheel(players, rot, null, false);
        } else {
            const pad = 20, W = canvas.width - pad*2, H = canvas.height - pad*2;
            const perRow = Math.ceil(Math.sqrt(players.length));
            const cellW = W / perRow, cellH = H / Math.ceil(players.length / perRow);
            const col = winnerIdx % perRow, row = Math.floor(winnerIdx / perRow);
            const tx = pad + col*cellW + cellW/2, ty = pad + row*cellH + cellH/2;
            const sx = Math.random() * canvas.width, sy = Math.random() * canvas.height;
            const x = sx + (tx - sx) * ease + Math.sin(t*20) * (1-t) * 40;
            const y = sy + (ty - sy) * ease + Math.cos(t*20) * (1-t) * 40;
            drawSquare(players, { x, y }, null);
        }
        if (t < 1) spinAnimation = requestAnimationFrame(animate);
        else {
            if (currentGame === "wheel") {
                const target = -winMid - Math.PI / 2;
                drawWheel(players, target, winnerIdx, true);
            } else {
                const pad = 20, W = canvas.width - pad*2, H = canvas.height - pad*2;
                const perRow = Math.ceil(Math.sqrt(players.length));
                const cellW = W / perRow, cellH = H / Math.ceil(players.length / perRow);
                const col = winnerIdx % perRow, row = Math.floor(winnerIdx / perRow);
                const tx = pad + col*cellW + cellW/2, ty = pad + row*cellH + cellH/2;
                drawSquare(players, { x: tx, y: ty }, winnerIdx);
            }
            spinAnimation = null;
        }
    };
    if (spinAnimation) cancelAnimationFrame(spinAnimation);
    spinAnimation = requestAnimationFrame(animate);
}

function showResult(d) {
    const el = document.getElementById("topWinner");
    el.classList.remove("hidden");
    el.innerHTML = `🏆 Победитель<div class="who">@${d.winner_name}</div><div class="sum">+${d.payout}⭐</div>`;
    loadMe();
    setTimeout(() => loadHistory(), 1500);
}

// ========== Ставка ==========
async function placeBet() {
    const amount = parseInt(document.getElementById("betAmount").value, 10);
    const err = document.getElementById("betError");
    err.textContent = "";
    if (!amount || amount < 10) { err.textContent = "Минимум 10⭐"; return; }
    try {
        await api(`/api/game/${currentGame}/bet`, { method: "POST", body: JSON.stringify({ amount }) });
        loadMe();
    } catch (e) { err.textContent = e.message; }
}

// ========== Пополнение ==========
const modal = document.getElementById("topupModal");
document.getElementById("balanceBtn").addEventListener("click", () => modal.classList.remove("hidden"));
function closeTopup() { modal.classList.add("hidden"); }
document.querySelectorAll(".quick-btns button").forEach(b => {
    b.addEventListener("click", () => document.getElementById("customAmount").value = b.dataset.amount);
});
async function submitTopup() {
    const amount = parseInt(document.getElementById("customAmount").value, 10);
    if (!amount || amount < 50) { tg.showAlert("Минимум 50⭐"); return; }
    try {
        const r = await api("/api/topup", { method: "POST", body: JSON.stringify({ amount }) });
        tg.showAlert(r.message); modal.classList.add("hidden");
    } catch (e) { tg.showAlert(e.message); }
}

// ========== Вывод ==========
async function submitWithdraw() {
    const amount = parseInt(document.getElementById("wAmount").value, 10);
    const req = document.getElementById("wRequisites").value.trim();
    try {
        const r = await api("/api/withdraw", { method: "POST", body: JSON.stringify({ amount, requisites: req }) });
        tg.showAlert(`✅ Заявка №${r.withdrawal_id} отправлена`);
        document.getElementById("wAmount").value = "";
        document.getElementById("wRequisites").value = "";
        loadMe();
    } catch (e) { tg.showAlert(e.message); }
}

// ========== Админка ==========
async function loadAdmin() {
    try {
        const s = await api("/api/admin/stats");
        document.getElementById("adminStats").innerHTML = `
            <div class="row"><span>Юзеров:</span><b>${s.users}</b></div>
            <div class="row"><span>Общий баланс:</span><b>${s.total_balance}⭐</b></div>
            <div class="row"><span>Пополнено:</span><b>${s.total_topup}⭐</b></div>
            <div class="row"><span>Раундов:</span><b>${s.rounds}</b></div>
        `;
        const wds = await api("/api/admin/withdrawals");
        document.getElementById("adminWithdrawals").innerHTML = wds.length ? wds.map(w => `
            <div class="card">
                <div class="row"><span>№${w.id} @${w.user_id}</span><b>${w.amount}⭐</b></div>
                <div class="muted">${w.requisites}</div>
                <button class="btn-primary" onclick="adminWdDone(${w.id})" style="margin-top:8px;">✅ Выполнено</button>
            </div>
        `).join("") : '<div class="muted">Заявок нет</div>';
    } catch (e) { console.error(e); }
}

async function adminWdDone(wid) {
    try {
        await api(`/api/admin/withdrawal/${wid}/done`, { method: "POST" });
        tg.showAlert("Готово");
        loadAdmin();
    } catch (e) { tg.showAlert(e.message); }
}

async function adminSetBalance() {
    const uid = parseInt(document.getElementById("adminUserId").value, 10);
    const amount = parseInt(document.getElementById("adminAmount").value, 10);
    if (!uid || !amount) return tg.showAlert("Заполни поля");
    try {
        const r = await api("/api/admin/set_balance", { method: "POST", body: JSON.stringify({ user_id: uid, amount }) });
        tg.showAlert(`✅ Новый баланс: ${r.new_balance}⭐`);
    } catch (e) { tg.showAlert(e.message); }
}

loadMe();
loadHistory();
loadTop();
