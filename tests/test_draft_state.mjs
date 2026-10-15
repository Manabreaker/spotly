import test from 'node:test';
import assert from 'node:assert/strict';
import {photoConflicts, recoveryCandidate, uploadOperation} from '../spotly/static/draft-state.js';
test('photo response cannot advance an unrelated text revision',()=>{
  const before={attempt_id:'a',raw:{title:'old',description:'d'}};
  assert.equal(photoConflicts(before,{attempt_id:'a',last_session:'other',raw:{title:'new'}},'me'),true);
  assert.equal(photoConflicts(before,{attempt_id:'a',last_session:'other',raw:{description:'d',title:'old'}},'me'),false);
  assert.equal(photoConflicts(before,{attempt_id:'a',last_session:'me',raw:{title:'new'}},'me'),false);
});
test('local edits remain recoverable after an unacknowledged save',()=>{
  const local={attempt_id:'a',base_version:0,raw:{title:'AB'}};
  assert.deepEqual(recoveryCandidate(local,{attempt_id:'a',version:1,raw:{title:'A'}}),{raw:{title:'AB'},conflict:true});
  assert.equal(recoveryCandidate(local,{attempt_id:'b',version:1}),null);
});
test('uncertain upload retries keep the same operation identifier',()=>{
  const ops=new Map(),file={name:'photo.heic',size:100,lastModified:500,type:'image/heic'};
  assert.equal(uploadOperation(ops,file).id,uploadOperation(ops,{...file}).id);
});
