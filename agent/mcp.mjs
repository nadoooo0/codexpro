// One private MCP session per task: workspace selection never affects web chats.
import fs from 'node:fs';
import os from 'node:os';
import readline from 'node:readline';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
const token = fs.readFileSync(process.env.AGENT_MCP_TOKEN_FILE || `${os.homedir()}/.config/codexpro/http-token`, 'utf8').trim();
const client = new Client({name:'codexpro-autonomous-agent',version:'1.0.0'});
try {
  await client.connect(new StreamableHTTPClientTransport(new URL(process.env.AGENT_MCP_URL || 'http://127.0.0.1:8787/mcp'), {requestInit:{headers:{Authorization:`Bearer ${token}`}}}));
  const workspace = await client.callTool({name:'open_workspace',arguments:{root:process.argv[2]}});
  if (workspace.isError) throw new Error('Workspace could not be opened');
  const allowed = new Set(['read','write','edit','apply_patch','bash','show_changes']);
  const {tools} = await client.listTools();
  process.stdout.write(JSON.stringify({ready:true,workspace:workspace.structuredContent,tools:tools.filter(t=>allowed.has(t.name))})+'\n');
  for await (const line of readline.createInterface({input:process.stdin})) {
    let request;
    try {
      request=JSON.parse(line);
      if (!allowed.has(request.name) && !['bash_status','bash_cancel'].includes(request.name)) throw new Error('Unknown tool');
      const result=await client.callTool({name:request.name,arguments:request.arguments||{}},undefined,{timeout:60000});
      process.stdout.write(JSON.stringify({id:request.id,result})+'\n');
    } catch(error) {
      // A transport error is not an execution failure; the controller reconciles it.
      process.stdout.write(JSON.stringify({id:request?.id,transport_error:true,error_type:error.constructor.name})+'\n');
    }
  }
} catch(error) { process.stdout.write(JSON.stringify({ready:false,error_type:error.constructor.name})+'\n');process.exitCode=1; }
finally { await client.close().catch(()=>{}); }
