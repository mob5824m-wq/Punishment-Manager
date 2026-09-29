// Block registry + procedurally generated pixel-art texture atlas.
import * as THREE from 'three';

export const TILE = 16;         // px per tile
export const ATLAS_COLS = 4;    // 4x4 = 16 tiles
const ATLAS_PX = TILE * ATLAS_COLS;

// deterministic rng for texture painting
function mulberry(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}

function hex(c){return '#'+c.toString(16).padStart(6,'0');}
function shade(c,f){
  const r=Math.min(255,Math.max(0,Math.round(((c>>16)&255)*f)));
  const g=Math.min(255,Math.max(0,Math.round(((c>>8)&255)*f)));
  const b=Math.min(255,Math.max(0,Math.round((c&255)*f)));
  return (r<<16)|(g<<8)|b;
}

/** Painters: each fills a TILE x TILE cell at (ox,oy). */
function noiseTile(ctx,ox,oy,base,amount,seed,dotted){
  const rnd=mulberry(seed);
  for(let y=0;y<TILE;y++)for(let x=0;x<TILE;x++){
    let f=1+(rnd()*2-1)*amount;
    if(dotted && rnd()<0.06) f*= (rnd()<.5?0.72:1.3);
    ctx.fillStyle=hex(shade(base,f));
    ctx.fillRect(ox+x,oy+y,1,1);
  }
}
function grassTop(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0x5fbe45,0.16,seed,true);
  const rnd=mulberry(seed+9);
  for(let i=0;i<26;i++){const x=(rnd()*TILE)|0,y=(rnd()*TILE)|0;
    ctx.fillStyle=hex(shade(0x5fbe45,rnd()<.5?1.28:0.76));ctx.fillRect(ox+x,oy+y,1,1);}
}
function grassSide(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0x8b6239,0.14,seed,true);
  const rnd=mulberry(seed+3);
  for(let x=0;x<TILE;x++){
    const h=3+((rnd()*3)|0);
    for(let y=0;y<h;y++){ctx.fillStyle=hex(shade(0x5fbe45,1+(rnd()*.3-.15)));ctx.fillRect(ox+x,oy+y,1,1);}
  }
}
function logSide(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0x7a5231,0.10,seed,false);
  const rnd=mulberry(seed+5);
  for(let x=0;x<TILE;x+=3){for(let y=0;y<TILE;y++){
    ctx.fillStyle=hex(shade(0x7a5231,0.72+rnd()*0.1));ctx.fillRect(ox+x,oy+y,1,1);}}
}
function logTop(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0xbb8b53,0.08,seed,false);
  ctx.strokeStyle=hex(shade(0xbb8b53,0.68));ctx.lineWidth=1;
  for(let r=2;r<8;r+=2){ctx.beginPath();ctx.arc(ox+8,oy+8,r,0,6.3);ctx.stroke();}
}
function leaves(ctx,ox,oy,seed){
  const rnd=mulberry(seed);
  for(let y=0;y<TILE;y++)for(let x=0;x<TILE;x++){
    const v=rnd();
    ctx.fillStyle=hex(shade(0x3f9c33,0.7+v*0.7));
    ctx.fillRect(ox+x,oy+y,1,1);
    if(v<0.10){ctx.fillStyle='#24361f';ctx.fillRect(ox+x,oy+y,1,1);}
  }
}
function planks(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0xb98a4e,0.09,seed,false);
  ctx.fillStyle=hex(shade(0xb98a4e,0.6));
  for(let y=3;y<TILE;y+=4)ctx.fillRect(ox,oy+y,TILE,1);
  ctx.fillRect(ox+7,oy,1,4);ctx.fillRect(ox+3,oy+4,1,4);ctx.fillRect(ox+11,oy+8,1,4);ctx.fillRect(ox+5,oy+12,1,4);
}
function cobble(ctx,ox,oy,seed){
  const rnd=mulberry(seed);
  noiseTile(ctx,ox,oy,0x8a8f96,0.10,seed,false);
  ctx.fillStyle=hex(shade(0x8a8f96,0.55));
  for(let i=0;i<10;i++){
    const x=(rnd()*13)|0,y=(rnd()*13)|0,w=2+((rnd()*3)|0),h=2+((rnd()*3)|0);
    ctx.strokeStyle=hex(shade(0x8a8f96,0.5));ctx.strokeRect(ox+x+.5,oy+y+.5,w,h);
  }
}
function glow(ctx,ox,oy,seed){
  const rnd=mulberry(seed);
  noiseTile(ctx,ox,oy,0xffc63f,0.12,seed,false);
  for(let i=0;i<22;i++){const x=(rnd()*TILE)|0,y=(rnd()*TILE)|0;
    ctx.fillStyle=rnd()<.5?'#fff3bd':'#c98a0d';ctx.fillRect(ox+x,oy+y,2,2);}
}
function brick(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0xa6402f,0.10,seed,false);
  ctx.fillStyle='#d8cfc4';
  for(let y=0;y<TILE;y+=4)ctx.fillRect(ox,oy+y,TILE,1);
  for(let y=0;y<TILE;y+=4){const off=(y/4)%2?0:8;
    for(let x=off;x<TILE;x+=8)ctx.fillRect(ox+x,oy+y,1,4);}
}
function water(ctx,ox,oy,seed){
  const rnd=mulberry(seed);
  noiseTile(ctx,ox,oy,0x2f7fd6,0.10,seed,false);
  for(let i=0;i<10;i++){const y=(rnd()*TILE)|0,x=(rnd()*10)|0;
    ctx.fillStyle='#8fd0ff';ctx.fillRect(ox+x,oy+y,3+((rnd()*3)|0),1);}
}
function obsidian(ctx,ox,oy,seed){
  noiseTile(ctx,ox,oy,0x2a1f3d,0.25,seed,true);
  const rnd=mulberry(seed+2);
  for(let i=0;i<8;i++){ctx.fillStyle='#8a5cff';ctx.fillRect(ox+((rnd()*TILE)|0),oy+((rnd()*TILE)|0),1,1);}
}

const TILES=[
  grassTop, grassSide,
  (c,x,y,s)=>noiseTile(c,x,y,0x8b6239,0.14,s,true),   // 2 dirt
  (c,x,y,s)=>noiseTile(c,x,y,0x7d848c,0.12,s,true),   // 3 stone
  (c,x,y,s)=>noiseTile(c,x,y,0xe0d29a,0.10,s,true),   // 4 sand
  logSide, logTop, leaves, planks, glow, water, cobble, brick, obsidian,
  (c,x,y,s)=>noiseTile(c,x,y,0xd9e6ef,0.07,s,true),   // 14 snow
  (c,x,y,s)=>noiseTile(c,x,y,0x4a4f57,0.14,s,true),   // 15 bedrock
];

export function buildAtlas(){
  const cv=document.createElement('canvas');
  cv.width=cv.height=ATLAS_PX;
  const ctx=cv.getContext('2d');
  ctx.imageSmoothingEnabled=false;
  TILES.forEach((fn,i)=>{
    const ox=(i%ATLAS_COLS)*TILE, oy=((i/ATLAS_COLS)|0)*TILE;
    fn(ctx,ox,oy,1000+i*137);
  });
  const tex=new THREE.CanvasTexture(cv);
  tex.magFilter=THREE.NearestFilter;
  tex.minFilter=THREE.NearestFilter;
  tex.generateMipmaps=false;
  tex.colorSpace=THREE.SRGBColorSpace;
  return {texture:tex,canvas:cv};
}

// id -> {name, tiles:[top,side,bottom], hardness, transparent, solid, light, color}
export const BLOCKS=[
  null,
  {name:'Grass', t:[0,1,2],  hard:0.35, color:0x5fbe45},
  {name:'Dirt',  t:[2,2,2],  hard:0.35, color:0x8b6239},
  {name:'Stone', t:[3,3,3],  hard:0.9,  color:0x7d848c},
  {name:'Sand',  t:[4,4,4],  hard:0.3,  color:0xe0d29a},
  {name:'Log',   t:[6,5,6],  hard:0.7,  color:0x7a5231},
  {name:'Leaves',t:[7,7,7],  hard:0.2,  color:0x3f9c33, transparent:true},
  {name:'Planks',t:[8,8,8],  hard:0.6,  color:0xb98a4e},
  {name:'Glow',  t:[9,9,9],  hard:0.5,  color:0xffc63f, light:true, score:25},
  {name:'Water', t:[10,10,10],hard:99,  color:0x2f7fd6, transparent:true, liquid:true},
  {name:'Cobble',t:[11,11,11],hard:1.0, color:0x8a8f96},
  {name:'Brick', t:[12,12,12],hard:1.1, color:0xa6402f},
  {name:'Obsidian',t:[13,13,13],hard:2.2,color:0x2a1f3d, score:40},
  {name:'Snow',  t:[14,14,14],hard:0.25,color:0xd9e6ef},
  {name:'Bedrock',t:[15,15,15],hard:Infinity,color:0x4a4f57},
];

export const AIR=0, WATER=9, BEDROCK=14;
export const isSolid=(id)=> id!==0 && id!==WATER;
export const isOpaque=(id)=> id!==0 && !(BLOCKS[id] && BLOCKS[id].transparent);

// hotbar contents (ids)
export const HOTBAR=[1,3,7,5,6,10,11,8];
