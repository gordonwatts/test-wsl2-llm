"""Exercise the generated page's filters in a tiny DOM under Node.js."""

import shutil
import subprocess

import pytest

from test_wsl2_llm.performance import render_performance

NODE_SMOKE = r"""
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync(0,'utf8');
const data=html.match(/<script id="records" type="application\/json">([\s\S]*?)<\/script>/)[1];
const code=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const elements=new Map();
function element(id){
 if(!elements.has(id)){
  const value={id,textContent:id==='records'?data:'',value:'',disabled:false,inputs:[],handlers:{},
   addEventListener(name,fn){this.handlers[name]=fn},
   fire(name,event={}){this.handlers[name]({...event,target:event.target||this})},
   querySelectorAll(){return this.inputs},
   get innerHTML(){return this.html||''},
   set innerHTML(html){this.html=html;if(id.endsWith('-options')){
    this.inputs=[...html.matchAll(/<input type="checkbox" value="([^"]*)" checked>/g)]
      .map(match=>({type:'checkbox',value:match[1],checked:true}));
   }}
  };elements.set(id,value);
 }
 return elements.get(id);
}
vm.runInNewContext(code,{document:{getElementById:element}});
const count=()=>Number(element('cards').innerHTML.match(/Trials<strong>(\d+)<\/strong>/)[1]);
function choose(field,values){
 element(field+'-clear').fire('click');
 for(const value of values){const box=element(field+'-options').inputs.find(x=>x.value===value);
  assert.ok(box,field+': '+value);box.checked=true;
  element(field+'-options').fire('change',{target:box});
 }
}
assert.equal(count(),6);
choose('model',['model-a','model-b']);assert.equal(count(),4);
assert.equal(element('model-summary').textContent,'2 models selected');
choose('question',['q1','q2']);assert.equal(count(),3);
assert.equal(element('question-summary').textContent,'2 questions selected');
assert.match(element('pass-matrix').innerHTML,/batch-a \/ q1/);
assert.match(element('pass-matrix').innerHTML,/batch-b \/ q2/);
choose('directory',['batch-b']);assert.equal(count(),1);
assert.match(element('pass-matrix').innerHTML,/<th title="q2">q2<\/th>/);
choose('agent',['claude']);assert.equal(count(),1);
choose('target',['local']);assert.equal(count(),1);
choose('outcome',['fail']);assert.equal(count(),0);
choose('outcome',['pass','fail']);assert.equal(count(),1);
element('agent-all').fire('click');element('target-all').fire('click');
element('directory-all').fire('click');assert.equal(count(),3);
assert.equal(element('directory-summary').textContent,'All directories');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for HTML behavior test")
def test_all_filters_multiselect_and_intersect() -> None:
    cases = [
        ("batch-a", "model-a", "q1", "codex", "wsl", True),
        ("batch-a", "model-b", "q2", "codex", "wsl", False),
        ("batch-b", "model-a", "q2", "claude", "local", True),
        ("batch-b", "model-c", "q1", "claude", "wsl", False),
        ("batch-b", "model-b", "q3", "codex", "local", True),
        ("batch-a", "model-c", "q3", "claude", "local", False),
    ]
    records = [
        {
            "directory": directory,
            "model": model,
            "question": question,
            "question_label": f"{directory} / {question}",
            "agent": agent,
            "target": target,
            "passed": passed,
            "cost": 0.1,
            "currency": "USD",
            "tokens": 10,
            "trial": 1,
            "file": f"{directory}/{question}.yaml",
            "started": "now",
            "seconds": 1,
            "validation": [],
            "error": None,
            "prompt": "long prompt",
            "response": "done",
        }
        for directory, model, question, agent, target, passed in cases
    ]
    result = subprocess.run(
        [shutil.which("node"), "-e", NODE_SMOKE],
        input=render_performance(records),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
