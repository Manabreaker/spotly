import test from "node:test";
import assert from "node:assert/strict";
import {Telemetry} from "../spotly/static/telemetry.js";

function setup() {
  let clock=0;
  Object.defineProperty(globalThis,"performance",{configurable:true,value:{now:()=>clock}});
  globalThis.innerWidth=390;globalThis.innerHeight=844;
  const handlers={};
  globalThis.document={hidden:false,addEventListener:(name,fn)=>handlers[name]=fn};
  globalThis.window={addEventListener:()=>{}};
  globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
  globalThis.setInterval=()=>1;globalThis.setTimeout=()=>1;globalThis.clearTimeout=()=>{};
  globalThis.fetch=()=>new Promise(()=>{});
  const telemetry=new Telemetry("test","attempt");
  telemetry.start();
  return {telemetry,handlers,advance:v=>clock=v};
}
test("counts time before a tab becomes hidden",()=>{
  const {telemetry,handlers,advance}=setup();
  advance(5000);document.hidden=true;handlers.visibilitychange();
  const intervals=telemetry.queue.filter(e=>e.name==="active_interval");
  assert.equal(intervals.length,1);
  assert.equal(intervals[0].details.duration_ms,5000);
});
test("does not count idle time again when activity resumes",()=>{
  const {telemetry,advance}=setup();
  advance(65000);telemetry.checkpoint();
  assert.equal(telemetry.queue.filter(e=>e.name==="active_interval")[0].details.duration_ms,60000);
  advance(70000);telemetry.touch();
  advance(71000);telemetry.checkpoint();
  const intervals=telemetry.queue.filter(e=>e.name==="active_interval");
  assert.equal(intervals.at(-1).details.duration_ms,1000);
});

test("sends in-memory events when local storage cannot persist",async()=>{
  const {telemetry}=setup();
  const requests=[];
  globalThis.localStorage.setItem=()=>{throw Error("quota");};
  globalThis.fetch=async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true};};
  telemetry.sending=false;
  await telemetry.flush();
  assert.equal(requests.length,1);
  assert.equal(requests[0].events[0].name,"page_open");
  assert.equal(telemetry.queue.length,0);
});
