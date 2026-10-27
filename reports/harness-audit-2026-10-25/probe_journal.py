import os,sys,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()));root=Path(tempfile.mkdtemp(prefix='shamsu-audit-journal-'));os.environ['SHAMSU_HOME']=str(root/'home')
from shamsu.agents.simple_chat import SimpleChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
w=root/'workspace';w.mkdir();f=w/'a.py';before='def main():\n    return 1\n\ndef helper():\n    return 2\n';f.write_text(before)
loop=SimpleChatLoop(w,client=object(),tools=AgentToolRegistry(w,approval_func=lambda _:True),model_name='qwen2.5-coder:7b')
r=loop._execute('patch_file',{'filepath':'a.py','old_string':'\ndef helper():\n    return 2\n','new_string':''})
manifests=[json.loads(p.read_text()) for p in (w/'.shamsu/mutations').glob('*/manifest.json')]
print(json.dumps({'tool_ok':r.ok,'data':r.data,'file_restored':f.read_text()==before,'journal_states':[m.get('status') for m in manifests],'manifests':manifests},indent=2))
(root/'journal-evidence.json').write_text(json.dumps({'tool_ok':r.ok,'data':r.data,'file_restored':f.read_text()==before,'manifests':manifests},indent=2));print('ARTIFACT_DIR',root)
