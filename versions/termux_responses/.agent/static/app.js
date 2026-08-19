const messagesEl=document.getElementById('messages'), emptyEl=document.getElementById('empty'), composer=document.getElementById('composer'), runBtn=document.getElementById('run'), msgInput=document.getElementById('message'), fileInput=document.getElementById('fileInput'), drop=document.getElementById('drop'), filesEl=document.getElementById('files'), usageText=document.getElementById('usageText'), tokenBar=document.getElementById('tokenBar'), runnerStatus=document.getElementById('runnerStatus'), runnerLabel=document.getElementById('runnerLabel'), newMessagesBtn=document.getElementById('newMessages'), killProcessBtn=document.getElementById('killProcess'), themeToggleBtn=document.getElementById('themeToggle');
let selectedFiles=[], isRunning=false, runnerIdle=false, phaseLabel='空闲', activeChatId='default', lastUpdated=0, messageCount=0, usageLoaded=false, usageLoading=false, usageGeneration=0, usageReloadQueued=false, unseenMessages=0, firstPaint=true, editInFlight=false;
let pollTimer=null, pollInFlight=false, pollQueued=false, pollGeneration=0, pollUnchangedStreak=0;
const POLL_FAST=120, POLL_RUN=500, POLL_IDLE=1800;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function currentTheme(){
  return document.documentElement.getAttribute('data-theme')==='dark'?'dark':'light';
}
function applyTheme(theme, persist){
  const t=theme==='dark'?'dark':'light';
  document.documentElement.setAttribute('data-theme', t);
  if(persist!==false){
    try{localStorage.setItem('ae-theme', t)}catch(e){}
  }
  const btn=document.getElementById('themeToggle');
  if(btn){
    btn.title=t==='dark'?'切换为亮色主题':'切换为暗色主题';
    btn.setAttribute('aria-label', btn.title);
  }
}
function toggleTheme(){
  applyTheme(currentTheme()==='dark'?'light':'dark');
}
function enhanceCodeBlocks(root){
  if(!root||!window.hljs) return;
  root.querySelectorAll('pre code').forEach((code)=>{
    if(code.dataset.highlighted) return;
    try{hljs.highlightElement(code)}catch(e){}
  });
}
function highlightToolOutput(raw){
  const text=String(raw??'');
  if(!text) return esc('(无输出)');
  const lines=text.split('\n');
  const MAX_LINES=4000;
  const head=lines.length>MAX_LINES?lines.slice(0,MAX_LINES):lines;
  const rest=lines.length>MAX_LINES?lines.slice(MAX_LINES):null;
  const reTbHeader=/^Traceback \(most recent call last\):\s*$/;
  const reTbFile=/^\s*File "([^"]+)", line (\d+)(?:, in (.+))?\s*$/;
  const reTbCaret=/^\s*[\^~]+\s*$/;
  const reExc=/^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Error(?:Group)?(?:\s*:|\s*$)|^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Exception(?:\s*:|\s*$)/;
  const reToolErr=/^\[tool_error\]/;
  const reWarn=/^\s*(?:WARNING|WARN|Warning)\b/;
  const reOk=/^\s*(?:OK|SUCCESS|PASS(?:ED)?)\b/i;
  const rePrompt=/^(?:>>> |\.\.\. |\$ |# |> )/;
  const reSection=/^={3,}|^-{3,}\s*$|^#{1,6}\s+\S/;
  const span=(cls,s)=>`<span class="hl ${cls}">${s}</span>`;

  function highlightJsonish(line){
    let out='', i=0; const s=line;
    while(i<s.length){
      const ch=s[i];
      if(ch==='"' || ch==="'"){
        let j=i+1, escb=false;
        while(j<s.length){
          const c=s[j];
          if(escb){escb=false;j++;continue}
          if(c==='\\'){escb=true;j++;continue}
          if(c===ch){j++;break}
          j++;
        }
        const body=s.slice(i,j);
        let k=j; while(k<s.length && /\s/.test(s[k])) k++;
        out+=span(s[k]===':'?'hl-key':'hl-str', esc(body));
        i=j; continue;
      }
      const nm=/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(s.slice(i));
      if(nm){ out+=span('hl-num', esc(nm[0])); i+=nm[0].length; continue; }
      const bw=/^(?:true|false|null|True|False|None)\b/.exec(s.slice(i));
      if(bw){ out+=span('hl-bool', esc(bw[0])); i+=bw[0].length; continue; }
      out+=esc(ch); i++;
    }
    return out;
  }

  function matchAt(s, i, re){
    re.lastIndex=0;
    const m=re.exec(s.slice(i));
    return m && m.index===0 ? m[0] : null;
  }

  function inlineHighlight(line){
    if(!line) return '';
    if((line.includes('{')||line.includes('[')) && line.includes(':') && line.length<2000){
      return highlightJsonish(line);
    }
    const rules=[
      {cls:'hl-url', re:/^https?:\/\/[^\s<>"']+/},
      {cls:'hl-path', re:/^(?:[A-Za-z]:\\|\/|\.\/|\.\.\/)[^\s:*,;"'<>|]+/},
      {cls:'hl-str', re:/^(['"`])(?:\\.|(?!\1)[\s\S])*\1/},
      {cls:'hl-num', re:/^-?\d+(?:\.\d+)?/},
      {cls:'hl-bool', re:/^(?:true|false|null|True|False|None)\b/},
      {cls:'hl-kw', re:/^(?:def|class|return|import|from|raise|except|try|with|async|await|print|len|if|else|elif|for|while|in|not|and|or)\b/},
      {cls:'hl-marker', re:/^(?:ERROR|FAIL(?:ED)?|Traceback|Exception|Error)\b/},
      {cls:'hl-ok', re:/^(?:OK|SUCCESS|PASS(?:ED)?)\b/},
      {cls:'hl-warn', re:/^(?:WARNING|WARN)\b/},
    ];
    let out='', i=0;
    while(i<line.length){
      let hit=null, cls='';
      const atWord = i===0 || !/[A-Za-z0-9_]/.test(line[i-1]);
      for(const r of rules){
        if((r.cls==='hl-num'||r.cls==='hl-bool'||r.cls==='hl-kw'||r.cls==='hl-marker'||r.cls==='hl-ok'||r.cls==='hl-warn') && !atWord) continue;
        const t=matchAt(line, i, r.re);
        if(t){ hit=t; cls=r.cls; break; }
      }
      if(hit){ out+=span(cls, esc(hit)); i+=hit.length; }
      else { out+=esc(line[i]); i++; }
    }
    return out;
  }

  function highlightTbFile(line){
    const m=line.match(/^\s*File "([^"]+)", line (\d+)(?:, in (.+))?\s*$/);
    if(!m) return inlineHighlight(line);
    const indent=line.match(/^\s*/)[0];
    let html=esc(indent)+span('hl-kw', esc('File'))+' '+span('hl-str', esc('"'+m[1]+'"'))+', ';
    html+=span('hl-kw', esc('line'))+' '+span('hl-line', esc(m[2]));
    if(m[3]) html+=', '+span('hl-kw', esc('in'))+' '+span('hl-file', esc(m[3]));
    return html;
  }

  function highlightExc(line){
    const em=line.match(/^((?:[A-Za-z_][\w]*\.)*[A-Za-z_]*(?:Error(?:Group)?|Exception))(\s*:?\s*)(.*)$/);
    if(em) return span('hl-err', esc(em[1]))+esc(em[2])+inlineHighlight(em[3]||'');
    return span('hl-err', esc(line));
  }

  const joined=head.join('\n');
  const looksTb=/Traceback \(most recent call last\):/.test(joined) || /^\[tool_error\]/m.test(joined);
  const looksJson=(()=>{
    const t=joined.trim();
    if(!(t.startsWith('{')||t.startsWith('[')) || t.length>200000) return false;
    try{ JSON.parse(t); return true; }catch(e){ return false; }
  })();

  const outLines=[];
  let inTb=false;
  for(const line of head){
    if(reToolErr.test(line)){ outLines.push(span('hl-err', esc(line))); inTb=true; continue; }
    if(reTbHeader.test(line)){ outLines.push(span('hl-tb', esc(line))); inTb=true; continue; }
    if(inTb || looksTb){
      if(reTbFile.test(line)){ outLines.push(highlightTbFile(line)); continue; }
      if(reExc.test(line)){ outLines.push(highlightExc(line)); continue; }
      if(reTbCaret.test(line)){ outLines.push(span('hl-err', esc(line))); continue; }
      if(!line.trim()){ outLines.push(''); continue; }
      if(/^\s{2,}/.test(line)){ outLines.push(inlineHighlight(line)); continue; }
    }
    if(looksJson){ outLines.push(highlightJsonish(line)); continue; }
    if(rePrompt.test(line)){
      const pm=line.match(/^(>>> |\.\.\. |\$ |# |> )([\s\S]*)$/);
      outLines.push(span('hl-prompt', esc(pm[1]))+inlineHighlight(pm[2]||''));
      continue;
    }
    if(reSection.test(line) && line.trim().length>=3){ outLines.push(span('hl-marker', esc(line))); continue; }
    if(reWarn.test(line)){ outLines.push('<span class="hl hl-warn">'+inlineHighlight(line)+'</span>'); continue; }
    if(reOk.test(line) && line.trim().length<80){ outLines.push('<span class="hl hl-ok">'+inlineHighlight(line)+'</span>'); continue; }
    if(reExc.test(line)){ outLines.push(highlightExc(line)); continue; }
    outLines.push(inlineHighlight(line));
  }
  let html=outLines.join('\n');
  if(rest){
    html+='\n'+span('hl-dim', esc('… 已截断高亮，剩余 '+rest.length+' 行未着色'))+'\n'+esc(rest.join('\n'));
  }
  return html;
}
function fillToolOutputEl(el, raw){
  if(!el) return;
  el.classList.add('tool-output');
  el.innerHTML=highlightToolOutput(raw);
}
applyTheme(currentTheme(), false);

const safeUrl=u=>{
  const s=String(u??'').trim();
  if(!s) return '';
  if(/^(https?:|mailto:|tel:|#|\/|\.\/|\.\.\/)/i.test(s)) return s;
  if(/^data:image\/[a-z0-9.+-]+;base64,/i.test(s)) return s;
  return '';
};
// marked + DOMPurify (inlined). Fallback is escaped plain text.
const mdReady=(()=>{
  try{
    if(typeof marked==='undefined' || typeof DOMPurify==='undefined') return false;
    const lib=marked; // UMD build exposes {parse,setOptions,Renderer,...}
    const renderer=new lib.Renderer();
    // marked v11 classic signature: link(href, title, text) / image(href, title, text)
    renderer.link=function(href, title, text){
      if(href && typeof href==='object'){
        const tok=href;
        const body=(tok.tokens && this.parser?.parseInline)?this.parser.parseInline(tok.tokens):esc(String(tok.text||''));
        const safe=safeUrl(tok.href);
        if(!safe) return body;
        const t=tok.title?` title="${esc(tok.title)}"`:'';
        const ext=/^https?:/i.test(safe);
        return `<a href="${esc(safe)}"${t}${ext?' target="_blank" rel="noreferrer noopener"':''}>${body}</a>`;
      }
      const safe=safeUrl(href);
      if(!safe) return text||'';
      const t=title?` title="${esc(title)}"`:'';
      const ext=/^https?:/i.test(safe);
      return `<a href="${esc(safe)}"${t}${ext?' target="_blank" rel="noreferrer noopener"':''}>${text}</a>`;
    };
    renderer.image=function(href, title, text){
      if(href && typeof href==='object'){
        const tok=href; href=tok.href; title=tok.title; text=tok.text;
      }
      const safe=safeUrl(href);
      if(!safe) return esc(text||'');
      const t=title?` title="${esc(title)}"`:'';
      return `<img src="${esc(safe)}" alt="${esc(text||'')}"${t} loading="lazy">`;
    };
    lib.setOptions({
      gfm:true,
      breaks:false,
      pedantic:false,
      renderer,
      headerIds:false,
      mangle:false
    });
    return {lib, purify:DOMPurify};
  }catch(err){
    console.warn('markdown libs init failed', err);
    return false;
  }
})();
function markdown(text){
  const raw=String(text??'');
  if(!raw) return '';
  if(mdReady){
    try{
      const html=mdReady.lib.parse(raw);
      // Allow common markdown tags/attrs; strip scripts/handlers.
      return mdReady.purify.sanitize(html, {
        USE_PROFILES:{html:true},
        ADD_TAGS:['input'],
        ADD_ATTR:['target','rel','align','checked','disabled','type','start','colspan','rowspan','loading','class'],
      });
    }catch(err){
      console.warn('marked parse failed', err);
    }
  }
  return `<p>${esc(raw).replace(/\n\n+/g,'</p><p>').replace(/\n/g,'<br>')}</p>`;
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
function imageHtml(src){return `<img src="${esc(src)}" alt="attached image" loading="lazy">`;}
function hasVisibleContent(content){
  if(content==null) return false;
  if(Array.isArray(content)) return content.some(part=>part?.type==='image_url' || String(part?.text??'').trim());
  return String(content).trim().length>0;
}
function messageHtml(m){
  if(m.role==='tool' || !hasVisibleContent(m.content)) return '';
  const role=m.role||'message';
  const roleHtml=role==='assistant'?'':`<div class="role"><span>${esc(role)}</span></div>`;
  return `<article class="msg ${esc(role)}">${roleHtml}${contentHtml(m.content)}</article>`;
}
function newToolGroup(){
  messagesEl.insertAdjacentHTML('beforeend','<details class="tool-group"><summary><span class="tool-title">工具活动</span><span class="tool-meta"></span><span class="tool-duration"></span></summary><div class="tool-events"></div></details>');
  return messagesEl.lastElementChild;
}
function matchingToolEvent(group,id){
  if(!id)return null;
  return [...group.querySelectorAll('.tool-event')].find(el=>el.dataset.callId===String(id))||null;
}
function durationText(ms){return ms<1000?`${Math.max(0,Math.round(ms))}ms`:`${(ms/1000).toFixed(ms<10000?1:0)}s`}
function refreshToolGroup(group){
  const events=[...group.querySelectorAll('.tool-event')], done=events.filter(el=>el.classList.contains('done')).length, failed=events.filter(el=>el.classList.contains('failed')).length;
  const names={};
  events.forEach(el=>{const n=el.dataset.toolName||'tool';names[n]=(names[n]||0)+1});
  // 仅多种工具时在名称后显示 ×c，避免「python ×n」与「n 次」重复
  const nameEntries=Object.entries(names);
  const multiKinds=nameEntries.length>1;
  const nameText=nameEntries.slice(0,2).map(([n,c])=>(multiKinds&&c>1)?`${n} ×${c}`:n).join('、')+(nameEntries.length>2?' 等':'');
  const complete=events.length>0&&events.length===done;
  group.classList.toggle('running',!complete);
  group.classList.toggle('has-failure',failed>0);
  if(!group.dataset.started)group.dataset.started=String(Date.now());
  if(complete&&!group.dataset.ended)group.dataset.ended=String(Date.now());
  if(!complete)delete group.dataset.ended;
  const status=failed?`${events.length} 次 · ${failed} 失败`:complete?`${events.length} 次 · 已完成`:`${events.length} 次 · 返回 ${done}/${events.length}`;
  const elapsed=Number(group.dataset.ended||Date.now())-Number(group.dataset.started);
  const dur=elapsed>=300?durationText(elapsed):'';
  const head=nameText?`工具活动 · ${nameText}`:'工具活动';
  group.querySelector('.tool-title').textContent=dur?`${head}  ${status}  ${dur}`:`${head}  ${status}`;
  group.querySelector('.tool-meta').textContent='';
  group.querySelector('.tool-duration').textContent='';
  if(failed&&!firstPaint)group.open=true;
}
function addToolActivity(group,m){
  const box=group.querySelector('.tool-events');
  if(m.tool_calls?.length){
    m.tool_calls.forEach(call=>{
      const id=String(call.id||''), name=String(call.function?.name||'python');
      if(matchingToolEvent(group,id))return;
      const row=document.createElement('div');
      row.className='tool-event';row.dataset.callId=id;row.dataset.toolName=name;
      row.innerHTML=`<span class="tool-name">${esc(name)}</span><span class="tool-state">等待结果</span><span class="tool-event-duration"></span><button class="tool-output-button hidden" type="button">查看输出</button>`;
      row.dataset.started=String(Date.now());
      box.appendChild(row);
    });
  }
  if(m.role==='tool'){
    const id=String(m.tool_call_id||'');
    let row=matchingToolEvent(group,id);
    if(!row){
      row=document.createElement('div');row.className='tool-event';row.dataset.callId=id;row.dataset.toolName='tool';
      row.innerHTML='<span class="tool-name">tool</span><span class="tool-state"></span><span class="tool-event-duration"></span><button class="tool-output-button hidden" type="button">查看输出</button>';
      row.dataset.started=String(Date.now());
      box.appendChild(row);
    }
    row.classList.add('done');
    row.classList.toggle('failed',!!m.tool_failed);
    if(!row.dataset.started)row.dataset.started=String(Date.now());
    if(!row.dataset.ended)row.dataset.ended=String(Date.now());
    const length=m.tool_content_length??String(m.content||'').length;
    row.querySelector('.tool-state').textContent=m.tool_failed?`执行失败 · ${length} 字符`:`已返回 · ${length} 字符`;
    row.querySelector('.tool-output-button').classList.remove('hidden');
    const oneElapsed=Number(row.dataset.ended)-Number(row.dataset.started);
    const oneDur=row.querySelector('.tool-event-duration');
    if(oneDur)oneDur.textContent=durationText(Math.max(0,oneElapsed));
  }
  refreshToolGroup(group);
}
function updateToolDurations(){
  document.querySelectorAll('.tool-group.running').forEach(refreshToolGroup);
  document.querySelectorAll('.tool-event:not(.done)').forEach(row=>{
    if(!row.dataset.started)row.dataset.started=String(Date.now());
    const elapsed=Date.now()-Number(row.dataset.started);
    const el=row.querySelector('.tool-event-duration');
    if(el)el.textContent=elapsed>=100?durationText(elapsed):'';
  });
}
setInterval(updateToolDurations,100);
function appendMessageBatch(msgs){
  for(const m of msgs){
    // A message may contain useful prose and tool calls; prose remains a normal card.
    const body=messageHtml(m);
    if(body){
      messagesEl.insertAdjacentHTML('beforeend',body);
      const last=messagesEl.lastElementChild;
      if(last){
        enhanceCodeBlocks(last);
        if(m.role==='user') last.dataset.editText=plainTextFromContent(m.content);
      }
    }
    const isActivity=m.role==='tool' || !!m.tool_calls?.length;
    if(isActivity){
      let group=messagesEl.lastElementChild;
      if(!group?.classList.contains('tool-group'))group=newToolGroup();
      addToolActivity(group,m);
    }
  }
}
function runnerBusy(){return isRunning&&!runnerIdle}
function updateRunLabel(){
  if(runBtn.textContent==='结束中…') return;
  runBtn.textContent=runnerBusy()?'停止':(msgInput.value.trim()||selectedFiles.length?'发送并运行':'继续运行');
}
function setRunningUi(){
  runnerStatus.classList.toggle('hidden',!runnerBusy());
  runnerLabel.textContent=phaseLabel||'正在运行…';
  runBtn.classList.toggle('stop',runnerBusy());
  // Heal stuck disabled state after stop; submit handler manages its own disable window.
  if(!runnerBusy() && runBtn.textContent!=='结束中…') runBtn.disabled=false;
  updateRunLabel();
}
function nearBottom(){return window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-140}
function hideNewMessages(){unseenMessages=0;newMessagesBtn.classList.add('hidden')}
function showNewMessages(amount){unseenMessages+=Math.max(1,amount);newMessagesBtn.textContent=`↓ ${unseenMessages} 项新动态`;newMessagesBtn.classList.remove('hidden')}
function afterMessageUpdate(wasNear,added,reset=false){
  requestAnimationFrame(()=>{
    if(firstPaint||wasNear){window.scrollTo({top:document.documentElement.scrollHeight,behavior:(firstPaint||reset)?'auto':'smooth'});hideNewMessages()}
    else if(added)showNewMessages(added);
    firstPaint=false;
    refreshEditableLastUser();
  });
}
function plainTextFromContent(content){
  if(content==null) return '';
  if(typeof content==='string') return content;
  if(Array.isArray(content)){
    return content.map(part=>{
      if(!part) return '';
      if(typeof part==='string') return part;
      if(part.type==='image_url') return '';
      return String(part.text??'');
    }).filter(Boolean).join('\n');
  }
  return String(content);
}
function refreshEditableLastUser(){
  messagesEl.querySelectorAll('.msg.user.editable-last').forEach(el=>{
    el.classList.remove('editable-last');
    el.removeAttribute('title');
  });
  // Only the true last DOM child may be edited. Tool groups sit after the user
  // card when tools are in flight without assistant prose — those must not look editable.
  const last=messagesEl.lastElementChild;
  if(!last || !last.classList.contains('msg') || !last.classList.contains('user')) return;
  last.classList.add('editable-last');
  last.title='双击以撤回并编辑这条消息';
  if(!last.dataset.editText){
    const content=last.querySelectorAll('.content');
    last.dataset.editText=[...content].map(node=>node.innerText||node.textContent||'').join('\n').trim();
  }
}
function requestFullResync(){
  // Invalidate in-flight incremental polls so a partial window cannot wipe history.
  pollGeneration++;
  messageCount=0;
  lastUpdated=0;
  invalidateUsage();
  schedulePoll(0);
}
async function editLastUserMessage(el){
  if(!el || !el.classList.contains('editable-last')) return;
  if(editInFlight) return;
  editInFlight=true;
  const fallback=el.dataset.editText||'';
  try{
    const r=await fetch('/api/retract-last-user?chat='+chatParam(),{method:'POST'});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    const text=(data && typeof data.text==='string')?data.text:fallback;
    // Put the retracted user message back into the composer for editing.
    msgInput.value=text;
    writeDraft(msgInput.value);
    resizeComposer();
    updateRunLabel();
    msgInput.focus();
    const len=msgInput.value.length;
    try{msgInput.setSelectionRange(len,len)}catch(e){}
    // Force full refresh so the retracted user bubble disappears
    requestFullResync();
  }catch(err){
    alert(err.message||'无法编辑该消息');
  }finally{
    editInFlight=false;
  }
}
function applyMessages(data){
  const msgs=data.messages||[], count=data.count??messageCount, offset=data.offset??0, wasNear=nearBottom();
  // Full replace only when the server sent a full window (reset / offset 0).
  // Do NOT treat local messageCount===0 as "this payload is complete" — a stale
  // incremental response could otherwise wipe earlier bubbles.
  if(data.reset || offset===0){
    // 整窗重建（切对话/首次加载/压缩）时，历史消息不应再逐条弹入。
    messagesEl.classList.add('no-msg-anim');
    messagesEl.innerHTML='';appendMessageBatch(msgs);messageCount=count;afterMessageUpdate(wasNear,msgs.length,true);
    requestAnimationFrame(()=>messagesEl.classList.remove('no-msg-anim'));
    return;
  }
  if(offset===messageCount){
    if(msgs.length)appendMessageBatch(msgs);messageCount=count;afterMessageUpdate(wasNear,msgs.length);return;
  }
  if(offset<messageCount && offset+msgs.length>=messageCount){
    const fresh=msgs.slice(messageCount-offset);
    if(fresh.length)appendMessageBatch(fresh);messageCount=count;afterMessageUpdate(wasNear,fresh.length);return;
  }
  requestFullResync();
}
function render(data){
  isRunning=!!data.running;
  runnerIdle=data.phase==='idle_wait';
  phaseLabel=data.label||(isRunning?'运行中':'空闲');
  document.getElementById('model').textContent=data.model||'model';
  setRunningUi();
  applyMessages(data);
  emptyEl.classList.toggle('hidden', messageCount>0);
  if(!usageLoaded)loadUsage();
  if(typeof scheduleSoloPlusOverlap==='function') scheduleSoloPlusOverlap();
}
function schedulePoll(delay){
  // If a poll is already in flight, just ask for another pass after it finishes.
  // This avoids try{schedulePoll(0);return} being overwritten by finally's delayed schedule.
  if(pollInFlight){pollQueued=true;return;}
  if(pollTimer!=null) clearTimeout(pollTimer);
  pollTimer=setTimeout(poll, delay);
}
async function poll(){
  if(pollInFlight){pollQueued=true;return;}
  pollInFlight=true;
  pollTimer=null;
  const gen=pollGeneration;
  const reqAfter=messageCount;
  const reqSince=lastUpdated;
  let changed=false;
  try{
    const r=await fetch('/api/state?since='+encodeURIComponent(reqSince)+'&after='+encodeURIComponent(reqAfter)+'&chat='+chatParam());
    const data=await r.json();
    if(r.status===404||(data&&data.missing)){
      // This chat is gone (deleted elsewhere): fall back to the server's active one.
      chatBootstrapped=false;
      await loadChats();
      requestFullResync();
      return;
    }
    if(gen!==pollGeneration){
      // Stale response after requestFullResync / transcript rewrite — drop it.
      return;
    }
    if(data.unchanged){
      isRunning=!!data.running;
      runnerIdle=data.phase==='idle_wait';
      phaseLabel=data.label||(isRunning?'运行中':'空闲');
      if(data.count!=null && data.count<messageCount){
        // Transcript shrank (compaction): full resync.
        requestFullResync();
        return;
      }
      if(data.count!=null) messageCount=data.count;
      setRunningUi();
    }else{
      changed=true;
      lastUpdated=data.updated||0;
      invalidateUsage();
      render(data);
    }
  }catch(e){
    if(gen===pollGeneration) usageText.textContent='Token：连接失败';
  }finally{
    pollInFlight=false;
    if(pollQueued || gen!==pollGeneration){
      pollQueued=false;
      schedulePoll(0);
    }else{
      if(typeof loadChats==='function' && (runnerBusy() || (Array.isArray(chatsCache)&&chatsCache.some(c=>c&&c.running&&!c.idle)))){
        if(!window.__aeChatRefreshAt || Date.now()-window.__aeChatRefreshAt>2500){
          window.__aeChatRefreshAt=Date.now();
          loadChats();
        }
      }
      // 轮询降级: 有变化 -> 快轮询; 连续无变化 -> 逐步退避到 500ms; 空闲 -> 1800ms。
      schedulePoll(pollDelay(changed));
    }
  }
}
function pollDelay(changed){
  if(!runnerBusy()) return POLL_IDLE;
  if(changed){ pollUnchangedStreak=0; return POLL_FAST; }
  pollUnchangedStreak++;
  // 运行中但一直没新内容: 120ms -> 250ms -> 400ms -> 500ms 封顶。
  return [POLL_FAST,250,400,POLL_RUN][Math.min(pollUnchangedStreak-1,3)];
}
function addFiles(files){const incoming=[...files].filter(Boolean);if(incoming.length){selectedFiles.push(...incoming);refreshFiles();updateRunLabel()}}
function refreshFiles(){filesEl.innerHTML=selectedFiles.map((f,i)=>`<span class="file">${esc(f.name)} <button type="button" data-i="${i}">x</button></span>`).join('');updateRunLabel()}
function resizeComposer(){msgInput.style.height='auto';msgInput.style.height=Math.max(72,msgInput.scrollHeight)+'px'}
drop.addEventListener('click',e=>{if(e.target===drop)fileInput.click()});
drop.addEventListener('paste',e=>{const files=[...e.clipboardData.files];if(files.length){e.preventDefault();addFiles(files)}});
fileInput.addEventListener('change',()=>{addFiles(fileInput.files);fileInput.value=''});
filesEl.addEventListener('click',e=>{if(e.target.dataset.i!==undefined){selectedFiles.splice(Number(e.target.dataset.i),1);refreshFiles()}});
for(const event of ['dragenter','dragover'])document.addEventListener(event,e=>{if([...e.dataTransfer.types].includes('Files')){e.preventDefault();drop.classList.add('drag')}});
for(const event of ['dragleave','drop'])document.addEventListener(event,e=>{if(event==='drop'&&e.dataTransfer?.files?.length){e.preventDefault();addFiles(e.dataTransfer.files)}drop.classList.remove('drag')});
function draftKey(id){return 'ae-draft:'+String(id||activeChatId||'default')}
// Every transcript request is scoped to THIS tab's chat, so a chat switch or a
// second tab can never redirect a send/stop/poll to another conversation.
function chatParam(){return encodeURIComponent(activeChatId||'default')}
function readDraft(id){try{return localStorage.getItem(draftKey(id))||''}catch(e){return ''}}
function writeDraft(val,id){try{localStorage.setItem(draftKey(id), String(val??''))}catch(e){}}
function clearDraft(id){try{localStorage.removeItem(draftKey(id))}catch(e){}}
function syncRunningFromCache(){
  // Only UPGRADE active running state from the chat list (e.g. switch onto a
  // background task). Downgrades are owned by /api/state polling so a slightly
  // stale chats list cannot flip a just-finished task back to "运行中".
  const row=(chatsCache||[]).find(c=>c && String(c.id)===String(activeChatId));
  const run=!!(row && row.running);
  if(run && !isRunning){
    isRunning=true;
    runnerIdle=!!row.idle;
    if(runnerIdle) phaseLabel='等待消息';
    else if(!phaseLabel || phaseLabel==='空闲') phaseLabel='运行中';
    setRunningUi();
  }
}
msgInput.value=readDraft();resizeComposer();updateRunLabel();
msgInput.addEventListener('input',()=>{writeDraft(msgInput.value);resizeComposer();updateRunLabel()});
msgInput.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!runnerBusy()){e.preventDefault();composer.requestSubmit()}});
document.addEventListener('keydown',async e=>{if(e.key==='Escape'&&runnerBusy()){e.preventDefault();await stopRunner(runBtn)}});
newMessagesBtn.addEventListener('click',()=>{window.scrollTo({top:document.documentElement.scrollHeight,behavior:'smooth'});hideNewMessages()});
window.addEventListener('scroll',()=>{if(nearBottom())hideNewMessages()},{passive:true});
messagesEl.addEventListener('dblclick',e=>{
  const card=e.target.closest('article.msg.user.editable-last');
  if(!card) return;
  // Ignore double-clicks on interactive children if any appear later
  if(e.target.closest('button,a,summary,input,textarea')) return;
  e.preventDefault();
  editLastUserMessage(card);
});
messagesEl.addEventListener('click',async e=>{
  const button=e.target.closest('.tool-output-button');if(!button)return;
  const row=button.closest('.tool-event');let out=row.querySelector('.tool-output');
  if(out){out.classList.toggle('hidden');button.textContent=out.classList.contains('hidden')?'查看输出':'收起输出';return}
  button.disabled=true;button.textContent='加载中…';
  try{const r=await fetch('/api/tool-output?id='+encodeURIComponent(row.dataset.callId)+'&chat='+chatParam());if(!r.ok)throw new Error();const data=await r.json();out=document.createElement('pre');fillToolOutputEl(out, data.output||'(无输出)');row.appendChild(out);button.textContent='收起输出'}catch(err){button.textContent='加载失败'}finally{button.disabled=false}
});
function invalidateUsage(){
  usageGeneration++;
  usageLoaded=false;
  usageReloadQueued=true;
}
async function loadUsage(){
  if(usageLoaded)return;
  if(usageLoading){usageReloadQueued=true;return}
  usageLoading=true;
  usageReloadQueued=false;
  const generation=usageGeneration;
  let stale=false;
  usageText.textContent='Token：计算中…';
  try{
    const r=await fetch('/api/usage?chat='+chatParam());
    if(!r.ok)throw new Error();
    const data=await r.json();
    if(generation!==usageGeneration)return;
    const total=Number(data.usage?.estimated_total||0), limit=Number(data.context_limit||0), pct=limit?Math.round(total/limit*100):0;
    usageText.textContent=limit?`估算 Token：${total.toLocaleString()} / ${limit.toLocaleString()} · ${pct}%`:`估算 Token：${total.toLocaleString()}`;
    tokenBar.style.width=Math.min(100,pct)+'%';
    tokenBar.classList.toggle('warn',pct>=60&&pct<85);
    tokenBar.classList.toggle('danger',pct>=85);
    usageText.title=pct>=85?'上下文接近上限，建议压缩历史消息':'';
    stale=Number(data.updated||0)<lastUpdated;
    usageLoaded=!stale;
  }catch(e){
    if(generation===usageGeneration)usageText.textContent='Token：获取失败';
  }finally{
    usageLoading=false;
    const reload=usageReloadQueued||generation!==usageGeneration||stale;
    usageReloadQueued=false;
    if(reload&&!usageLoaded)setTimeout(loadUsage,0);
  }
}
async function stopRunner(btn){
  if(!isRunning)return;
  if(btn){btn.disabled=true;btn.textContent='结束中…'}
  try{
    await fetch('/api/stop?chat='+chatParam(),{method:'POST'});
  }finally{
    // Always re-enable: setRunningUi only updates labels, not disabled.
    if(btn) btn.disabled=false;
    isRunning=false;
    runnerIdle=false;
    phaseLabel='空闲';
    setRunningUi();
    schedulePoll(0);
  }
}
async function shutdownViewer(){
  if(!killProcessBtn)return;
  killProcessBtn.disabled=true;
  killProcessBtn.textContent='关闭中…';
  try{
    await fetch('/api/shutdown',{method:'POST'});
  }catch(err){
    /* server may close connection mid-request */
  }
  killProcessBtn.textContent='已关闭';
  try{window.close()}catch(e){}
  document.body.innerHTML='<div style="display:grid;place-items:center;min-height:100vh;font:15px/1.5 system-ui,sans-serif;color:#6b7280">查看器已关闭</div>';
}
if(killProcessBtn)killProcessBtn.addEventListener('click',()=>shutdownViewer());
if(themeToggleBtn) themeToggleBtn.addEventListener('click',()=>toggleTheme());
runBtn.addEventListener('click',async e=>{if(runnerBusy()){e.preventDefault();await stopRunner(runBtn)}});
composer.addEventListener('submit',async e=>{
  e.preventDefault();if(runnerBusy())return;const submitted=msgInput.value,fd=new FormData();fd.append('message',submitted);selectedFiles.forEach(f=>fd.append('files',f,f.name));runBtn.disabled=true;
  try{const r=await fetch('/api/send?chat='+chatParam(),{method:'POST',body:fd});if(!r.ok)throw new Error(await r.text());if(msgInput.value===submitted){msgInput.value='';clearDraft();resizeComposer()}selectedFiles=[];refreshFiles();isRunning=true;runnerIdle=false;phaseLabel='等待 AI';setRunningUi();schedulePoll(0)}catch(err){alert(err.message||'运行失败')}finally{runBtn.disabled=false}
});


const chatRailEl=document.getElementById('chatRail');
const chatListEl=document.getElementById('chatList');
const chatNewBtn=document.getElementById('chatNew');
const chatSearchEl=document.getElementById('chatSearch');
const chatMenuEl=document.getElementById('chatMenu');
const chatModalEl=document.getElementById('chatModal');
const chatModalTitleEl=document.getElementById('chatModalTitle');
const chatModalDescEl=document.getElementById('chatModalDesc');
const chatModalInputEl=document.getElementById('chatModalInput');
const chatModalCancelBtn=document.getElementById('chatModalCancel');
const chatModalOkBtn=document.getElementById('chatModalOk');
const chatToastEl=document.getElementById('chatToast');
let chatsCache=[];
let chatBootstrapped=false;
let chatsLoading=false;
let chatsReloadQueued=false;
let chatsLoadPromise=null;
let chatBusy=false;
let chatMenuId=null;
let chatModalMode=null;
let chatModalTargetId=null;
let chatToastTimer=null;

function relativeTime(ts){
  if(!ts) return '';
  const diff=Math.max(0,(Date.now()/1000)-Number(ts));
  if(diff<60) return '刚刚';
  if(diff<3600) return Math.floor(diff/60)+' 分钟前';
  if(diff<86400) return Math.floor(diff/3600)+' 小时前';
  if(diff<86400*30) return Math.floor(diff/86400)+' 天前';
  const d=new Date(Number(ts)*1000);
  return `${d.getMonth()+1}/${d.getDate()}`;
}
function showChatToast(msg){
  if(!chatToastEl) return;
  chatToastEl.textContent=String(msg||'');
  chatToastEl.classList.add('show');
  clearTimeout(chatToastTimer);
  chatToastTimer=setTimeout(()=>chatToastEl.classList.remove('show'),1800);
}
function setChatBusy(on){
  chatBusy=!!on;
  if(chatRailEl) chatRailEl.classList.toggle('busy', chatBusy);
}
function closeChatMenu(){
  chatMenuId=null;
  if(!chatMenuEl) return;
  chatMenuEl.classList.remove('open');
  chatMenuEl.innerHTML='';
  chatMenuEl.setAttribute('aria-hidden','true');
}
function openChatMenu(id, x, y){
  if(!chatMenuEl) return;
  chatMenuId=id;
  const isDefault=id==='default';
  const row=(chatsCache||[]).find(c=>c && String(c.id)===String(id));
  const isRun=!!(row && row.running);
  chatMenuEl.innerHTML=`
    ${isRun?'<button type="button" data-act="stop" role="menuitem">\u505c\u6b62\u8fd0\u884c</button>':''}
    <button type="button" data-act="rename" ${isDefault?'disabled style="opacity:.45;cursor:not-allowed"':''} role="menuitem">重命名</button>
    <button type="button" data-act="delete" class="danger" ${isDefault?'disabled style="opacity:.45;cursor:not-allowed"':''} role="menuitem">删除</button>
  `;
  chatMenuEl.classList.add('open');
  chatMenuEl.setAttribute('aria-hidden','false');
  const pad=8;
  const rect=chatMenuEl.getBoundingClientRect();
  const left=Math.min(Math.max(pad, x), window.innerWidth-rect.width-pad);
  const top=Math.min(Math.max(pad, y), window.innerHeight-rect.height-pad);
  chatMenuEl.style.left=left+'px';
  chatMenuEl.style.top=top+'px';
}
function closeChatModal(){
  chatModalMode=null;
  chatModalTargetId=null;
  if(!chatModalEl) return;
  chatModalEl.classList.remove('open');
  chatModalEl.setAttribute('aria-hidden','true');
  if(chatModalInputEl) chatModalInputEl.value='';
}
function openChatModal(mode, targetId, preset){
  if(!chatModalEl) return;
  chatModalMode=mode;
  chatModalTargetId=targetId||null;
  if(mode==='create'){
    chatModalTitleEl.textContent='新建对话';
    chatModalDescEl.textContent='可留空自动命名。将保存为 input_名称.json，并复制默认配置（不含历史消息）。';
    chatModalOkBtn.textContent='创建';
    chatModalInputEl.placeholder='例如：重构计划';
    chatModalInputEl.value=preset||'';
  }else{
    chatModalTitleEl.textContent='重命名对话';
    chatModalDescEl.textContent='将同步重命名对应的 input_名称.json 文件。';
    chatModalOkBtn.textContent='保存';
    chatModalInputEl.placeholder='新的对话名';
    chatModalInputEl.value=preset||'';
  }
  chatModalEl.classList.add('open');
  chatModalEl.setAttribute('aria-hidden','false');
  setTimeout(()=>{chatModalInputEl.focus(); chatModalInputEl.select()}, 20);
}
function filteredChats(){
  const q=(chatSearchEl&&chatSearchEl.value||'').trim().toLowerCase();
  if(!q) return chatsCache.slice();
  return chatsCache.filter(c=>String(c.name||c.id||'').toLowerCase().includes(q));
}
function chatTileLabel(name, id){
  const s = String(name || id || '').trim();
  if(!s) return '?';
  const m = s.match(/[\u4e00-\u9fffA-Za-z0-9]/);
  return m ? m[0].toUpperCase() : s[0];
}
function updateChatRailMode(){
  // Density rules:
  //  - 0 chats: floating '+' only (no side rail)
  //  - 1 chat: floating '+' over messages (no side rail / no tile)
  //  - 2-3 chats: full list, no search
  //  - 4+ chats: list + search
  //  - viewport <=640px and 2+ chats: square tiles in left rail (always show items)
  const n = Array.isArray(chatsCache) ? chatsCache.length : 0;
  const isEmpty = n === 0;
  const isSolo = n === 1;
  const showList = n >= 2;
  const showSearch = n >= 4;
  const forceNarrow = (typeof window !== 'undefined') && window.matchMedia && window.matchMedia('(max-width:640px)').matches;
  const useNarrow = forceNarrow && n >= 2;
  if(chatRailEl){
    chatRailEl.dataset.count = String(n);
    chatRailEl.classList.toggle('has-list', showList);
    chatRailEl.classList.toggle('has-search', showSearch && !useNarrow);
    chatRailEl.classList.toggle('solo', isSolo);
    chatRailEl.classList.toggle('empty', isEmpty);
    chatRailEl.classList.toggle('narrow', useNarrow);
  }
  const root = document.documentElement;
  root.classList.toggle('chat-solo-rail', isSolo);
  root.classList.toggle('chat-empty-rail', isEmpty);
  root.classList.toggle('chat-rail-narrow', useNarrow);
  if((!showSearch || useNarrow) && chatSearchEl && chatSearchEl.value){
    chatSearchEl.value = '';
  }
  if(chatSearchEl){
    const searchOn = showSearch && !useNarrow;
    chatSearchEl.tabIndex = searchOn ? 0 : -1;
    chatSearchEl.setAttribute('aria-hidden', searchOn ? 'false' : 'true');
  }
  if(chatListEl){
    chatListEl.setAttribute('aria-hidden', showList ? 'false' : 'true');
  }
  if(typeof scheduleSoloPlusOverlap==='function') scheduleSoloPlusOverlap();
}

/* chat-rail-narrow-resize */
if(typeof window!=='undefined'){
  let _railNarrowMQ = window.matchMedia ? window.matchMedia('(max-width:640px)') : null;
  const _onRailNarrowChange = ()=>{ try{ if(typeof renderChatList==='function') renderChatList(); else updateChatRailMode(); }catch(_){ } };
  if(_railNarrowMQ){
    if(_railNarrowMQ.addEventListener) _railNarrowMQ.addEventListener('change', _onRailNarrowChange);
    else if(_railNarrowMQ.addListener) _railNarrowMQ.addListener(_onRailNarrowChange);
  }
}
/* solo '+' overlap shadow: only when it covers message content */
function updateSoloPlusOverlap(){
  const btn = (typeof chatNewEl!=='undefined' && chatNewEl) ? chatNewEl : document.getElementById('chatNew');
  if(!btn) return;
  const root = document.documentElement;
  const floating = root.classList.contains('chat-solo-rail') || root.classList.contains('chat-empty-rail');
  if(!floating){
    btn.classList.remove('is-over-content');
    return;
  }
  const br = btn.getBoundingClientRect();
  if(br.width < 1 || br.height < 1){
    btn.classList.remove('is-over-content');
    return;
  }
  const cx = (br.left + br.right) / 2;
  const cy = (br.top + br.bottom) / 2;
  let over = false;
  // Sample center + corners lightly so partial overlap still counts
  const pts = [[cx,cy],[br.left+4,br.top+4],[br.right-4,br.top+4],[br.left+4,br.bottom-4],[br.right-4,br.bottom-4]];
  for(const [x,y] of pts){
    const stack = (document.elementsFromPoint ? document.elementsFromPoint(x,y) : []);
    for(const el of stack){
      if(!el || el===btn || btn.contains(el)) continue;
      if(el.closest && (el.closest('.msg') || el.closest('.message') || el.closest('.message-list') || el.closest('.messages') || el.closest('.model-message') || el.closest('.user-message') || el.closest('.tool-message'))){
        // Ignore empty messages chrome that is just the scrolling page background:
        // require a real content node (not the messages container itself alone at bg)
        if(el===document.documentElement || el===document.body) continue;
        if(el.classList && (el.classList.contains('messages') || el.classList.contains('message-list') || el.classList.contains('app'))) continue;
        over = true;
        break;
      }
    }
    if(over) break;
  }
  btn.classList.toggle('is-over-content', over);
}
let _soloPlusOverlapRaf = 0;
function scheduleSoloPlusOverlap(){
  if(_soloPlusOverlapRaf) return;
  _soloPlusOverlapRaf = requestAnimationFrame(()=>{
    _soloPlusOverlapRaf = 0;
    try{ updateSoloPlusOverlap(); }catch(_){ }
  });
}
if(typeof window!=='undefined'){
  window.addEventListener('scroll', scheduleSoloPlusOverlap, {passive:true, capture:true});
  window.addEventListener('resize', scheduleSoloPlusOverlap, {passive:true});
}

function renderChatList(){
  if(!chatListEl) return;
  updateChatRailMode();
  const items=filteredChats();
  if(!items.length){
    // When the full list is empty (0 chats) keep the rail clean; only show
    // "no match" when search is active over existing chats.
    if((chatsCache||[]).length === 0){
      chatListEl.innerHTML='';
      return;
    }
    chatListEl.innerHTML='<div class="chat-empty">没有匹配的对话</div>';
    return;
  }
  chatListEl.innerHTML=items.map(c=>{
    const id=String(c.id||'default');
    const name=String(c.name||id);
    const active=id===activeChatId; // per-tab selection, not the server-global one
    const isRun=!!c.running;
    const isIdle=!!c.idle;
    const meta=isRun ? (isIdle ? '等待消息' : '运行中') : relativeTime(c.mtime);
    return `<div class="chat-item${active?' active':''}${isRun&&!isIdle?' running':''}" data-chat-id="${esc(id)}" role="listitem" title="${esc(name)}${isRun?(isIdle?' · 等待消息':' · 运行中'):''}">
      <span class="chat-item-tile" aria-hidden="true">${esc(chatTileLabel(name, id))}</span>
      <div class="chat-item-main">
        <div class="chat-item-name">${esc(name)}</div>
        <div class="chat-item-meta">${esc(meta|| (id==='default'?'主对话':'对话'))}</div>
      </div>
      <button type="button" class="chat-item-more" data-chat-more="${esc(id)}" title="更多" aria-label="更多操作">
        <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>
      </button>
    </div>`;
  }).join('');
  if(typeof scheduleSoloPlusOverlap==='function') scheduleSoloPlusOverlap();
}
async function loadChats(){
  if(!chatListEl) return;
  if(chatsLoading){
    chatsReloadQueued=true;
    return chatsLoadPromise;
  }
  chatsLoading=true;
  const task=(async()=>{
    do{
      chatsReloadQueued=false;
      try{
        const r=await fetch('/api/chats');
        if(!r.ok) throw new Error(await r.text());
        const data=await r.json();
        // Adopt the server-side active chat only once per tab, so two tabs can
        // watch two conversations without stealing each other's view.
        if(data && data.active && !chatBootstrapped){activeChatId=data.active;chatBootstrapped=true}
        chatsCache=Array.isArray(data.chats)?data.chats:[];
        renderChatList();
        syncRunningFromCache();
      }catch(err){
        console.warn('load chats failed', err);
      }
    }while(chatsReloadQueued);
  })();
  chatsLoadPromise=task;
  try{
    await task;
  }finally{
    if(chatsLoadPromise===task){
      chatsLoading=false;
      chatsLoadPromise=null;
    }
  }
}
function resetComposerLocal(opts){
  const clear= !opts || opts.clearDraft!==false;
  if(clear) clearDraft();
  msgInput.value= clear ? '' : readDraft();
  resizeComposer();
  selectedFiles=[];
  refreshFiles();
  usageLoaded=false;
  // Avoid carrying the previous chat's running button state until /api/state arrives.
  isRunning=false;
  runnerIdle=false;
  phaseLabel='空闲';
  setRunningUi();
}
function playSwitchVisual(){
  const box=document.querySelector('.messages');
  if(!box) return;
  box.classList.add('chat-switching');
  setTimeout(()=>box.classList.remove('chat-switching'), 220);
}
async function selectChat(id){
  if(!id || id===activeChatId || chatBusy) return;
  setChatBusy(true);
  playSwitchVisual();
  try{
    writeDraft(msgInput.value, activeChatId);
    const r=await fetch('/api/chats/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    activeChatId=data.active||id;
    chatBootstrapped=true;
    if(Array.isArray(data.chats)) chatsCache=data.chats;
    resetComposerLocal({clearDraft:false});
    requestFullResync();
    loadUsage();
    await loadChats();
    syncRunningFromCache();
    showChatToast('已切换对话');
  }catch(err){
    showChatToast(err.message||'切换失败');
  }finally{
    setChatBusy(false);
  }
}
async function createChat(name){
  if(chatBusy) return;
  const trimmed=String(name||'').trim();
  setChatBusy(true);
  playSwitchVisual();
  try{
    writeDraft(msgInput.value, activeChatId);
    const r=await fetch('/api/chats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:trimmed})});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    activeChatId=(data.chat&&data.chat.id)||trimmed;
    chatBootstrapped=true;
    if(Array.isArray(data.chats)) chatsCache=data.chats;
    resetComposerLocal({clearDraft:true});
    requestFullResync();
    loadUsage();
    await loadChats();
    syncRunningFromCache();
    closeChatModal();
    showChatToast('已创建对话');
  }catch(err){
    showChatToast(err.message||'创建失败');
  }finally{
    setChatBusy(false);
  }
}
async function renameChat(id, name){
  const trimmed=String(name||'').trim();
  if(!id || !trimmed || chatBusy) return;
  setChatBusy(true);
  try{
    const r=await fetch('/api/chats/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,name:trimmed})});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    const newId=(data.chat&&data.chat.id)||trimmed;
    // Migrate per-chat draft key when this conversation is renamed.
    try{
      const oldDraft=readDraft(id);
      if(oldDraft){writeDraft(oldDraft, newId); clearDraft(id)}
    }catch(e){}
    if(id===activeChatId) activeChatId=newId; // renaming a background chat must not move this tab
    await loadChats();
    closeChatModal();
    showChatToast('已重命名');
  }catch(err){
    showChatToast(err.message||'重命名失败');
  }finally{
    setChatBusy(false);
  }
}
async function stopChat(id){
  // Stop any chat's runner straight from the rail (background tasks included).
  if(!id) return;
  try{
    const r=await fetch('/api/stop?chat='+encodeURIComponent(id),{method:'POST'});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    if(String(id)===String(activeChatId)){
      isRunning=false;
      runnerIdle=false;
      phaseLabel='\u7a7a\u95f2';
      setRunningUi();
      schedulePoll(0);
    }
    showChatToast(data && data.stopped ? '\u5df2\u505c\u6b62' : '\u672a\u5728\u8fd0\u884c');
    loadChats();
  }catch(err){
    showChatToast(err.message||'\u505c\u6b62\u5931\u8d25');
  }
}
async function deleteChat(id){
  if(!id || id==='default' || chatBusy) return;
  const beforeActive=activeChatId;
  const wasActive=id===beforeActive;
  setChatBusy(true);
  if(wasActive)playSwitchVisual();
  try{
    const r=await fetch('/api/chats/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    clearDraft(id);
    if(wasActive) activeChatId=(data && data.active) || 'default';
    if(wasActive || activeChatId!==beforeActive){
      resetComposerLocal({clearDraft:false});
      requestFullResync();
      loadUsage();
    }
    await loadChats();
    syncRunningFromCache();
    showChatToast('已删除对话');
  }catch(err){
    showChatToast(err.message||'删除失败');
  }finally{
    setChatBusy(false);
  }
}
async function submitChatModal(){
  const val=(chatModalInputEl&&chatModalInputEl.value||'').trim();
  if(chatModalMode==='create'){
    await createChat(val); // empty => auto name
    return;
  }
  if(chatModalMode==='rename'){
    if(!val){showChatToast('请输入名称');return}
    await renameChat(chatModalTargetId,val);
  }
}
if(chatListEl){
  chatListEl.addEventListener('click',(e)=>{
    const more=e.target.closest('[data-chat-more]');
    if(more){
      e.preventDefault();
      e.stopPropagation();
      const id=more.getAttribute('data-chat-more');
      const rect=more.getBoundingClientRect();
      openChatMenu(id, rect.right+4, rect.top);
      return;
    }
    const item=e.target.closest('[data-chat-id]');
    if(!item) return;
    selectChat(item.getAttribute('data-chat-id'));
  });
  chatListEl.addEventListener('contextmenu',(e)=>{
    const item=e.target.closest('[data-chat-id]');
    if(!item) return;
    e.preventDefault();
    openChatMenu(item.getAttribute('data-chat-id'), e.clientX, e.clientY);
  });
}
if(chatMenuEl){
  chatMenuEl.addEventListener('click',(e)=>{
    const btn=e.target.closest('button[data-act]');
    if(!btn || btn.disabled) return;
    const act=btn.getAttribute('data-act');
    const id=chatMenuId;
    closeChatMenu();
    if(!id) return;
    if(act==='stop'){
      stopChat(id);
    }else if(act==='rename'){
      const cur=(chatsCache.find(c=>c.id===id)||{}).name||id;
      openChatModal('rename', id, cur);
    }else if(act==='delete'){
      deleteChat(id);
    }
  });
}
if(chatNewBtn) chatNewBtn.addEventListener('click',()=>createChat(''));
if(chatSearchEl) chatSearchEl.addEventListener('input',()=>renderChatList());
if(chatModalCancelBtn) chatModalCancelBtn.addEventListener('click', closeChatModal);
if(chatModalOkBtn) chatModalOkBtn.addEventListener('click', submitChatModal);
if(chatModalInputEl){
  chatModalInputEl.addEventListener('keydown',(e)=>{
    if(e.key==='Enter'){e.preventDefault();submitChatModal()}
    if(e.key==='Escape'){e.preventDefault();closeChatModal()}
  });
}
if(chatModalEl){
  chatModalEl.addEventListener('click',(e)=>{
    if(e.target===chatModalEl) closeChatModal();
  });
}
document.addEventListener('click',(e)=>{
  if(!chatMenuEl||!chatMenuEl.classList.contains('open')) return;
  if(chatMenuEl.contains(e.target)) return;
  if(e.target.closest && e.target.closest('[data-chat-more]')) return;
  closeChatMenu();
});
document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'){
    closeChatMenu();
    closeChatModal();
  }
});
loadChats();

schedulePoll(0);
