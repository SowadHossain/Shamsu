import asyncio,json,tempfile,sys,urllib.parse
from pathlib import Path
ROOT=Path.cwd();ART=Path(tempfile.mkdtemp(prefix='shamsu-audit-ui-'))
async def main():
 from playwright.async_api import async_playwright
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True);page=await browser.new_page(viewport={'width':1000,'height':800})
  async def route(r):
   path=urllib.parse.urlparse(r.request.url).path
   if path in ['/','/app.js','/app.css']:
    name='app.html' if path=='/' else path[1:]
    await r.fulfill(body=(ROOT/'shamsu/webui/static'/name).read_bytes(),content_type={'app.html':'text/html','app.js':'text/javascript','app.css':'text/css'}[name]);return
   await r.fulfill(json={'workspaces':[],'approvals':[],'queued':[],'commands':[]})
  await page.route('http://audit.local/**',route);await page.goto('http://audit.local/');await page.wait_for_function("typeof approvalCard==='function'")
  out=await page.evaluate("""() => {el('conversation').replaceChildren();el('conversation').append(approvalCard({approval_id:'probe',workspace:'/checkout',description:'Overwrite payments.py',action_type:'file_edit',risk_level:'medium',preview:'DELETE_ALL_PAYMENT_CHECKS_AUDIT_MARKER'}));let submitted=0;el('composer').requestSubmit=()=>submitted++;onComposerKey({key:'Enter',shiftKey:false,isComposing:true,preventDefault(){}});return {preview_shown:el('conversation').textContent.includes('DELETE_ALL_PAYMENT_CHECKS_AUDIT_MARKER'),ime_enter_submits:submitted};} """)
  await page.screenshot(path=str(ART/'approval-preview-missing.png'));await browser.close()
  (ART/'ui-extra-evidence.json').write_text(json.dumps(out,indent=2));print(json.dumps(out));print('ARTIFACT_DIR',ART)
asyncio.run(main())
