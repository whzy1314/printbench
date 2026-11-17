import test from 'node:test';
import assert from 'node:assert/strict';
import { parseSTL, projectTriangles } from '../web/viewer.mjs';

function binary() {
  const buffer = new ArrayBuffer(134);
  const view = new DataView(buffer);
  view.setUint32(80,1,true);
  [0,0,0,30,0,0,0,20,3].forEach((v,i)=>view.setFloat32(96+i*4,v,true));
  return buffer;
}

test('binary STL uses actual vertices and computes bounds',()=>{
  const mesh = parseSTL(binary());
  assert.equal(mesh.triangles.length,1);
  assert.deepEqual(mesh.dimensions,[30,20,3]);
  assert.deepEqual(mesh.triangles[0][1],[30,0,0]);
});
test('ASCII STL renders and rotation changes projected geometry',()=>{
  const text = 'solid tile\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 30 0 0\nvertex 0 20 3\nendloop\nendfacet\nendsolid tile';
  const mesh = parseSTL(new TextEncoder().encode(text).buffer);
  const first = projectTriangles(mesh,0.3,0.5,1,600,400);
  const rotated = projectTriangles(mesh,1.2,0.8,1,600,400);
  assert.notDeepEqual(first,rotated);
  assert.equal(first.length,1);
  assert.ok(first.flatMap(t=>t.points.flat()).every(Number.isFinite));
});
test('hidden back faces cannot paint over a visible triangle',()=>{
  const visible=[[0,0,0],[30,0,0],[0,20,3]];
  const mesh={triangles:[visible,[...visible].reverse()],dimensions:[30,20,3],center:[15,10,1.5]};
  assert.equal(projectTriangles(mesh,0.3,0.5,1,600,400).length,1);
});

test('malformed, empty, nonfinite, oversized and truncated STL reject',()=>{
  for (const buffer of [new ArrayBuffer(0),binary().slice(0,130),new TextEncoder().encode('not STL').buffer]) {
    assert.throws(()=>parseSTL(buffer));
  }
  const nan = binary(); new DataView(nan).setFloat32(96,NaN,true);
  assert.throws(()=>parseSTL(nan));
  const large = binary(); new DataView(large).setUint32(80,1000000,true);
  assert.throws(()=>parseSTL(large));
});
