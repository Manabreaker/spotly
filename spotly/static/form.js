import {Telemetry} from "./telemetry.js";
import {photoConflicts,recoveryCandidate,uploadOperation} from "./draft-state.js";
const uploadOperations=new Map();
const $=id=>document.getElementById(id), form=$("event-form"), key=$("app").dataset.key;
const base="/api/forms/"+encodeURIComponent(key), localKey="spotly:draft:"+key;
const fields=["title","description","date","time","timezone","hours","minutes","format","address",
  "join_url","join_later","participation","price","capacity_mode","capacity","registration_url"];
let state,tm,dirty=false,paused=true,blocked=false,savePromise=null,saveTimer,maxTimer,
  uploadCount=0,uploadChain=Promise.resolve(),candidate,conflictState,started=false,submitting=false;
const defaultZone=Intl.DateTimeFormat().resolvedOptions().timeZone||"Europe/Moscow";

function raw(){
  const r={};
  for(const name of fields){
    const el=form.elements.namedItem(name);
    r[name]=name==="join_later"?el.checked:el.value;
  }
  return r;
}
function fill(values){
  for(const name of fields){
    const el=form.elements.namedItem(name),v=values[name];
    if(name==="join_later")el.checked=v===true;
    else if(el instanceof RadioNodeList){for(const item of el)item.checked=item.value===v;}
    else el.value=v??(name==="timezone"?defaultZone:"");
  }
  if(!$("timezone").value)$("timezone").value="Europe/Moscow";
  conditional();updateEnd();
  $("description-count").textContent=$("description").value.length+" / 10 000";
}
function localDraft(value=raw()){
  try{localStorage.setItem(localKey,JSON.stringify({attempt_id:state.attempt_id,raw:value,
    base_version:state.version,saved_at:new Date().toISOString()}));}catch{
    $("save-status").textContent="Локальная копия недоступна; сохраняем на сервере";
  }
}
function pendingLabel(){savedLabel(dirty||savePromise?"Сохраняем…":"Сохранено");}
function savedLabel(text,failed=false){$("save-status").textContent=text;$("save-status").classList.toggle("failed",failed);}
function conditional(){
  const r=raw();
  $("address-wrap").hidden=r.format!=="offline";$("online-wrap").hidden=r.format!=="online";
  $("join_url").disabled=r.join_later;$("price-wrap").hidden=r.participation!=="paid";
  $("capacity-wrap").hidden=r.capacity_mode!=="limited";
}
function parts(date,zone){
  return Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:zone,year:"numeric",
    month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"})
    .formatToParts(date).filter(x=>x.type!=="literal").map(x=>[x.type,Number(x.value)]));
}
function zonedTime(r){
  if(!r.date||!r.time||!r.timezone)return null;
  const [y,m,d]=r.date.split("-").map(Number),[h,minute]=r.time.split(":").map(Number);
  const target=Date.UTC(y,m-1,d,h,minute);let guess=target;
  for(let i=0;i<3;i++){
    const p=parts(new Date(guess),r.timezone);
    const actual=Date.UTC(p.year,p.month-1,p.day,p.hour,p.minute);
    if(actual===target)return new Date(guess);
    guess+=target-actual;
  }
  return null;
}
function updateEnd(){
  try{
    const r=raw(),start=zonedTime(r),duration=Number(r.hours||0)*60+Number(r.minutes||0);
    if(!start||!Number.isFinite(duration)||duration<=0||duration>43200){$("end-time").hidden=true;return;}
    const end=new Date(start.getTime()+duration*60000);
    $("end-time").textContent="Окончание: "+new Intl.DateTimeFormat("ru-RU",{timeZone:r.timezone,
      day:"numeric",month:"long",year:"numeric",hour:"2-digit",minute:"2-digit"}).format(end)+" · "+r.timezone;
    $("end-time").hidden=false;
  }catch{$("end-time").hidden=true;}
}
function validURL(value){
  if(!value.trim())return true;
  try{
    if(value.length>4096||/[\s\\<>]/.test(value))return false;
    const u=new URL(value.includes("://")?value:"https://"+value);
    return ["http:","https:"].includes(u.protocol)&&u.hostname.includes(".")&&!u.username&&!u.password;
  }catch{return false;}
}
function validate(r){
  const e={};
  for(const[name,max]of [["title",150],["description",10000]]){
    if(!r[name].trim())e[name]="Заполните это поле";
    else if(r[name].trim().length>max)e[name]="Не больше "+max+" символов";
  }
  if(!r.date||!r.time)e.date="Укажите дату и время начала";
  else{
    try{const start=zonedTime(r);if(!start)e.time="Проверьте время в выбранном часовом поясе";
      else if(start<=new Date())e.date="Начало мероприятия должно быть в будущем";
    }catch{e.timezone="Выберите часовой пояс";}
  }
  const h=r.hours||"0",m=r.minutes||"0",duration=Number(h)*60+Number(m);
  if(!/^\d+$/.test(h)||!/^\d+$/.test(m)||Number(m)>59||duration<1||duration>43200)
    e.duration="Укажите длительность от 1 минуты до 30 суток; минуты — от 0 до 59";
  if(!["offline","online"].includes(r.format))e.format="Выберите формат мероприятия";
  if(r.format==="offline"&&(!r.address.trim()||r.address.length>500))e.address="Укажите адрес длиной до 500 символов";
  if(r.format==="online"&&!r.join_later){
    if(!r.join_url.trim())e.join_url="Укажите ссылку или выберите «Ссылку укажу позже»";
    else if(!validURL(r.join_url))e.join_url="Укажите веб-ссылку, например zoom.us/j/123";
  }
  if(!["free","paid"].includes(r.participation))e.participation="Выберите, платное ли участие";
  if(r.participation==="paid"&&(!/^\d{1,7}([.,]\d{1,2})?$/.test(r.price.trim())
      ||Number(r.price.replace(",","."))<=0||Number(r.price.replace(",","."))>9999999.99))
    e.price="Укажите стоимость больше нуля, до 9 999 999.99 ₽";
  if(!["limited","unlimited"].includes(r.capacity_mode))e.capacity_mode="Укажите, ограничено ли количество мест";
  if(r.capacity_mode==="limited"&&(!/^\d{1,7}$/.test(r.capacity)||Number(r.capacity)<1||Number(r.capacity)>1000000))
    e.capacity="Укажите целое число мест от 1 до 1 000 000";
  if(!validURL(r.registration_url))e.registration_url="Укажите корректную ссылку на регистрацию или профиль организатора";
  return e;
}
function clearErrors(){
  for(const el of form.querySelectorAll(".error"))el.textContent="";
  for(const el of form.querySelectorAll(".invalid"))el.classList.remove("invalid");
  for(const el of form.querySelectorAll("[aria-invalid]"))el.removeAttribute("aria-invalid");
  $("error-summary").hidden=true;
}
function setError(name,message){
  const error=$(name+"-error");if(!error)return;
  error.textContent=message;
  const container=form.querySelector('[data-field="'+name+'"]');
  container?.classList.toggle("invalid",Boolean(message));
  container?.querySelectorAll("input,textarea,select").forEach(e=>e.setAttribute("aria-invalid",message?"true":"false"));
}
function renderErrors(errors,summary=false){
  clearErrors();
  for(const[name,message]of Object.entries(errors)){
    setError(name,message);
    tm?.emit("field_error",name,raw()[name]??null,{message,source:"client"});
  }
  if(summary&&Object.keys(errors).length){
    $("error-summary").textContent="Проверьте отмеченные поля — осталось заполнить или исправить "+Object.keys(errors).length+".";
    $("error-summary").hidden=false;
    const first=form.querySelector(".invalid input,.invalid textarea,.invalid select");
    first?.focus();first?.scrollIntoView({behavior:"smooth",block:"center"});
  }
}
async function request(path,method="GET",body=null,isFile=false){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),isFile?180000:15000);
  try{
    const res=await fetch(base+path,{method,headers:isFile?{}:{"Content-Type":"application/json"},
      body:body===null?null:isFile?body:JSON.stringify(body),signal:controller.signal});
    let data;try{data=await res.json();}catch{data={message:"Сервер временно недоступен"};}
    if(!res.ok){const error=new Error(data.message||(typeof data.detail==="string"?data.detail:"Не удалось сохранить данные"));
      error.status=res.status;error.data=data;throw error;}
    return data;
  }finally{clearTimeout(timer);}
}
function body(value=raw()){return {attempt_id:state.attempt_id,version:state.version,session_id:tm.session,raw:value};}
function adopt(next){
  if(next.attempt_id!==state.attempt_id||next.version>=state.version)state=next;
  drawPhotos();if(state.status==="completed")showSuccess();
}
function adoptPhoto(next){
  if(photoConflicts(state,next,tm.session)){showConflict(next);return false;}
  adopt(next);localDraft();pendingLabel();return true;
}
function showConflict(next){
  blocked=true;paused=true;conflictState=next;$("conflict").hidden=false;
  $("submit-button").disabled=true;savedLabel("Правки не отправлены: форма изменилась",true);
  tm.emit("draft_conflict",null,null,{server_version:next.version});
}
function schedule(immediate=false){
  dirty=true;localDraft();savedLabel(blocked?"Правки сохранены в этом браузере":"Сохраняем…",blocked);
  if(paused||blocked)return;
  clearTimeout(saveTimer);
  if(immediate){void saveNow();return;}
  saveTimer=setTimeout(()=>saveNow(),1000);
  if(!maxTimer)maxTimer=setTimeout(()=>{maxTimer=null;saveNow();},5000);
}
async function saveNow(){
  clearTimeout(saveTimer);clearTimeout(maxTimer);maxTimer=null;
  if(savePromise)return savePromise;
  if(paused||blocked||!dirty||state.status!=="active")return;
  savePromise=(async()=>{
    let retries=0;
    while(dirty&&!paused&&!blocked){
      dirty=false;const snapshot=raw();
      try{
        const next=await request("/draft","PUT",body(snapshot));adopt(next);
        localDraft();savedLabel(dirty?"Сохраняем…":"Сохранено");
        tm.emit("draft_saved",null,null,{version:state.version});
      }catch(error){
        dirty=true;
        if(error.status===409&&error.data.state){
          const next=error.data.state;
          if(next.attempt_id===state.attempt_id&&next.status==="active"&&next.last_session===tm.session&&retries++<2){
            state.version=next.version;continue;
          }
          showConflict(next);return;
        }
        savedLabel("Не сохранено на сервере. Повторим отправку",true);
        tm.emit("draft_save_error",null,null,{status:error.status||"network"});
        setTimeout(()=>saveNow(),5000);return;
      }
    }
  })().finally(()=>{savePromise=null;});
  return savePromise;
}
function showForm(values){
  $("restore").hidden=true;form.hidden=false;paused=false;blocked=false;
  fill(values);tm.start();drawPhotos();
  savedLabel(state.saved_at?"Сохранено":"Черновик сохраняется автоматически");
}
function drawPhotos(readonly=false,target=$("photos")){
  if(!state)return;
  target.replaceChildren();
  state.photos.forEach((p,i)=>{
    const card=document.createElement("div");card.className="photo";
    const img=document.createElement("img");img.src=p.thumbnail;img.alt=p.name;card.append(img);
    if(i===0){const badge=document.createElement("span");badge.className="cover-badge";badge.textContent="Обложка";card.append(badge);}
    const name=document.createElement("span");name.className="photo-name";name.textContent=p.name;card.append(name);
    if(!readonly){
      const controls=document.createElement("div");controls.className="photo-actions";
      for(const[label,title,disabled,action]of [
        ["←","Переместить раньше",i===0,()=>orderPhoto(i,i-1)],
        ["→","Переместить позже",i===state.photos.length-1,()=>orderPhoto(i,i+1)],
        ["×","Удалить фотографию",false,()=>deletePhoto(p.id)]]){
        const btn=document.createElement("button");btn.type="button";btn.textContent=label;btn.title=title;
        btn.setAttribute("aria-label",title+" "+p.name);btn.disabled=disabled;btn.onclick=action;controls.append(btn);
      }
      card.append(controls);
    }
    target.append(card);
  });
}
async function photoMutation(path,method,extra={}){
  await saveNow();if(blocked||dirty)return;
  try{const next=await request(path,method,{...body(),...extra});adoptPhoto(next);}
  catch(error){if(error.status===409)showConflict(error.data.state);else $("photo-error").textContent=error.message;}
}
function orderPhoto(from,to){
  const ids=state.photos.map(p=>p.id);[ids[from],ids[to]]=[ids[to],ids[from]];
  return photoMutation("/photos/order","PUT",{photo_ids:ids});
}
function deletePhoto(id){tm.emit("photo_remove_clicked",null,null,{photo_id:id});return photoMutation("/photos/"+id,"DELETE");}
function queueUpload(operation){
  uploadCount++;$("submit-button").disabled=true;
  uploadChain=uploadChain.then(async()=>{
    if(blocked){$("photo-error").textContent="Сначала выберите версию черновика";return;}
    $("upload-status").textContent="Загружаем и обрабатываем "+operation.file.name+"…";
    $("photo-error").textContent="";
    const startedAt=performance.now(),data=new FormData();
    data.append("file",operation.file);data.append("attempt_id",operation.attempt);
    data.append("session_id",tm.session);data.append("upload_id",operation.id);
    try{
      const next=await request("/photos","POST",data,true);
      uploadOperations.delete(operation.fingerprint);adoptPhoto(next);
      tm.emit("photo_upload_success","photos",null,{name:operation.file.name,duration_ms:Math.round(performance.now()-startedAt)});
    }catch(error){
      tm.emit("photo_error","photos",null,{name:operation.file.name,status:error.status||"network"});
      if(error.status===409&&error.data.state){showConflict(error.data.state);return;}
      // An uncertain response may already have committed. Reconcile before retrying.
      if(!error.status||error.status>=500){
        try{
          const next=await request("");
          if(next.photos.some(p=>p.upload_id===operation.id)){
            uploadOperations.delete(operation.fingerprint);adoptPhoto(next);return;
          }
        }catch{}
        $("photo-error").textContent=operation.file.name+": результат загрузки пока неизвестен. ";
        const retry=document.createElement("button");retry.type="button";retry.className="secondary";
        retry.textContent="Повторить загрузку";retry.onclick=()=>{retry.disabled=true;queueUpload(operation);};
        $("photo-error").append(retry);
      }else{
        uploadOperations.delete(operation.fingerprint);
        $("photo-error").textContent=operation.file.name+": "+error.message;
      }
    }
  }).finally(()=>{
    uploadCount--;$("submit-button").disabled=uploadCount>0||blocked||submitting;
    if(!uploadCount)$("upload-status").textContent=state.photos.length?"Загружено фотографий: "+state.photos.length:"";
  });
}
function uploadFiles(files){
  for(const file of files){
    tm.emit("photo_selected","photos",null,{name:file.name,size:file.size,type:file.type});
    if(file.size>30000000){$("photo-error").textContent=file.name+": файл больше 30 МБ";
      tm.emit("photo_error","photos",null,{name:file.name,code:"file_size"});continue;}
    const operation=uploadOperation(uploadOperations,file);operation.attempt=state.attempt_id;
    queueUpload(operation);
  }
  $("photo-input").value="";
}
function showSuccess(){
  paused=true;dirty=false;tm?.stop();form.hidden=true;$("restore").hidden=true;$("conflict").hidden=true;
  $("success").hidden=false;document.querySelector(".intro").hidden=true;
  try{localStorage.removeItem(localKey);}catch{}
  const event=state.event;if(!event)return;
  const dl=document.createElement("dl");dl.className="summary-list";
  const date=new Intl.DateTimeFormat("ru-RU",{timeZone:event.timezone,day:"numeric",month:"long",
    year:"numeric",hour:"2-digit",minute:"2-digit"}).format(new Date(event.starts_at));
  const list=[["Название",event.title],["Описание",event.description],["Начало",date+" · "+event.timezone],
    ["Продолжительность",Math.floor(event.duration_minutes/60)+" ч "+event.duration_minutes%60+" мин"],
    ["Формат",event.format==="offline"?"Очно":"Онлайн"],
    [event.format==="offline"?"Адрес":"Подключение",event.format==="offline"?event.address:event.join_url||"Ссылка будет указана позже"],
    ["Участие",event.price_kopecks===0?"Бесплатно":(event.price_kopecks/100).toLocaleString("ru-RU",{minimumFractionDigits:2})+" ₽"],
    ["Количество участников",event.capacity===null?"Без ограничения":String(event.capacity)],
    ["Регистрация или контакт организатора",event.registration_url||"Ссылка не указана"]];
  for(const[label,value]of list){
    const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;
    if(typeof value==="string"&&/^https?:\/\//.test(value)){
      const a=document.createElement("a");a.href=value;a.textContent=value;a.target="_blank";a.rel="noopener noreferrer";dd.append(a);
    }else dd.textContent=value;dl.append(dt,dd);
  }
  $("event-summary").replaceChildren(dl);drawPhotos(true,$("success-photos"));
  if(state.feedback_final){$("final-status").textContent="Спасибо! Ваш отзыв сохранён.";$("send-final").disabled=true;}
}
async function restart(){
  $("restart-error").textContent="";$("confirm-restart").disabled=true;
  try{
    if(!form.hidden){await saveNow();if(blocked||dirty)throw Error("Сначала дождитесь сохранения формы");}
    const next=await request("/restart","POST",body(form.hidden?(candidate||state.raw):raw()));
    await tm.flush();state=next;tm.attempt=next.attempt_id;started=false;
    try{localStorage.removeItem(localKey);}catch{}
    $("restart-dialog").close();clearErrors();showForm({});
    savedLabel("Новая форма");tm.emit("attempt_restarted");
  }catch(error){$("restart-error").textContent=error.message;if(error.status===409)showConflict(error.data.state);}
  finally{$("confirm-restart").disabled=false;}
}
async function submit(event){
  event.preventDefault();if(submitting)return;
  tm.emit("submit_clicked",null,raw());
  if(uploadCount){$("photo-error").textContent="Дождитесь завершения загрузки фотографий";return;}
  const errors=validate(raw());renderErrors(errors,true);
  if(Object.keys(errors).length){tm.emit("submit_validation_failed",null,null,{fields:Object.keys(errors)});void saveNow();return;}
  submitting=true;$("submit-button").disabled=true;$("submit-button").textContent="Сохраняем мероприятие…";
  try{
    await saveNow();if(blocked||dirty)throw Error("Не удалось сохранить черновик. Попробуйте ещё раз");
    const next=await request("/submit","POST",body());adopt(next);
    tm.emit("submit_success",null,null,{event_id:next.event_id});void tm.flush();
    $("success").scrollIntoView({block:"start"});
  }catch(error){
    if(error.status===422&&error.data.errors){
      if(error.data.state)adopt(error.data.state);
      renderErrors(error.data.errors,true);
    }else if(error.status===409&&error.data.state)showConflict(error.data.state);
    else{$("error-summary").textContent=error.message;$("error-summary").hidden=false;}
    tm.emit("submit_error",null,null,{status:error.status||"network"});
  }finally{submitting=false;$("submit-button").disabled=blocked;$("submit-button").innerHTML='Создать мероприятие <span aria-hidden="true">↗</span>';}
}
const feedbackIds={};
async function feedback(kind){
  const button=kind==="final"?$("send-final"):$("send-difficulty");
  const comment=kind==="final"?$("final-comment").value:$("difficulty-comment").value;
  const score=document.querySelector('input[name="score"]:checked');
  const data={id:feedbackIds[kind]??=crypto.randomUUID(),attempt_id:state.attempt_id,kind,comment};
  if(kind==="final"&&score)data.score=Number(score.value);
  button.disabled=true;
  try{
    await request("/feedback","POST",data);
    tm.emit("feedback_sent",null,null,{kind,score:data.score??null,comment});
    if(kind==="final"){$("final-status").textContent="Спасибо! Ваш отзыв сохранён.";}
    else{delete feedbackIds[kind];$("difficulty-dialog").close();$("difficulty-comment").value="";savedLabel(dirty?"Сохраняем…":"Отзыв отправлен. Можно продолжить");}
  }catch(error){
    if(kind==="final")$("final-status").textContent="Не удалось отправить отзыв. Попробуйте ещё раз.";
    else $("difficulty-error").textContent="Не удалось отправить отзыв. Попробуйте ещё раз.";
    button.disabled=false;
  }
}
function wire(){
  form.addEventListener("input",event=>{
    const el=event.target;if(!el.name||!fields.includes(el.name))return;
    tm.touch();if(!started){started=true;tm.emit("form_started");}
    if(el.name==="price"&&el.value.includes(",")){
      const before=el.value;el.value=el.value.replaceAll(",",".");
      tm.emit("field_normalized","price",before,{normalized:el.value});
    }
    setError(["hours","minutes"].includes(el.name)?"duration":el.name,"");
    $("description-count").textContent=$("description").value.length+" / 10 000";
    conditional();updateEnd();schedule();
  });
  form.addEventListener("change",event=>{
    const el=event.target;if(!el.name||!fields.includes(el.name))return;
    conditional();updateEnd();tm.emit("field_change",el.name,raw()[el.name]);schedule(true);
  });
  form.addEventListener("focusin",event=>{
    const name=event.target.name;if(!fields.includes(name))return;
    tm.setField(name);tm.emit("field_focus",name);
  });
  form.addEventListener("focusout",event=>{
    const name=event.target.name;if(!fields.includes(name))return;
    tm.emit("field_blur",name,raw()[name]);tm.setField(null);
    const all=validate(raw()),group=["hours","minutes"].includes(name)?"duration":name;
    if(all[group]){setError(group,all[group]);tm.emit("field_error",group,raw()[name],{message:all[group],source:"blur"});}
    schedule(true);
  });
  form.addEventListener("pointerdown",()=>tm.touch());
  form.addEventListener("keydown",()=>tm.touch());
  form.addEventListener("submit",submit);
  $("use-local").onclick=()=>{
    $("recovery").hidden=true;$("restore").hidden=true;
    tm.emit("local_recovery_chosen");showForm(candidate);schedule(true);
  };
  $("use-server").onclick=()=>{
    $("recovery").hidden=true;$("restore").hidden=true;
    tm.emit("server_recovery_chosen");candidate=null;showForm(state.raw);localDraft();
  };
  $("resume").onclick=()=>{tm.emit("draft_resumed");showForm(candidate||state.raw);if(candidate&&JSON.stringify(candidate)!==JSON.stringify(state.raw))schedule(true);};
  $("start-over").onclick=()=>{tm.emit("restart_requested");$("restart-dialog").showModal();};
  $("confirm-restart").onclick=restart;
  $("reload-draft").onclick=async()=>{
    try{
      localStorage.setItem(localKey+":conflict:"+Date.now(),localStorage.getItem(localKey)||"");
      state=await request("");tm.attempt=state.attempt_id;$("conflict").hidden=true;dirty=false;
      if(state.status==="completed")showSuccess();else{showForm(state.raw);$("submit-button").disabled=false;}
    }catch{savedLabel("Не удалось загрузить актуальную версию",true);}
  };
  $("difficulty").onclick=()=>{tm.emit("difficulty_opened");$("send-difficulty").disabled=false;$("difficulty-error").textContent="";$("difficulty-dialog").showModal();};
  $("send-difficulty").onclick=()=>feedback("difficulty");
  $("send-final").onclick=()=>feedback("final");
  document.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>{tm.emit("dialog_cancelled",null,null,{dialog:button.dataset.close});$(button.dataset.close).close();});
  $("photo-input").onchange=event=>uploadFiles([...event.target.files]);
  const drop=$("dropzone");
  for(const name of ["dragenter","dragover"])drop.addEventListener(name,event=>{event.preventDefault();drop.classList.add("dragover");});
  for(const name of ["dragleave","drop"])drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove("dragover");});
  drop.addEventListener("drop",event=>uploadFiles([...event.dataTransfer.files]));
  window.addEventListener("online",()=>{tm.emit("network_online");void saveNow();void tm.flush();});
  window.addEventListener("offline",()=>{savedLabel("Нет сети. Правки сохраняются в этом браузере",true);tm.emit("network_offline");});
}
async function boot(){
  try{
    state=await request("");tm=new Telemetry(key,state.attempt_id);wire();$("loading").hidden=true;
    let local;try{local=JSON.parse(localStorage.getItem(localKey)||"null");}catch{}
    const recovery=recoveryCandidate(local,state);candidate=recovery?.raw||null;
    if(state.status==="completed"){showSuccess();return;}
    if(recovery?.conflict){
      $("recovery").hidden=false;
      $("local-version").textContent=JSON.stringify(candidate,null,2);
      $("server-version").textContent=JSON.stringify(state.raw,null,2);
      tm.emit("local_recovery_conflict");return;
    }
    if(state.saved_at||candidate){
      $("restore").hidden=false;tm.emit("restore_shown");
      const date=state.saved_at||local?.saved_at;
      $("restore-info").textContent="Можно продолжить с того места, где вы остановились."+
        (date?" Последнее сохранение: "+new Date(date).toLocaleString("ru-RU")+".":"");
    }else showForm({});
  }catch{
    $("loading").textContent="Не удалось открыть форму. Проверьте соединение и обновите страницу.";
  }
}
void boot();
