let state, layout, particles = [], incident = null, decision = null, previousRoute = [];
let lastFrame = 0, rerouteUntil = 0, liveSocket = null, reconnectTimer = null;
const canvas = document.getElementById("flow-canvas"), context = canvas.getContext("2d"), $ = (id) => document.getElementById(id);
const logicalWidth = 840, logicalHeight = 510;

async function api(path, options = {}) { const response = await fetch(path, {headers:{"Content-Type":"application/json"}, ...options}); if (!response.ok) throw new Error((await response.json()).detail); return response.json(); }
const densityColor = (density) => `hsl(${Math.round(145 - density * 145)} 82% 60%)`;
const label = (text) => text.replaceAll("_", " ");
const key = (a,b) => [a,b].sort().join(" <-> ");
function resizeCanvas() { const ratio = window.devicePixelRatio || 1, width = canvas.clientWidth; canvas.width = Math.round(width * ratio); canvas.height = Math.round(width * (logicalHeight / logicalWidth) * ratio); context.setTransform(canvas.width / logicalWidth, 0, 0, canvas.height / logicalHeight, 0, 0); }
window.addEventListener("resize", resizeCanvas);
function nodes() { return new Map(layout.nodes.map(n => [n.id, n])); }
function densityForEdge(edge) { return edge.zone ? state.zone_congestion[edge.zone] || .1 : .08; }
function routePoints(route = decision?.route || []) { const n = nodes(); return route.map(id => n.get(id)).filter(Boolean); }
function createParticles() { const n = nodes(); particles = []; layout.edges.forEach((edge, edgeIndex) => { const amount = Math.max(2, Math.round(3 + densityForEdge(edge) * 35)); for (let i=0;i<amount;i++) particles.push({edgeIndex, offset:Math.random(), speed:(.09 + Math.random()*.16) * (1 + densityForEdge(edge)), forward:Math.random()>.5, radius:1.3 + Math.random()*2.2}); }); $("particle-count").textContent = particles.length; }
function strokeRoute(route, color, width, dash = []) { if (route.length < 2) return; context.save(); context.strokeStyle=color; context.lineWidth=width; context.lineCap="round"; context.lineJoin="round"; context.setLineDash(dash); context.beginPath(); context.moveTo(route[0].x,route[0].y); for(let i=1;i<route.length;i++) context.lineTo(route[i].x,route[i].y); context.stroke(); context.restore(); }
function draw(delta) {
  if (!state || !layout) return; const n = nodes(); context.clearRect(0,0,logicalWidth,logicalHeight); context.fillStyle="#f7faf7"; context.fillRect(0,0,logicalWidth,logicalHeight);
  layout.edges.forEach(edge => { const a=n.get(edge.source),b=n.get(edge.destination), closed=state.closed_corridors.includes(key(edge.source,edge.destination)); context.lineWidth=closed?5:7; context.strokeStyle=closed?"#ad5353":"#b8c7bf"; context.setLineDash(closed?[10,8]:[]); context.beginPath();context.moveTo(a.x,a.y);context.lineTo(b.x,b.y);context.stroke(); }); context.setLineDash([]);
  particles.forEach(p => { const edge=layout.edges[p.edgeIndex],a=n.get(edge.source),b=n.get(edge.destination),d=densityForEdge(edge); p.offset += (p.forward?1:-1)*p.speed*delta; if(p.offset>1){p.offset=1;p.forward=false} if(p.offset<0){p.offset=0;p.forward=true} const x=a.x+(b.x-a.x)*p.offset,y=a.y+(b.y-a.y)*p.offset; context.beginPath();context.fillStyle=densityColor(d);context.globalAlpha=.55+d*.45;context.arc(x,y,p.radius,0,Math.PI*2);context.fill(); }); context.globalAlpha=1;
  if (performance.now() < rerouteUntil) strokeRoute(previousRoute,"#ad5353",6,[12,10]); const activeRoute = routePoints(); strokeRoute(activeRoute,"#36766f",5); activeRoute.slice(1,-1).forEach(point => { context.fillStyle="#36766f"; context.beginPath(); context.arc(point.x,point.y,4,0,Math.PI*2); context.fill(); });
  layout.nodes.forEach(node => { const isIncident=incident?.location===node.id, isKaaba=node.id==="kaaba_tawaf", isSelectedGate=decision?.selected_gate===node.id; context.beginPath(); context.fillStyle=node.kind==="gate"?"#eff7f5":isIncident?"#f9e9e9":isKaaba?"#eee4cf":"#fff"; context.strokeStyle=isSelectedGate?"#a86e25":node.kind==="gate"?"#36766f":isIncident?"#ad5353":"#789087"; context.lineWidth=isSelectedGate?4:2; context.arc(node.x,node.y,node.kind==="gate"?12:isKaaba?14:9,0,Math.PI*2); context.fill();context.stroke(); });
  drawNodeLabels();
}
// Nodes sit close together on the schematic, so a label parked above every one
// of them collided with its neighbours. Each label takes the first free slot
// around its node, and important nodes choose first.
function nodeRadius(node) { return node.kind === "gate" ? 12 : node.id === "kaaba_tawaf" ? 14 : 9; }
function labelSlots(node, width) {
  const r = nodeRadius(node) + 7, d = Math.round(r * 0.75);
  // Four sides first, then the diagonals, which is what a dense corner of the
  // graph needs when every side is crossed by a corridor.
  return [
    { x: node.x, y: node.y - r - 4, align: "center" },
    { x: node.x, y: node.y + r + 12, align: "center" },
    { x: node.x - r, y: node.y + 4, align: "right" },
    { x: node.x + r, y: node.y + 4, align: "left" },
    { x: node.x - d, y: node.y - d - 4, align: "right" },
    { x: node.x + d, y: node.y - d - 4, align: "left" },
    { x: node.x - d, y: node.y + d + 10, align: "right" },
    { x: node.x + d, y: node.y + d + 10, align: "left" },
  ].map(slot => ({ ...slot, box: labelBox(slot, width) }));
}
function labelBox(slot, width) {
  const left = slot.align === "center" ? slot.x - width / 2 : slot.align === "right" ? slot.x - width : slot.x;
  return { x: left - 4, y: slot.y - 11, w: width + 8, h: 15 };
}
const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
// A slot also has to miss the corridors themselves: a label parked above a node
// whose corridor arrives from above sat right on top of that line.
function segmentHitsBox(p, q, box) {
  const { x, y, w, h } = box;
  if (Math.max(p.x, q.x) < x || Math.min(p.x, q.x) > x + w) return false;
  if (Math.max(p.y, q.y) < y || Math.min(p.y, q.y) > y + h) return false;
  const side = (cx, cy) => (q.x - p.x) * (cy - p.y) - (q.y - p.y) * (cx - p.x);
  const corners = [side(x, y), side(x + w, y), side(x, y + h), side(x + w, y + h)];
  return !(corners.every(v => v > 0) || corners.every(v => v < 0));
}
function slotHitsAnyEdge(box, nodeMap) {
  return layout.edges.some(edge => {
    const a = nodeMap.get(edge.source), b = nodeMap.get(edge.destination);
    return a && b && segmentHitsBox(a, b, box);
  });
}
function drawNodeLabels() {
  context.font = "12px system-ui";
  const nodeMap = nodes();
  const ranked = [...layout.nodes].sort((a, b) =>
    priority(b) - priority(a) || a.id.localeCompare(b.id));
  const taken = [];
  for (const node of ranked) {
    const text = label(node.id), width = context.measureText(text).width;
    const slots = labelSlots(node, width);
    const free = candidate => !taken.some(box => overlaps(box, candidate.box));
    // Prefer a slot that clears both the other labels and the corridors; fall
    // back to one that at least clears the other labels.
    const slot = slots.find(c => free(c) && !slotHitsAnyEdge(c.box, nodeMap))
      || slots.find(free) || slots[0];
    taken.push(slot.box);
    context.fillStyle = "#f7faf7d9";
    context.fillRect(slot.box.x, slot.box.y, slot.box.w, slot.box.h);
    context.fillStyle = node.id === incident?.location ? "#ad5353" : "#3f5048";
    context.textAlign = slot.align;
    context.fillText(text, slot.x, slot.y);
  }
  context.textAlign = "center";
}
// The incident, the chosen gate and the entrances matter most on screen.
function priority(node) {
  if (incident?.location === node.id) return 3;
  if (decision?.selected_gate === node.id) return 2;
  return node.kind === "gate" ? 1 : 0;
}
function animate(now) { const delta=Math.min(.05,(now-lastFrame)/1000||0);lastFrame=now;draw(delta);requestAnimationFrame(animate); }
function updatePanels() { if (!state) return; const entries=Object.entries(state.zone_congestion), peak=entries.reduce((a,b)=>a[1]>b[1]?a:b); $("peak-zone").textContent=label(peak[0]); $("peak-density").textContent=`${Math.round(peak[1]*100)}%`; $("time").textContent=`T+${state.simulated_minutes} min · ${state.title}`; $("incident-status").textContent=incident?label(incident.location):"No active incident"; $("route-status").textContent=decision?`${label(decision.selected_gate)} · ${Math.ceil(decision.estimated_arrival_seconds/60)} min` : "Awaiting dispatch"; $("zone-bars").innerHTML=entries.map(([zone,d])=>`<div class="zone"><span>${label(zone)}</span><b>${Math.round(d*100)}%</b><div class="bar"><i style="width:${d*100}%;background:${densityColor(d)}"></i></div></div>`).join(""); }
async function applyState(nextState) { state=nextState; if (!layout || layout.template!==state.template) layout=await api("/simulation/layout"); $("template").value=state.template; createParticles(); updatePanels(); }
async function load() { await applyState(await api("/simulation/configure",{method:"POST",body:JSON.stringify({template:$("template").value,crowd_pattern:$("pattern").value,seed:Number($("seed").value)})})); }
function connectLive() { clearTimeout(reconnectTimer); const scheme=location.protocol==="https:"?"wss":"ws"; liveSocket=new WebSocket(`${scheme}://${location.host}/ws/dashboard`); liveSocket.onopen=()=>$("connection-status").textContent="Shared live state connected"; liveSocket.onmessage=message=>{ const event=JSON.parse(message.data); if(event.type==="snapshot"||event.type==="simulation_state") applyState(event.state).catch(()=>{}); if(event.type==="incident_created"){incident=event.incident;updatePanels();} if(event.type==="dispatch"||event.type==="reroute"){ if(decision&&event.type==="reroute") { previousRoute=routePoints(); rerouteUntil=performance.now()+5000; } incident=event.response.incident; decision=event.response.decision; updatePanels(); } if(event.type==="geofence_progress"&&incident?.id===event.incident.id){incident=event.incident;updatePanels();} if(event.type==="incidents_cancelled"&&incident&&event.incident_ids.includes(incident.id)){incident=decision=null;previousRoute=[];rerouteUntil=0;updatePanels();} }; liveSocket.onclose=()=>{ $("connection-status").textContent="Reconnecting shared state…"; reconnectTimer=setTimeout(connectLive,2000); }; liveSocket.onerror=()=>liveSocket.close(); }
$("apply").addEventListener("click",()=>load().catch(console.error)); $("surge").addEventListener("click",async()=>{ const entries=Object.entries(state.zone_congestion), peak=entries.reduce((a,b)=>a[1]>b[1]?a:b); await applyState(await api("/simulation/events/congestion",{method:"POST",body:JSON.stringify({zone:peak[0],density:.96})})); });
resizeCanvas(); connectLive(); api("/simulation/state").then(applyState).then(()=>requestAnimationFrame(animate));
