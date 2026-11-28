/* 名条管理子页面 */
(function () {
  const $ = (id) => document.getElementById(id);
  const jget = (u) => fetch(u, { credentials: "same-origin" }).then((r) => r.json());
  const jpost = (u, b) => fetch(u, {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify(b || {}),
  }).then((r) => r.json());

  function msg(t, err) {
    const el = $("msg");
    if (!el) return;
    el.textContent = t || "";
    el.style.color = err ? "#b3261e" : "#12805c";
  }

  async function load() {
    let d;
    try { d = await jget("/api/roster"); } catch (e) { msg("读取失败", true); return; }
    const items = (d && (d.students || d.roster || d.items)) || [];
    const box = $("list");
    box.innerHTML = items.map((s) => {
      const num = s.number || s.num || "";
      const name = s.name || "";
      return '<li><span class="num">' + num + '</span><span class="nm">' + name +
        '</span><button class="mini danger" data-del="' + num + '">删除</button></li>';
    }).join("");
    box.querySelectorAll("button[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        await jpost("/api/roster/delete", { number: b.getAttribute("data-del") });
        load();
      });
    });
    const sum = $("summary");
    if (sum) sum.textContent = "共 " + items.length + " 条名条";
  }

  if ($("btn-add")) $("btn-add").addEventListener("click", async () => {
    const number = ($("add-number").value || "").trim();
    const name = ($("add-name").value || "").trim();
    if (!number || !name) { msg("学号和姓名都要填", true); return; }
    const r = await jpost("/api/roster", { number: number, name: name });
    if (r && r.ok === false) { msg(r.error || "添加失败", true); return; }
    $("add-number").value = ""; $("add-name").value = "";
    msg("已添加 " + number);
    load();
  });

  if ($("btn-import")) $("btn-import").addEventListener("click", () => $("csv-file").click());
  if ($("csv-file")) $("csv-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    msg("正在导入…");
    try {
      const text = await f.text();
      const rows = text.replace(/\r/g, "").split("\n").filter((x) => x.trim());
      let added = 0;
      for (const line of rows) {
        const cells = line.split(",").map((c) => c.trim().replace(/^"|"$/g, ""));
        if (cells.length < 2) continue;
        const num = cells[0];
        const name = cells[1];
        if (!num || !name) continue;
        if (/^(学号|编号|number|id)$/i.test(num) || /^(姓名|名字|name)$/i.test(name)) continue;
        const r = await jpost("/api/roster", { number: num, name: name });
        if (!r || r.ok !== false) added += 1;
      }
      msg("已导入 " + added + " 条");
      load();
    } catch (err) {
      msg("导入失败：" + err.message, true);
    }
  });

  if ($("btn-clear-all")) $("btn-clear-all").addEventListener("click", async () => {
    if (!confirm("清空全部名条？此操作不可撤销。")) return;
    await jpost("/api/roster/clear", {});
    msg("已清空");
    load();
  });

  if ($("sub-back")) $("sub-back").addEventListener("click", () => { location.href = "/"; });
  load();
})();
