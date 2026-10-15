function sameRaw(a,b){
  const keys=new Set([...Object.keys(a||{}),...Object.keys(b||{})]);
  return [...keys].every(key=>a?.[key]===b?.[key]);
}
export function photoConflicts(before,next,session){
  return next.status!=="completed"&&(before.attempt_id!==next.attempt_id||
    (next.last_session!==session&&!sameRaw(before.raw,next.raw)));
}
export function recoveryCandidate(local,state){
  if(local?.attempt_id!==state.attempt_id||!local.raw)return null;
  return {raw:local.raw,conflict:local.base_version!==state.version&&!sameRaw(local.raw,state.raw)};
}
export function uploadOperation(operations,file){
  const fingerprint=JSON.stringify([file.name,file.size,file.lastModified,file.type]);
  if(!operations.has(fingerprint))operations.set(fingerprint,{id:crypto.randomUUID(),file,fingerprint});
  const operation=operations.get(fingerprint);operation.file=file;return operation;
}
