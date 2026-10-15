export class Telemetry {
  constructor(key, attempt) {
    this.key=key; this.attempt=attempt; this.session=crypto.randomUUID(); this.sequence=0;
    this.storageKey="spotly:events:"+key+":"+this.session;
    this.queue=[]; this.sending=false; this.field=null; this.active=false;
    this.lastAction=performance.now(); this.lastCheckpoint=this.lastAction; this.visible=!document.hidden;
    this.origin=Date.now()-performance.now();
    this.retry=0;
    this.timer=setInterval(()=>{this.checkpoint();this.flush();},5000);
    document.addEventListener("visibilitychange",()=>{
      this.checkpoint(); this.visible=!document.hidden; this.emit(document.hidden?"page_hidden":"page_visible");
      this.lastCheckpoint=performance.now(); if(!document.hidden)this.touch(); this.flush();
    });
    window.addEventListener("pagehide",()=>{this.checkpoint();this.emit("page_leave");this.flush(true);});
    this.emit("page_open",null,null,{device:innerWidth<700?"mobile":"desktop",
      viewport:{width:innerWidth,height:innerHeight},timezone:Intl.DateTimeFormat().resolvedOptions().timeZone});
    this.flush();
  }
  persist() {try {localStorage.setItem(this.storageKey,JSON.stringify(this.queue));}catch{}}
  emit(name,field=null,value=null,details={}) {
    this.queue.push({id:crypto.randomUUID(),attempt_id:this.attempt,session_id:this.session,
      sequence:++this.sequence,name,field,value,details,occurred_at:new Date().toISOString()});
    this.persist(); if(this.queue.length>=50)this.flush();
  }
  touch() {
    const now=performance.now();
    if(now-this.lastAction>60000 && this.active){
      this.checkpoint();this.lastCheckpoint=now;this.emit("activity_resumed",this.field);
    }
    this.lastAction=now;
  }
  start() {this.active=true;this.lastAction=performance.now();this.lastCheckpoint=this.lastAction;}
  stop() {this.checkpoint();this.active=false;}
  checkpoint() {
    const now=performance.now(),end=Math.min(now,this.lastAction+60000);
    if(this.active && this.visible && end>this.lastCheckpoint){
      this.emit("active_interval",this.field,null,{duration_ms:Math.round(end-this.lastCheckpoint),
        start_ms:Math.round(this.origin+this.lastCheckpoint),end_ms:Math.round(this.origin+end)});
    }
    if(this.active && this.lastCheckpoint<this.lastAction+60000 && now>=this.lastAction+60000)
      this.emit("activity_idle",this.field);
    this.lastCheckpoint=now;
  }
  setField(field){this.checkpoint();this.field=field;this.touch();}
  async flush(keepalive=false) {
    if(this.sending)return;
    this.sending=true;
    try{
      let stored=[];try{stored=Object.keys(localStorage).filter(k=>k.startsWith("spotly:events:"+this.key+":"));}catch{}
      const keys=[...new Set([this.storageKey,...stored])];
      for(const key of keys){
        let queue;
        try{queue=key===this.storageKey?this.queue:JSON.parse(localStorage.getItem(key)||"[]");}catch{continue;}
        if(!queue.length)continue;
        const batch=[];let size=0;
        for(const item of queue){
          const bytes=new TextEncoder().encode(JSON.stringify(item)).length;
          if(batch.length>=100 || size+bytes>(keepalive?50000:950000))break;
          batch.push(item);size+=bytes;
        }
        if(!batch.length)continue;
        const response=await fetch("/api/forms/"+encodeURIComponent(this.key)+"/events",{
          method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({events:batch}),keepalive});
        if(!response.ok)throw Error("events "+response.status);
        const sent=new Set(batch.map(e=>e.id));
        if(key===this.storageKey){
          this.queue=this.queue.filter(e=>!sent.has(e.id));this.persist();
        }else{
          const latest=JSON.parse(localStorage.getItem(key)||"[]").filter(e=>!sent.has(e.id));
          if(latest.length)localStorage.setItem(key,JSON.stringify(latest));else localStorage.removeItem(key);
        }
      }
      this.retry=0;
    }catch{
      const delays=[1000,2000,5000,10000,30000];
      clearTimeout(this.retryTimer);
      this.retryTimer=setTimeout(()=>this.flush(),delays[Math.min(this.retry++,4)]);
    }finally{this.sending=false;}
  }
}
