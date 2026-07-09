import base64
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
INPUT_FILE = ROOT / "input.json"
AE_FILE = ROOT / "ae.py"
HOST = "127.0.0.1"
PORT = int(os.environ.get("AE_VIEWER_PORT", "8765"))
_process = None
_process_lock = threading.Lock()


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


def state_payload():
    data = read_input()
    body = data.get("json", {})
    messages = body.get("messages", [])
    return {
        "running": running(),
        "model": body.get("model", ""),
        "messages": messages,
        "usage": usage_from_messages(messages),
        "updated": INPUT_FILE.stat().st_mtime,
    }


PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>input.json 查看器</title>
<style>
:root{color-scheme:light;--bg:#f7f7f4;--panel:#ffffff;--text:#202124;--muted:#62676f;--line:#d8dadd;--accent:#0f766e;--danger:#b42318;--tool:#eef2f6;--user:#e8f3ee;--assistant:#fff;--system:#f4efe6}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}.app{min-height:100vh;padding:16px 16px 150px}.top{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:12px;padding:10px 12px;margin:-16px -16px 14px;background:rgba(247,247,244,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}h1{font-size:17px;margin:0;font-weight:650}.pill{border:1px solid var(--line);background:#fff;border-radius:999px;padding:5px 10px;color:var(--muted);font-size:12px}.usage{margin-left:auto;position:relative}.usage button{border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 10px;color:var(--text);cursor:pointer}.usage-pop{display:none;position:absolute;right:0;top:36px;width:230px;background:#fff;border:1px solid var(--line);box-shadow:0 12px 35px rgba(0,0,0,.12);border-radius:8px;padding:10px}.usage.open .usage-pop{display:block}.messages{max-width:980px;margin:0 auto;display:flex;flex-direction:column;gap:10px}.msg{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:10px 12px}.msg.user{background:var(--user)}.msg.system{background:var(--system)}.role{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;margin-bottom:6px;text-transform:uppercase;letter-spacing:0}.content{overflow-wrap:anywhere}.content p{margin:0 0 8px}.content p:last-child{margin-bottom:0}.content h1,.content h2,.content h3{margin:10px 0 6px;line-height:1.25}.content h1{font-size:22px}.content h2{font-size:18px}.content h3{font-size:16px}.content ul,.content ol{margin:6px 0 8px 22px;padding:0}.content blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--line);background:#fafafa;color:#4f5660}.content pre{margin:8px 0;padding:10px;overflow:auto;background:#1f2328;color:#f6f8fa;border-radius:6px}.content code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}.content :not(pre)>code{background:#eef2f6;border-radius:4px;padding:1px 4px}.content img{display:block;max-width:min(100%,560px);max-height:520px;border:1px solid var(--line);border-radius:6px;margin:8px 0;background:#fff}.tool-summary{background:var(--tool);color:#49515a;border-radius:6px;padding:8px 10px;font-size:13px}.tools{display:flex;flex-direction:column;gap:6px;margin-top:8px}.composer{position:fixed;left:0;right:0;bottom:0;background:rgba(247,247,244,.95);backdrop-filter:blur(10px);border-top:1px solid var(--line);padding:12px}.composer-inner{max-width:980px;margin:0 auto;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}.drop{border:1px solid var(--line);background:#fff;border-radius:8px;padding:8px}.drop.drag{outline:2px solid var(--accent)}textarea{width:100%;min-height:58px;max-height:180px;resize:vertical;border:0;outline:0;font:inherit}.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.file{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:#fafafa}.file button{border:0;background:transparent;cursor:pointer;color:var(--muted)}button.run{height:42px;border:0;border-radius:8px;padding:0 18px;background:var(--accent);color:#fff;font-weight:650;cursor:pointer;min-width:94px}button.stop{background:var(--danger)}.hidden{display:none}.empty{max-width:980px;margin:40px auto;color:var(--muted);text-align:center}@media(max-width:700px){.app{padding-left:10px;padding-right:10px}.composer-inner{grid-template-columns:1fr}.usage-pop{right:-6px}.top{gap:8px}.pill{display:none}}
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
let selectedFiles=[], isRunning=false;
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
  let html='', list=null, para=[], code=false, codeLines=[];
  const flushPara=()=>{if(para.length){html+=`<p>${inlineMd(para.join(' '))}</p>`;para=[]}};
  const closeList=()=>{if(list){html+=`</${list}>`;list=null}};
  for(const line of lines){
    const fence=line.match(/^```/);
    if(fence){
      if(code){html+=`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`;code=false;codeLines=[]}else{flushPara();closeList();code=true}
      continue;
    }
    if(code){codeLines.push(line);continue}
    if(!line.trim()){flushPara();closeList();continue}
    let m=line.match(/^(#{1,3})\s+(.+)$/);
    if(m){flushPara();closeList();html+=`<h${m[1].length}>${inlineMd(m[2])}</h${m[1].length}>`;continue}
    m=line.match(/^>\s?(.+)$/);
    if(m){flushPara();closeList();html+=`<blockquote>${inlineMd(m[1])}</blockquote>`;continue}
    m=line.match(/^[-*+]\s+(.+)$/);
    if(m){flushPara();if(list!=='ul'){closeList();html+='<ul>';list='ul'}html+=`<li>${inlineMd(m[1])}</li>`;continue}
    m=line.match(/^\d+[.)]\s+(.+)$/);
    if(m){flushPara();if(list!=='ol'){closeList();html+='<ol>';list='ol'}html+=`<li>${inlineMd(m[1])}</li>`;continue}
    para.push(line.trim());
  }
  if(code) html+=`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`;
  flushPara();closeList();
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
function render(data){
  isRunning=data.running; document.getElementById('model').textContent=data.model||'model'; document.getElementById('status').textContent=isRunning?'运行中':'空闲';
  drop.classList.toggle('hidden', isRunning); runBtn.textContent=isRunning?'结束进程':'运行'; runBtn.classList.toggle('stop', isRunning);
  const msgs=data.messages||[]; emptyEl.classList.toggle('hidden', msgs.length>0);
  messagesEl.innerHTML=msgs.map(m=>m.role==='tool'?toolSummary(m):`<article class="msg ${esc(m.role||'')}"><div class="role"><span>${esc(m.role||'message')}</span></div>${contentHtml(m.content)}${toolSummary(m)}</article>`).join('');
  const by=data.usage?.by_role||{}; usagePop.innerHTML=`<strong>估算总量：${esc(data.usage?.estimated_total||0)}</strong><br>${Object.entries(by).map(([k,v])=>`${esc(k)}: ${esc(v)}`).join('<br>')}`;
}
async function poll(){try{const r=await fetch('/api/state'); render(await r.json());}catch(e){document.getElementById('status').textContent='连接失败';}setTimeout(poll,isRunning?900:1800)}
function addFiles(files){const incoming=[...files].filter(Boolean);if(incoming.length){selectedFiles.push(...incoming);refreshFiles()}}
function refreshFiles(){filesEl.innerHTML=selectedFiles.map((f,i)=>`<span class="file">${esc(f.name)} <button type="button" data-i="${i}">x</button></span>`).join('')}
drop.addEventListener('click',e=>{if(e.target===drop)fileInput.click()});
drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('drag')});
drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));
drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drag');addFiles(e.dataTransfer.files)});
drop.addEventListener('paste',e=>{const files=[...e.clipboardData.files];if(files.length){e.preventDefault();addFiles(files)}});
fileInput.addEventListener('change',()=>{addFiles(fileInput.files);fileInput.value=''});
filesEl.addEventListener('click',e=>{if(e.target.dataset.i){selectedFiles.splice(Number(e.target.dataset.i),1);refreshFiles()}});
usage.querySelector('button').addEventListener('click',()=>usage.classList.toggle('open'));
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
        path = urlparse(self.path).path
        if path == "/api/state":
            return self.send_json(state_payload())
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    if sys.stdout:
        print(f"input.json viewer: {url}")
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_process()
