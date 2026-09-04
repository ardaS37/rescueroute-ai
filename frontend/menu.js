const root = document.documentElement;
const toggle = document.getElementById("theme-toggle");
const themeLabel = toggle.querySelector(".theme-label");
const themeColor = document.querySelector('meta[name="theme-color"]');
const stage = document.getElementById("hero-stage");

function applyTheme(theme, persist = true) {
  root.dataset.theme = theme;
  const isLight = theme === "light";
  toggle.setAttribute("aria-pressed", String(isLight));
  toggle.setAttribute("aria-label", `Switch to ${isLight ? "dark" : "light"} mode`);
  themeLabel.textContent = isLight ? "Light" : "Dark";
  themeColor.content = isLight ? "#e9f2f0" : "#061316";
  if (persist) {
    try { localStorage.setItem("rescueroute-theme", theme); } catch (_) { /* Storage can be unavailable in privacy mode. */ }
  }
}

applyTheme(root.dataset.theme || "dark", false);
toggle.addEventListener("click", () => applyTheme(root.dataset.theme === "light" ? "dark" : "light"));

if (!matchMedia("(prefers-reduced-motion: reduce)").matches && matchMedia("(pointer: fine)").matches) {
  const mapGlass = stage.querySelector(".map-glass");
  stage.addEventListener("pointermove", (event) => {
    const bounds = stage.getBoundingClientRect();
    const x = (event.clientX - bounds.left) / bounds.width - .5;
    const y = (event.clientY - bounds.top) / bounds.height - .5;
    mapGlass.style.transform = `rotateY(${x * 5 - 2}deg) rotateX(${y * -5 + 1}deg) translate3d(${x * 3}px, ${y * 3}px, 0)`;
  });
  stage.addEventListener("pointerleave", () => { mapGlass.style.transform = "rotateY(-2deg) rotateX(1deg)"; });
}
