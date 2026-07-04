/* PDFWala reusable tool widget.
 * Each SEO landing page sets window.TOOL_CFG = { endpoint, field, multi, accept, params[] }
 * and includes <div id="tool-widget"></div>. This turns every content page into a
 * working tool (upload -> process -> download) that talks to the existing API.
 * No framework, no build step. ~5 KB. */
(function () {
  var cfg = window.TOOL_CFG;
  var root = document.getElementById('tool-widget');
  if (!cfg || !root) return;
  var API = window.location.origin;
  var files = [];

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (html != null) e.innerHTML = html;
    return e;
  }
  function human(n){ if(n>1048576) return (n/1048576).toFixed(1)+' MB'; return (n/1024).toFixed(0)+' KB'; }

  // ---- build UI ----
  var drop = el('div', {class:'tw-drop', tabindex:'0', role:'button',
                        'aria-label':'Choose files'});
  drop.innerHTML =
    '<input type="file" '+(cfg.multi?'multiple':'')+' accept="'+(cfg.accept||'')+'" style="display:none">'
    + '<div class="tw-drop-inner"><svg viewBox="0 0 24 24" width="30" height="30" fill="none" '
    + 'stroke="currentColor" stroke-width="1.6"><path d="M12 16V4M6 10l6-6 6 6"/>'
    + '<path d="M4 20h16"/></svg><div class="tw-drop-t">Drop file'+(cfg.multi?'s':'')+' here or click to browse</div>'
    + '<div class="tw-drop-s">Max 10 MB per file. Files auto-delete within 2 hours.</div></div>';
  var input = drop.querySelector('input');
  var list = el('div', {class:'tw-list'});
  var opts = el('div', {class:'tw-opts'});
  var btn = el('button', {class:'tw-go', type:'button', disabled:'disabled'}, cfg.cta || 'Process file');
  var status = el('div', {class:'tw-status', 'aria-live':'polite'});
  root.appendChild(drop); root.appendChild(list); root.appendChild(opts);
  root.appendChild(btn); root.appendChild(status);

  // ---- params ----
  var paramValues = {};
  (cfg.params || []).forEach(function (p) {
    paramValues[p.name] = p.default != null ? p.default : '';
    var wrap = el('label', {class:'tw-field'});
    wrap.appendChild(el('span', {class:'tw-field-l'}, p.label));
    var ctl;
    if (p.type === 'select') {
      ctl = el('select');
      (p.options||[]).forEach(function(o){
        var op=el('option',{value:o.value}, o.label); if(o.value===p.default) op.selected=true; ctl.appendChild(op);
      });
    } else {
      ctl = el('input', {type: p.type||'text', placeholder: p.placeholder||''});
    }
    ctl.addEventListener('input', function(){ paramValues[p.name]=ctl.value; });
    ctl.addEventListener('change', function(){ paramValues[p.name]=ctl.value; });
    wrap.appendChild(ctl); opts.appendChild(wrap);
  });

  function renderFiles(){
    list.innerHTML='';
    files.forEach(function(f,i){
      var row=el('div',{class:'tw-file'});
      row.innerHTML='<span class="tw-file-n">'+f.name.replace(/[<>&]/g,'')+'</span>'
        +'<span class="tw-file-s">'+human(f.size)+'</span>';
      var x=el('button',{class:'tw-file-x',type:'button','aria-label':'Remove'},'&times;');
      x.onclick=function(){ files.splice(i,1); renderFiles(); sync(); };
      row.appendChild(x); list.appendChild(row);
    });
  }
  function sync(){ btn.disabled = files.length===0; }

  function addFiles(fl){
    for (var i=0;i<fl.length;i++){
      if (fl[i].size > 10.5*1024*1024){ status.innerHTML='<div class="tw-err">'+fl[i].name+' is over 10 MB. Try Compress first or split it.</div>'; continue; }
      files.push(fl[i]);
    }
    if (!cfg.multi) files = files.slice(-1);
    renderFiles(); sync();
  }
  drop.addEventListener('click', function(){ input.click(); });
  drop.addEventListener('keydown', function(e){ if(e.key==='Enter'||e.key===' '){e.preventDefault();input.click();} });
  input.addEventListener('change', function(){ addFiles(input.files); input.value=''; });
  ['dragenter','dragover'].forEach(function(ev){ drop.addEventListener(ev,function(e){e.preventDefault();drop.classList.add('tw-over');}); });
  ['dragleave','drop'].forEach(function(ev){ drop.addEventListener(ev,function(e){e.preventDefault();drop.classList.remove('tw-over');}); });
  drop.addEventListener('drop', function(e){ addFiles(e.dataTransfer.files); });

  function poll(url, tries){
    fetch(API+url).then(function(r){return r.json();}).then(function(d){
      if (d.status==='completed'){ done(d); }
      else if (d.status==='failed'){ fail(d.error||'Processing failed'); }
      else if (tries>0){ status.innerHTML='<div class="tw-work">Processing… '+(d.progress||0)+'%</div>'; setTimeout(function(){poll(url,tries-1);},1500); }
      else fail('Timed out. Please try again.');
    }).catch(function(){ if(tries>0) setTimeout(function(){poll(url,tries-1);},1500); else fail('Network error.'); });
  }
  function done(d){
    var extra=[];
    if (d.already_optimized) extra.push('Already optimized');
    else if (d.reduction_pct>0) extra.push(d.reduction_pct+'% smaller');
    if (d.size_human) extra.push(d.size_human);
    status.innerHTML='<div class="tw-ok"><div class="tw-ok-t">Your file is ready</div>'
      +(extra.length?'<div class="tw-ok-m">'+extra.join(' · ')+'</div>':'')+'</div>';
    var a=el('a',{class:'tw-dl',href:API+d.download_url,download:d.filename||''},'Download your file');
    status.appendChild(a);
    var again=el('button',{class:'tw-again',type:'button'},'Process another file');
    again.onclick=function(){ files=[]; renderFiles(); sync(); status.innerHTML=''; btn.style.display=''; };
    status.appendChild(again);
    btn.style.display='none';
  }
  function fail(msg){ status.innerHTML='<div class="tw-err">'+(msg||'Something went wrong.')+'</div>'; btn.disabled=false; btn.textContent=cfg.cta||'Process file'; }

  btn.addEventListener('click', function(){
    if (!files.length) return;
    btn.disabled=true; btn.textContent='Processing…'; status.innerHTML='<div class="tw-work">Uploading…</div>';
    var fd=new FormData();
    var field=cfg.field||(cfg.multi?'files':'file');
    if (cfg.multi) files.forEach(function(f){ fd.append(field,f); });
    else fd.append(field, files[0]);
    for (var k in paramValues) if(paramValues[k]!=='') fd.append(k, paramValues[k]);
    fetch(API+cfg.endpoint, {method:'POST', body:fd}).then(function(r){
      return r.json().then(function(d){ return {ok:r.ok, d:d}; });
    }).then(function(res){
      var d=res.d;
      if (!res.ok || d.success===false){ fail(d.error||'Could not process this file.'); return; }
      if (d.status==='queued' && d.status_url){ status.innerHTML='<div class="tw-work">Processing…</div>'; poll(d.status_url, 60); }
      else if (d.download_url){ done(d); }
      else if (d.metadata){ done({download_url:'', filename:''}); status.innerHTML='<div class="tw-ok"><div class="tw-ok-t">Done</div></div>'; }
      else fail('Unexpected response.');
    }).catch(function(){ fail('Network error. Please try again.'); });
  });
})();
