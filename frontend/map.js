/* Shared venue schematic renderer.
 *
 * The control centre and the demo page draw the same map, and the label
 * placement below is subtle enough that keeping two copies of it guaranteed
 * they would drift.  Loaded as a plain script, so it exposes one global. */
(function (global) {
  "use strict";

  const label = (value) => value.replaceAll("_", " ");
  const edgeKey = (a, b) => [a, b].sort().join(" <-> ");
  const densityColor = (value) => `hsl(${Math.round(140 - value * 140)} 75% 56%)`;

  // A label slot has to miss the corridors themselves: one parked above a node
  // whose corridor arrives from above sat right on top of that line.
  function segmentHitsBox(p, q, box) {
    const { x, y, w, h } = box;
    if (Math.max(p.x, q.x) < x || Math.min(p.x, q.x) > x + w) return false;
    if (Math.max(p.y, q.y) < y || Math.min(p.y, q.y) > y + h) return false;
    const side = (cx, cy) => (q.x - p.x) * (cy - p.y) - (q.y - p.y) * (cx - p.x);
    const corners = [side(x, y), side(x + w, y), side(x, y + h), side(x + w, y + h)];
    return !(corners.every((v) => v > 0) || corners.every((v) => v < 0));
  }

  const overlaps = (a, b) =>
    a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

  /* The schematic packs nodes together, so labels parked above every node
   * overlapped.  Each one takes the first free slot around its node; the
   * incident, the selected gate and the entrances get first choice. */
  function placeLabels(layout, priorityOf) {
    const points = new Map(layout.nodes.map((node) => [node.id, node]));
    const hitsEdge = (box) =>
      layout.edges.some((edge) => {
        const a = points.get(edge.source), b = points.get(edge.destination);
        return a && b && segmentHitsBox(a, b, box);
      });
    const ranked = [...layout.nodes].sort(
      (a, b) => priorityOf(b) - priorityOf(a) || a.id.localeCompare(b.id)
    );
    const taken = [];
    return ranked.map((node) => {
      const text = label(node.id);
      // No text metrics in a static SVG string. Measured against the rendered
      // labels, 6.6px per character never under-estimates the 12px font by
      // more than the padding below absorbs.
      const width = text.length * 6.6, r = (node.kind === "gate" ? 13 : 10) + 8;
      const o = Math.round(r * 0.75);
      // Four sides first, then the diagonals, which is what a dense corner of
      // the graph needs when every side is crossed by a corridor.
      const slots = [
        { x: node.x, y: node.y - r, anchor: "middle" },
        { x: node.x, y: node.y + r + 9, anchor: "middle" },
        { x: node.x - r, y: node.y + 4, anchor: "end" },
        { x: node.x + r, y: node.y + 4, anchor: "start" },
        { x: node.x - o, y: node.y - o, anchor: "end" },
        { x: node.x + o, y: node.y - o, anchor: "start" },
        { x: node.x - o, y: node.y + o + 7, anchor: "end" },
        { x: node.x + o, y: node.y + o + 7, anchor: "start" },
      ].map((slot) => {
        const left = slot.anchor === "middle" ? slot.x - width / 2
          : slot.anchor === "end" ? slot.x - width : slot.x;
        return { ...slot, box: { x: left - 3, y: slot.y - 11, w: width + 6, h: 15 } };
      });
      const free = (candidate) => !taken.some((box) => overlaps(box, candidate.box));
      const slot = slots.find((c) => free(c) && !hitsEdge(c.box)) || slots.find(free) || slots[0];
      taken.push(slot.box);
      return `<text class="node-label" text-anchor="${slot.anchor}" x="${slot.x}" y="${slot.y}">${text}</text>`;
    }).join("");
  }

  /* Draw one venue schematic into an <svg> element.
   *
   * view: { layout, state, route, incidentLocation, selectedGate } — route is
   * the ordered node list of the active decision, or an empty list. */
  function draw(svg, view) {
    const { layout, state } = view;
    if (!svg || !layout || !state) return;
    const route = view.route || [];
    const nodes = new Map(layout.nodes.map((node) => [node.id, node]));
    const routePairs = new Set();
    for (let i = 0; i < route.length - 1; i++) routePairs.add(edgeKey(route[i], route[i + 1]));

    const lines = layout.edges.map((edge) => {
      const a = nodes.get(edge.source), b = nodes.get(edge.destination);
      const key = edgeKey(edge.source, edge.destination);
      const closed = state.closed_corridors.includes(key);
      const restricted = (state.restricted_corridors || []).includes(key);
      const classes = `edge${closed ? " closed" : ""}${restricted ? " restricted" : ""}`;
      return `<line class="${classes}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>`
        + (routePairs.has(key)
          ? `<line class="route" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>`
          : "");
    }).join("");

    const halos = layout.nodes.filter((n) => n.id !== "ambulance_bay").map((node) => {
      const relevant = layout.edges.find((e) => e.source === node.id || e.destination === node.id);
      const d = relevant?.zone ? state.zone_congestion[relevant.zone] || 0.2 : 0.1;
      return `<circle class="crowd-halo" cx="${node.x}" cy="${node.y}" r="${30 + d * 46}" fill="${densityColor(d)}"/>`;
    }).join("");

    const circles = layout.nodes.map((node) => {
      const marks = `${node.kind}${view.incidentLocation === node.id ? " incident" : ""}`
        + `${view.selectedGate === node.id ? " selected-gate" : ""}`;
      return `<circle class="node ${marks}" cx="${node.x}" cy="${node.y}" r="${node.kind === "gate" ? 13 : 10}"/>`;
    }).join("");

    const priorityOf = (node) => {
      if (view.incidentLocation === node.id) return 3;
      if (view.selectedGate === node.id) return 2;
      return node.kind === "gate" ? 1 : 0;
    };
    // The Hajj layout names real places inside Masjid al-Haram and draws a
    // response route across them.  Saying on the map itself what the map is
    // keeps that claim where a viewer sees it, not only in the README.
    const disclaimer = state.template === "pilgrimage_flow"
      ? `<text class="map-disclaimer" x="420" y="500" text-anchor="middle">`
        + `Demonstration model only — not an official map of Masjid al-Haram`
        + `</text>`
      : "";

    svg.innerHTML = `<rect width="840" height="510" fill="#f7faf7"/>`
      + halos + lines + circles + placeLabels(layout, priorityOf) + disclaimer;
  }

  global.RescueRouteMap = { draw, label, edgeKey, densityColor };
})(window);
