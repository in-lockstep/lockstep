// in-lockstep — the site's whole script.
//
// Four jobs, no framework, no build step: the theme toggle, copy buttons, the entrance for content
// as you reach it, and the hairline under the nav once the page has moved. Everything degrades to a
// working page with JavaScript switched off, which is the reason none of it is load-bearing.
(() => {
  "use strict";

  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- theme ----------
   *
   * The page follows the operating system until somebody says otherwise, and then it remembers.
   * The stored value is the explicit choice only; there is no "system" third state to store,
   * because absence of a stored value already means that. */
  const root = document.documentElement;
  const toggle = document.getElementById("theme");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const dark = root.dataset.theme
        ? root.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      const next = dark ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("ls-theme", next); } catch (e) {}
    });
  }

  /* ---------- copy ----------
   *
   * The prompt character is never part of what gets copied; a reader pasting `$ uv tool install`
   * into a shell gets an error, and it is the kind of error that reads as the tool's fault. */

  // The async clipboard API needs a secure context and a permission that some browsers will refuse.
  // The textarea fallback is the old way and it still works everywhere, so a reader on an older
  // Safari gets a working button rather than a dead one.
  const writeText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) {
        /* fall through */
      }
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  };

  for (const btn of document.querySelectorAll(".copy")) {
    const label = btn.getAttribute("aria-label");
    btn.addEventListener("click", async () => {
      if (!(await writeText(btn.dataset.copy))) return;
      btn.classList.add("done");
      btn.setAttribute("aria-label", "Copied");
      setTimeout(() => {
        btn.classList.remove("done");
        btn.setAttribute("aria-label", label);
      }, 1600);
    });
  }

  /* ---------- entrances ----------
   *
   * An IntersectionObserver rather than a scroll handler: it fires when a boundary is crossed, so a
   * fast scroll costs a handful of callbacks instead of one per frame. Elements are marked in JS
   * rather than in the markup, so the page is fully visible when this file does not run. */
  const risers = [
    ...document.querySelectorAll(
      ".hero-h1, .hero-def, .hero-copy, .hero-proof, .hed, .fig, .two-up > *, .two-col, .after-fig, " +
      ".jobs-after > *, .ledger > *, .edges > *, .ships > *, .install-lg, .doors > *"
    ),
  ];

  if (reduced || !("IntersectionObserver" in window)) {
    for (const el of risers) el.classList.add("in");
  } else {
    for (const el of risers) el.classList.add("rise");

    const io = new IntersectionObserver(
      (entries, obs) => {
        // Entries arrive in an arbitrary order, so siblings are staggered by their position among
        // the elements arriving together rather than by their index on the page. Two things
        // entering at once read as one gesture; a fixed per-element delay would make the second
        // one look late.
        const arriving = entries.filter((e) => e.isIntersecting);
        arriving.forEach((entry, i) => {
          entry.target.style.setProperty("--d", `${Math.min(i, 4) * 70}ms`);
          entry.target.classList.add("in");
          obs.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.08 },
    );
    for (const el of risers) io.observe(el);

    // The diagrams draw themselves, and only once. `drawn` is separate from `in` because the rails
    // should start after the figure has finished arriving, not underneath it.
    const figs = new IntersectionObserver(
      (entries, obs) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          entry.target.classList.add("drawn");
          obs.unobserve(entry.target);
        }
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.2 },
    );
    for (const dia of document.querySelectorAll(".dia")) figs.observe(dia);
  }

  /* ---------- deep links ----------
   *
   * A link to #loop has to land on the loop. The browser resolves a hash before web fonts have
   * settled the line heights above it, and it will also restore a previous scroll position over
   * the top of the jump, so the anchor is re-applied once after layout rather than trusted. Only
   * on first load, and only when a hash was actually asked for, so it never fights the reader. */
  if (location.hash.length > 1) {
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target) {
      const land = () => target.scrollIntoView({ block: "start", behavior: "auto" });
      land();
      // Fonts change every line height above the target, so the first answer is usually wrong.
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(land);
      addEventListener("load", land, { once: true });
    }
  }

  /* ---------- nav hairline ----------
   *
   * A sentinel at the top of the document, watched by the same mechanism. The alternative is a
   * scroll listener that recomputes on every frame to decide whether one border is visible. */
  const nav = document.getElementById("nav");
  if (nav && "IntersectionObserver" in window) {
    const mark = document.createElement("div");
    mark.setAttribute("aria-hidden", "true");
    mark.style.cssText = "position:absolute;top:0;left:0;width:1px;height:1px;pointer-events:none";
    document.body.prepend(mark);
    new IntersectionObserver(
      ([e]) => nav.classList.toggle("stuck", !e.isIntersecting),
      { threshold: 0 },
    ).observe(mark);
  }
})();
