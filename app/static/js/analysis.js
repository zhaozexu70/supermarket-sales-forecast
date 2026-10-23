// 销售分析页：向 /api/analysis/... 要数据，画 8 张图；图表之间可以联动
const config = JSON.parse(document.getElementById("analysisConfig").textContent);
const BLUE = "#1f4e79", ORANGE = "#e08e36", GREEN = "#2e8b57", RED = "#c0392b", GRAY = "#b8c2cc";
const CATEGORY_COLORS = { "办公用品": BLUE, "技术": ORANGE, "家具": GREEN };

const charts = {};                              // 图表名 -> ECharts 图表
const lastData = {};                            // 图表名 -> 最近一次拿到的数据
const link = { region: "", category: "" };     // 联动：点了哪个地区、哪个类别
let round = 0;                                  // 第几轮加载，用来丢掉过时的结果

const wan = v => (v / 10000).toFixed(1);        // 元 -> 万元
// 每张图右上角的“保存为图片”按钮，论文里的图可以直接从这里存
const saveButton = name => ({ right: 8, feature: { saveAsImage: { title: "保存为图片", name: name, pixelRatio: 2 } } });
// 联动选中的那一项正常显示，其他的变淡
const fade = (selected, name) => (selected && selected !== name ? 0.3 : 1);

// ---------------- 每张图怎么画 ----------------
const builders = {
  trend: d => ({
    toolbox: saveButton("月度走势"), tooltip: { trigger: "axis" }, legend: { top: 0 },
    grid: { left: 60, right: 60, top: 40, bottom: 80 },
    xAxis: { type: "category", data: d.months },
    yAxis: [{ type: "value", name: "销售额（万元）" }, { type: "value", name: "销量（件）", splitLine: { show: false } }],
    dataZoom: [{ type: "slider", bottom: 20 }, { type: "inside" }],
    series: [
      { name: "销售额（万元）", type: "bar", data: d.sales.map(v => +wan(v)), itemStyle: { color: BLUE, opacity: 0.75 } },
      { name: "销量（件）", type: "line", yAxisIndex: 1, data: d.quantity, smooth: true, showSymbol: false,
        itemStyle: { color: ORANGE } }]
  }),

  season: d => ({
    toolbox: saveButton("淡旺季"), tooltip: { trigger: "axis", valueFormatter: v => v + "%" },
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: d.months },
    yAxis: { type: "value", name: "占总销量（%）" },
    series: [{ name: "销量占比", type: "bar", label: { show: true, position: "top", formatter: "{c}%" },
               data: d.share.map(v => ({ value: v, itemStyle: { color: v > 100 / 12 ? ORANGE : BLUE } })) }]
  }),

  yoy: d => ({
    toolbox: saveButton("各年同期对比"), tooltip: { trigger: "axis" }, legend: { top: 0 },
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: d.months },
    yAxis: { type: "value", name: "销量（件）" },
    series: d.years.map(y => ({ name: y.year + " 年", type: "line", data: y.quantity, smooth: true }))
  }),

  annual: d => ({
    toolbox: saveButton("年度增长"), tooltip: { trigger: "axis", valueFormatter: v => v + " 万元" },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: d.years.map(y => y.year + " 年") },
    yAxis: { type: "value", name: "销售额（万元）" },
    series: [{ name: "销售额", type: "bar", barWidth: "45%", itemStyle: { color: BLUE },
               data: d.years.map(y => +wan(y.sales)),
               label: { show: true, position: "top", formatter: p => {
                 const y = d.years[p.dataIndex];
                 if (y.growth !== null) return (y.growth >= 0 ? "+" : "") + y.growth + "%";
                 return y.months < 12 ? "只有 " + y.months + " 个月" : "";
               } } }]
  }),

  region: d => ({
    toolbox: saveButton("地区对比"), tooltip: { trigger: "axis", valueFormatter: v => v + " 万元" }, legend: { top: 0 },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: d.regions.map(r => r.region), triggerEvent: true },
    yAxis: { type: "value", name: "万元" },
    series: [
      { name: "销售额", type: "bar", cursor: "pointer",
        label: { show: true, position: "top", formatter: p => d.regions[p.dataIndex].share + "%" },
        data: d.regions.map(r => ({ value: +wan(r.sales), itemStyle: { color: BLUE, opacity: fade(link.region, r.region) } })) },
      { name: "利润", type: "bar", cursor: "pointer",
        data: d.regions.map(r => ({ value: +wan(r.profit), itemStyle: { color: GREEN, opacity: fade(link.region, r.region) } })) }]
  }),

  subcategory: d => {
    const subs = d.subs.slice().reverse();      // 横着的柱状图从下往上画，倒过来以后销售额最高的在最上面
    return {
      toolbox: saveButton("品类贡献"), tooltip: { trigger: "axis", valueFormatter: v => v + " 万元" }, legend: { top: 0 },
      grid: { left: 70, right: 30, top: 40, bottom: 50 },
      xAxis: { type: "value", name: "万元", nameLocation: "middle", nameGap: 28 },
      yAxis: { type: "category", data: subs.map(s => s.sub_category) },
      series: [
        { name: "销售额", type: "bar", cursor: "pointer",
          data: subs.map(s => ({ value: +wan(s.sales), itemStyle: { color: BLUE, opacity: fade(link.category, s.category) } })) },
        { name: "利润", type: "bar", cursor: "pointer", itemStyle: { color: GREEN },
          data: subs.map(s => ({ value: +wan(s.profit),
                                 itemStyle: { color: s.profit < 0 ? RED : GREEN, opacity: fade(link.category, s.category) } })) }]
    };
  },

  heatmap: d => ({
    toolbox: saveButton("地区子类别销量热力图"),
    tooltip: { formatter: p => d.regions[p.value[1]] + " · " + d.subs[p.value[0]] + "：" + p.value[2] + " 件" },
    grid: { left: 60, right: 20, top: 40, bottom: 90 },
    xAxis: { type: "category", data: d.subs, axisLabel: { interval: 0, rotate: 40 }, splitArea: { show: true } },
    yAxis: { type: "category", data: d.regions, splitArea: { show: true } },
    visualMap: { min: 0, max: Math.max(1, ...d.cells.map(c => c[2])), calculable: true, orient: "horizontal",
                 left: "center", bottom: 0, inRange: { color: ["#eef4fa", "#6c9bc7", BLUE] } },
    series: [{ name: "销量", type: "heatmap", data: d.cells, label: { show: true, fontSize: 11 } }]
  }),

  quadrant: d => {
    const xs = d.points.map(p => p.quantity), ys = d.points.map(p => p.profit_rate);
    const x0 = Math.floor(Math.min(...xs) * 0.85 / 100) * 100, x1 = Math.ceil(Math.max(...xs) * 1.1 / 100) * 100;
    const y0 = Math.floor((Math.min(...ys) - 5) / 5) * 5, y1 = Math.ceil((Math.max(...ys) + 5) / 5) * 5;
    const mx = d.median_quantity, my = d.median_rate;
    const area = (name, from, to, position) => [
      { name: name, coord: from, label: { position: position, color: "#6c757d", fontSize: 13 },
        itemStyle: { color: "rgba(31, 78, 121, 0.03)" } }, { coord: to }];
    return {
      toolbox: saveButton("品类四象限"),
      tooltip: { formatter: p => p.value[2] + "（" + p.value[3] + "）<br>销量 " + p.value[0] + " 件<br>利润率 " + p.value[1] + "%" },
      grid: { left: 60, right: 40, top: 30, bottom: 50 },
      xAxis: { type: "value", name: "销量（件）", nameLocation: "middle", nameGap: 30, min: x0, max: x1, splitLine: { show: false } },
      yAxis: { type: "value", name: "利润率（%）", min: y0, max: y1, splitLine: { show: false } },
      series: [{
        name: "子类别", type: "scatter", symbolSize: 14,
        data: d.points.map(p => ({ value: [p.quantity, p.profit_rate, p.sub_category, p.category],
                                   itemStyle: { color: CATEGORY_COLORS[p.category] } })),
        // 每个点旁边写上子类别的名字。名字放在点的哪一边由下面的 placeLabels 决定，保证互相不压住
        label: { show: true, formatter: p => p.value[2], position: "right", fontSize: 13, color: "#212529" },
        // 两条虚线是销量、利润率的中位数；四个角上写着象限的名字和建议
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed", color: "#8a8f98" },
                    label: { formatter: p => p.name, position: "insideEndTop", color: "#6c757d" },
                    data: [{ name: "销量中位数 " + mx + " 件", xAxis: mx, label: { position: "insideStartTop" } },
                           { name: "利润率中位数 " + my + "%", yAxis: my }] },
        markArea: { silent: true, data: [
          area("高销量高利润：重点保障", [mx, my], [x1, y1], "insideTopRight"),
          area("高销量低利润：控制折扣", [mx, y0], [x1, my], "insideBottomRight"),
          area("低销量高利润：适度备货", [x0, my], [mx, y1], "insideTopLeft"),
          area("低销量低利润：减少进货", [x0, y0], [mx, my], "insideBottomLeft")] }
      }]
    };
  }
};

// 四象限图上的点有时挤在一起，名字会叠住。这里给每个点挑一个位置放名字：
// 先试紧挨着点的 8 个位置；都被占了，就放到远一点的地方，再画一条细线连回这个点
function placeLabels(chart, d) {
  const SIZE = 13, HALF = 7;                    // 字号、圆点的半径
  const centers = d.points.map(p => chart.convertToPixel({ seriesIndex: 0 }, [p.quantity, p.profit_rate]));
  const taken = centers.map(([x, y]) => [x - HALF - 1, y - HALF - 1, x + HALF + 1, y + HALF + 1]);
  const hit = (r, s) => r[0] < s[2] && s[0] < r[2] && r[1] < s[3] && s[1] < r[3];
  const lines = [];
  // 周围点越多的越先放，这样最挤的地方先占到近处的位置
  const order = d.points.map((p, i) => i).sort((i, k) => crowd(k) - crowd(i));
  function crowd(i) {
    return centers.filter(c => Math.abs(c[0] - centers[i][0]) < 90 && Math.abs(c[1] - centers[i][1]) < 40).length;
  }
  const data = [];
  for (const i of order) {
    const p = d.points[i], [cx, cy] = centers[i];
    const w = p.sub_category.length * SIZE + 2, h = SIZE + 3;
    // 候选位置：名字左上角相对圆心的偏移。前 8 个紧挨着点，后面的一圈比一圈远
    const spots = [[10, -h / 2], [-10 - w, -h / 2], [-w / 2, -HALF - 3 - h], [-w / 2, HALF + 3],
                   [8, -HALF - h], [8, HALF], [-8 - w, -HALF - h], [-8 - w, HALF]];
    for (const far of [34, 56, 80, 106]) {
      spots.push([far * 0.7, -far - h / 2], [far * 0.7, far - h / 2], [-far * 0.7 - w, -far - h / 2],
                 [-far * 0.7 - w, far - h / 2], [far + 6, -h / 2], [-far - 6 - w, -h / 2],
                 [-w / 2, -far - h], [-w / 2, far]);
    }
    const index = spots.findIndex(([dx, dy]) => {
      const r = [cx + dx - 2, cy + dy - 1, cx + dx + w + 2, cy + dy + h + 1];
      return !taken.some((s, k) => k !== i && hit(r, s));
    });
    const [dx, dy] = spots[Math.max(index, 0)];
    taken.push([cx + dx, cy + dy, cx + dx + w, cy + dy + h]);
    if (index >= 8) {                            // 放得远的，画一条线连回圆点
      const tx = Math.min(Math.max(cx, cx + dx), cx + dx + w), ty = cy + dy + (dy < 0 ? h : 0);
      lines.push({ type: "line", silent: true, z: 1, shape: { x1: cx, y1: cy, x2: tx, y2: ty },
                   style: { stroke: "#9aa3ad", lineWidth: 1 } });
    }
    data[i] = { value: [p.quantity, p.profit_rate, p.sub_category, p.category],
                itemStyle: { color: CATEGORY_COLORS[p.category] },
                label: { position: [dx + HALF, dy + HALF], align: "left", verticalAlign: "top" } };
  }
  chart.setOption({ series: [{ data: data }], graphic: [{ id: "labelLines", type: "group", children: lines }] },
                  { replaceMerge: ["graphic"] });
}

// 四象限旁边的建议表
function fillQuadrantTable(d) {
  const body = document.querySelector("#quadrantTable tbody");
  body.innerHTML = "";
  for (const g of d.groups || []) {
    const row = body.insertRow();
    row.insertCell().textContent = g.quadrant;
    const advice = row.insertCell();
    advice.innerHTML = "<b></b><div class='stat-note'></div>";
    advice.querySelector("b").textContent = g.advice;
    advice.querySelector("div").textContent = g.why;
    row.insertCell().textContent = g.subs.length ? g.subs.join("、") : "—";
  }
  document.getElementById("quadrantNote").textContent = d.points && d.points.length
    ? "共 " + d.points.length + " 个子类别；销量中位数 " + d.median_quantity + " 件，利润率中位数 " + d.median_rate + "%。" : "";
}

// ---------------- 取数据、画图 ----------------
// 某张图该带哪些条件：页面上选的条件，加上联动的条件。发起联动的那张图自己不受联动影响，只把选中的标出来
function paramsFor(name) {
  const p = new URLSearchParams();
  for (const [key, value] of Object.entries(config.filters)) if (value !== "" && value !== null) p.set(key, value);
  if (link.region && name !== "region") p.set("region", link.region);
  if (link.category && name !== "subcategory") p.set("category", link.category);
  return p;
}

function isEmpty(name, d) {
  const size = { trend: d.months, season: d.quantity && d.quantity.filter(v => v > 0), yoy: d.years, annual: d.years,
                 region: d.regions, subcategory: d.subs, heatmap: d.cells, quadrant: d.points }[name];
  return d.empty || !size || size.length === 0;
}

async function load(name, thisRound) {
  const resp = await fetch(config.api.replace("NAME", name) + "?" + paramsFor(name));
  if (!resp.ok) throw new Error("取“" + name + "”的数据失败（" + resp.status + "）");
  const d = await resp.json();
  if (thisRound !== round) return;              // 这期间条件又变了，这份结果不要了
  lastData[name] = d;
  if (isEmpty(name, d)) {
    charts[name].clear();
    charts[name].setOption({ title: { text: "所选条件下没有数据", left: "center", top: "middle",
                                      textStyle: { color: "#8a8f98", fontSize: 14, fontWeight: "normal" } } });
  } else {
    charts[name].setOption(builders[name](d), true);
    if (name === "quadrant") placeLabels(charts[name], d);
  }
  if (name === "quadrant") fillQuadrantTable(d);
}

async function loadAll() {
  const thisRound = ++round;
  document.body.dataset.ready = "0";
  const errorBox = document.getElementById("loadError");
  errorBox.classList.add("d-none");
  try {
    await Promise.all(config.charts.map(name => load(name, thisRound)));
  } catch (err) {
    errorBox.textContent = err.message + "。请刷新页面重试。";
    errorBox.classList.remove("d-none");
  }
  if (thisRound === round) document.body.dataset.ready = "1";   // 全部画完的记号，自动测试靠它知道可以检查了
}

// ---------------- 联动 ----------------
function showLink() {
  const parts = [];
  if (link.region) parts.push("地区 = " + link.region);
  if (link.category) parts.push("类别 = " + link.category);
  const status = document.getElementById("linkStatus");
  status.textContent = parts.length ? "当前联动：" + parts.join("，") : "点“地区对比”里的柱子，其他图只看这个地区";
  status.classList.toggle("text-muted", parts.length === 0);
  status.classList.toggle("fw-bold", parts.length > 0);
  document.getElementById("clearLink").disabled = parts.length === 0;
}

function setLink(key, value) {
  link[key] = link[key] === value ? "" : value;   // 再点一次同一个，就取消
  showLink();
  loadAll();
}

function start() {
  for (const name of config.charts) charts[name] = echarts.init(document.getElementById("chart_" + name));
  window.analysisCharts = charts;
  window.addEventListener("resize", () => {
    Object.values(charts).forEach(c => c.resize());
    if (lastData.quadrant && lastData.quadrant.points && lastData.quadrant.points.length) {
      placeLabels(charts.quadrant, lastData.quadrant);      // 图的大小变了，名字的位置要重新排
    }
  });

  charts.region.on("click", p => {
    const name = p.componentType === "xAxis" ? p.value : p.name;      // 点柱子或点下面的地区名字都行
    if (name) setLink("region", name);
  });
  charts.subcategory.on("click", p => {
    const sub = (lastData.subcategory.subs || []).find(s => s.sub_category === p.name);
    if (sub) setLink("category", sub.category);
  });
  document.getElementById("clearLink").addEventListener("click", () => {
    link.region = link.category = "";
    showLink();
    loadAll();
  });

  // 起始年不能晚于结束年
  const startYear = document.getElementById("start"), endYear = document.getElementById("end");
  startYear.addEventListener("change", () => { if (+startYear.value > +endYear.value) endYear.value = startYear.value; });
  endYear.addEventListener("change", () => { if (+endYear.value < +startYear.value) startYear.value = endYear.value; });

  loadAll();
}
start();
