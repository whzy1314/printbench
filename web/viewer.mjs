const MAX_TRIANGLES = 50000;

export function parseSTL(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength > 20 * 1024 * 1024) throw Error('Preview limit: 20 MiB');
  let triangles = [];
  if (buffer.byteLength >= 84) {
    const view = new DataView(buffer), count = view.getUint32(80,true);
    if (84 + count*50 === buffer.byteLength) {
      if (!count || count > MAX_TRIANGLES) throw Error('Preview limit: 50,000 triangles');
      for (let i=0;i<count;i++) {
        triangles.push(Array.from({length:3},(_,v)=>Array.from({length:3},(_,a)=>view.getFloat32(96+i*50+v*12+a*4,true))));
      }
    }
  }
  if (!triangles.length) {
    const text = new TextDecoder('utf-8',{fatal:true}).decode(buffer).trim();
    if (!/^solid(?:\s|$)/.test(text) || !/endsolid[^\n]*$/.test(text)) throw Error('Invalid or truncated STL');
    const vertices = [...text.matchAll(/\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)/g)].map(m=>m.slice(1).map(Number));
    if (!vertices.length || vertices.length%3 || vertices.length/3 > MAX_TRIANGLES) throw Error('Invalid STL triangle count');
    for(let i=0;i<vertices.length;i+=3) triangles.push(vertices.slice(i,i+3));
  }
  const min=[Infinity,Infinity,Infinity], max=[-Infinity,-Infinity,-Infinity];
  for(const triangle of triangles) for(const v of triangle) for(let a=0;a<3;a++) {
    if(!Number.isFinite(v[a])) throw Error('Nonfinite STL coordinate');
    min[a]=Math.min(min[a],v[a]); max[a]=Math.max(max[a],v[a]);
  }
  const dimensions=max.map((v,a)=>v-min[a]);
  if (!Math.max(...dimensions) || dimensions.some(v=>!Number.isFinite(v))) throw Error('Empty STL bounds');
  return {triangles,dimensions,center:min.map((v,a)=>(v+max[a])/2)};
}

export function projectTriangles(mesh,yaw,pitch,zoom,width,height) {
  const scale=Math.min(width,height)*0.64/Math.hypot(...mesh.dimensions)*zoom;
  const rotate=v=>{
    const [x,y,z]=v.map((c,a)=>c-mesh.center[a]);
    const u=x*Math.cos(yaw)-y*Math.sin(yaw), w=x*Math.sin(yaw)+y*Math.cos(yaw);
    return [u,w*Math.sin(pitch)-z*Math.cos(pitch),w*Math.cos(pitch)+z*Math.sin(pitch)];
  };
  return mesh.triangles.map(triangle=>{
    const v=triangle.map(rotate);
    const a=v[1].map((n,i)=>n-v[0][i]), b=v[2].map((n,i)=>n-v[0][i]);
    const normal=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
    const light=0.55+0.4*Math.abs((normal[0]*0.2-normal[1]*0.5+normal[2]*0.84)/(Math.hypot(...normal)||1));
    return {points:v.map(p=>[width/2+p[0]*scale,height/2+p[1]*scale]),depth:v.reduce((s,p)=>s+p[2],0)/3,light,facing:normal[2]>0};
  }).filter(t=>t.facing).sort((a,b)=>a.depth-b.depth);
}

export class STLViewer {
  constructor(canvas,mesh) {
    this.canvas=canvas; this.mesh=mesh;
    this.reset();
    this.resize=new ResizeObserver(()=>this.draw()); this.resize.observe(canvas);
    let last;
    canvas.addEventListener('pointerdown',e=>{last=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);});
    canvas.addEventListener('pointermove',e=>{
      if(last){this.rotate((e.clientX-last[0])*0.012,(e.clientY-last[1])*0.012);last=[e.clientX,e.clientY];}
    });
    const end=()=>{last=null;};
    canvas.addEventListener('pointerup',end);canvas.addEventListener('pointercancel',end);
    canvas.addEventListener('keydown',e=>{
      const moves={ArrowLeft:[-0.2,0],ArrowRight:[0.2,0],ArrowUp:[0,0.2],ArrowDown:[0,-0.2]};
      if(moves[e.key]){e.preventDefault();this.rotate(...moves[e.key]);}
      if(e.key==='Home'){e.preventDefault();this.reset();}
    });
  }
  rotate(yaw,pitch=0){this.yaw+=yaw;this.pitch+=pitch;this.draw();}
  zoomBy(factor){this.zoom=Math.min(3,Math.max(0.4,this.zoom*factor));this.draw();}
  reset(){this.yaw=-0.6;this.pitch=0.65;this.zoom=1;this.draw();}
  draw(){
    const canvas=this.canvas, width=canvas.clientWidth, height=canvas.clientHeight;
    const ratio=Math.min(window.devicePixelRatio||1,2);
    canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);
    const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
    ctx.fillStyle='#101d27';ctx.fillRect(0,0,width,height);
    ctx.strokeStyle='#20303d';ctx.lineWidth=1;
    for(let x=width/2%32;x<width;x+=32){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();}
    for(let y=height/2%32;y<height;y+=32){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();}
    for(const t of projectTriangles(this.mesh,this.yaw,this.pitch,this.zoom,width,height)){
      ctx.beginPath();t.points.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.closePath();
      ctx.fillStyle=`rgb(${Math.round(41*t.light)},${Math.round(204*t.light)},${Math.round(180*t.light)})`;
      ctx.fill();ctx.strokeStyle='#144b48';ctx.lineWidth=0.7;ctx.stroke();
    }
  }
}
