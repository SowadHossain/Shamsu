import os,sys,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()));root=Path(tempfile.mkdtemp(prefix='shamsu-audit-scope-'));os.environ['SHAMSU_HOME']=str(root/'home')
from shamsu.agents.simple_chat import SimpleChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
w=root/'workspace';w.mkdir();outside=root/'outside';outside.mkdir();(outside/'private.py').write_text('OUTSIDE_AUDIT_SENTINEL = "outside workspace"\n')
loop=SimpleChatLoop(w,client=object(),tools=AgentToolRegistry(w,approval_func=lambda _:False),model_name='qwen2.5-coder:7b')
r=loop._execute('search_files',{'query':'OUTSIDE_AUDIT_SENTINEL','path':str(outside),'mode':'keyword'})
print(json.dumps({'outside_path_search_ok':r.ok,'result':r.message,'data':r.data},indent=2))
(root/'scope-evidence.json').write_text(json.dumps({'outside_path_search_ok':r.ok,'message':r.message,'data':r.data},indent=2))
print('ARTIFACT_DIR',root)
