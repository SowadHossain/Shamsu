import os,sys,json,tempfile,asyncio,time,urllib.request
from pathlib import Path
ROOT=Path.cwd(); ART=Path(tempfile.mkdtemp(prefix='shamsu-audit-'));os.environ['SHAMSU_HOME']=str(ART/'home')
sys.path.insert(0,str(ROOT))
from shamsu.agents.simple_memory import MemoryStore
from shamsu.agents.simple_chat import SimpleChatLoop,active_tool_schemas
from shamsu.agents.chat_state import ChatState
from shamsu.context.budget import count_tokens
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.agents import simple_contract
results={}
def case(name,fn):
 try: results[name]=fn()
 except Exception as e: results[name]={'probe_error':repr(e)}

def lost_update():
 w=ART/'memory';w.mkdir();a=MemoryStore(w);b=MemoryStore(w)
 a.remember('decision','first choice','Use SQLite')
 b.remember('decision','second choice','Use React')
 return {'notes_after_two_successful_writes':[n.title for n in MemoryStore(w).all_notes()]}
case('memory_lost_update',lost_update)

def failed_persistence():
 w=ART/'failed-memory';w.mkdir();s=MemoryStore(w);s.root.mkdir(parents=True);s.index_path.mkdir()
 ok,msg=s.remember('decision','must persist','Use SQLite')
 return {'reported_success':ok,'reload_count':len(MemoryStore(w).all_notes())}
case('memory_false_success',failed_persistence)

def edit_markdown():
 w=ART/'manual-memory';w.mkdir();s=MemoryStore(w);s.remember('decision','Database','Use SQLite')
 note=next(s.root.glob('*.md'));note.write_text('# Database\nUse PostgreSQL',encoding='utf-8')
 before=MemoryStore(w).all_notes()[0].content
 MemoryStore(w).recall('Database')
 return {'recalled_after_manual_edit':before,'markdown_edit_overwritten':'PostgreSQL' not in note.read_text(encoding='utf-8')}
case('manual_memory_edits_ignored',edit_markdown)

w=ART/'loop';w.mkdir()
# No server config: constructing the registry does not start MCP connections.
loop=SimpleChatLoop(w,client=object(),tools=AgentToolRegistry(w,approval_func=lambda _:False),model_name='qwen2.5-coder:7b')

def mcp_unreachable():
 offered=[s['function']['name'] for s in active_tool_schemas(32768)]
 r=loop._execute('mcp__audit__read_docs',{})
 return {'mcp_schema_count':sum(n.startswith('mcp__') for n in offered),'dispatch_ok':r.ok,'dispatch_message':r.message}
case('default_mcp_unreachable',mcp_unreachable)

def unrelated_evidence():
 loop._contract_tool('contract_create',{'title':'Checkout','assertions':['Payments complete correctly']})
 loop._observed_runs.append('run_command(echo hello) exited 0')
 r=loop._contract_tool('contract_assert_pass',{'assertion_id':'a01','evidence':'Payments work'})
 c=simple_contract.load_contract(w)
 return {'accepted':r.ok,'verified':c.assertions[0].is_verified,'observation':c.assertions[0].observation}
case('unrelated_command_verifies_assertion',unrelated_evidence)

def undercount():
 loop._files=[];loop._request='explain';loop._brief='';loop._skill=''
 loop._project='project fact '*600
 loop._skeletons=lambda:''
 baseline=loop._fixed_overhead()
 loop._standing_plan=lambda:'standing requirement '*600
 with_plan=loop._fixed_overhead()
 msgs=loop._messages()
 return {'fixed_overhead_before':baseline,'fixed_overhead_after_large_plan':with_plan,'actual_grounding_tokens':count_tokens(msgs[-1]['content']),'project_tokens_not_in_fixed_overhead':count_tokens(loop._project)}
case('context_grounding_undercount',undercount)

def orphan_tools():
 state=ChatState('system');state.append_user('fix');state.append_assistant('',tool_calls=[{'id':'c1','function':{'name':'read_file','arguments':{'filepath':'a.py'}}}]);state.append_tool('c1','read_file','X'*200)
 tail,start=state.select_for_budget(30,len)
 return {'selected_roles':[m.role for m in tail],'selected_start':start,'tool_call_ids':[m.tool_call_id for m in tail]}
case('budget_can_orphan_tool_result',orphan_tools)

def foreign_host():
 from shamsu.webui.server import WebPortal
 portal=WebPortal(port=0);portal.start()
 try:
  req=urllib.request.Request(portal.base_url+'/api/workspaces',headers={'Host':'attacker.invalid:'+str(portal.port)})
  with urllib.request.urlopen(req,timeout=5) as r: return {'status':r.status,'foreign_host_accepted':True,'token_required':portal.requires_token}
 finally:portal.stop()
case('portal_foreign_host',foreign_host)

async def ui():
 from playwright.async_api import async_playwright
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  page=await browser.new_page(viewport={'width':1280,'height':800})
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  static=ROOT/'shamsu/webui/static'
  async def route(r):
   path=urllib.parse.urlparse(r.request.url).path
   if path in ['/','/app.js','/app.css']:
    name='app.html' if path=='/' else path[1:]
    await r.fulfill(body=(static/name).read_bytes(),content_type={'app.html':'text/html','app.js':'text/javascript','app.css':'text/css'}[name]);return
   payload={'workspaces':[],'approvals':[],'queued':[],'commands':[]}
   if path.endswith('/messages'):payload={'messages':[]}
   await r.fulfill(json=payload)
  await page.route('http://audit.local/**',route)
  await page.goto('http://audit.local/');await page.wait_for_function("typeof handleEvent === 'function'")
  # The browser optimistically appends a local prompt. Its turn.start echoes it again.
  await page.evaluate("""() => {document.getElementById('conversation').replaceChildren();document.getElementById('conversation').append(bubble('user','Fix checkout'));handleEvent({kind:'turn.start',turn_id:'x',source:'web',text:'Fix checkout'});} """)
  results['ui_duplicate_prompt']={'user_bubble_count':await page.locator('.msg.user').count()}
  await page.screenshot(path=str(ART/'desktop.png'))
  # A first session request comes back after the second one. No request guard.
  race=await page.evaluate("""async () => {subscribe=()=>{};refreshQueue=()=>{};let finishA;api=(path)=>path.includes('/A/')?new Promise(r=>finishA=r):Promise.resolve({messages:[{role:'assistant',content:'ANSWER B'}]});const ws={id:'w',path:'/project'};const first=openSession({session_id:'A',title:'Thread A'},ws);await openSession({session_id:'B',title:'Thread B'},ws);finishA({messages:[{role:'assistant',content:'ANSWER A'}]});await first;return {title:el('thread-title').textContent,body:el('conversation').textContent,session:state.session.session_id};} """)
  results['ui_session_fetch_race']=race
  await page.evaluate("() => {el('drawer').hidden=false;el('close-settings').focus();}")
  await page.keyboard.press('Escape')
  results['ui_settings_escape']={'drawer_still_open':await page.locator('#drawer').is_visible()}
  await page.set_viewport_size({'width':390,'height':844})
  await page.evaluate("() => {el('drawer').hidden=true;normalizeRailForViewport();}")
  await page.screenshot(path=str(ART/'mobile.png'))
  results['ui_runtime']={'errors':errors,'horizontal_overflow':await page.evaluate('document.documentElement.scrollWidth>innerWidth')}
  await browser.close()
try:asyncio.run(ui())
except Exception as e:results['ui_probe_error']=repr(e)
(ART/'evidence.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps({'artifact_dir':str(ART),'results':results},indent=2))
