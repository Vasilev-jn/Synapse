function addItemRow(){
  const list=document.getElementById('items-list');
  const row=list.querySelector('.item-row').cloneNode(true);
  row.querySelectorAll('input').forEach(i=>{ if(i.type !== 'hidden') i.value=''; });
  row.querySelectorAll('select').forEach(s=>{ s.selectedIndex=0; });
  const platform=row.querySelector('[name="item_platform"]');
  if(platform){ platform.value='PlayStation 4'; }
  const quantity=row.querySelector('[name="item_quantity"]');
  if(quantity){ quantity.value='1'; }
  list.appendChild(row);
}
function duplicateLastRow(){
  const list=document.getElementById('items-list');
  const rows=list.querySelectorAll('.item-row');
  list.appendChild(rows[rows.length-1].cloneNode(true));
}
function removeRow(btn){
  const rows=document.querySelectorAll('.item-row');
  if(rows.length>1){ btn.closest('.item-row').remove(); }
}

function initDiscWizard(form){
  const steps=Array.from(form.querySelectorAll('.wizard-step'));
  const dots=Array.from(form.querySelectorAll('.wizard-dot'));
  let index=0;
  function selectedCategory(){
    const selected=form.querySelector('input[name="category_id"]:checked');
    return selected ? (selected.dataset.itemCategory || 'game') : 'game';
  }
  function setCategory(category){
    const input=form.querySelector(`input[name="category_id"][data-item-category="${category}"]`);
    if(input && !input.checked){
      input.checked=true;
    }
  }
  function inferCategory(value){
    const text=value.trim().toLowerCase();
    if(!text){ return null; }
    const accessoryTerms=['dualshock','dualsense','геймпад','джойстик','джостик','hdmi','шдмай','кабель','провод','заряд','подстав','гарнитур','наушник','камера','пульт'];
    if(accessoryTerms.some(term=>text.includes(term))){ return 'accessory'; }
    if(/\b(ps4|ps5)\b/.test(text) || text.includes('playstation 4') || text.includes('playstation 5') || text.includes('play station 4') || text.includes('play station 5')){
      return 'console';
    }
    return null;
  }
  function syncConsoleVersion(value){
    const version=form.querySelector('[name="console_version"]');
    if(!version || version.value){ return; }
    const text=value.trim().toLowerCase();
    if(text.includes('slim') || text.includes('слим')){ version.value='Slim'; }
    else if(text.includes('pro') || text.includes('про')){ version.value='Pro'; }
    else if(text.includes('fat') || text.includes('фет')){ version.value='Fat'; }
  }
  function syncCategoryFromTitle(){
    const title=form.querySelector('#catalog_name');
    if(!title){ return; }
    const category=inferCategory(title.value);
    if(category){ setCategory(category); }
    if(category==='console'){ syncConsoleVersion(title.value); }
  }
  function activeSteps(){
    const category=selectedCategory();
    return steps.filter(step=>{
      const scope=step.dataset.categoryScope;
      return !scope || scope.split(/\s+/).includes(category);
    });
  }
  function hasHiddenAncestor(control){
    let node=control;
    while(node && node!==form){
      if(node.hidden){ return true; }
      node=node.parentElement;
    }
    return false;
  }
  function updateCategoryPanels(){
    const category=selectedCategory();
    form.querySelectorAll('[data-category-panel]').forEach(panel=>{
      panel.hidden=panel.dataset.categoryPanel!==category;
    });
    const complete=form.querySelector('[data-console-complete]');
    const missing=form.querySelector('[data-missing-parts]');
    if(complete && missing){
      missing.hidden=complete.value!=='no';
    }
    const accessoryType=form.querySelector('[data-accessory-type]');
    const originality=form.querySelector('[data-gamepad-originality]');
    const title=form.querySelector('#catalog_name');
    if(accessoryType && originality){
      const text=((accessoryType.value || '')+' '+(title ? title.value : '')).toLowerCase();
      const isGamepad=text.includes('геймпад') || text.includes('джойстик') || text.includes('джостик') || text.includes('dualshock') || text.includes('dualsense') || text.includes('controller');
      originality.hidden=selectedCategory()!=='accessory' || !isGamepad;
    }
  }
  function show(nextIndex){
    updateCategoryPanels();
    const active=activeSteps();
    index=Math.max(0,Math.min(nextIndex,active.length-1));
    steps.forEach(step=>{ step.hidden=true; });
    if(active[index]){ active[index].hidden=false; }
    dots.forEach((dot,i)=>{
      dot.hidden=i>=active.length;
      dot.textContent=String(i+1);
      dot.classList.toggle('active',i===index);
    });
    const first=active[index] ? Array.from(active[index].querySelectorAll('input:not([type="hidden"]),select,textarea,button')).find(control=>!hasHiddenAncestor(control)) : null;
    if(first){ first.focus({preventScroll:true}); }
  }
  function validStep(){
    const active=activeSteps();
    const controls=active[index] ? Array.from(active[index].querySelectorAll('input,select,textarea')) : [];
    for(const control of controls){
      if(hasHiddenAncestor(control)){ continue; }
      if(!control.checkValidity()){
        control.reportValidity();
        return false;
      }
    }
    return true;
  }
  form.querySelectorAll('[data-next]').forEach(button=>{
    button.addEventListener('click',()=>{ if(validStep()){ show(index+1); } });
  });
  form.querySelectorAll('[data-back]').forEach(button=>{
    button.addEventListener('click',()=>show(index-1));
  });
  form.querySelectorAll('input[name="category_id"]').forEach(input=>{
    input.addEventListener('change',()=>show(index));
  });
  const complete=form.querySelector('[data-console-complete]');
  if(complete){ complete.addEventListener('change',()=>show(index)); }
  const accessoryType=form.querySelector('[data-accessory-type]');
  if(accessoryType){ accessoryType.addEventListener('change',()=>show(index)); }
  const title=form.querySelector('#catalog_name');
  if(title){
    title.addEventListener('input',()=>{
      syncCategoryFromTitle();
      updateCategoryPanels();
    });
  }
  form.querySelectorAll('[data-fill][data-category]').forEach(button=>{
    button.addEventListener('click',()=>{
      setCategory(button.dataset.category || 'game');
      updateCategoryPanels();
    });
  });
  syncCategoryFromTitle();
  show(0);
}

function initMenu(){
  const toggle=document.querySelector('[data-menu-toggle]');
  const drawer=document.querySelector('[data-menu-drawer]');
  const backdrop=document.querySelector('[data-menu-close].drawer-backdrop');
  const closers=document.querySelectorAll('[data-menu-close]');
  if(!toggle || !drawer || !backdrop){ return; }
  function setOpen(open){
    drawer.classList.toggle('open',open);
    document.body.classList.toggle('menu-open',open);
    toggle.setAttribute('aria-expanded',String(open));
    drawer.setAttribute('aria-hidden',String(!open));
    backdrop.hidden=!open;
  }
  toggle.addEventListener('click',()=>setOpen(!drawer.classList.contains('open')));
  closers.forEach(item=>item.addEventListener('click',()=>setOpen(false)));
  document.addEventListener('keydown',event=>{ if(event.key==='Escape'){ setOpen(false); } });
}

function initSuggestions(){
  function normalize(value){
    return value.trim().toLowerCase();
  }
  document.querySelectorAll('[data-suggest-input]').forEach(input=>{
    const group=input.dataset.suggestInput || '';
    const box=document.querySelector(`[data-suggestions="${group}"]`);
    if(!box){ return; }
    const chips=Array.from(box.querySelectorAll('[data-suggestion]'));
    const showDefault=box.dataset.showDefault==='true';
    const titleInput=box.dataset.titleSource ? document.querySelector(box.dataset.titleSource) : null;
    function showChip(chip,state){
      chip.hidden=!state;
      return state ? 1 : 0;
    }
    function matchesTitle(chip,titleQuery){
      const search=chip.dataset.gameSearch || '';
      return titleQuery.length>1 && search.includes(titleQuery);
    }
    function render(){
      const query=normalize(input.value);
      const titleQuery=titleInput ? normalize(titleInput.value) : '';
      let shown=0;
      chips.forEach(chip=>{ chip.hidden=true; });
      chips.forEach(chip=>{
        if(shown>=8){ return; }
        if(matchesTitle(chip,titleQuery)){
          shown+=showChip(chip,true);
        }
      });
      chips.forEach(chip=>{
        if(shown>=8 || !chip.hidden){ return; }
        const match=query.length>0 ? chip.dataset.suggestion.includes(query) : showDefault && chip.dataset.defaultSuggestion==='true';
        shown+=showChip(chip,match);
      });
      box.hidden=shown===0;
    }
    input.addEventListener('input',render);
    input.addEventListener('focus',render);
    if(titleInput){ titleInput.addEventListener('input',render); }
    render();
  });
}

function initAutoFilters(){
  document.querySelectorAll('[data-auto-filter]').forEach(form=>{
    let timer=null;
    let controller=null;
    async function submitFilters(){
      clearTimeout(timer);
      if(controller){ controller.abort(); }
      controller=new AbortController();
      const url=new URL(form.action || window.location.href,window.location.origin);
      url.search='';
      for(const [key,value] of new FormData(form).entries()){
        const clean=String(value || '').trim();
        if(clean){ url.searchParams.set(key,clean); }
      }
      try{
        const response=await fetch(url.toString(),{
          headers:{'X-Requested-With':'fetch'},
          signal:controller.signal
        });
        if(!response.ok){ throw new Error(`HTTP ${response.status}`); }
        const text=await response.text();
        const doc=new DOMParser().parseFromString(text,'text/html');
        const nextResults=doc.querySelector('[data-inventory-results]');
        const currentResults=document.querySelector('[data-inventory-results]');
        if(nextResults && currentResults){
          currentResults.replaceWith(nextResults);
          initResizableTables();
          initAdListBuilder();
          window.history.replaceState({},'',url.pathname+(url.search ? url.search : ''));
        }
      }catch(error){
        if(error.name!=='AbortError'){ console.error(error); }
      }
    }
    function submitSoon(){
      clearTimeout(timer);
      timer=setTimeout(submitFilters,450);
    }
    form.addEventListener('submit',event=>{
      event.preventDefault();
      submitFilters();
    });
    form.querySelectorAll('input,select').forEach(control=>{
      const eventName=control.tagName==='SELECT' ? 'change' : 'input';
      control.addEventListener(eventName,submitSoon);
    });
    form.querySelectorAll('[data-filter-chip]').forEach(button=>{
      button.addEventListener('click',()=>{
        const target=form.querySelector(`[name="${button.dataset.filterTarget || 'q'}"]`);
        if(target){
          target.value=button.dataset.value || button.textContent.trim();
          submitFilters();
        }
      });
    });
  });
}
function initResizableTables(){
  document.querySelectorAll('[data-resizable-table]').forEach(table=>{
    const key='table-widths:'+table.dataset.resizableTable;
    const cols=Array.from(table.querySelectorAll('col'));
    try{
      const saved=JSON.parse(localStorage.getItem(key) || '{}');
      cols.forEach(col=>{
        const width=saved[col.dataset.col];
        if(width){ col.style.width=width+'px'; }
      });
    }catch(_error){}

    table.querySelectorAll('.col-resizer').forEach((handle,index)=>{
      handle.addEventListener('mousedown',event=>{
        event.preventDefault();
        const col=cols[index];
        if(!col){ return; }
        const startX=event.clientX;
        const startWidth=col.getBoundingClientRect().width;
        document.body.classList.add('is-resizing-col');
        function move(moveEvent){
          const width=Math.max(56,Math.round(startWidth+moveEvent.clientX-startX));
          col.style.width=width+'px';
        }
        function up(){
          document.removeEventListener('mousemove',move);
          document.removeEventListener('mouseup',up);
          document.body.classList.remove('is-resizing-col');
          const saved={};
          cols.forEach(item=>{
            saved[item.dataset.col]=Math.round(item.getBoundingClientRect().width);
          });
          localStorage.setItem(key,JSON.stringify(saved));
        }
        document.addEventListener('mousemove',move);
        document.addEventListener('mouseup',up);
      });
    });
  });
}

function initToast(){
  const toast=document.querySelector('.toast');
  if(!toast){ return; }
  setTimeout(()=>toast.classList.add('hide'),4200);
  const url=new URL(window.location.href);
  if(url.searchParams.has('created')){
    url.searchParams.delete('created');
    window.history.replaceState({},'',url.pathname+(url.search ? url.search : ''));
  }
}

function initDeleteConfirmation(){
  const key='delete-warning-enabled';
  const toggle=document.querySelector('[data-delete-warning-toggle]');
  const panel=document.querySelector('[data-delete-confirm]');
  const idSlot=document.querySelector('[data-delete-confirm-id]');
  const disable=document.querySelector('[data-delete-warning-disable]');
  const cancel=document.querySelector('[data-delete-cancel]');
  const submit=document.querySelector('[data-delete-submit]');
  let pendingForm=null;
  function isEnabled(){
    return localStorage.getItem(key)!=='off';
  }
  function setEnabled(value){
    localStorage.setItem(key,value ? 'on' : 'off');
    if(toggle){ toggle.checked=value; }
  }
  function hide(){
    if(panel){ panel.hidden=true; }
    if(disable){ disable.checked=false; }
    pendingForm=null;
  }
  if(toggle){
    toggle.checked=isEnabled();
    toggle.addEventListener('change',()=>setEnabled(toggle.checked));
  }
  document.addEventListener('submit',event=>{
    const form=event.target.closest ? event.target.closest('[data-delete-form]') : null;
    if(!form){ return; }
    if(form.dataset.skipDeleteConfirm==='1'){
      delete form.dataset.skipDeleteConfirm;
      return;
    }
    if(!isEnabled() || !panel){
      return;
    }
    event.preventDefault();
    pendingForm=form;
    if(idSlot){ idSlot.textContent=form.dataset.deleteId || ''; }
    if(disable){ disable.checked=false; }
    panel.hidden=false;
  });
  if(cancel){ cancel.addEventListener('click',hide); }
  if(submit){
    submit.addEventListener('click',()=>{
      if(!pendingForm){ return; }
      if(disable && disable.checked){ setEnabled(false); }
      const form=pendingForm;
      hide();
      form.dataset.skipDeleteConfirm='1';
      form.submit();
    });
  }
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape' && panel && !panel.hidden){ hide(); }
  });
}

function initExchangeActions(){
  document.querySelectorAll('[data-auto-submit-file]').forEach(input=>{
    input.addEventListener('change',()=>{
      if(input.files && input.files.length && input.form){
        input.form.submit();
      }
    });
  });
  document.addEventListener('click',event=>{
    document.querySelectorAll('.export-dropdown[open]').forEach(dropdown=>{
      if(!dropdown.contains(event.target)){
        dropdown.removeAttribute('open');
      }
    });
  });
}

const adListSelections=new Map();
let adListMode=false;

function selectedAdLine(row){
  return `${row.dataset.adTitle || ''} - ${row.dataset.adPrice || 'без цены'}`.trim();
}

function showClientToast(message,isError=false){
  const toast=document.createElement('div');
  toast.className='toast';
  if(isError){
    toast.style.borderColor='var(--danger)';
    toast.style.borderLeftColor='var(--danger)';
  }
  toast.setAttribute('role','status');
  toast.textContent=message;
  document.body.appendChild(toast);
  setTimeout(()=>toast.classList.add('hide'),2800);
  setTimeout(()=>toast.remove(),3200);
}

async function copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea=document.createElement('textarea');
  textarea.value=text;
  textarea.setAttribute('readonly','');
  textarea.style.position='fixed';
  textarea.style.left='-9999px';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
}

function updateAdListUi(){
  const toggle=document.querySelector('[data-ad-list-toggle]');
  const cancel=document.querySelector('[data-ad-list-cancel]');
  document.body.classList.toggle('ad-list-mode',adListMode);
  if(toggle){
    toggle.textContent=adListMode ? 'Скопировать' : 'Сформировать список';
    toggle.classList.toggle('primary',adListMode);
  }
  if(cancel){
    cancel.hidden=!adListMode;
  }
  document.querySelectorAll('[data-ad-list-row]').forEach(row=>{
    const checkbox=row.querySelector('[data-ad-list-checkbox]');
    const selected=adListSelections.has(row.dataset.adId || '');
    if(checkbox){ checkbox.checked=selected; }
    row.classList.toggle('ad-selected',selected);
  });
}

function setAdRowSelected(row,selected){
  const id=row.dataset.adId || '';
  if(!id){ return; }
  if(selected){
    adListSelections.set(id,selectedAdLine(row));
  }else{
    adListSelections.delete(id);
  }
  updateAdListUi();
}

function initAdListBuilder(){
  const toggle=document.querySelector('[data-ad-list-toggle]');
  if(!toggle){ return; }
  const cancel=document.querySelector('[data-ad-list-cancel]');
  if(toggle.dataset.adListBound!=='1'){
    toggle.dataset.adListBound='1';
    toggle.addEventListener('click',async()=>{
      if(!adListMode){
        adListMode=true;
        updateAdListUi();
        return;
      }
      if(!adListSelections.size){
        showClientToast('Выберите хотя бы одну игру.',true);
        return;
      }
      try{
        await copyText(Array.from(adListSelections.values()).join('\n'));
        showClientToast(`Список скопирован: ${adListSelections.size} поз.`);
      }catch(error){
        console.error(error);
        showClientToast('Не получилось скопировать список.',true);
      }
    });
  }
  if(cancel && cancel.dataset.adListBound!=='1'){
    cancel.dataset.adListBound='1';
    cancel.addEventListener('click',()=>{
      adListMode=false;
      adListSelections.clear();
      updateAdListUi();
    });
  }
  document.querySelectorAll('[data-ad-list-row]').forEach(row=>{
    const checkbox=row.querySelector('[data-ad-list-checkbox]');
    if(checkbox && checkbox.dataset.adListBound!=='1'){
      checkbox.dataset.adListBound='1';
      checkbox.addEventListener('change',()=>setAdRowSelected(row,checkbox.checked));
    }
    if(row.dataset.adListBound!=='1'){
      row.dataset.adListBound='1';
      row.addEventListener('click',event=>{
        if(!adListMode){ return; }
        if(event.target.closest('a,button,input,label,form')){ return; }
        const current=row.querySelector('[data-ad-list-checkbox]');
        if(!current){ return; }
        current.checked=!current.checked;
        setAdRowSelected(row,current.checked);
      });
    }
  });
  updateAdListUi();
}

function initMarketApiLiveSummary(){
  const root=document.querySelector('[data-market-api-summary]');
  if(!root){ return; }
  const endpoint=root.dataset.endpoint || '/api/market/imports/avito/summary';
  const rawCount=root.querySelector('[data-api-raw-count]');
  const duplicateInline=root.querySelector('[data-api-duplicate-inline]');
  const list=root.querySelector('[data-api-recent-list]');
  let inFlight=false;
  let timer=null;
  let lastSignature='';

  function setText(node,value){
    if(node){ node.textContent=String(value); }
  }
  function renderRecent(rows){
    if(!list){ return; }
    list.innerHTML='';
    if(!rows || !rows.length){
      const empty=document.createElement('div');
      empty.className='empty-state compact-empty';
      empty.textContent='Передач от бота пока нет.';
      list.appendChild(empty);
      return;
    }
    rows.forEach(row=>{
      const article=document.createElement('article');
      article.className='api-transfer-row';
      const title=document.createElement('div');
      const name=document.createElement('strong');
      name.textContent=row.title || 'Без названия';
      const sent=document.createElement('span');
      sent.className='muted-line';
      sent.textContent=`отправлено ${row.sent_at_label || '—'}`;
      title.appendChild(name);
      title.appendChild(sent);
      article.appendChild(title);
      list.appendChild(article);
    });
  }
  async function refresh(){
    if(inFlight){ return; }
    inFlight=true;
    try{
      const response=await fetch(endpoint,{headers:{Accept:'application/json'},cache:'no-store'});
      if(!response.ok){ throw new Error(`HTTP ${response.status}`); }
      const data=await response.json();
      const signature=JSON.stringify([
        data.raw_count,
        data.unique_count,
        data.duplicate_count,
        data.batches_count,
        data.recent_listings
      ]);
      if(signature===lastSignature){ return; }
      lastSignature=signature;
      setText(rawCount,data.raw_count || 0);
      setText(duplicateInline,data.duplicate_count || 0);
      renderRecent(data.recent_listings || []);
    }catch(error){
      console.error(error);
    }finally{
      inFlight=false;
    }
  }
  function start(){
    if(timer){ return; }
    timer=window.setInterval(refresh,4000);
  }
  function stop(){
    if(!timer){ return; }
    window.clearInterval(timer);
    timer=null;
  }
  refresh();
  start();
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){
      stop();
    }else{
      refresh();
      start();
    }
  });
}

function initLiquidSelects(){
  document.querySelectorAll('select').forEach(select=>{
    if(select.dataset.liquidSelectInitialized==='true'){ return; }
    if(select.multiple){ return; }
    select.dataset.liquidSelectInitialized='true';
    select.classList.add('native-liquid-select');

    const wrap=document.createElement('div');
    wrap.className='liquid-select';
    const button=document.createElement('button');
    button.type='button';
    button.className='liquid-select-button';
    button.setAttribute('aria-haspopup','listbox');
    button.setAttribute('aria-expanded','false');
    const label=document.createElement('span');
    const arrow=document.createElement('span');
    arrow.className='liquid-select-arrow';
    arrow.setAttribute('aria-hidden','true');
    button.append(label,arrow);

    const menu=document.createElement('div');
    menu.className='liquid-select-menu';
    menu.setAttribute('role','listbox');
    menu.hidden=true;

    select.parentNode.insertBefore(wrap,select);
    wrap.append(select,button,menu);

    function options(){
      return Array.from(select.options);
    }
    function optionText(option){
      return option ? option.textContent.trim() : '';
    }
    function syncButton(){
      label.textContent=optionText(select.selectedOptions[0] || select.options[select.selectedIndex]) || 'Выбрать';
    }
    function renderMenu(){
      menu.innerHTML='';
      options().forEach((option,index)=>{
        const item=document.createElement('button');
        item.type='button';
        item.className='liquid-select-option';
        item.setAttribute('role','option');
        item.setAttribute('aria-selected',String(option.selected));
        item.textContent=optionText(option);
        item.addEventListener('click',()=>{
          select.selectedIndex=index;
          select.dispatchEvent(new Event('change',{bubbles:true}));
          syncButton();
          renderMenu();
          close();
          button.focus();
        });
        menu.appendChild(item);
      });
    }
    function open(){
      document.querySelectorAll('.liquid-select.open').forEach(node=>{
        if(node!==wrap){
          node.classList.remove('open');
          const otherMenu=node.querySelector('.liquid-select-menu');
          const otherButton=node.querySelector('.liquid-select-button');
          if(otherMenu){ otherMenu.hidden=true; }
          if(otherButton){ otherButton.setAttribute('aria-expanded','false'); }
        }
      });
      renderMenu();
      wrap.classList.add('open');
      menu.hidden=false;
      button.setAttribute('aria-expanded','true');
    }
    function close(){
      wrap.classList.remove('open');
      menu.hidden=true;
      button.setAttribute('aria-expanded','false');
    }

    button.addEventListener('click',event=>{
      event.stopPropagation();
      if(wrap.classList.contains('open')){ close(); }
      else{ open(); }
    });
    button.addEventListener('keydown',event=>{
      if(event.key==='ArrowDown' || event.key==='Enter' || event.key===' '){
        event.preventDefault();
        open();
        const selected=menu.querySelector('[aria-selected="true"]') || menu.querySelector('.liquid-select-option');
        if(selected){ selected.focus(); }
      }
      if(event.key==='Escape'){ close(); }
    });
    menu.addEventListener('keydown',event=>{
      const items=Array.from(menu.querySelectorAll('.liquid-select-option'));
      const current=items.indexOf(document.activeElement);
      if(event.key==='Escape'){
        close();
        button.focus();
      }
      if(event.key==='ArrowDown'){
        event.preventDefault();
        (items[Math.min(items.length-1,current+1)] || items[0])?.focus();
      }
      if(event.key==='ArrowUp'){
        event.preventDefault();
        (items[Math.max(0,current-1)] || items[items.length-1])?.focus();
      }
    });
    select.addEventListener('change',syncButton);
    syncButton();
  });

  document.addEventListener('click',event=>{
    if(event.target.closest('.liquid-select')){ return; }
    document.querySelectorAll('.liquid-select.open').forEach(node=>{
      node.classList.remove('open');
      const menu=node.querySelector('.liquid-select-menu');
      const button=node.querySelector('.liquid-select-button');
      if(menu){ menu.hidden=true; }
      if(button){ button.setAttribute('aria-expanded','false'); }
    });
  });
}

function initSalesChart(){
  const chart=document.querySelector('[data-sales-chart]');
  if(!chart){ return; }
  const endpoint=chart.dataset.endpoint || '/api/stats/sales-chart';
  const range=document.querySelector('[data-sales-chart-range]');
  const buttons=Array.from(document.querySelectorAll('[data-sales-chart-period]'));
  function escapeHtml(value){
    return String(value ?? '').replace(/[&<>"']/g,char=>({
      '&':'&amp;',
      '<':'&lt;',
      '>':'&gt;',
      '"':'&quot;',
      "'":'&#39;'
    }[char]));
  }
  function render(payload){
    chart.dataset.period=payload.period || '';
    if(range){ range.textContent=payload.range_label || ''; }
    const rows=payload.rows || [];
    const svg=chart.querySelector('.sales-line-svg');
    const axis=chart.querySelector('.sales-line-axis');
    if(!svg || !axis){ return; }
    const width=1000;
    const height=300;
    const padX=42;
    const padTop=34;
    const padBottom=44;
    const values=rows.map(row=>Number(row.revenue) || 0);
    const maxValue=Math.max(...values,1);
    const step=rows.length>1 ? (width-padX*2)/(rows.length-1) : 0;
    const points=rows.map((row,index)=>{
      const x=rows.length>1 ? padX + step*index : width/2;
      const value=Number(row.revenue) || 0;
      const y=padTop + (height-padTop-padBottom) * (1 - value/maxValue);
      return {row,index,x,y,value};
    });
    const pointAttr=points.map(point=>`${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
    const linePath=points.map((point,index)=>`${index ? 'L' : 'M'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
    const areaPath=points.length
      ? `M ${points[0].x.toFixed(2)} ${height-padBottom} ${points.map(point=>`L ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ')} L ${points[points.length-1].x.toFixed(2)} ${height-padBottom} Z`
      : '';
    const gridLines=[0,.25,.5,.75,1].map(ratio=>{
      const y=padTop + (height-padTop-padBottom)*ratio;
      return `<line class="sales-grid-line" x1="${padX}" y1="${y.toFixed(2)}" x2="${width-padX}" y2="${y.toFixed(2)}"></line>`;
    }).join('');
    const circles=points.map(point=>{
      const row=point.row;
      const title=`${row.full_label || row.label}: ${row.count} шт, выручка ${row.revenue_label}, прибыль ${row.profit_label}`;
      const tone=row.profit_class || 'metric-neutral';
      const countLabel=Number(row.count) ? `<text class="sales-point-count" x="${point.x.toFixed(2)}" y="${(point.y-14).toFixed(2)}">${escapeHtml(row.count)}</text>` : '';
      return `
        <g class="sales-point ${escapeHtml(tone)}">
          <title>${escapeHtml(title)}</title>
          <circle cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="${Number(row.count) ? 7 : 4}"></circle>
          ${countLabel}
        </g>`;
    }).join('');
    svg.innerHTML=`
      <defs>
        <linearGradient id="salesLineFill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="rgba(88,166,255,.35)"></stop>
          <stop offset="100%" stop-color="rgba(88,166,255,0)"></stop>
        </linearGradient>
      </defs>
      ${gridLines}
      <path class="sales-line-area" d="${areaPath}"></path>
      <polyline class="sales-line-path" points="${pointAttr}"></polyline>
      ${circles}`;
    const every=rows.length>18 ? Math.ceil(rows.length/12) : 1;
    axis.innerHTML=rows.map((row,index)=>{
      const muted=index % every ? ' muted' : '';
      return `<span class="${muted}">${escapeHtml(index % every ? '•' : row.label)}</span>`;
    }).join('');
  }
  async function load(period){
    buttons.forEach(button=>{
      button.classList.toggle('active',button.dataset.salesChartPeriod===period);
      button.disabled=true;
    });
    chart.classList.add('loading');
    try{
      const response=await fetch(`${endpoint}?period=${encodeURIComponent(period)}`,{headers:{'Accept':'application/json'}});
      if(!response.ok){ throw new Error(`HTTP ${response.status}`); }
      render(await response.json());
    }catch(error){
      if(range){ range.textContent='не удалось загрузить график'; }
      console.error('sales chart load failed',error);
    }finally{
      chart.classList.remove('loading');
      buttons.forEach(button=>{ button.disabled=false; });
    }
  }
  buttons.forEach(button=>{
    button.addEventListener('click',()=>{
      const period=button.dataset.salesChartPeriod || 'month';
      if(period===chart.dataset.period){ return; }
      load(period);
    });
  });
  load(chart.dataset.period || 'month');
}

document.addEventListener('DOMContentLoaded',()=>{
  initMenu();
  const wizard=document.querySelector('[data-disc-wizard]');
  if(wizard){ initDiscWizard(wizard); }
  initLiquidSelects();
  initSuggestions();
  initAutoFilters();
  initResizableTables();
  initToast();
  initDeleteConfirmation();
  initExchangeActions();
  initAdListBuilder();
  initMarketApiLiveSummary();
  initSalesChart();
  document.querySelectorAll('[data-fill]').forEach(button=>{
    button.addEventListener('click',()=>{
      const target=document.querySelector(button.dataset.fill);
      if(target){
        target.value=button.dataset.value || button.textContent.trim();
        target.dispatchEvent(new Event('input',{bubbles:true}));
        target.focus();
      }
    });
  });
});

