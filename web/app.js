import {parseSTL,STLViewer} from './viewer.mjs';

const app=document.querySelector('#app');
function el(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function link(text,href,className){const node=el('a',text,className);node.href=href;return node;}
const enc=encodeURIComponent;
const projectURL=(id,rev)=>`/?project=${enc(id)}${rev?`&revision=${enc(rev)}`:''}`;
const fileURL=(p,name)=>`/api/projects/${enc(p.id)}/revisions/${enc(p.revision)}/files/${enc(name)}`;
async function api(path){const response=await fetch(path);if(!response.ok)throw Error((await response.json()).error||'Request failed');return response.json();}
function statuses(p){
  const group=el('div',undefined,'statuses');
  for(const key of ['mesh','slicer','physical']){
    const value=p.status[key]||'unknown', badge=el('span',`${key[0].toUpperCase()+key.slice(1)} · ${value}`,`status ${value}`);
    group.append(badge);
  }
  return group;
}
function heading(kicker,title,description){const node=el('section',undefined,'heading');node.append(el('p',kicker,'eyebrow'),el('h1',title),el('p',description,'muted'));app.append(node);}
async function library(){
  const projects=await api('/api/projects');app.replaceChildren();
  heading('YOUR MAKER WORKSPACE','Project library','A clear home for every model, revision and piece of evidence.');
  const bar=el('div',undefined,'section-bar');bar.append(el('h2','All projects'),el('span',`${projects.length} project${projects.length===1?'':'s'}`,'muted'));app.append(bar);
  const grid=el('div',undefined,'library');
  for(const p of projects){
    const card=link('',projectURL(p.id), 'project-card');
    if(p.thumbnail){const image=el('img');image.src=fileURL(p,p.thumbnail);image.alt=`${p.name} evidence thumbnail`;card.append(image);}else card.append(el('div','No thumbnail supplied','no-thumbnail'));
    const body=el('div',undefined,'card-body');body.append(el('p',`LATEST · ${p.revision}`,'eyebrow'),el('h3',p.name),el('p',`${p.dimensions_mm.join(' × ')} mm`,'muted'),statuses(p),el('span','Open project →','card-action'));card.append(body);grid.append(card);
  }
  if(!projects.length)grid.append(el('p','No projects yet. Publish a revision with the local CLI.'));
  app.append(grid);
}
async function detail(id,revision){
  const base=`/api/projects/${enc(id)}`;
  const [history,p]=await Promise.all([api(`${base}/history`),api(revision?`${base}/revisions/${enc(revision)}`:base)]);
  document.title=`${p.name} · PrintBench`;app.replaceChildren(link('← Project library','/','back'));
  heading('PROJECT / '+p.id,p.name,'Immutable files. Explicit evidence. A separate record for physical testing.');
  const revisionBar=el('div',undefined,'revision-bar');revisionBar.append(el('strong',`Revision ${p.revision}`),el('span',p.revision===history.current?'Current revision':'Historical revision','muted'));
  const label=el('label','Browse revisions '),select=el('select');select.setAttribute('aria-label','Browse revisions');
  for(const rev of history.revisions.toReversed()){const option=el('option',rev+(rev===history.current?' · current':''));option.value=rev;option.selected=rev===p.revision;select.append(option);}
  select.addEventListener('change',()=>{window.location.href=projectURL(p.id,select.value);});label.append(select);revisionBar.append(label);app.append(revisionBar,statuses(p));
  const layout=el('div',undefined,'detail-grid'),preview=el('section',undefined,'panel preview'),info=el('div',undefined,'info');
  preview.append(el('div','STL PREVIEW','eyebrow'));const status=el('p','Loading actual mesh…','muted');preview.append(status);
  const stl=p.artifacts.find(a=>a.name.toLowerCase().endsWith('.stl'));
  if(stl){
    const canvas=el('canvas');canvas.tabIndex=0;canvas.setAttribute('role','img');canvas.setAttribute('aria-label',`${p.name} STL. Drag to rotate or use arrow keys; Home resets.`);preview.append(canvas);
    const controls=el('div',undefined,'viewer-controls');preview.append(controls,el('p','Drag to orbit · Arrow keys to rotate · Home to reset. Preview only; not a geometry check.','viewer-help'));
    layout.append(preview,info);app.append(layout);
    try{
      const response=await fetch(fileURL(p,stl.name));if(!response.ok)throw Error('STL integrity or download failure');
      const buffer=await response.arrayBuffer(), hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',buffer)),b=>b.toString(16).padStart(2,'0')).join('');
      if(hash!==stl.sha256)throw Error('STL hash mismatch');
      const mesh=parseSTL(buffer),viewer=new STLViewer(canvas,mesh);
      status.textContent=`${mesh.triangles.length} triangles · ${mesh.dimensions.map(v=>Number(v.toFixed(3))).join(' × ')} mm · SHA-256 verified`;
      for(const [text,action] of [['Rotate left',()=>viewer.rotate(-0.4)],['Rotate right',()=>viewer.rotate(0.4)],['Tilt',()=>viewer.rotate(0,0.3)],['Zoom in',()=>viewer.zoomBy(1.2)],['Zoom out',()=>viewer.zoomBy(1/1.2)],['Reset view',()=>viewer.reset()]]){const button=el('button',text);button.type='button';button.addEventListener('click',action);controls.append(button);}
    }catch(error){status.textContent=`Preview unavailable: ${error.message}. Original files remain downloadable.`;canvas.remove();}
  }else{status.textContent='No STL supplied for this revision.';layout.append(preview,info);app.append(layout);}
  const panel=(title)=>{const node=el('section',undefined,'panel');node.append(el('h2',title));info.append(node);return node;};
  const requirements=panel('Design requirements'),list=el('ul');p.requirements.forEach(r=>list.append(el('li',r)));requirements.append(list);
  const dimensions=panel('Dimensions');dimensions.append(el('p',`${p.dimensions_mm.join(' × ')} mm`,'dimension-value'),el('p','X × Y × Z · declared design dimensions','muted'));
  panel('Revision notes').append(el('p',p.notes,'notes'));
  const evidence=panel('Check evidence');for(const key of ['mesh','slicer','physical']){
    const row=el('p');row.append(el('strong',`${key[0].toUpperCase()+key.slice(1)}: ${p.status[key]}. `));
    row.append(p.status_evidence[key]?link(p.status_evidence[key],fileURL(p,p.status_evidence[key])):el('span','No evidence supplied.','muted'));evidence.append(row);
  }
  const files=el('section',undefined,'panel downloads');files.append(el('div','REVISION ARTIFACTS','eyebrow'),el('h2','Files & provenance'),el('p','Downloads are served only after SHA-256 verification. Originals are never modified.','muted'));
  for(const a of p.artifacts){
    const row=el('article',undefined,'file-row'),body=el('div');body.append(link(a.name,fileURL(p,a.name),'filename'),el('p',`${a.bytes.toLocaleString()} bytes · SHA-256`,'muted'),el('code',a.sha256),el('p',`Source: ${a.provenance.source_directory}/${a.provenance.source_name}`,'provenance'));
    const download=link('Download ↓',fileURL(p,a.name),'download');download.download=a.name;row.append(body,download);files.append(row);
  }
  app.append(files);
}
const params=new URLSearchParams(window.location.search);
try{if(params.has('project'))await detail(params.get('project'),params.get('revision'));else await library();}
catch(error){app.replaceChildren(el('h1','Unable to load workspace'),el('p',error.message),link('Return to library','/'));}
