// 全站公共脚本
// 页面里用 JavaScript 提交数据（POST）时，要带上 CSRF 令牌，否则服务器会拒绝
function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]').getAttribute("content");
}

// 发送 JSON 的 POST 请求，自动带上 CSRF 令牌，返回服务器给的 JSON
async function postJSON(url, data) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
    body: JSON.stringify(data || {}),
  });
  // 令牌过期或系统出错时，服务器给的是一个网页而不是 JSON
  if (!(resp.headers.get("Content-Type") || "").includes("application/json")) {
    return { ok: false, errors: [{ field: "", msg: "页面已过期或系统出错，请刷新页面后重试" }] };
  }
  return resp.json();
}
