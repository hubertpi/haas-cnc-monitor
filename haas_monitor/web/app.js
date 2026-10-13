"use strict";
const $ = id => document.getElementById(id);
let scheduleDraft = null;
let titleRevision = 0;
let status = null, editing = null, removing = null, detailId = null, testToken = "", originalEndpoint = "";
const endpoint = () => ({host: $("host").value.trim(), port: Number($("port").value), timeout: Number($("timeout").value), protocol:$("protocol").value, device:$("device").value.trim()});
const endpointKey = value => JSON.stringify([value.host, value.port, value.timeout, value.protocol || "mtconnect", value.device || ""]);
const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; };
const when = value => value ? new Date(value).toLocaleString() : "No successful read";
async function api(path, method = "GET", body) {
  const response = await fetch(`api/${path}`, {method, headers: {"Content-Type": "application/json", "X-Monitor-CSRF": status?.csrf || "", "X-Connection-Test": testToken}, body: body ? JSON.stringify(body) : undefined});
  const result = await response.json();
  if (!response.ok) { const error = result.detail || result.error || "Request failed"; throw new Error(Array.isArray(error) ? error.map(e => `${e.loc.at(-1)}: ${e.msg}`).join("; ") : error); }
  return result;
}
function openEditor(machine = null) {
  editing = machine; testToken = ""; originalEndpoint = machine ? endpointKey(machine) : "";
  $("editor-title").textContent = machine ? "Edit machine" : "Add machine";
  for (const [field, fallback] of Object.entries({protocol:"mtconnect", device:"", host: "", port:8082, timeout:3, name:"", interval:1, fast_interval:1, diagnostic_interval:60, boost_interval:0.5, boost_minutes:10})) $(field).value = machine?.[field] ?? fallback;
  if (!$("scan-network").value) {
    const host = machine?.host || status?.machines?.[0]?.host;
    if (host && /^\d+\.\d+\.\d+\.\d+$/.test(host)) $("scan-network").value=host.split(".").slice(0,3).join(".")+".0/24";
  }
  $("device").disabled = $("protocol").value === "mdc";
  $("enabled").checked = machine?.enabled ?? true;
  $("test-result").className = "test-result";
  $("test-result").textContent = machine ? "The saved address is already configured. Test again if you change it." : "Enter an address and test to detect controller information.";
  $("form-error").textContent = ""; $("save").disabled = !machine; $("editor").showModal();
}
for (const field of ["host", "port", "timeout", "protocol", "device"]) $(field).addEventListener("input", () => {testToken = ""; $("save").disabled = endpointKey(endpoint()) !== originalEndpoint; $("test-result").textContent = "Connection settings changed. Test this address before saving."; $("test-result").className = "test-result";});
$("test").onclick = async () => {
  if (!$("host").reportValidity() || !$("port").reportValidity() || !$("timeout").reportValidity()) return;
  const tested = endpointKey(endpoint()); $("test").disabled = true; $("save").disabled = true;
  $("test-result").className = "test-result"; $("test-result").textContent = "Testing connection and reading controller information…";
  try {
    const result = await api("test", "POST", endpoint());
    if (tested !== endpointKey(endpoint())) throw new Error("Address changed during the test. Please test again.");
    testToken = result.test_token;
    const c = result.controller;
    $("test-result").textContent = `${result.simulation ? "SIMULATION — no CNC connection was made" : "Haas read succeeded"}\n${MonitorUI.controllerSummary(c)}\n${result.values_collected} collected values. Name suggested below; you can edit it.`;
    $("test-result").className = "test-result ok";
    if (!editing || !$("name").value.trim()) $("name").value = result.suggested_name;
    $("save").disabled = false;
  } catch (error) { testToken = ""; $("test-result").textContent = error.message; $("test-result").className = "test-result fail"; }
  finally { $("test").disabled = false; }
};
$("machine-form").onsubmit = async event => {
  event.preventDefault(); $("save").disabled = true; $("form-error").textContent = "";
  const value = {...endpoint(), name:$("name").value.trim(), interval:Number($("interval").value), enabled:$("enabled").checked, fast_interval:Number($("fast_interval").value), diagnostic_interval:Number($("diagnostic_interval").value), boost_interval:Number($("boost_interval").value), boost_minutes:Number($("boost_minutes").value)};
  if (editing) value.revision = editing.revision;
  try {await api(editing ? `machines/${editing.id}` : "machines", editing ? "PUT" : "POST", value); $("editor").close(); await refresh();}
  catch (error) {$("form-error").textContent = error.message;}
  finally {$("save").disabled = false;}
};
function showDetails(machine) {
  detailId = machine.id; $("details-title").textContent = machine.name;
  const body = $("details-body"); body.replaceChildren();
  body.append(text("div", `${machine.host}:${machine.port}\nConnection: ${machine.online ? "Online" : "Offline"} · Collector: ${status.mode}\nLast attempted read: ${when(machine.last_attempt)}\nLast successful data read: ${when(machine.last_success)}\nReconnect attempts: ${machine.reconnect_attempts}\nError: ${machine.error || "None"}${machine.error_code != null ? ` (${machine.error_code})` : ""}`, "diagnostic-summary"));
  body.append(text("h3", "Collected observations", "details-signals"));
  const wrap = text("div", "", "table-scroll"), table = document.createElement("table"), head = document.createElement("tr");
  for (const label of ["Signal / source", "Value", "Last read", "MQTT"]) head.append(text("th", label)); table.append(head);
  for (const [name, signal] of Object.entries(machine.signals)) {
    const row = document.createElement("tr"), source = text("td", signal.label || name.replaceAll("_"," ")); source.append(text("small", signal.source)); row.append(source, text("td", MonitorUI.formatReading(signal)), text("td", when(signal.last_read)), text("td", signal.published && signal.discovery_published ? "Acknowledged" : "Pending")); table.append(row);
  }
  wrap.append(table); body.append(wrap);
  if (Object.keys(machine.unsupported || {}).length) body.append(text("p", "Unsupported reads: " + JSON.stringify(machine.unsupported)));
  if (!$("details").open) $("details").showModal();
}
function render() {
  const title = status.ui_settings?.title || "Your machine floor";
  if ($("floor-title").textContent !== title) $("floor-title").textContent = title;
  document.title = `${title} · Haas CNC Monitor`;
  const machines = MonitorUI.sortedMachines(status.machines, uiPreferences.sort);
  $("total").textContent = machines.length; $("connected").textContent = machines.filter(m => m.online).length;
  $("disconnected").textContent = machines.filter(m => !m.online).length;
  $("mqtt").textContent = status.mqtt_connected ? "Connected" : "Disconnected";
  $("mode-banner").textContent = status.mode === "simulation" ? "SIMULATION MODE · All machine data is simulated. No CNC equipment is contacted." : "HAAS MODE · Read-only CNC monitoring";
  $("mode-banner").className = status.mode === "simulation" ? "banner" : "banner live";
  const error = status.startup_error || status.mqtt_error; $("notice").hidden = !error; $("notice").textContent = error || "";
  $("empty").hidden = machines.length !== 0; $("machines").replaceChildren();
  for (const machine of machines) {
    const card = text("article", "", "machine"), top = text("div", "", "machine-top"), title = document.createElement("div");
    title.append(text("h3", machine.name), text("span", machine.controller.model || "Controller identification pending", "model"));
    top.append(title, text("span", machine.online ? "● ONLINE" : machine.enabled ? "● OFFLINE" : "● DISABLED", "badge" + (machine.online ? "" : " off"))); card.append(top);
    const data = document.createElement("dl");
    for (const [label,value] of [["DATA ENDPOINT",`${machine.host}:${machine.port}`],["READ INTERVALS",`${machine.fast_interval}s fast / ${machine.interval}s status / ${machine.diagnostic_interval}s diagnostics`],["LAST SUCCESSFUL READ",when(machine.last_success)],["COLLECTED VALUES",Object.keys(machine.signals).length],["SHIFT OEE",status.schedule.shifts.map((shift,index)=>`${shift.name}: ${(machine.signals[`production_shift_${index+1}_utilization`]?.available === false ? "—" : machine.signals[`production_shift_${index+1}_utilization`]?.value ?? "—")}%`).join(" / ")],["READ DURATION",`${machine.read_duration_ms ?? 0} ms`]]) {const item = document.createElement("div"); item.append(text("dt",label), text("dd",value)); data.append(item);} card.append(data);
    if (machine.error) card.append(text("p", machine.error, "error"));
    const actions = text("div", "", "machine-actions");
    for (const [label,fn,cls] of [[machine.boost_remaining ? `Stop boost (${machine.boost_remaining}s)` : "Diagnostic boost",async()=>{try{await api(`machines/${machine.id}/boost`,"POST",{enabled:!machine.boost_remaining});await refresh();}catch(error){$("notice").hidden=false;$("notice").textContent=error.message;}},""],["Details",()=>showDetails(machine),""],["Edit",()=>openEditor(machine),""],["Remove",()=>{removing=machine; $("remove-name").textContent=machine.name; $("remove-error").textContent=""; $("removal").showModal();},"remove"]]) {const button=text("button",label,cls);button.onclick=fn;actions.append(button);} card.append(actions); $("machines").append(card);
  }
  renderScan();
  if ($("details").open) {const machine=machines.find(m=>m.id===detailId); if(machine)showDetails(machine); else $("details").close();}
}
async function refresh() {try {status=await api("status"); render(); $("refresh-state").textContent="Updated " + new Date().toLocaleTimeString();} catch(error) {$("notice").hidden=false;$("notice").textContent="Cannot reach collector: " + error.message;}}
function renderScan() {
  const scan = status.discovery || {};
  const configured = new Set(status.machines.map(m=>JSON.stringify([m.host,m.port])));
  $("scan-state").textContent = scan.error || (scan.total ? `${scan.simulation ? "SIMULATION · " : ""}${scan.running ? "Scanning" : scan.cancelled ? "Stopped" : "Finished"}: ${scan.checked}/${scan.total} addresses · ${scan.already_configured || 0} already configured` : "Choose a subnet to scan.");
  $("scan-start").disabled = !!scan.running; $("scan-cancel").disabled = !scan.running;
  $("scan-results").replaceChildren();
  for (const candidate of scan.candidates || []) {
    if (configured.has(JSON.stringify([candidate.host,candidate.port]))) continue;
    const button = text("button",`${candidate.host}:${candidate.port}`,"secondary"); button.type="button";
    button.onclick=()=>{ $("host").value=candidate.host;$("port").value=candidate.port;$("host").dispatchEvent(new Event("input"));$("discovery-panel").open=false;};
    $("scan-results").append(button);
  }
}
$("scan-start").onclick=async()=>{try{await api("discovery","POST",{network:$("scan-network").value.trim(),port:Number($("scan-port").value)});await refresh();}catch(error){$("scan-state").textContent=error.message;}};
$("scan-cancel").onclick=async()=>{try{await api("discovery","DELETE");await refresh();}catch(error){$("scan-state").textContent=error.message;}};
$("add").onclick=()=>openEditor(); $("empty-add").onclick=()=>openEditor();
for (const id of ["close-editor","cancel-editor"]) $(id).onclick=()=>$("editor").close();
$("close-details").onclick=()=>$("details").close(); $("cancel-remove").onclick=()=>$("removal").close();
$("confirm-remove").onclick=async()=>{ $("confirm-remove").disabled=true;try{await api(`machines/${removing.id}`,"DELETE");$("removal").close();await refresh();}catch(error){$("remove-error").textContent=error.message;}finally{$("confirm-remove").disabled=false;}};
$("export").onclick=async()=>{try{const blob=new Blob([JSON.stringify(await api("diagnostics"),null,2)],{type:"application/json"});const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download="haas-diagnostics.json";link.click();URL.revokeObjectURL(url);}catch(error){$("notice").hidden=false;$("notice").textContent=error.message;}};
function scheduleInput(label,value,type,update) {
  const wrapper=document.createElement("label");wrapper.append(text("span",label));
  const input=document.createElement("input");input.type=type;input.value=value;input.required=true;
  input.oninput=()=>update(type==="number"?Number(input.value):input.value);
  if(type==="number"){input.min="1";input.max="240";input.step="1";}
  wrapper.append(input);return wrapper;
}
function renderSchedule() {
  $("schedule-days").replaceChildren();
  ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"].forEach((day,index)=>{
    const label=text("label",day,"checkbox");const box=document.createElement("input");box.type="checkbox";box.checked=scheduleDraft.weekdays.includes(index);
    box.onchange=()=>{scheduleDraft.weekdays=scheduleDraft.weekdays.filter(d=>d!==index);if(box.checked)scheduleDraft.weekdays.push(index);};
    label.prepend(box);$("schedule-days").append(label);
  });
  $("schedule-shifts").replaceChildren();
  scheduleDraft.shifts.forEach((shift,index)=>{
    const card=text("section","","schedule-shift"),heading=text("div","","dialog-heading");
    heading.append(text("h3",`Shift ${index+1}`));
    const remove=text("button","Remove shift","secondary");remove.type="button";remove.disabled=scheduleDraft.shifts.length===1;
    remove.onclick=()=>{scheduleDraft.shifts.splice(index,1);renderSchedule();};heading.append(remove);card.append(heading);
    card.append(scheduleInput("Name",shift.name,"text",v=>shift.name=v));
    const times=text("div","","grid");times.append(scheduleInput("Starts",shift.start,"time",v=>shift.start=v),scheduleInput("Ends",shift.end,"time",v=>shift.end=v));card.append(times);
    card.append(text("h4","Breaks"));
    shift.breaks.forEach((item,breakIndex)=>{
      const row=text("div","","schedule-break");
      row.append(scheduleInput("Starts",item.start,"time",v=>item.start=v),scheduleInput("Minutes",item.minutes,"number",v=>item.minutes=v));
      const remove=text("button","Remove","secondary");remove.type="button";remove.onclick=()=>{shift.breaks.splice(breakIndex,1);renderSchedule();};row.append(remove);card.append(row);
    });
    const add=text("button","＋ Add break","secondary");add.type="button";add.disabled=shift.breaks.length>=12;
    add.onclick=()=>{shift.breaks.push({start:shift.start,minutes:10});renderSchedule();};card.append(add);$("schedule-shifts").append(card);
  });
  $("schedule-add").disabled=scheduleDraft.shifts.length>=8;
}
$("schedule-open").onclick=()=>{if(!status?.schedule)return;scheduleDraft=structuredClone(status.schedule);delete scheduleDraft.effective;$("schedule-timezone").value=scheduleDraft.timezone;$("schedule-error").textContent="";renderSchedule();$("schedule-dialog").showModal();};
for(const id of ["schedule-close","schedule-cancel"])$(id).onclick=()=>$("schedule-dialog").close();
$("schedule-add").onclick=()=>{const previous=scheduleDraft.shifts.at(-1);scheduleDraft.shifts.push({name:`Shift ${scheduleDraft.shifts.length+1}`,start:previous?.end || "06:00",end:"06:00",breaks:[]});renderSchedule();};
$("schedule-form").onsubmit=async event=>{
  event.preventDefault();$("schedule-save").disabled=true;$("schedule-error").textContent="";
  scheduleDraft.timezone=$("schedule-timezone").value.trim();
  try{await api("schedule","PUT",scheduleDraft);$("schedule-dialog").close();await refresh();}
  catch(error){$("schedule-error").textContent=error.message;}
  finally{$("schedule-save").disabled=false;}
};
$("title-edit").onclick = () => {
  $("page-title").value = status?.ui_settings?.title || "Your machine floor";
  titleRevision = status?.ui_settings?.revision || 0;
  $("title-error").textContent = ""; $("title-dialog").showModal();
};
for (const id of ["title-close", "title-cancel"]) $(id).onclick = () => $("title-dialog").close();
$("title-form").onsubmit = async event => {
  event.preventDefault(); $("title-save").disabled = true; $("title-error").textContent = "";
  try {await api("ui-settings", "PUT", {title:$("page-title").value, revision:titleRevision}); $("title-dialog").close(); await refresh();}
  catch(error) {$("title-error").textContent = error.message;}
  finally {$("title-save").disabled = false;}
};
const uiPreferences = MonitorUI.start(window, () => {if (status) render();});
refresh(); setInterval(refresh,2000);

$("protocol").addEventListener("change",()=>{$("port").value=$("protocol").value==="mdc"?5051:8082;$("device").disabled=$("protocol").value==="mdc";$("host").dispatchEvent(new Event("input"));});
