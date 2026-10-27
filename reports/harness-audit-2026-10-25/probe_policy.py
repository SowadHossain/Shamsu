import os,sys,json,tempfile,asyncio
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path.cwd()))
root=Path(tempfile.mkdtemp(prefix='shamsu-audit-policy-'));os.environ['SHAMSU_HOME']=str(root/'home');os.environ.pop('SHAMSU_LEGACY_ROUTING',None)
from shamsu.agents.simple_chat import SimpleChatLoop
from shamsu.cli.noninteractive import run_prompt
from shamsu.agents.simple_verify import check_text
async def fake_call(self):
 self._probe_count=getattr(self,'_probe_count',0)+1
 if self._probe_count==1:
  return {'message':{'role':'assistant','content':'','tool_calls':[{'id':'c1','type':'function','function':{'name':'write_file','arguments':{'filepath':'forbidden.py','content':'print(42)\n'}}}]}}
 return {'message':{'role':'assistant','content':'Created forbidden.py.'}}
async def main():
 out={}
 for name,approval,dry,prompt in [
  ('deny','deny',False,'Create forbidden.py'),
  ('dryrun','deny',True,'Create forbidden.py'),
  ('read_only','allow',False,'Review the project only. Do not create, edit, or delete any files.')]:
  w=root/name;w.mkdir();(w/'keep.py').write_text('x=1\n')
  with patch('shamsu.agents.chat_loop._default_ollama_client',return_value=object()),patch.object(SimpleChatLoop,'_call_model',fake_call),patch.object(SimpleChatLoop,'_refresh_code_graph',lambda self:None),patch.object(SimpleChatLoop,'_warn_if_spilled_to_ram',lambda self:None):
   result=await run_prompt(w,prompt,approval=approval,dry_run=dry,timeout_s=15)
  out[name]={'file_created':(w/'forbidden.py').exists(),'status':result.status,'error':result.error,'approvals':len(result.approvals),'planned_mutations':getattr(result,'planned_mutations',None),'final':result.final_response[:200]}
 out['typescript_false_syntax_pass']={'text':'const x: = 1;','verdict':str(check_text('const x: = 1;','.ts'))}
 print(json.dumps({'artifact_dir':str(root),'results':out},indent=2))
 (root/'policy-evidence.json').write_text(json.dumps(out,indent=2))
asyncio.run(main())
