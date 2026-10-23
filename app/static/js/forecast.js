// 数据预测页：预测设置对话框
const forecastConfig = JSON.parse(document.getElementById("forecastConfig").textContent);
const form = document.getElementById("forecastForm");
const el = id => document.getElementById(id);
const targetYear = el("target_year"), historyStart = el("history_start");
const region = el("f_region"), category = el("f_category"), level = el("f_level"), sub = el("f_sub");
const inventory = el("f_inventory");

// ① 先选预测年份，② 再选历史起始年：历史起始年的选项跟着预测年份变，历史数据固定到预测年份的前一年
function fillStarts() {
  const starts = forecastConfig.options[targetYear.value] || [];
  historyStart.innerHTML = "";
  for (const y of starts) historyStart.add(new Option(y, y));
  showHistory();
}

function showHistory() {
  const years = +targetYear.value - +historyStart.value;
  el("historyText").value = historyStart.value + "–" + (targetYear.value - 1) + " 年（" + years + " 年）";
}

// 子类别下拉框：只在层级是“子类别”而且选了具体某个类别时出现
function fillSubs() {
  const show = level.value === "子类别" && category.value !== "全部";
  el("subBox").classList.toggle("d-none", !show);
  sub.innerHTML = "";
  if (show) {
    sub.add(new Option("全部", "全部"));
    for (const s of forecastConfig.subs[category.value]) sub.add(new Option(s, s));
  }
  updateInventory();
}

// 当前库存：只预测一条序列时才能填，否则固定为 0 并置灰
function updateInventory() {
  const subShown = !el("subBox").classList.contains("d-none");
  const single = region.value !== "全部" && category.value !== "全部" &&
                 (level.value === "类别" || (subShown && sub.value !== "全部"));
  inventory.disabled = !single;
  if (!single) inventory.value = 0;
  el("inventoryHint").textContent = single ? "这条序列现在的库存，不知道就填 0" : "只预测一条序列时才能填";
  // 顺便告诉用户这次要算多少条序列
  const n = (region.value === "全部" ? 6 : 1) * (category.value === "全部" ? 3 : 1);
  el("seriesHint").textContent = "这次要计算 " + n + " 条“地区 × 类别”序列" +
    (level.value === "子类别" ? "，再按销量占比分给各个子类别。" : "。");
}

function showError(result) {
  el("forecastErrorText").textContent = result.message;
  el("goProcess").classList.toggle("d-none", !result.need_process);      // 有年份没处理：给一个去数据处理的按钮
  el("goRunning").classList.toggle("d-none", !result.running_url);       // 已经有任务在跑：给一个去看它的按钮
  if (result.running_url) el("goRunning").href = result.running_url;
  el("forecastError").classList.remove("d-none");
}

for (const y of Object.keys(forecastConfig.options)) targetYear.add(new Option(y, y));
targetYear.value = Object.keys(forecastConfig.options).pop() || "";      // 默认预测最晚的那一年
fillStarts();
fillSubs();
targetYear.addEventListener("change", fillStarts);
historyStart.addEventListener("change", showHistory);
level.addEventListener("change", fillSubs);
category.addEventListener("change", fillSubs);
region.addEventListener("change", updateInventory);
sub.addEventListener("change", updateInventory);
el("forecastModal").addEventListener("hidden.bs.modal", () => el("forecastError").classList.add("d-none"));

// 点“开始预测”：用 fetch 提交。通过了就转到结果页（那里显示进度）；没通过就在对话框里说明原因
form.addEventListener("submit", async e => {
  e.preventDefault();
  const btn = el("startBtn");
  if (btn.disabled) return;
  el("forecastError").classList.add("d-none");
  btn.disabled = true;
  btn.textContent = "正在提交…";
  const data = Object.fromEntries(new FormData(form));
  data.inventory = inventory.disabled ? 0 : inventory.value;             // 置灰的输入框不会被提交，这里补上
  let result;
  try {
    result = await postJSON(form.dataset.url, data);
    if (result.errors) result = { ok: false, message: result.errors[0].msg };
  } catch (err) {
    result = { ok: false, message: "连不上服务器，请确认网站还在运行" };
  }
  if (result.ok) {
    location.href = result.url;                                          // 按钮保持不可点，直到新页面出来
    return;
  }
  btn.disabled = false;
  btn.textContent = "开始预测";
  showError(result);
});
