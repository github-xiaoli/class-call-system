/* 子页面左边缘应用列表：滑出 / 高亮当前 / 移到中间自动收起 */
(function () {
  const $ = (id) => document.getElementById(id);
  const open = () => document.body.classList.add("edge-open");
  const close = () => document.body.classList.remove("edge-open", "drawer-open");

  document.addEventListener("mousemove", (e) => {
    const opened = document.body.classList.contains("edge-open");
    if (!opened && e.clientX <= 16) open();
    else if (opened && e.clientX > 300) close();
  });
  const tab = $("edge-tab");
  if (tab) tab.addEventListener("click", open);
  const closeBtn = $("drawer-close");
  if (closeBtn) closeBtn.addEventListener("click", close);
  const mask = $("drawer-mask");
  if (mask) mask.addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  (function markActive() {
    const here = location.pathname.replace(/\/$/, "") || "/";
    document.querySelectorAll(".app-item").forEach((a) => {
      const href = (a.getAttribute("href") || "").replace(/\/$/, "") || "/";
      if (href === here) a.classList.add("active");
      if (a.classList.contains("home")) a.classList.toggle("active", here === "/");
    });
  })();
})();
