// 预测结果页：正在计算时显示进度；算完后画走势图、填表格、切换序列

// ---------------- 进度：每 2 秒问一次，算完（或失败）就刷新页面 ----------------
const progressCard = document.getElementById("progressCard");
if (progressCard) {
  const ask = async () => {
    let s;
    try {
      s = await (await fetch(progressCard.dataset.url)).json();
    } catch (err) {
      return;                                   // 这一次没问到，下一次再问
    }
    document.getElementById("progressText").textContent = "已完成 " + s.done + " / " + s.total;
    document.getElementById("progressBar").style.width = (s.total ? Math.round(s.done / s.total * 100) : 0) + "%";
    if (s.status !== "运行中") {
      clearInterval(timer);
      location.reload();
    }
  };
  const timer = setInterval(ask, 2000);
  ask();
}

// ---------------- 结果：走势图和表格 ----------------
const configTag = document.getElementById("resultConfig");
if (configTag) {
  const config = JSON.parse(configTag.textContent);
  const regionSelect = document.getElementById("s_region");
  const categorySelect = document.getElementById("s_category");
  const subSelect = document.getElementById("s_sub");          // 类别层级时没有这个下拉框
  const chart = echarts.init(document.getElementById("forecastChart"));
  window.forecastChart = chart;
  window.addEventListener("resize", () => chart.resize());
  let round = 0;

  // 子类别下拉框跟着类别变
  function fillSubs(keep) {
    if (!subSelect) return;
    subSelect.innerHTML = "";
    for (const s of config.subs[categorySelect.value] || []) subSelect.add(new Option(s, s, false, s === keep));
  }

  const fixed = v => Number(v).toFixed(2);

  function drawChart(d) {
    const history = d.history, rows = d.rows;
    const months = history.months.concat(rows.map(r => r.ym));
    const gap = history.months.map(() => null);                // 历史那一段，预测的几条线是空的
    const last = history.quantity.length ? history.quantity[history.quantity.length - 1] : null;
    // 预测线从历史的最后一个点接出去，看起来是连着的
    const joined = values => gap.slice(0, -1).concat([last], values);
    const series = [
      { name: "历史销量", type: "line", data: history.quantity.concat(rows.map(() => null)), showSymbol: false,
        lineStyle: { width: 2 }, itemStyle: { color: "#1f4e79" } },
      // 预测区间的浅色带：先画一条看不见的下限线，再在它上面叠一块“上限 − 下限”那么高的面积
      { name: "区间下限", type: "line", data: gap.concat(rows.map(r => r.lower)), stack: "band", symbol: "none",
        lineStyle: { opacity: 0 }, tooltip: { show: false }, silent: true },
      { name: "95% 预测区间", type: "line", data: gap.concat(rows.map(r => +(r.upper - r.lower).toFixed(2))),
        stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: "#e08e36", opacity: 0.18 },
        itemStyle: { color: "#f3d3ad" }, tooltip: { show: false }, silent: true },
      { name: "预测销量", type: "line", data: joined(rows.map(r => r.forecast)), showSymbol: false,
        lineStyle: { width: 2, type: "dashed" }, itemStyle: { color: "#e08e36" } },
      { name: "预测销量 + 安全库存", type: "line", data: gap.concat(rows.map(r => +(r.forecast + r.safety_stock).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.5, type: "dotted" }, itemStyle: { color: "#c0392b" } },
    ];
    if (d.has_actual) {
      series.push({ name: "实际销量", type: "line", data: joined(rows.map(r => r.actual)), showSymbol: true,
                    symbolSize: 5, lineStyle: { width: 2 }, itemStyle: { color: "#2e8b57" } });
    }
    chart.setOption({
      title: { text: d.title + "：历史销量与 " + config.targetYear + " 年预测", left: "center", textStyle: { fontSize: 14 } },
      toolbox: { right: 8, feature: { saveAsImage: { title: "保存为图片", name: "预测结果_" + d.title.replaceAll(" · ", "_"), pixelRatio: 2 } } },
      tooltip: { trigger: "axis", valueFormatter: v => (v === null || v === undefined ? "—" : v + " 件") },
      legend: { top: 28, data: series.map(s => s.name).filter(n => n !== "区间下限") },
      grid: { left: 60, right: 30, top: 70, bottom: 80 },
      xAxis: { type: "category", data: months, boundaryGap: false },
      yAxis: { type: "value", name: "销量（件）" },
      dataZoom: [{ type: "slider", bottom: 20 }, { type: "inside" }],
      series: series
    }, true);
  }

  function fillTable(d) {
    const body = document.querySelector("#forecastTable tbody");
    body.innerHTML = "";
    let sumForecast = 0, sumActual = 0, sumPurchase = 0;
    for (const r of d.rows) {
      const row = body.insertRow();
      const cells = [r.ym, fixed(r.forecast), fixed(r.lower), fixed(r.upper), r.actual === null ? "—" : r.actual,
                     fixed(r.safety_stock), r.purchase];
      cells.forEach((value, i) => {
        const cell = row.insertCell();
        cell.textContent = value;
        if (i > 0) cell.className = "text-end";
      });
      if (r.purchase === 0) row.cells[6].classList.add("text-muted");   // 不进货的月份
      sumForecast += r.forecast;
      sumActual += r.actual || 0;
      sumPurchase += r.purchase;
    }
    document.getElementById("tableTitle").textContent = d.title + "：" + config.targetYear + " 年每月预测";
    document.getElementById("sumForecast").textContent = fixed(sumForecast);
    document.getElementById("sumActual").textContent = d.has_actual ? sumActual : "—";
    document.getElementById("sumPurchase").textContent = sumPurchase;
    // 有的月份区间下限是 0：加一句说明，免得以为是算错了
    const lowerNote = document.getElementById("lowerNote");
    lowerNote.textContent = d.lower_note ? "区间" + d.lower_note + "。" : "";
    lowerNote.classList.toggle("d-none", !d.lower_note);
  }

  async function load() {
    const thisRound = ++round;
    document.body.dataset.ready = "0";
    const params = new URLSearchParams({ region: regionSelect.value, category: categorySelect.value });
    if (subSelect) params.set("sub_category", subSelect.value);
    const d = await (await fetch(config.api + "?" + params)).json();
    if (thisRound !== round) return;            // 这期间又切换了序列，这份结果不要了
    window.forecastData = d;
    drawChart(d);
    fillTable(d);
    document.body.dataset.ready = "1";          // 画完的记号，自动测试靠它知道可以检查了
  }

  fillSubs(subSelect ? subSelect.dataset.selected : "");
  regionSelect.addEventListener("change", load);
  categorySelect.addEventListener("change", () => { fillSubs(""); load(); });
  if (subSelect) subSelect.addEventListener("change", load);
  load();
}
