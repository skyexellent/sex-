const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
tg.setHeaderColor("secondary_bg_color");

const initData = tg.initData;
let currentUser = null;

// ============ API ============
async function api(path, options = {}) {
    const resp = await fetch(path, {
        ...options,
        headers: {
            "X-Init-Data": initData,
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
    });
    if (!resp.ok) {
        const err = await resp.text();
        console.error("API error:", resp.status, err);
        throw new Error(err);
    }
    return resp.json();
}

// ============ Баланс ============
async function loadMe() {
    try {
        const data = await api("/api/me");
        currentUser = data;
        document.getElementById("balanceValue").textContent = data.balance;

        document.getElementById("pUsername").textContent = data.username ? "@" + data.username : data.first_name;
        document.getElementById("pUserId").textContent = data.user_id;
        document.getElementById("pBalance").textContent = data.balance + " ⭐";
        document.getElementById("pTopup").textContent = data.total_topup + " ⭐";
        document.getElementById("refLink").textContent =
            `https://t.me/${tg.initDataUnsafe.user?.username ? "" : ""}${BOT_USERNAME}?start=ref_${data.user_id}`;
    } catch (e) {
        console.error("loadMe failed:", e);
    }
}

// ============ Вкладки ============
document.querySelectorAll(".nav-btn").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        document.querySelectorAll(".tab").forEach(t => t.classList.add("hidden"));
        document.getElementById("tab-" + btn.dataset.tab).classList.remove("hidden");
    });
});

// ============ Пополнение ============
const modal = document.getElementById("topupModal");
document.getElementById("balanceBtn").addEventListener("click", () => {
    modal.classList.remove("hidden");
});
function closeTopup() { modal.classList.add("hidden"); }

document.querySelectorAll(".quick-btns button").forEach(b => {
    b.addEventListener("click", () => {
        document.getElementById("customAmount").value = b.dataset.amount;
    });
});

async function submitTopup() {
    const amount = parseInt(document.getElementById("customAmount").value, 10);
    if (!amount || amount < 50) {
        tg.showAlert("Минимум 50⭐");
        return;
    }
    try {
        const res = await api("/api/topup", {
            method: "POST",
            body: JSON.stringify({ amount }),
        });
        tg.showAlert(res.message || "Инвойс отправлен в чат с ботом");
        modal.classList.add("hidden");
    } catch (e) {
        tg.showAlert("Ошибка: " + e.message);
    }
}

// ============ Рефералка ============
function getRefLink() {
    const uid = currentUser?.user_id || tg.initDataUnsafe.user?.id;
    const bot = BOT_USERNAME;
    return `https://t.me/${bot}?start=ref_${uid}`;
}
function copyRef() {
    const link = getRefLink();
    navigator.clipboard.writeText(link);
    tg.showAlert("Ссылка скопирована!");
}
function shareRef() {
    const link = getRefLink();
    const text = encodeURIComponent("🎰 Заходи в казино, играй и выигрывай звёзды!");
    const url = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${text}`;
    tg.openTelegramLink(url);
}

// ============ Инициализация ============
// BOT_USERNAME подставляется из бэкенда — но пока захардкодим через /api/me
let BOT_USERNAME = "your_bot";  // ← поменяй на username своего бота
loadMe();