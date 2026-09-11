import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { loadConfig } from '../dist/config.js';
import { createCodexProServer } from '../dist/server.js';
const root=await fs.mkdtemp(path.join(os.tmpdir(),'codexpro-generated-access-'));
const dirs=['dist','build','node_modules','.next','coverage','.cache'];
const old=process.env.CODEXPRO_ALLOW_GENERATED_FILES;
async function connect(enabled, extra=[]){
 process.env.CODEXPRO_ALLOW_GENERATED_FILES=enabled?'1':'0';
 const config=loadConfig(['--root',root,'--write','workspace','--tool-mode','minimal']);
 config.toolMode='standard';
 config.blockedGlobs.push(...extra);
 const server=createCodexProServer(config);const [ct,st]=InMemoryTransport.createLinkedPair();await server.connect(st);
 const client=new Client({name:'generated-access-verification',version:'1'});await client.connect(ct);
 return {call:(name,args={})=>client.callTool({name,arguments:args}),close:async()=>{await client.close();await server.close();}};
}
let connection;
try{
 for(const dir of dirs){await fs.mkdir(path.join(root,dir));await fs.writeFile(path.join(root,dir,'probe.txt'),'alpha');}
 await fs.writeFile(path.join(root,'.env'),'DUMMY=not-a-real-secret');
 await fs.mkdir(path.join(root,'dist','.git'));await fs.writeFile(path.join(root,'dist','.git','config'),'fixture');
 await fs.writeFile(path.join(root,'dist','.env'),'DUMMY=fixture');
 await fs.writeFile(path.join(root,'dist','sample.key'),'dummy fixture');
 await fs.symlink(path.join(root,'.env'),path.join(root,'dist','linked.txt'));
 connection=await connect(false);
 assert.equal((await connection.call('read',{path:'dist/probe.txt'})).isError,true,'Default generated-file policy must remain unchanged');
 await connection.close();connection=await connect(true);
 const info=(await connection.call('server_config')).structuredContent;
 for(const dir of dirs){const r=await connection.call('read',{path:`${dir}/probe.txt`});assert.notEqual(r.isError,true,`${dir} must be readable by explicit path`);assert.match(r.structuredContent.text,/alpha/);}
 assert.equal(info.allowGeneratedFiles,true,'Opt-in must be visible to callers');
 assert.notEqual((await connection.call('write',{path:'dist/new.txt',content:'alpha',overwrite:false})).isError,true);
 assert.notEqual((await connection.call('edit',{path:'dist/new.txt',old_text:'alpha',new_text:'beta',expected_replacements:1})).isError,true);
 assert.equal(await fs.readFile(path.join(root,'dist','new.txt'),'utf8'),'beta');
 for(const p of ['.env','dist/.env','dist/.git/config','dist/sample.key','dist/linked.txt'])assert.equal((await connection.call('read',{path:p})).isError,true,`Protected path must stay blocked: ${p}`);
 const tree=await connection.call('tree',{include_hidden:true,max_depth:3});
 assert.notEqual(tree.isError,true,'Discovery tool must execute successfully');
 assert.ok(!JSON.stringify(tree.structuredContent).includes('probe.txt'),'Routine discovery must still omit generated files');
 await connection.close();connection=await connect(true,['dist/probe.txt']);
 assert.equal((await connection.call('read',{path:'dist/probe.txt'})).isError,true,'Explicit custom blocks still apply');
 console.log('PASS: default policy; explicit generated-file read/write/edit; nested protected paths; secret symlink; custom block; discovery exclusion; live config fields.');
}finally{await connection?.close();await fs.rm(root,{recursive:true,force:true});if(old===undefined)delete process.env.CODEXPRO_ALLOW_GENERATED_FILES;else process.env.CODEXPRO_ALLOW_GENERATED_FILES=old;}
