;const tg = window.Telegram.WebApp;
tg.ready(); tg.expand(); tg.setHeaderColor("secondary_bg_color");

const API_URL = "https://cash-xxxx.onrender.com";   // ← ЗАМЕНИ на свой Render URL
const BOT_USERNAME = "@free_stars_infbot";           // ← ЗАМЕНИ на username бота (без @)

const initData = tg.initData;
let currentUser = null;
let currentGame = null;
let ws = null;
let canvas, ctx;
let spinAnimation = null;

// ============ API ============
async function api(path, options = {}) {
    const resp = await fetch(API_URL + path, {
        ...options,
        headers: {
            "X-Init-Data": initData,
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Ошибка" }));
        throw new Error(err.detail || err.message || "Ошибка запроса");
    }
    return resp.json();
}

// ============ Профиль и баланс ============
async function loadMe() {
    try {
        const data = await api("/api/me");
        currentUser = data;
        document.getElementById("balanceValue").textContent = data.balance;
        document.getElementById("pUsername").textContent = data.username ? "@" + data.username : data.first_name;
        document.getElementById("pUserId").textContent = data.user_id;
        document.getElementById("pBalance").textContent = data.balance + " ⭐";
        document.getElementById("pTopup").textContent = data.total_topup + " ⭐";
        document.getElementById("refLink").textContent = getRefLink();
    } catch (e) { console.error("loadMe:", e); }
}

function getRefLink() {
    const uid = currentUser?.user_id || tg.initDataUnsafe.user?.id;
    return `https://t.me/${BOT_USERNAME}?start=ref_${uid}`;
}
function copyRef() {
    navigator.clipboard.writeText(getRefLink());
    tg.showAlert("Ссылка скопирована!");
}
function shareRef() {
    const link = getRefLink();
    const text = encodeURIComponent("🎰 Заходи в казино, играй и выигрывай звёзды!");
    tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(link)}&text=${text}`);
}

// ============ История игр ============
async function loadHistory() {
    try {
        const rounds = await api("/api/history");
        const el = document.getElementById("history");
        if (!rounds.length) {
            el.innerHTML = '<div class="muted">Пока нет игр</div>';
            return;
        }
        el.innerHTML = rounds.map(r => `
            <div class="history-item">
                <span>${r.game === "wheel" ? "🎡" : "🟦"} @${r.winner_name || r.winner_id}</span>
                <span class="win">+${r.payout}⭐</span>
            </div>
        `).join("");
    } catch (e) { console.error("history:", e); }
}

// ============ Вкладки ============
document.querySelectorAll(".nav-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.querySelectorAll(".tab").forEach(t => t.classList.add("hidden"));
        document.getElementById("tab-" + btn.dataset.tab).classList.remove("hidden");
        if (btn.dataset.tab === "games") loadHistory();
    });
});

document.querySelectorAll(".game-btn").forEach(btn => {
    btn.addEventListener("click", () => openGame(btn.dataset.game));
});

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
    loadHistory();
}

// ============ Canvas ============
function initCanvas() {
    canvas = document.getElementById("gameCanvas");
    ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#232e3c";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#7d8b99";
    ctx.font = "16px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Ожидание игроков…", canvas.width / 2, canvas.height / 2);
}

function drawWheel(players, rotation, winnerIndex, highlight) {
    const cx = canvas.width / 2, cy = canvas.height / 2;
    const radius = Math.min(cx, cy) - 10;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const total = players.reduce((s, p) => s + p.stake, 0);
    if (total === 0) return;

    let startAngle = rotation;
    players.forEach((p, i) => {
        const slice = (p.stake / total) * Math.PI * 2;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, radius, startAngle, startAngle + slice);
        ctx.closePath();
        ctx.fillStyle = p.color;
        ctx.fill();
        if (highlight && i === winnerIndex) {
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 4;
            ctx.stroke();
        }
        // label
        const mid = startAngle + slice / 2;
        const tx = cx + Math.cos(mid) * radius * 0.65;
        const ty = cy + Math.sin(mid) * radius * 0.65;
        ctx.save();
        ctx.translate(tx, ty);
        ctx.rotate(mid + Math.PI / 2);
        ctx.fillStyle = "#000";
        ctx.font = "bold 12px sans-serif";
        ctx.textAlign = "center";
        const label = p.username ? "@" + p.username : p.first_name;
        ctx.fillText(label.length > 10 ? label.slice(0, 10) + "…" : label, 0, 0);
        ctx.fillText(p.stake + "⭐", 0, 14);
        ctx.restore();
        startAngle += slice;
    });

    // стрелка сверху
    ctx.beginPath();
    ctx.moveTo(cx, cy - radius - 8);
    ctx.lineTo(cx - 10, cy - radius + 12);
    ctx.lineTo(cx + 10, cy - radius + 12);
    ctx.closePath();
    ctx.fillStyle = "#fff";
    ctx.fill();
}

function drawSquare(players, dotPos, winnerIndex) {
    const pad = 20;
    const W = canvas.width - pad * 2;
    const H = canvas.height - pad * 2;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const total = players.reduce((s, p) => s + p.stake, 0);
    if (total === 0) return;

    let x = pad, y = pad, rowH = 0;
    const perRow = Math.ceil(Math.sqrt(players.length));
    const cellW = W / perRow;
    const cellH = H / Math.ceil(players.length / perRow);
    players.forEach((p, i) => {
        const col = i % perRow;
        const row = Math.floor(i / perRow);
        const cx = pad + col * cellW;
        const cy = pad + row * cellH;
        ctx.fillStyle = p.color;
        ctx.fillRect(cx, cy, cellW - 2, cellH - 2);
        if (winnerIndex !== null && i === winnerIndex) {
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 4;
            ctx.strokeRect(cx, cy, cellW - 2, cellH - 2);
        }
        ctx.fillStyle = "#000";
        ctx.font = "bold 12px sans-serif";
        ctx.textAlign = "center";
        const label = p.username ? "@" + p.username : p.first_name;
        ctx.fillText(label.length > 10 ? label.slice(0, 10) + "…" : label, cx + cellW / 2, cy + cellH / 2 - 6);
        ctx.fillText(p.stake + "⭐", cx + cellW / 2, cy + cellH / 2 + 12);
    });

    // точка
    if (dotPos) {
        ctx.beginPath();
        ctx.arc(dotPos.x, dotPos.y, 8, 0, Math.PI * 2);
        ctx.fillStyle = "#fff";
        ctx.fill();
        ctx.strokeStyle = "#000";
        ctx.lineWidth = 2;
        ctx.stroke();
    }
}

// ============ WebSocket ============
function connectWS() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    const wsUrl = API_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws/" + currentUser.user_id;
    ws = new WebSocket(wsUrl);
    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        handleWSEvent(msg);
    };
    ws.onerror = (e) => console.error("WS error:", e);
    ws.onclose = () => {
        setTimeout(() => { if (currentGame) connectWS(); }, 2000);
    };
}

function handleWSEvent(msg) {
    const { event, data } = msg;
    if (!currentGame) return;
    if (data.game && data.game !== currentGame) return;

    if (event === "state") {
        renderState(data);
    } else if (event === "spin") {
        startSpin(data);
    } else if (event === "result") {
        showResult(data);
    } else if (event === "refunded") {
        tg.showAlert("Никто не присоединился, ставки возвращены");
        loadMe();
    }
}

function renderState(state) {
    document.getElementById("pool").textContent = state.pool;
    document.getElementById("timer").textContent = state.time_left > 0 ? state.time_left : "—";

    // участники
    const el = document.getElementById("players");
    if (!state.players.length) {
        el.innerHTML = '<div class="muted">Пока никого. Сделай первую ставку!</div>';
    } else {
        el.innerHTML = state.players.map(p => `
            <div class="player-row" style="border-left-color: ${p.color};">
                <div class="name">${p.username ? "@" + p.username : p.first_name}</div>
                <div><span class="stake">${p.stake}⭐</span><span class="share">${p.share}%</span></div>
            </div>
        `).join("");
    }

    // поле
    if (currentGame === "wheel") {
        drawWheel(state.players, 0, null, false);
    } else if (currentGame === "square") {
        drawSquare(state.players, null, null);
    }
}

// ============ Анимация спина ============
function startSpin(data) {
    const duration = (data.duration || 6) * 1000;
    const startTime = performance.now();
    const players = data.players;
    const total = players.reduce((s, p) => s + p.stake, 0);
    const winnerIdx = data.winner_index;

    // угол победителя (для колеса)
    let acc = 0;
    for (let i = 0; i < winnerIdx; i++) acc += players[i].stake;
    const winMidAngle = ((acc + players[winnerIdx].stake / 2) / total) * Math.PI * 2;

    const spinState = { players, winnerIdx, total };

    const animate = (now) => {
        const t = Math.min(1, (now - startTime) / duration);
        const ease = 1 - Math.pow(1 - t, 3); // ease-out cubic

        if (currentGame === "wheel") {
            // Вращаем колесо так, чтобы победитель оказался под стрелкой (сверху)
            const targetRotation = -winMidAngle - Math.PI / 2;
            const rotation = targetRotation + (1 - ease) * Math.PI * 12;
            drawWheel(players, rotation, null, false);
        } else {
            // Точка летает по полю, замедляясь
            const pad = 20;
            const W = canvas.width - pad * 2;
            const H = canvas.height - pad * 2;
            const perRow = Math.ceil(Math.sqrt(players.length));
            const cellW = W / perRow;
            const cellH = H / Math.ceil(players.length / perRow);

            // позиция победителя — центр его клетки
            const col = winnerIdx % perRow;
            const row = Math.floor(winnerIdx / perRow);
            const targetX = pad + col * cellW + cellW / 2;
            const targetY = pad + row * cellH + cellH / 2;

            // случайное начало
            const startX = Math.random() * canvas.width;
            const startY = Math.random() * canvas.height;

            const x = startX + (targetX - startX) * ease + Math.sin(t * 20) * (1 - t) * 40;
            const y = startY + (targetY - startY) * ease + Math.cos(t * 20) * (1 - t) * 40;

            drawSquare(players, { x, y }, null);
        }

        if (t < 1) {
            spinAnimation = requestAnimationFrame(animate);
        } else {
            // финальный кадр — показать победителя
            if (currentGame === "wheel") {
                const targetRotation = -winMidAngle - Math.PI / 2;
                drawWheel(players, targetRotation, winnerIdx, true);
            } else {
                const pad = 20;
                const W = canvas.width - pad * 2;
                const H = canvas.height - pad * 2;
                const perRow = Math.ceil(Math.sqrt(players.length));
                const cellW = W / perRow;
                const cellH = H / Math.ceil(players.length / perRow);
                const col = winnerIdx % perRow;
                const row = Math.floor(winnerIdx / perRow);
                const targetX = pad + col * cellW + cellW / 2;
                const targetY = pad + row * cellH + cellH / 2;
                drawSquare(players, { x: targetX, y: targetY }, winnerIdx);
            }
            spinAnimation = null;
        }
    };
    if (spinAnimation) cancelAnimationFrame(spinAnimation);
    spinAnimation = requestAnimationFrame(animate);
}

function showResult(data) {
    const el = document.getElementById("topWinner");
    el.classList.remove("hidden");
    el.innerHTML = `
        🏆 Победитель
        <div class="who">@${data.winner_name || data.winner_id}</div>
        <div class="sum">+${data.payout}⭐</div>
    `;
    loadMe();
    setTimeout(() => loadHistory(), 1500);
}

// ============ Ставка ============
async function placeBet() {
    const amount = parseInt(document.getElementById("betAmount").value, 10);
    const errEl = document.getElementById("betError");
    errEl.textContent = "";
    if (!amount || amount < 10) {
        errEl.textContent = "Минимум 10⭐";
        return;
    }
    try {
        await api(`/api/game/${currentGame}/bet`, {
            method: "POST",
            body: JSON.stringify({ amount }),
        });
        loadMe();
    } catch (e) {
        errEl.textContent = e.message;
    }
}

// ============ Пополнение ============
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
        const res = await api("/api/topup", { method: "POST", body: JSON.stringify({ amount }) });
        tg.showAlert(res.message);
        modal.classList.add("hidden");
    } catch (e) { tg.showAlert("Ошибка: " + e.message); }
}

// ============ Init ============
loadMe();
loadHistory();
