// Lights the entry in the second row for the section you are actually looking at.
//
// The bar already answers "which page am I on" without any of this — that is markup, and it works
// with JavaScript switched off. What this adds is the smaller question the long pages raise: five
// screens into `governance.html`, which of those five sections is this? A reader who cannot answer
// that scrolls back up to find out, which is the cost the highlight removes.
//
// Deliberately not a scroll handler. An IntersectionObserver fires only when a boundary is crossed,
// so a fast scroll costs a handful of callbacks rather than one per frame.
(() => {
  const links = [...document.querySelectorAll(".onpage a[href^='#']")];
  if (links.length === 0 || !("IntersectionObserver" in window)) return;

  const forId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  const sections = [...forId.keys()]
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  if (sections.length === 0) return;

  // Which sections are on screen right now. A tall section and a short one can both be visible,
  // so the set is kept rather than the latest entry: the answer is the FIRST of them in document
  // order, which is the one a reader is reading down out of.
  const visible = new Set();

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) visible.add(entry.target.id);
        else visible.delete(entry.target.id);
      }
      const current = sections.find((s) => visible.has(s.id));
      for (const [id, link] of forId) link.classList.toggle("here", !!current && id === current.id);
    },
    // The top margin clears the sticky bar, so a section counts as "reached" when its heading
    // arrives under the bar rather than behind it. The bottom margin keeps the last section from
    // lighting up while it is still a screen away.
    { rootMargin: "-120px 0px -55% 0px", threshold: 0 },
  );

  for (const section of sections) observer.observe(section);
})();
