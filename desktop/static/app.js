"use strict";
const $ = id => document.getElementById(id);
const state = {source:"", project:"", query:"", kind:"", items:[], total:0, selected:null, detail:null, offset:0, matches:[], matchIndex:-1, summary:null, searchVersion:0, readerVersion:0, findVersion:0};
function stored(key, fallback=null) {try {return JSON.parse(localStorage.getItem(key)) ?? fallback;} catch {return fallback;}}
function store(key, value) {try {localStorage.setItem(key,JSON.stringify(value));} catch {}}
const preferences = stored("history.preferences",{});
Object.assign(state,{role:preferences.role || "",dateFrom:preferences.dateFrom || "",dateTo:preferences.dateTo || "",restoring:false,restoreAttempted:false,stopped:false});
let draftSettings = {auto_discover:true,sources:[]};
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const projectName = path => path ? path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path : "未记录项目";
const shortEnv = value => value.startsWith("WSL") ? value.replace(" · ", " / ") : value;
const dateLabel = timestamp => new Date(timestamp * 1000).toLocaleDateString("zh-CN",{month:"2-digit",day:"2-digit"});
const timeLabel = stamp => {const date = new Date(stamp); return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});};
let toastTimer, searchTimer, findTimer, previousRunning = true, summarySignature = "";

function savePreferences() {
  store("history.preferences",{role:state.role,dateFrom:state.dateFrom,dateTo:state.dateTo,showTools:$("show-tools").checked});
}
function saveReadingPosition() {
  if (state.restoring || !state.selected || state.detail?.id !== state.selected) return;
  const readerRect = $("reader").getBoundingClientRect();
  const articles = [...$("messages").querySelectorAll(".message")];
  const anchor = articles.find(article => article.getBoundingClientRect().bottom > readerRect.top+65) || articles[0];
  const positions = stored("history.positions",{});
  positions[state.selected] = {offset:state.offset,position:anchor ? Number(anchor.id.replace("message-","")) : state.offset,delta:anchor ? anchor.getBoundingClientRect().top-readerRect.top : 0,updated:Date.now()};
  const newest = Object.entries(positions).sort((a,b)=>b[1].updated-a[1].updated).slice(0,500);
  store("history.positions",Object.fromEntries(newest));
  store("history.lastSession",state.selected);
}
function restoreReadingPosition(saved) {
  const anchor = $(`message-${saved.position}`);
  if (anchor) $("reader").scrollTop += anchor.getBoundingClientRect().top - $("reader").getBoundingClientRect().top - saved.delta;
}
function dateBoundary(value, nextDay=false) {
  const date = new Date(value+"T00:00:00");
  if (nextDay) date.setDate(date.getDate()+1);
  return String(date.getTime()/1000);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "读取失败");
  return value;
}
function toast(message) {
  $("toast").textContent = message; $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 2600);
}
function terms(query) {return [...query.matchAll(/"([^"]+)"|(\S+)/g)].map(match => match[1] || match[2]).slice(0,12);}
function highlightedText(text, query) {
  const words = terms(query).filter(Boolean).sort((a,b) => b.length-a.length);
  if (!words.length) return escapeHtml(text);
  const expression = new RegExp(words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join("|"),"gi");
  let result = "", position = 0;
  for (const match of text.matchAll(expression)) {
    result += escapeHtml(text.slice(position,match.index)) + "<mark>" + escapeHtml(match[0]) + "</mark>";
    position = match.index + match[0].length;
  }
  return result + escapeHtml(text.slice(position));
}
function highlightBody(element, query) {
  if (!query.trim()) return;
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const span = document.createElement("span");
    span.innerHTML = highlightedText(node.textContent,query);
    if (span.querySelector("mark")) node.replaceWith(...span.childNodes);
  }
}

function renderSidebar() {
  const summary = state.summary; if (!summary) return;
  $("all-count").textContent = summary.sessions;
  $("all-sessions").classList.toggle("selected",!state.source && !state.project);
  $("sources").replaceChildren();
  // 空来源同样保留入口，用户可以看到尚未产生记录的环境。
  const sources = new Map(summary.status.sources.map(source => [source.id,{...source,source:source.id,count:0}]));
  summary.sources.forEach(source => sources.set(source.source,{...sources.get(source.source),...source}));
  for (const source of sources.values()) {
    const button = document.createElement("button");
    button.className = "nav-item" + (state.source === source.source ? " selected" : "");
    button.innerHTML = `<span class="source-indicator ${source.provider === "Claude Code" ? "claude" : ""}"></span><span class="source-name">${escapeHtml(source.provider)}<span class="source-environment">${escapeHtml(shortEnv(source.environment))}</span></span><span class="count">${source.count}</span>`;
    button.title = source.path || source.environment;
    button.onclick = () => {state.source = state.source === source.source ? "" : source.source; runSearch();renderSidebar();};
    $("sources").append(button);
  }
  renderProjects();
}
function renderProjects() {
  if (!state.summary) return;
  const filter = $("project-filter").value.toLowerCase();
  $("project-count").textContent = state.summary.projects.length;
  $("projects").replaceChildren();
  for (const project of state.summary.projects.filter(item => item.project.toLowerCase().includes(filter))) {
    const button = document.createElement("button");
    button.className = "project-item" + (project.project === state.project && state.project ? " selected" : "");
    button.innerHTML = `<span>${escapeHtml(projectName(project.project))}</span><span class="count">${project.count}</span>`;
    button.title = project.project || "未记录项目";
    button.onclick = () => {state.project = state.project === project.project ? "" : project.project;renderSidebar();runSearch();};
    $("projects").append(button);
  }
}
function renderList() {
  $("session-list").replaceChildren();
  const source = state.summary?.sources.find(item => item.source === state.source);
  $("list-title").textContent = state.project ? projectName(state.project) : source ? source.provider : "全部会话";
  $("result-count").textContent = `${state.total.toLocaleString()} 段${state.query ? "匹配的" : ""}会话 · 最近更新在前`;
  const filters = [state.query ? `“${state.query}”` : "", state.source ? source?.environment : "", state.project ? "已筛选项目" : "",state.role ? $("role-filter").selectedOptions[0].textContent : "",state.dateFrom||state.dateTo ? `${state.dateFrom||"不限"} 至 ${state.dateTo||"不限"}` : ""].filter(Boolean);
  $("active-filters").hidden = !filters.length;
  $("filter-description").textContent = filters.join(" · ");
  for (const item of state.items) {
    const button = document.createElement("button");
    button.className = "session-item" + (item.id === state.selected ? " selected" : "");
    button.dataset.id = item.id;
    button.innerHTML = `<div class="session-topline"><span class="provider-badge ${item.provider === "Claude Code" ? "claude" : ""}">${escapeHtml(item.provider)}</span><span>${escapeHtml(item.environment.startsWith("WSL") ? "WSL" : item.environment)}</span><time title="${escapeHtml(new Date(item.activity*1000).toLocaleString("zh-CN"))}">${dateLabel(item.activity)}</time></div><h3 class="session-title">${highlightedText(item.title,state.query)}</h3><p class="session-snippet">${highlightedText(item.snippet,state.query)}</p><div class="session-bottom"><span class="project-name">${escapeHtml(projectName(item.project))}</span>${item.archived ? '<span>已归档</span>' : ""}${item.kind !== "chat" ? `<span>${item.kind === "subagent" ? "子会话" : "自动任务"}</span>` : ""}<span class="message-number">${item.count} 条记录</span></div>`;
    button.onclick = () => openSession(item);
    $("session-list").append(button);
  }
  if (!state.items.length) $("session-list").innerHTML = `<div class="empty-list"><strong>${state.summary?.status.running ? "正在读取会话" : "没有找到会话"}</strong>${state.query || state.source || state.project ? "试试其他关键词，或清除筛选条件。" : state.summary?.status.running ? "首次扫描需要一点时间，记录会陆续出现。" : "点击“数据源与索引”检查会话目录。"}</div>`;
  $("more-sessions").hidden = state.items.length >= state.total;
}
async function runSearch(append=false) {
  const version = ++state.searchVersion;
  const params = new URLSearchParams({q:state.query,source:state.source,project:state.project,kind:state.kind,role:state.role,offset:append ? state.items.length : 0});
  if (state.dateFrom) params.set("after",dateBoundary(state.dateFrom));
  if (state.dateTo) params.set("before",dateBoundary(state.dateTo,true));
  try {
    const result = await api("/api/sessions?" + params);
    if (version !== state.searchVersion) return;
    state.items = append ? state.items.concat(result.items) : result.items;
    state.total = result.total;
    const scroll = $("session-list").scrollTop;
    renderList(); $("session-list").scrollTop = append ? scroll : 0;
  } catch(error) {toast(error.message);}
}

async function openSession(item) {
  saveReadingPosition();
  const saved = stored("history.positions",{})[item.id];
  state.restoring = true;
  state.selected = item.id; state.offset = 0; state.matches = []; state.matchIndex = -1;
  state.findVersion++;
  $("find").value = state.query;
  $("reader-content").hidden = false; $("reader-empty").hidden = true;
  document.querySelector(".workbench").classList.add("reading");
  renderList();
  try {
    await loadPage(state.query ? Math.floor((item.match_position||0)/100)*100 : saved?.offset || 0);
    if (state.selected === item.id) {
      await findMatches(Boolean(state.query));
      if (!state.query && saved) restoreReadingPosition(saved);
      state.restoring = false;
      saveReadingPosition();
    }
  } catch(error) {state.restoring=false;toast(error.message);}
}
async function loadPage(offset=0, position=null) {
  const identity = state.selected; if (!identity) return;
  const version = ++state.readerVersion;
  const detail = await api(`/api/sessions/${identity}?offset=${offset}`);
  if (version !== state.readerVersion || identity !== state.selected) return;
  state.detail = detail; state.offset = offset;
  $("reader-title").textContent = detail.title;
  $("reader-source").innerHTML = `<span class="source-indicator ${detail.provider === "Claude Code" ? "claude" : ""}"></span>${escapeHtml(detail.provider)}<span> / </span>${escapeHtml(detail.environment)}${detail.archived ? " · 已归档" : ""}`;
  $("reader-project").textContent = detail.project || "未记录项目目录";
  $("reader-project").title = detail.path;
  $("message-total").textContent = `${detail.count.toLocaleString()} 条记录`;
  $("export").href = `/api/sessions/${identity}/export`;
  renderMessages();
  $("reader").scrollTop = 0;
  if (position !== null) scrollToMessage(position);
}
function renderMessages() {
  if (!state.detail) return;
  const query = $("find").value;
  $("messages").replaceChildren();
  const showTools = $("show-tools").checked;
  let visible = 0;
  for (const message of state.detail.messages) {
    const isMatch = state.matches.includes(message.position);
    if (message.role === "tool" && !showTools && !isMatch) continue;
    visible++;
    const article = document.createElement("article");
    article.id = `message-${message.position}`;
    article.className = `message ${message.role}${isMatch ? " matched" : ""}`;
    if (message.role === "tool") {
      const details = document.createElement("details"); details.open = isMatch;
      const summary = document.createElement("summary"); summary.textContent = "工具记录 · " + message.text.split("\n")[0].slice(0,110);
      const pre = document.createElement("pre"); pre.textContent = message.text;
      details.append(summary,pre); article.append(details); highlightBody(pre,query);
    } else {
      const label = message.role === "user" ? "我" : state.detail.provider;
      article.innerHTML = `<div class="message-heading"><span class="role-symbol">${message.role === "user" ? "我" : "✳"}</span><span class="role-label ${message.role}">${label}</span><time>${escapeHtml(timeLabel(message.timestamp))}</time><button class="copy-message">复制</button></div><div class="message-body">${message.html}</div>`;
      article.querySelector(".copy-message").onclick = () => copyText(message.text,"已复制这条消息");
      highlightBody(article.querySelector(".message-body"),query);
    }
    $("messages").append(article);
  }
  if (!visible) $("messages").innerHTML = '<div class="empty-list">这一页是工具记录，勾选“显示工具记录”查看，或继续翻页。</div>';
  $("previous-page").disabled = state.offset === 0;
  $("next-page").disabled = state.offset + 100 >= state.detail.count;
  $("page-info").textContent = `${Math.min(state.offset+1,state.detail.count)}–${Math.min(state.offset+100,state.detail.count)} / ${state.detail.count}`;
  renderMatchCount();
}
async function findMatches(jump=false) {
  const identity = state.selected; if (!identity) return;
  const query = $("find").value;
  const version = ++state.findVersion;
  try {
    const result = await api(`/api/sessions/${identity}/matches?q=${encodeURIComponent(query)}&role=${encodeURIComponent(state.role)}`);
    if (version !== state.findVersion || identity !== state.selected || query !== $("find").value) return;
    state.matches = result.positions; state.matchIndex = result.positions.length ? 0 : -1;
    renderMessages();
    if (jump && state.matches.length) await jumpMatch(0);
  } catch(error) {toast(error.message);}
}
function renderMatchCount() {
  $("match-count").textContent = $("find").value.trim() ? (state.matches.length ? `${state.matchIndex+1} / ${state.matches.length}` : "无匹配") : "";
  $("previous-match").disabled = $("next-match").disabled = !state.matches.length;
}
function scrollToMessage(position) {
  document.querySelectorAll(".current-match").forEach(element => element.classList.remove("current-match"));
  const target = $(`message-${position}`);
  if (target) {target.classList.add("current-match");target.scrollIntoView({block:"start"});}
}
async function jumpMatch(direction) {
  if (!state.matches.length) return;
  state.matchIndex = (state.matchIndex + direction + state.matches.length) % state.matches.length;
  const position = state.matches[state.matchIndex];
  if (position < state.offset || position >= state.offset + 100) await loadPage(Math.floor(position/100)*100,position);
  else scrollToMessage(position);
  renderMatchCount();
}
async function copyText(text,message) {
  try {await navigator.clipboard.writeText(text);toast(message);} catch {toast("复制失败，请检查浏览器剪贴板权限");}
}
function renderSourceDialog() {
  const summary = state.summary; if (!summary) return;
  $("source-details").innerHTML = summary.status.sources.map(source => `<div class="source-detail"><strong>${escapeHtml(source.provider)}</strong><span>${escapeHtml(source.environment)}</span><span class="source-state ${source.available ? "" : "offline"}">${source.available ? `${source.files} 个文件` : "目录不可用，保留缓存"}</span><code>${escapeHtml(source.path)}</code><span class="source-updated">上次同步：${source.updated ? escapeHtml(new Date(source.updated*1000).toLocaleString("zh-CN")) : "尚未成功同步"}</span></div>`).join("");
  $("warnings").replaceChildren();
  for (const warning of summary.status.warnings) {const p=document.createElement("p");p.textContent=warning;$("warnings").append(p);}
  $("index-totals").textContent = `${summary.sessions.toLocaleString()} 段会话 · ${summary.messages.toLocaleString()} 条记录`;
}
async function pollSummary() {
  if (state.stopped) return;
  try {
    const summary = await api("/api/summary"); state.summary = summary;
    const running = summary.status.running;
    const signature = JSON.stringify([summary.sessions,summary.messages,summary.status.sources,summary.status.updated,running]);
    if (signature !== summarySignature) {renderSidebar();renderSourceDialog();await runSearch();summarySignature=signature;}
    if (!state.restoreAttempted && !state.selected) {
      state.restoreAttempted=true;
      const previous=stored("history.lastSession");
      if (previous) {
        try {const item=state.items.find(item=>item.id===previous)||await api(`/api/sessions/${previous}`);await openSession(item);} catch {store("history.lastSession",null);}
      }
    }
    $("index-status").textContent = running ? `正在扫描 · ${summary.status.scanned} 个文件` : summary.status.warnings.length ? `索引完成 · ${summary.status.warnings.length} 条提示` : `已索引 ${summary.sessions.toLocaleString()} 段会话`;
    $("index-dot").className = "status-dot" + (running ? " working" : summary.status.warnings.length ? " error" : "");
    $("refresh").disabled = $("dialog-refresh").disabled = running;
    if (previousRunning && !running && summary.sessions) toast(`索引已就绪 · ${summary.sessions} 段会话`);
    previousRunning = running;
  } catch(error) {$("index-status").textContent="服务连接中断，请检查启动窗口";$("index-dot").className="status-dot error";}
  setTimeout(pollSummary,state.summary?.status.running ? 1800 : 7000);
}
async function refresh() {
  try {await api("/api/refresh",{method:"POST",headers:{"X-History-Request":"1"}});toast("正在检查新增和更新的会话");} catch(error) {toast(error.message);}
}
$("search").addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.query=$("search").value;runSearch();},240);});
$("project-filter").addEventListener("input",renderProjects);
$("kind-filter").onchange=()=>{state.kind=$("kind-filter").value;runSearch();};
$("role-filter").value=state.role;
$("date-from").value=state.dateFrom;$("date-to").value=state.dateTo;
$("show-tools").checked=Boolean(preferences.showTools);
$("role-filter").onchange=()=>{state.role=$("role-filter").value;savePreferences();runSearch();if(state.selected)findMatches(true);};
for (const id of ["date-from","date-to"]) $(id).onchange=()=>{
  if ($("date-from").value && $("date-to").value && $("date-from").value>$("date-to").value) {toast("开始日期不能晚于结束日期");$(id).value=id==="date-from"?state.dateFrom:state.dateTo;return;}
  state.dateFrom=$("date-from").value;state.dateTo=$("date-to").value;savePreferences();runSearch();
};
$("all-sessions").onclick=()=>{state.source="";state.project="";renderSidebar();runSearch();};
$("clear-filters").onclick=()=>{clearTimeout(searchTimer);state.source="";state.project="";state.query="";state.role="";state.dateFrom="";state.dateTo="";$("search").value="";$("role-filter").value="";$("date-from").value="";$("date-to").value="";savePreferences();renderSidebar();runSearch();};
$("more-sessions").onclick=()=>runSearch(true);
$("refresh").onclick=$("dialog-refresh").onclick=refresh;
$("source-info").onclick=async()=>{renderSourceDialog();$("sources-dialog").showModal();try{draftSettings=await api("/api/settings");$("auto-discover").checked=draftSettings.auto_discover;$("settings-feedback").textContent="";renderCustomSources();}catch(error){toast(error.message);}};
$("close-dialog").onclick=()=>$("sources-dialog").close();
$("sources-dialog").onclick=event=>{if(event.target===$("sources-dialog")){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)event.target.close();}};
$("show-tools").onchange=()=>{savePreferences();renderMessages();};
$("find").addEventListener("input",()=>{clearTimeout(findTimer);findTimer=setTimeout(()=>findMatches(true),250);});
$("find").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();jumpMatch(event.shiftKey?-1:1);}});
$("previous-match").onclick=()=>jumpMatch(-1);$("next-match").onclick=()=>jumpMatch(1);
$("previous-page").onclick=()=>loadPage(Math.max(0,state.offset-100)).catch(error=>toast(error.message));
$("next-page").onclick=()=>loadPage(state.offset+100).catch(error=>toast(error.message));
$("copy-path").onclick=()=>{if(state.detail)copyText(state.detail.path,"已复制原始会话文件路径");};
$("back-list").onclick=()=>document.querySelector(".workbench").classList.remove("reading");
document.addEventListener("keydown",event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="k"){event.preventDefault();$("search").focus();$("search").select();}if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="f"&&state.selected){event.preventDefault();$("find").focus();$("find").select();}});
let readingTimer;
$("reader").addEventListener("scroll",()=>{clearTimeout(readingTimer);readingTimer=setTimeout(saveReadingPosition,180);},{passive:true});
window.addEventListener("pagehide",saveReadingPosition);

function settingsFeedback(message,error=false) {$("settings-feedback").textContent=message;$("settings-feedback").className=error?"error":"";}
function renderCustomSources() {
  $("custom-sources").replaceChildren();
  draftSettings.sources.forEach((source,index)=>{
    const row=document.createElement("div");row.className="custom-source";
    row.innerHTML=`<input type="checkbox" aria-label="启用此数据源" ${source.enabled!==false?"checked":""}><div class="custom-description">${escapeHtml(source.provider)} · ${escapeHtml(source.environment)}<code>${escapeHtml(source.home)}</code></div><button class="text-button">移除</button>`;
    row.querySelector("input").onchange=event=>{source.enabled=event.target.checked;};
    row.querySelector("button").onclick=()=>{draftSettings.sources.splice(index,1);renderCustomSources();settingsFeedback("已从列表移除，保存设置后生效");};
    $("custom-sources").append(row);
  });
  const distros=[...new Set((state.summary?.status.sources||[]).filter(source=>source.environment.startsWith("WSL · ")).map(source=>source.environment.slice(6)))];
  $("wsl-distros").innerHTML=distros.map(distro=>`<option value="${escapeHtml(distro)}"></option>`).join("");
}
$("custom-environment").onchange=()=>{const wsl=$("custom-environment").value==="WSL";$("distro-field").hidden=!wsl;$("custom-home").placeholder=wsl?"例如 /home/me/.codex-work":"例如 D:\\历史备份\\.codex";};
$("add-source").onclick=()=>{
  const provider=$("custom-provider").value;
  const wsl=$("custom-environment").value==="WSL";
  const distro=$("custom-distro").value.trim();
  const home=$("custom-home").value.trim();
  if (!home || (wsl && !distro)) return settingsFeedback("请填写数据目录和 WSL 发行版名称",true);
  const environment=wsl?`WSL · ${distro}`:"Windows";
  if (draftSettings.sources.some(source=>source.provider===provider&&source.environment===environment&&source.home===home)) return settingsFeedback("列表中已存在这个数据源",true);
  draftSettings.sources.push({provider,environment,home,enabled:true});renderCustomSources();$("custom-home").value="";settingsFeedback("已添加至列表，点击“保存设置”开始扫描");
};
$("save-settings").onclick=async()=>{
  $("save-settings").disabled=true;
  try {
    const value={...draftSettings,auto_discover:$("auto-discover").checked};
    draftSettings=await api("/api/settings",{method:"POST",headers:{"X-History-Request":"1","Content-Type":"application/json"},body:JSON.stringify(value)});
    renderCustomSources();settingsFeedback("设置已保存，正在更新索引");summarySignature="";toast("数据源设置已保存");
  } catch(error) {settingsFeedback(error.message,true);} finally {$("save-settings").disabled=false;}
};
$("quit-app").onclick=async()=>{try{saveReadingPosition();await api("/api/shutdown",{method:"POST",headers:{"X-History-Request":"1"}});state.stopped=true;$("sources-dialog").close();$("index-status").textContent="工具已退出，可重新双击程序启动";$("index-dot").className="status-dot";toast("本机服务已关闭");}catch(error){toast(error.message);}};
pollSummary();
