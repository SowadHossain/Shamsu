import os,sys,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()));root=Path(tempfile.mkdtemp(prefix='shamsu-audit-context-'));os.environ['SHAMSU_HOME']=str(root/'home')
from shamsu.agents.simple_chat import SimpleChatLoop,_budgeted,tool_result_budget
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.context.budget import count_tokens
from shamsu.agents.simple_skills import render_skill
from shamsu.memory.service import MemoryService
from shamsu.agents.simple_memory import render_memory
w=root/'workspace';w.mkdir();f=w/'a.py';f.write_text('def old_name():\n    return 1\n')
loop=SimpleChatLoop(w,client=object(),tools=AgentToolRegistry(w,approval_func=lambda _:False),model_name='qwen2.5-coder:7b');loop._files=['a.py']
a=loop._skeletons();f.write_text('def new_name():\n    return 2\n');b=loop._skeletons()
out={'skeleton_cache_stale':{'unchanged':a==b,'includes_old_name':'old_name' in b,'includes_new_name':'new_name' in b}}
os.environ['SHAMSU_SKILLS']='off';skill=render_skill(w,'debugger fix a python bug',1500)
out['skills_off_ignored']={'injected':bool(skill),'prefix':skill[:140]}
result=MemoryService(w).remember_local('Always use PostgreSQL for this project','user_preference',{'explicit':True,'confidence':1.0})
out['sqlite_memory_not_in_notes']={'saved_ok':result.get('ok'),'injected_memory':render_memory(w,'PostgreSQL')}
raw='字'*10000;clipped=_budgeted(raw,8192)
out['tool_cap_exceeded']={'cap':tool_result_budget(8192),'actual_tokens':count_tokens(clipped),'characters':len(clipped)}
print(json.dumps(out,indent=2));(root/'context-evidence.json').write_text(json.dumps(out,indent=2));print('ARTIFACT_DIR',root)
