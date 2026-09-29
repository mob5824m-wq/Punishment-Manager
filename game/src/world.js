// Voxel world: generation, chunk meshing (with baked AO), raycasting.
import * as THREE from 'three';
import {BLOCKS, AIR, WATER, isSolid, isOpaque, TILE, ATLAS_COLS} from './blocks.js';

export const CS = 16;              // chunk size (x,z)
export const H  = 40;              // world height
export const CHUNKS = 6;           // chunks per axis
export const WS = CS * CHUNKS;     // world size in blocks
export const SEA = 9;

/* ---------------- value noise ---------------- */
function hash2(x,y,s){
  let h=x*374761393+y*668265263+s*1442695040888963407;
  h=(h^(h>>13))*1274126177; h^=h>>16;
  return ((h>>>0)%100000)/100000;
}
function smooth(t){return t*t*(3-2*t);}
function vnoise(x,y,s){
  const xi=Math.floor(x), yi=Math.floor(y), xf=x-xi, yf=y-yi;
  const a=hash2(xi,yi,s), b=hash2(xi+1,yi,s), c=hash2(xi,yi+1,s), d=hash2(xi+1,yi+1,s);
  const u=smooth(xf), v=smooth(yf);
  return a*(1-u)*(1-v)+b*u*(1-v)+c*(1-u)*v+d*u*v;
}
function fbm(x,y,s,oct=4){
  let v=0,amp=.5,f=1,tot=0;
  for(let i=0;i<oct;i++){v+=vnoise(x*f,y*f,s+i*77)*amp;tot+=amp;amp*=.5;f*=2;}
  return v/tot;
}

/* ---------------- geometry helpers ---------------- */
// face: dir index 0:+x 1:-x 2:+y 3:-y 4:+z 5:-z
const FACES=[
  {n:[ 1,0,0], v:[[1,0,0],[1,1,0],[1,1,1],[1,0,1]], tile:1, shade:0.78,
   ao:[[[1,-1,0],[1,0,-1],[1,-1,-1]],[[1,1,0],[1,0,-1],[1,1,-1]],[[1,1,0],[1,0,1],[1,1,1]],[[1,-1,0],[1,0,1],[1,-1,1]]]},
  {n:[-1,0,0], v:[[0,0,1],[0,1,1],[0,1,0],[0,0,0]], tile:1, shade:0.78,
   ao:[[[-1,-1,0],[-1,0,1],[-1,-1,1]],[[-1,1,0],[-1,0,1],[-1,1,1]],[[-1,1,0],[-1,0,-1],[-1,1,-1]],[[-1,-1,0],[-1,0,-1],[-1,-1,-1]]]},
  {n:[0, 1,0], v:[[0,1,1],[1,1,1],[1,1,0],[0,1,0]], tile:0, shade:1.0,
   ao:[[[-1,1,0],[0,1,1],[-1,1,1]],[[1,1,0],[0,1,1],[1,1,1]],[[1,1,0],[0,1,-1],[1,1,-1]],[[-1,1,0],[0,1,-1],[-1,1,-1]]]},
  {n:[0,-1,0], v:[[0,0,0],[1,0,0],[1,0,1],[0,0,1]], tile:2, shade:0.55,
   ao:[[[-1,-1,0],[0,-1,-1],[-1,-1,-1]],[[1,-1,0],[0,-1,-1],[1,-1,-1]],[[1,-1,0],[0,-1,1],[1,-1,1]],[[-1,-1,0],[0,-1,1],[-1,-1,1]]]},
  {n:[0,0, 1], v:[[1,0,1],[1,1,1],[0,1,1],[0,0,1]], tile:1, shade:0.88,
   ao:[[[1,0,1],[0,-1,1],[1,-1,1]],[[1,0,1],[0,1,1],[1,1,1]],[[-1,0,1],[0,1,1],[-1,1,1]],[[-1,0,1],[0,-1,1],[-1,-1,1]]]},
  {n:[0,0,-1], v:[[0,0,0],[0,1,0],[1,1,0],[1,0,0]], tile:1, shade:0.68,
   ao:[[[-1,0,-1],[0,-1,-1],[-1,-1,-1]],[[-1,0,-1],[0,1,-1],[-1,1,-1]],[[1,0,-1],[0,1,-1],[1,1,-1]],[[1,0,-1],[0,-1,-1],[1,-1,-1]]]},
];
const UVQ=[[0,1],[0,0],[1,0],[1,1]];

export class World{
  constructor(scene, atlasTex){
    this.scene=scene;
    this.size=WS;
    this.data=new Uint8Array(WS*H*WS);
    this.chunks=[];
    this.group=new THREE.Group();
    scene.add(this.group);
    this.matOpaque=new THREE.MeshBasicMaterial({map:atlasTex,vertexColors:true});
    this.matAlpha=new THREE.MeshBasicMaterial({map:atlasTex,vertexColors:true,transparent:true,opacity:0.86,depthWrite:false,side:THREE.DoubleSide});
    this.dirty=new Set();
  }
  idx(x,y,z){return (y*WS+z)*WS+x;}
  inside(x,y,z){return x>=0&&z>=0&&y>=0&&x<WS&&z<WS&&y<H;}
  get(x,y,z){
    if(y<0) return 14;
    if(!this.inside(x,y,z)) return AIR;
    return this.data[this.idx(x,y,z)];
  }
  setRaw(x,y,z,v){ if(this.inside(x,y,z)) this.data[this.idx(x,y,z)]=v; }
  set(x,y,z,v){
    if(!this.inside(x,y,z))return false;
    this.data[this.idx(x,y,z)]=v;
    const cx=(x/CS)|0, cz=(z/CS)|0;
    this.dirty.add(cx+','+cz);
    const lx=x%CS, lz=z%CS;
    if(lx===0&&cx>0)this.dirty.add((cx-1)+','+cz);
    if(lx===CS-1&&cx<CHUNKS-1)this.dirty.add((cx+1)+','+cz);
    if(lz===0&&cz>0)this.dirty.add(cx+','+(cz-1));
    if(lz===CS-1&&cz<CHUNKS-1)this.dirty.add(cx+','+(cz+1));
    return true;
  }
  flush(){
    if(!this.dirty.size)return;
    for(const k of this.dirty){const [cx,cz]=k.split(',').map(Number);this.buildChunk(cx,cz);}
    this.dirty.clear();
  }

  /* ---------------- generation ---------------- */
  generate(seed=Math.floor(Math.random()*99999)){
    this.seed=seed;
    this.data.fill(0);
    this.heights=new Int16Array(WS*WS);
    for(let z=0;z<WS;z++)for(let x=0;x<WS;x++){
      const n=fbm(x/34,z/34,seed,4);
      const m=fbm(x/12+5,z/12+5,seed+999,3);
      let h=Math.round(6 + n*18 + m*5);
      // flatten a spawn plateau in the middle
      const dx=x-WS/2, dz=z-WS/2, d=Math.sqrt(dx*dx+dz*dz);
      if(d<11){ const t=1-Math.min(1,d/11); h=Math.round(h*(1-t)+12*t); }
      h=Math.max(3,Math.min(H-8,h));
      this.heights[z*WS+x]=h;
      const beach = h<=SEA+1;
      for(let y=0;y<=h;y++){
        let b=3;                                   // stone
        if(y===h) b = beach?4:1;                   // sand / grass
        else if(y>h-4) b = beach?4:2;              // dirt
        if(y===0) b=14;                            // bedrock
        this.setRaw(x,y,z,b);
      }
      for(let y=h+1;y<=SEA;y++) this.setRaw(x,y,z,WATER);
    }
    // ore veins: glow + obsidian
    for(let i=0;i<260;i++){
      const x=(hash2(i,1,seed)*WS)|0, z=(hash2(i,2,seed)*WS)|0;
      const hcol=this.heights[z*WS+x];
      const y=2+((hash2(i,3,seed)*Math.max(2,hcol-3))|0);
      const id = hash2(i,4,seed)<0.7?8:12;
      const n=2+((hash2(i,5,seed)*4)|0);
      for(let k=0;k<n;k++){
        const ox=x+((hash2(i,6+k,seed)*3)|0)-1, oy=y+((hash2(i,26+k,seed)*3)|0)-1, oz=z+((hash2(i,46+k,seed)*3)|0)-1;
        if(this.get(ox,oy,oz)===3) this.setRaw(ox,oy,oz,id);
      }
    }
    // trees
    this.trees=0;
    for(let i=0;i<150;i++){
      const x=3+((hash2(i,11,seed)*(WS-6))|0), z=3+((hash2(i,12,seed)*(WS-6))|0);
      const h=this.heights[z*WS+x];
      if(h<=SEA+1) continue;
      if(this.get(x,h,z)!==1) continue;
      const dx=x-WS/2, dz=z-WS/2; if(Math.sqrt(dx*dx+dz*dz)<7) continue;
      const th=4+((hash2(i,13,seed)*3)|0);
      for(let y=1;y<=th;y++) this.setRaw(x,h+y,z,5);
      for(let ly=-2;ly<=1;ly++)for(let lx=-2;lx<=2;lx++)for(let lz=-2;lz<=2;lz++){
        const r=Math.abs(lx)+Math.abs(lz)+Math.abs(ly);
        if(r>3+ (ly<0?1:0)) continue;
        const px=x+lx, py=h+th+ly, pz=z+lz;
        if(this.get(px,py,pz)===AIR) this.setRaw(px,py,pz,6);
      }
      this.trees++;
    }
    // a couple of glow pillars as landmarks near spawn
    for(const [ox,oz] of [[-9,-9],[9,-9],[-9,9],[9,9]]){
      const x=(WS/2+ox)|0, z=(WS/2+oz)|0, h=this.heights[z*WS+x];
      for(let y=1;y<=3;y++) this.setRaw(x,h+y,z,y===3?8:10);
    }
  }
  surfaceY(x,z){
    for(let y=H-1;y>=0;y--){const b=this.get(x,y,z);if(isSolid(b))return y;}
    return 1;
  }

  /* ---------------- meshing ---------------- */
  buildAll(onProgress){
    for(let cz=0;cz<CHUNKS;cz++)for(let cx=0;cx<CHUNKS;cx++){
      this.buildChunk(cx,cz);
      if(onProgress) onProgress((cz*CHUNKS+cx+1)/(CHUNKS*CHUNKS));
    }
  }
  buildChunk(cx,cz){
    const key=cx+','+cz;
    let rec=this.chunks[key];
    if(!rec){
      rec={};
      const g1=new THREE.BufferGeometry(), g2=new THREE.BufferGeometry();
      rec.mOpaque=new THREE.Mesh(g1,this.matOpaque);
      rec.mAlpha=new THREE.Mesh(g2,this.matAlpha);
      rec.mOpaque.frustumCulled=true; rec.mAlpha.frustumCulled=true;
      this.group.add(rec.mOpaque,rec.mAlpha);
      this.chunks[key]=rec;
    }
    const pos=[],uv=[],col=[];
    const pos2=[],uv2=[],col2=[];
    const x0=cx*CS, z0=cz*CS;
    for(let y=0;y<H;y++)
    for(let z=z0;z<z0+CS;z++)
    for(let x=x0;x<x0+CS;x++){
      const id=this.get(x,y,z);
      if(id===AIR)continue;
      const def=BLOCKS[id];
      const alpha=!!def.transparent;
      const P=alpha?pos2:pos, U=alpha?uv2:uv, C=alpha?col2:col;
      for(let f=0;f<6;f++){
        const F=FACES[f];
        const nb=this.get(x+F.n[0],y+F.n[1],z+F.n[2]);
        if(id===WATER){ if(nb===WATER||isOpaque(nb))continue; }
        else if(isOpaque(nb))continue;
        else if(nb===id && alpha) continue;

        const tile=def.t[F.tile];
        const tu=(tile%ATLAS_COLS)/ATLAS_COLS, tv=1-(((tile/ATLAS_COLS)|0)+1)/ATLAS_COLS;
        const s=ATLAS_COLS, pad=0.0008;
        const base=def.color;
        const shade=F.shade;
        const yTop = (id===WATER && F.n[1]===1) ? -0.12 : 0;

        // vertex AO
        const aov=[0,0,0,0];
        for(let k=0;k<4;k++){
          const t=F.ao[k];
          const s1=isOpaque(this.get(x+t[0][0],y+t[0][1],z+t[0][2]))?1:0;
          const s2=isOpaque(this.get(x+t[1][0],y+t[1][1],z+t[1][2]))?1:0;
          const cc=isOpaque(this.get(x+t[2][0],y+t[2][1],z+t[2][2]))?1:0;
          aov[k]=(s1&&s2)?0:(3-(s1+s2+cc))/3;
        }
        const quad=[0,1,2,0,2,3];
        for(const qi of quad){
          const v=F.v[qi];
          P.push(x+v[0], y+v[1]+ (v[1]===1?yTop:0), z+v[2]);
          U.push(tu+(UVQ[qi][0]?1/s-pad:pad), tv+(UVQ[qi][1]?1/s-pad:pad));
          const l=shade*(0.55+0.45*aov[qi]);
          const lit=def.light?1.35:1;
          C.push(Math.min(1,((base>>16&255)/255)*l*lit),
                 Math.min(1,((base>>8&255)/255)*l*lit),
                 Math.min(1,((base&255)/255)*l*lit));
        }
      }
    }
    const fill=(mesh,p,u,c)=>{
      const g=mesh.geometry;
      g.setAttribute('position',new THREE.Float32BufferAttribute(p,3));
      g.setAttribute('uv',new THREE.Float32BufferAttribute(u,2));
      g.setAttribute('color',new THREE.Float32BufferAttribute(c,3));
      g.computeBoundingSphere();
      mesh.visible=p.length>0;
    };
    fill(rec.mOpaque,pos,uv,col);
    fill(rec.mAlpha,pos2,uv2,col2);
  }

  /* ---------------- raycast (DDA) ---------------- */
  raycast(origin,dir,maxDist=6){
    let x=Math.floor(origin.x), y=Math.floor(origin.y), z=Math.floor(origin.z);
    const sx=Math.sign(dir.x), sy=Math.sign(dir.y), sz=Math.sign(dir.z);
    const dx=Math.abs(1/dir.x), dy=Math.abs(1/dir.y), dz=Math.abs(1/dir.z);
    let tx=(sx>0?(x+1-origin.x):(origin.x-x))*dx;
    let ty=(sy>0?(y+1-origin.y):(origin.y-y))*dy;
    let tz=(sz>0?(z+1-origin.z):(origin.z-z))*dz;
    let nx=0,ny=0,nz=0,t=0;
    for(let i=0;i<128 && t<=maxDist;i++){
      const b=this.get(x,y,z);
      if(b!==AIR && b!==WATER){
        return {x,y,z,id:b,nx,ny,nz,dist:t};
      }
      if(tx<ty&&tx<tz){x+=sx;t=tx;tx+=dx;nx=-sx;ny=0;nz=0;}
      else if(ty<tz){y+=sy;t=ty;ty+=dy;nx=0;ny=-sy;nz=0;}
      else {z+=sz;t=tz;tz+=dz;nx=0;ny=0;nz=-sz;}
    }
    return null;
  }
}
