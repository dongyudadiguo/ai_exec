import base64
import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
INPUT_FILE = ROOT / "input.json"
AE_FILE = ROOT / "ae.py"
HOST = "127.0.0.1"
PORT = int(os.environ.get("AE_VIEWER_PORT", "8765"))
_process = None
_process_lock = threading.Lock()
_state_cache = {"mtime": None, "messages": None, "model": "", "usage": None}
_state_cache_lock = threading.Lock()
_blob_cache = {}
TOOL_PREVIEW = int(os.environ.get("AE_TOOL_PREVIEW", "800"))


def read_input():
    return json.loads(INPUT_FILE.read_text(encoding="utf-8"))


def write_input(data):
    INPUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def running():
    with _process_lock:
        return _process is not None and _process.poll() is None


def start_process():
    global _process
    with _process_lock:
        if _process is not None and _process.poll() is None:
            return False
        _process = subprocess.Popen([sys.executable, str(AE_FILE), str(INPUT_FILE)], cwd=str(ROOT))
        return True


def stop_process():
    with _process_lock:
        if _process is None or _process.poll() is not None:
            return False
        if os.name == "nt":
            _process.terminate()
        else:
            os.kill(_process.pid, signal.SIGTERM)
        return True


def simple_token_count(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]", text))
    return cjk + latin


def usage_from_messages(messages):
    total = 0
    by_role = {}
    for message in messages:
        count = simple_token_count(message)
        total += count
        role = message.get("role", "unknown")
        by_role[role] = by_role.get(role, 0) + count
    return {"estimated_total": total, "by_role": by_role}


def file_part(part):
    filename = part.get_filename() or "attachment"
    raw = part.get_payload(decode=True) or b""
    mime = part.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if mime.startswith("image/"):
        encoded = base64.b64encode(raw).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
    try:
        text = raw.decode("utf-8")
        return {"type": "text", "text": f"附件 {filename}:\n{text}"}
    except UnicodeDecodeError:
        encoded = base64.b64encode(raw).decode("ascii")
        return {"type": "text", "text": f"附件 {filename} ({mime}, base64):\n{encoded}"}


def append_user_message(text, files):
    data = read_input()
    body = data.setdefault("json", {})
    messages = body.setdefault("messages", [])
    parts = []
    if text.strip():
        parts.append({"type": "text", "text": text.strip()})
    parts.extend(files)
    if not parts:
        raise ValueError("message is empty")
    content = parts[0]["text"] if len(parts) == 1 and parts[0].get("type") == "text" else parts
    messages.append({"role": "user", "content": content})
    write_input(data)


def parse_multipart(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    header = f"Content-Type: {handler.headers.get('Content-Type', '')}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=default).parsebytes(header + body)
    text = ""
    files = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "message":
            raw = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset)
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
        elif name == "files" and part.get_filename():
            files.append(file_part(part))
    return text, files



def _blob_url(data_url):
    key = str(abs(hash(data_url)))
    _blob_cache[key] = data_url
    return f"/api/blob?id={key}"


def display_content(content):
    if isinstance(content, list):
        out = []
        for part in content:
            if part.get("type") == "image_url":
                p = dict(part)
                img = dict(p.get("image_url") or {})
                url = img.get("url", "")
                if url.startswith("data:image/"):
                    img["url"] = _blob_url(url)
                p["image_url"] = img
                out.append(p)
            else:
                out.append(part)
        return out
    if isinstance(content, str) and "data:image/" in content:
        return re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+", lambda m: _blob_url(m.group(0).replace("\n", "").replace("\r", "")), content)
    return content


def display_message(m):
    d = {"role": m.get("role", "message")}
    if "content" in m:
        d["content"] = display_content(m.get("content"))
    if m.get("role") == "tool":
        c = str(m.get("content", ""))
        d["content"] = c[:TOOL_PREVIEW] + (f"\n\n…… 已截断，完整长度 {len(c)} 字符" if len(c) > TOOL_PREVIEW else "")
        d["tool_content_length"] = len(c)
    if m.get("tool_calls"):
        d["tool_calls"] = [{"id": c.get("id"), "function": {"name": (c.get("function") or {}).get("name", "python")}} for c in m.get("tool_calls", [])]
    return d


def load_cached():
    st = INPUT_FILE.stat()
    mtime = st.st_mtime
    with _state_cache_lock:
        if _state_cache["mtime"] != mtime or _state_cache["messages"] is None:
            data = read_input()
            body = data.get("json", {})
            messages = body.get("messages", [])
            _state_cache.update({"mtime": mtime, "messages": messages, "model": body.get("model", ""), "usage": None})
        return mtime, _state_cache["model"], _state_cache["messages"]


def state_payload(light_if_unchanged=False, since=None, after=None):
    mtime, model, messages = load_cached()
    is_running = running()
    if light_if_unchanged and since is not None and mtime <= since:
        return {"unchanged": True, "running": is_running, "updated": mtime, "count": len(messages)}
    reset = after is None or after < 0 or after > len(messages)
    selected = messages if reset else messages[after:]
    return {
        "running": is_running,
        "model": model,
        "messages": [display_message(m) for m in selected],
        "updated": mtime,
        "count": len(messages),
        "offset": 0 if reset else after,
        "reset": reset,
    }


def usage_payload():
    mtime, model, messages = load_cached()
    with _state_cache_lock:
        if _state_cache.get("usage") is None:
            _state_cache["usage"] = usage_from_messages(messages)
        usage = _state_cache["usage"]
    return {"updated": mtime, "usage": usage}


PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>input.json 查看器</title>
<style>
:root{color-scheme:light;--bg:#f7f7f4;--panel:#ffffff;--text:#202124;--muted:#62676f;--line:#d8dadd;--accent:#0f766e;--danger:#b42318;--tool:#eef2f6;--user:#e8f3ee;--assistant:#fff;--system:#f4efe6}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}.app{min-height:100vh;padding:16px 16px 150px}.top{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:12px;padding:10px 12px;margin:-16px -16px 14px;background:rgba(247,247,244,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}h1{font-size:17px;margin:0;font-weight:650}.pill{border:1px solid var(--line);background:#fff;border-radius:999px;padding:5px 10px;color:var(--muted);font-size:12px}.usage{margin-left:auto;position:relative}.usage button{border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 10px;color:var(--text);cursor:pointer}.usage-pop{display:none;position:absolute;right:0;top:36px;width:230px;background:#fff;border:1px solid var(--line);box-shadow:0 12px 35px rgba(0,0,0,.12);border-radius:8px;padding:10px}.usage.open .usage-pop{display:block}.messages{max-width:980px;margin:0 auto;display:flex;flex-direction:column;gap:10px}.msg{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:10px 12px}.msg.user{background:var(--user)}.msg.system{background:var(--system)}.role{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;margin-bottom:6px;text-transform:uppercase;letter-spacing:0}.content{overflow-wrap:anywhere}.content p{margin:0 0 8px}.content p:last-child{margin-bottom:0}.content h1,.content h2,.content h3,.content h4,.content h5,.content h6{margin:10px 0 6px;line-height:1.25}.content h1{font-size:22px}.content h2{font-size:18px}.content h3{font-size:16px}.content h4{font-size:15px}.content h5{font-size:14px}.content h6{font-size:13px;color:var(--muted)}.content ul,.content ol{margin:6px 0 8px 22px;padding:0}.content li>ul,.content li>ol{margin-top:4px;margin-bottom:4px}.content table{border-collapse:collapse;margin:8px 0;width:max-content;max-width:100%;display:block;overflow:auto}.content th,.content td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}.content th{background:#f1f3f5;font-weight:650}.content tr:nth-child(even) td{background:#fafafa}.content blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--line);background:#fafafa;color:#4f5660}.content pre{margin:8px 0;padding:10px;overflow:auto;background:#1f2328;color:#f6f8fa;border-radius:6px}.content code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}.content :not(pre)>code{background:#eef2f6;border-radius:4px;padding:1px 4px}.content img{display:block;max-width:min(100%,560px);max-height:520px;border:1px solid var(--line);border-radius:6px;margin:8px 0;background:#fff}.tool-summary{background:var(--tool);color:#49515a;border-radius:6px;padding:8px 10px;font-size:13px}.tools{display:flex;flex-direction:column;gap:6px;margin-top:8px}.composer{position:fixed;left:0;right:0;bottom:0;background:rgba(247,247,244,.95);backdrop-filter:blur(10px);border-top:1px solid var(--line);padding:12px}.composer-inner{max-width:980px;margin:0 auto;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}.drop{border:1px solid var(--line);background:#fff;border-radius:8px;padding:8px}.drop.drag{outline:2px solid var(--accent)}textarea{width:100%;min-height:58px;max-height:180px;resize:vertical;border:0;outline:0;font:inherit}.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.file{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:#fafafa}.file button{border:0;background:transparent;cursor:pointer;color:var(--muted)}button.run{height:42px;border:0;border-radius:8px;padding:0 18px;background:var(--accent);color:#fff;font-weight:650;cursor:pointer;min-width:94px}button.stop{background:var(--danger)}.hidden{display:none}.empty{max-width:980px;margin:40px auto;color:var(--muted);text-align:center}@media(max-width:700px){.app{padding-left:10px;padding-right:10px}.composer-inner{grid-template-columns:1fr}.usage-pop{right:-6px}.top{gap:8px}.pill{display:none}}
</style>
</head>
<body>
<main class="app">
  <div class="top"><h1>input.json 查看器</h1><span id="model" class="pill"></span><span id="status" class="pill"></span><div id="usage" class="usage"><button type="button">Token</button><div class="usage-pop" id="usagePop"></div></div></div>
  <section id="messages" class="messages"></section>
  <div id="empty" class="empty hidden">暂无消息</div>
</main>
<form id="composer" class="composer"><div class="composer-inner"><div id="drop" class="drop"><textarea id="message" name="message" placeholder="输入消息，或拖入/粘贴文件、图片"></textarea><div id="files" class="files"></div><input id="fileInput" name="files" type="file" multiple class="hidden"></div><button id="run" class="run" type="submit">运行</button></div></form>
<script>
const messagesEl=document.getElementById('messages'), emptyEl=document.getElementById('empty'), composer=document.getElementById('composer'), runBtn=document.getElementById('run'), msgInput=document.getElementById('message'), fileInput=document.getElementById('fileInput'), drop=document.getElementById('drop'), filesEl=document.getElementById('files'), usage=document.getElementById('usage'), usagePop=document.getElementById('usagePop');
let selectedFiles=[], isRunning=false, lastUpdated=0, messageCount=0, usageLoaded=false;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function inlineMd(text){
  return esc(text)
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/__([^_]+)__/g,'<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g,'<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}
function markdown(text){
  const lines=String(text??'').replace(/\r\n/g,'\n').split('\n');
  let html='', para=[], code=false, codeLines=[], listStack=[];
  const flushPara=()=>{if(para.length){html+=`<p>${inlineMd(para.join(' '))}</p>`;para=[]}};
  const closeLists=(to=0)=>{while(listStack.length>to){const it=listStack.pop();html+=`</li></${it.type}>`}};
  const listDepth=indent=>Math.floor(indent.replace(/\t/g,'    ').length/2);
  const openList=(type,depth,item)=>{
    flushPara();
    while(listStack.length>depth+1) closeLists(listStack.length-1);
    if(listStack.length===depth+1 && listStack[depth].type!==type) closeLists(depth);
    if(listStack.length<depth+1){html+=`<${type}>`;listStack.push({type,openLi:false})}
    const cur=listStack[listStack.length-1];
    if(cur.openLi) html+='</li>';
    html+=`<li>${inlineMd(item)}`;
    cur.openLi=true;
  };
  const isTableSep=s=>/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(s);
  const splitRow=s=>s.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
  const tableAt=i=>i+1<lines.length && lines[i].includes('|') && isTableSep(lines[i+1]);
  const readTable=i=>{
    const head=splitRow(lines[i]); let j=i+2, rows=[];
    while(j<lines.length && lines[j].trim() && lines[j].includes('|') && !/^```/.test(lines[j])) rows.push(splitRow(lines[j++]));
    let out='<table><thead><tr>'+head.map(c=>`<th>${inlineMd(c)}</th>`).join('')+'</tr></thead>';
    if(rows.length) out+='<tbody>'+rows.map(r=>'<tr>'+head.map((_,k)=>`<td>${inlineMd(r[k]||'')}</td>`).join('')+'</tr>').join('')+'</tbody>';
    return [out+'</table>', j];
  };
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    const fence=line.match(/^```/);
    if(fence){
      if(code){html+=`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`;code=false;codeLines=[]}else{flushPara();closeLists();code=true}
      continue;
    }
    if(code){codeLines.push(line);continue}
    if(!line.trim()){flushPara();closeLists();continue}
    if(tableAt(i)){flushPara();closeLists();const r=readTable(i);html+=r[0];i=r[1]-1;continue}
    let m=line.match(/^(#{1,6})\s+(.+)$/);
    if(m){flushPara();closeLists();html+=`<h${m[1].length}>${inlineMd(m[2])}</h${m[1].length}>`;continue}
    m=line.match(/^>\s?(.+)$/);
    if(m){flushPara();closeLists();html+=`<blockquote>${inlineMd(m[1])}</blockquote>`;continue}
    m=line.match(/^(\s*)([-*+])\s+(.+)$/);
    if(m){openList('ul',listDepth(m[1]),m[3]);continue}
    m=line.match(/^(\s*)\d+[.)]\s+(.+)$/);
    if(m){openList('ol',listDepth(m[1]),m[2]);continue}
    closeLists();
    para.push(line.trim());
  }
  if(code) html+=`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`;
  flushPara();closeLists();
  return html;
}
function contentHtml(content){
  if(Array.isArray(content)) return content.map(part=>part.type==='image_url'?imageHtml(part.image_url?.url):`<div class="content">${markdown(part.text||JSON.stringify(part,null,2))}</div>`).join('');
  const text=String(content??'');
  const dataImgs=[...text.matchAll(/data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+/g)].map(m=>m[0].replace(/\s/g,''));
  const cleaned=text.replace(/data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+/g,'[图片]');
  let out=`<div class="content">${markdown(cleaned)}</div>`;
  if(dataImgs.length) out+=dataImgs.map(imageHtml).join('');
  return out;
}
function imageHtml(src){return `<img src="${esc(src)}" alt="attached image">`;}
function toolSummary(m){
  if(m.role==='tool') return `<div class="msg"><div class="role">TOOL</div><div class="tool-summary">工具结果已返回，${esc(String(m.content||'').length)} 字符</div></div>`;
  if(m.tool_calls?.length) return `<div class="tools">${m.tool_calls.map(c=>`<div class="tool-summary">调用工具：${esc(c.function?.name||'python')}</div>`).join('')}</div>`;
  return '';
}
function messageHtml(m){return m.role==='tool'?toolSummary(m):`<article class="msg ${esc(m.role||'')}"><div class="role"><span>${esc(m.role||'message')}</span></div>${contentHtml(m.content)}${toolSummary(m)}</article>`}
function render(data){
  isRunning=data.running; document.getElementById('model').textContent=data.model||'model'; document.getElementById('status').textContent=isRunning?'运行中':'空闲';
  drop.classList.toggle('hidden', isRunning); runBtn.textContent=isRunning?'结束进程':'运行'; runBtn.classList.toggle('stop', isRunning);
  const msgs=data.messages||[];
  if(data.reset || data.offset===0){messagesEl.innerHTML=msgs.map(messageHtml).join('')}else if(msgs.length){messagesEl.insertAdjacentHTML('beforeend', msgs.map(messageHtml).join(''))}
  messageCount=data.count??(messageCount+msgs.length); emptyEl.classList.toggle('hidden', messageCount>0);
  if(!usageLoaded) usagePop.innerHTML='点击 Token 后计算';
}
async function poll(){try{const r=await fetch('/api/state?since='+encodeURIComponent(lastUpdated)+'&after='+encodeURIComponent(messageCount)); const data=await r.json(); if(data.unchanged){isRunning=data.running;messageCount=data.count??messageCount;document.getElementById('status').textContent=isRunning?'运行中':'空闲';runBtn.textContent=isRunning?'结束进程':'运行';runBtn.classList.toggle('stop',isRunning);drop.classList.toggle('hidden',isRunning)}else{lastUpdated=data.updated||0;usageLoaded=false;render(data)}}catch(e){document.getElementById('status').textContent='连接失败';}setTimeout(poll,isRunning?900:1800)}
function addFiles(files){const incoming=[...files].filter(Boolean);if(incoming.length){selectedFiles.push(...incoming);refreshFiles()}}
function refreshFiles(){filesEl.innerHTML=selectedFiles.map((f,i)=>`<span class="file">${esc(f.name)} <button type="button" data-i="${i}">x</button></span>`).join('')}
drop.addEventListener('click',e=>{if(e.target===drop)fileInput.click()});
drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('drag')});
drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));
drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drag');addFiles(e.dataTransfer.files)});
drop.addEventListener('paste',e=>{const files=[...e.clipboardData.files];if(files.length){e.preventDefault();addFiles(files)}});
fileInput.addEventListener('change',()=>{addFiles(fileInput.files);fileInput.value=''});
filesEl.addEventListener('click',e=>{if(e.target.dataset.i){selectedFiles.splice(Number(e.target.dataset.i),1);refreshFiles()}});
usage.querySelector('button').addEventListener('click',async()=>{usage.classList.toggle('open');if(usage.classList.contains('open')&&!usageLoaded){usagePop.innerHTML='计算中...';const r=await fetch('/api/usage');const data=await r.json();const by=data.usage?.by_role||{};usagePop.innerHTML=`<strong>估算总量：${esc(data.usage?.estimated_total||0)}</strong><br>${Object.entries(by).map(([k,v])=>`${esc(k)}: ${esc(v)}`).join('<br>')}`;usageLoaded=true}});
runBtn.addEventListener('click',async e=>{if(isRunning){e.preventDefault();await fetch('/api/stop',{method:'POST'});return}});
composer.addEventListener('submit',async e=>{e.preventDefault();if(isRunning)return;const fd=new FormData();fd.append('message',msgInput.value);selectedFiles.forEach(f=>fd.append('files',f,f.name));const r=await fetch('/api/send',{method:'POST',body:fd});if(r.ok){msgInput.value='';selectedFiles=[];refreshFiles();drop.classList.add('hidden');poll()}else alert(await r.text())});
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if sys.stderr:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            qs = parse_qs(parsed.query)
            since = None
            after = None
            try:
                since = float(qs.get("since", [""])[0])
            except ValueError:
                pass
            try:
                after = int(qs.get("after", [""])[0])
            except ValueError:
                pass
            return self.send_json(state_payload(light_if_unchanged=True, since=since, after=after))
        if path == "/api/usage":
            return self.send_json(usage_payload())
        if path == "/api/blob":
            key = parse_qs(parsed.query).get("id", [""])[0]
            data_url = _blob_cache.get(key)
            if not data_url:
                return self.send_error(404)
            m = re.match(r"data:([^;]+);base64,(.*)", data_url, re.S)
            if not m:
                return self.send_error(400)
            raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
            self.send_response(200)
            self.send_header("Content-Type", m.group(1))
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in ("/", "/index.html"):
            return self.send_text(PAGE, content_type="text/html; charset=utf-8")
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/send":
                if running():
                    return self.send_text("process is already running", 409)
                text, files = parse_multipart(self)
                append_user_message(text, files)
                start_process()
                return self.send_json({"ok": True})
            if path == "/api/stop":
                return self.send_json({"stopped": stop_process()})
        except Exception as exc:
            return self.send_text(str(exc), 500)
        self.send_error(404)


if __name__ == "__main__":
    os.chdir(ROOT)

    def port_is_free(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex((HOST, port)) != 0

    port = PORT
    while not port_is_free(port):
        port += 1

    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}"
    if sys.stdout:
        print(f"input.json viewer: {url}")
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_process()
