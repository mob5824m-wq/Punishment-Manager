// CubeClash — 3D pixel voxel sandbox. Game shell: states, loop, UI, juice.
import * as THREE from 'three';
import {buildAtlas, BLOCKS, HOTBAR, AIR, WATER, isSolid} from './blocks.js';
import {World, WS, H, SEA} from './world.js';
import {Player, Input, EYE} from './player.js';
import {MobManager, TYPES} from './mobs.js';
import {Particles, Shake, Sfx} from './fx.js';

/* ================= boot ================= */
const canvas=document.getElementById('view');
const renderer=new THREE.WebGLRenderer({canvas,antialias:false,powerPreference:'high-performance'});
renderer.setClearColor(0x79b9ff);
renderer.outputColorSpace=THREE.SRGBColorSpace;

let PX = 3;                       // pixelation factor
function resize(){
  const w=innerWidth, h=innerHeight;
  const dpr=Math.min(devicePixelRatio||1,2);
  const scale = (w*dpr>1800? PX+1 : PX);
  renderer.setPixelRatio(1);
  renderer.setSize(Math.max(160,Math.floor(w*dpr/scale)), Math.max(120,Math.floor(h*dpr/scale)), false);
  canvas.style.width='100%'; canvas.style.height='100%';
  camera.aspect=w/h; camera.updateProjectionMatrix();
}

const scene=new THREE.Scene();
const camera=new THREE.PerspectiveCamera(78,1,0.08,220);
scene.fog=new THREE.Fog(0x79b9ff,38,108);
scene.add(new THREE.HemisphereLight(0xffffff,0x5a6b7a,1.15));
const sun=new THREE.DirectionalLight(0xfff0d0,0.85); sun.position.set(0.5,1,0.3); scene.add(sun);

// sky dome (pixel gradient)
const skyGeo=new THREE.SphereGeometry(180,12,8);
const skyMat=new THREE.ShaderMaterial({
  side:THREE.BackSide, depthWrite:false, fog:false,
  uniforms:{top:{value:new THREE.Color(0x2f7fe0)},bot:{value:new THREE.Color(0xbfe4ff)},night:{value:0}},
  vertexShader:`varying vec3 vP; void main(){vP=position; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);} `,
  fragmentShader:`varying vec3 vP; uniform vec3 top; uniform vec3 bot; uniform float night;
    float h(vec2 p){return fract(sin(dot(p,vec2(12.9898,78.233)))*43758.5453);}
    void main(){
      float t=clamp(vP.y/180.0*0.5+0.5,0.0,1.0);
      vec3 c=mix(bot,top,pow(t,0.7));
      vec2 g=floor(vP.xz*0.35);
      float s=step(0.9985,h(g+floor(vP.y*0.35)));
      c+=vec3(s)*night*0.9;
      gl_FragColor=vec4(c,1.0);
    }`
});
const sky=new THREE.Mesh(skyGeo,skyMat); sky.frustumCulled=false; scene.add(sky);

const {texture:atlasTex,canvas:atlasCanvas}=buildAtlas();
const world=new World(scene,atlasTex);
const mobs=new MobManager(scene,world);
const particles=new Particles(scene);
const shake=new Shake();
const sfx=new Sfx();
const player=new Player();

// block highlight
const hlGeo=new THREE.BoxGeometry(1.002,1.002,1.002);
const highlight=new THREE.LineSegments(new THREE.EdgesGeometry(hlGeo),new THREE.LineBasicMaterial({color:0x000000}));
highlight.visible=false; scene.add(highlight);
const crack=new THREE.Mesh(new THREE.BoxGeometry(1.01,1.01,1.01),
  new THREE.MeshBasicMaterial({color:0x000000,transparent:true,opacity:0,depthWrite:false}));
crack.visible=false; scene.add(crack);

// first-person "hand" holding current block
const handGroup=new THREE.Group(); camera.add(handGroup); scene.add(camera);
const handBlock=new THREE.Mesh(new THREE.BoxGeometry(0.32,0.32,0.32),
  new THREE.MeshLambertMaterial({color:0x5fbe45}));
handBlock.position.set(0.42,-0.32,-0.62); handGroup.add(handBlock);
const handArm=new THREE.Mesh(new THREE.BoxGeometry(0.16,0.16,0.42),
  new THREE.MeshLambertMaterial({color:0xf0b48c}));
handArm.position.set(0.46,-0.45,-0.42); handGroup.add(handArm);

/* ================= state ================= */
const S={TITLE:0,PLAY:1,PAUSE:2,OVER:3};
let state=S.TITLE;
let mode='survival';
let score=0, mined=0, kills=0, placed=0, wave=1, dayT=0;
let waveTimer=0, spawnTimer=0, aliveTarget=0;
let running=false;
const DAY=110; // seconds per full cycle

const el=id=>document.getElementById(id);
function lockPointer(){ try{ const r=canvas.requestPointerLock(); if(r&&r.catch)r.catch(()=>{}); }catch(e){} }
function unlockPointer(){ try{ if(document.pointerLockElement)document.exitPointerLock(); }catch(e){} }
const UI={
  hud:el('hud'),start:el('start'),pause:el('pause'),over:el('over'),loading:el('loading'),
  score:el('score'),wave:el('wave'),fps:el('fps'),mode:el('modeName'),hearts:el('hearts'),
  hotbar:el('hotbar'),toast:el('toast'),touch:el('touch'),hitmark:el('hitmark'),dmg:el('dmgflash'),
};

/* ---------- high scores ---------- */
const HS_KEY='cubeclash.highscores.v1';
function loadHS(){ try{return JSON.parse(localStorage.getItem(HS_KEY))||[];}catch(e){return [];} }
function saveHS(list){ try{localStorage.setItem(HS_KEY,JSON.stringify(list.slice(0,8)));}catch(e){} }
function pushHS(entry){
  const list=loadHS(); list.push(entry);
  list.sort((a,b)=>b.score-a.score);
  const trimmed=list.slice(0,8); saveHS(trimmed);
  return trimmed.indexOf(entry);
}
function renderHS(target){
  const list=loadHS();
  if(!list.length){ target.innerHTML='<li class="empty">no runs yet — go clash</li>'; return; }
  target.innerHTML=list.map((e,i)=>
    `<li><span>${String(i+1).padStart(2,'0')} <i>${e.mode==='creative'?'CRE':'SUR'}</i></span><b>${e.score}</b><i>w${e.wave} · ${e.kills}k</i></li>`).join('');
}
renderHS(el('hsList'));

/* ---------- hotbar ---------- */
let slot=0;
function blockIcon(id,size=30){
  const c=document.createElement('canvas'); c.width=c.height=size;
  const ctx=c.getContext('2d'); ctx.imageSmoothingEnabled=false;
  const def=BLOCKS[id];
  const tile=(t,dx,dy,w,h,shadeF)=>{
    const tx=(t%4)*16, ty=((t/4)|0)*16;
    ctx.save();ctx.globalAlpha=1;ctx.drawImage(atlasCanvas,tx,ty,16,16,dx,dy,w,h);
    ctx.globalCompositeOperation='source-atop';
    ctx.fillStyle=shadeF>1?`rgba(255,255,255,${(shadeF-1)})`:`rgba(0,0,0,${1-shadeF})`;
    ctx.fillRect(dx,dy,w,h); ctx.restore();
  };
  const s=size;
  tile(def.t[0],s*0.12,s*0.06,s*0.76,s*0.4,1.05);   // top
  tile(def.t[1],s*0.12,s*0.4,s*0.38,s*0.5,0.82);    // left
  tile(def.t[1],s*0.5,s*0.4,s*0.38,s*0.5,0.62);     // right
  return c;
}
function buildHotbar(){
  UI.hotbar.innerHTML='';
  HOTBAR.forEach((id,i)=>{
    const d=document.createElement('div'); d.className='slot'+(i===slot?' sel':'');
    d.appendChild(blockIcon(id));
    const n=document.createElement('span'); n.className='num'; n.textContent=i+1; d.appendChild(n);
    d.addEventListener('pointerdown',ev=>{ev.preventDefault();setSlot(i);});
    UI.hotbar.appendChild(d);
  });
}
function setSlot(i){
  slot=(i+HOTBAR.length)%HOTBAR.length;
  [...UI.hotbar.children].forEach((c,k)=>c.classList.toggle('sel',k===slot));
  const def=BLOCKS[HOTBAR[slot]];
  handBlock.material.color.setHex(def.color);
  toast(def.name.toUpperCase(),false);
  sfx.ui();
}
buildHotbar();

/* ---------- hearts ---------- */
function renderHearts(){
  const n=5, hpPerHeart=2;
  if(UI.hearts.children.length!==n){
    UI.hearts.innerHTML=''; for(let i=0;i<n;i++){const d=document.createElement('div');d.className='heart';UI.hearts.appendChild(d);}
  }
  [...UI.hearts.children].forEach((h,i)=>{
    const v=player.hp-i*hpPerHeart;
    h.className='heart'+(v>=2?'':v>=1?' half':' empty');
  });
  UI.hearts.style.display = mode==='creative' ? 'none':'flex';
}

let toastT=0;
function toast(txt){ UI.toast.textContent=txt; UI.toast.classList.remove('on'); void UI.toast.offsetWidth; UI.toast.classList.add('on'); }
function addScore(n,label){
  score+=n; UI.score.textContent=score;
  const chip=el('scoreChip'); chip.classList.remove('bump'); void chip.offsetWidth; chip.classList.add('bump');
  if(label)toast(label+'  +'+n);
}

/* ================= input wiring ================= */
const input=new Input(canvas,{
  requestLock(){ if(state===S.PLAY && !input.touch) lockPointer(); },
  lockChange(l){ if(!l && state===S.PLAY && !input.touch) setPause(true); },
  toggleFly(){ if(state!==S.PLAY)return;
    if(mode!=='creative'){toast('FLY: CREATIVE ONLY');return;}
    player.flying=!player.flying; player.vel.y=0; toast(player.flying?'FLY ON':'FLY OFF'); sfx.ui(); },
  pause(){ if(state===S.PLAY)setPause(true); else if(state===S.PAUSE)setPause(false); },
  restartKey(){ if(state===S.OVER)startGame(mode); },
  selectSlot(i){ if(state===S.PLAY)setSlot(i); },
  cycle(d){ if(state===S.PLAY)setSlot(slot+d); },
  placeOnce(){ if(state===S.PLAY)placeBlock(); },
  tapMine(){ if(state===S.PLAY)tapMineOnce=true; },
});
let tapMineOnce=false;
if(input.touch){ UI.touch.classList.remove('hidden'); document.querySelectorAll('.touchonly').forEach(e=>e.style.display='block'); PX=3; }

document.querySelectorAll('.mode').forEach(b=>b.addEventListener('click',()=>{sfx.init();sfx.ui();startGame(b.dataset.mode);}));
el('pauseBtn').addEventListener('click',()=>setPause(true));
el('resumeBtn').addEventListener('click',()=>setPause(false));
el('restartBtn').addEventListener('click',()=>startGame(mode));
el('quitBtn').addEventListener('click',()=>{ if(score>0){pushHS({score,wave,kills,mined,mode,date:Date.now()});} toTitle(); });
el('againBtn').addEventListener('click',()=>startGame(mode));
el('titleBtn').addEventListener('click',()=>toTitle());

/* ================= game flow ================= */
function show(scr){
  for(const s of [UI.start,UI.pause,UI.over,UI.loading]) s.classList.add('hidden');
  if(scr) scr.classList.remove('hidden');
}
function toTitle(){
  state=S.TITLE; running=false;
  UI.hud.classList.add('hidden'); show(UI.start); renderHS(el('hsList'));
  unlockPointer();
}
function setPause(p){
  if(p&&state!==S.PLAY)return;
  if(!p&&state!==S.PAUSE)return;
  state=p?S.PAUSE:S.PLAY;
  if(p){
    el('pScore').textContent=score; el('pMined').textContent=mined; el('pKills').textContent=kills;
    show(UI.pause); unlockPointer();
  }else{
    show(null); if(!input.touch)lockPointer();
    last=performance.now();
  }
}
function gameOver(){
  state=S.OVER; player.hp=0; renderHearts();
  sfx.die(); shake.add(0.5,0.7);
  const entry={score,wave,kills,mined,mode,date:Date.now()};
  const rank=pushHS(entry);
  el('oScore').textContent=score;
  el('oWave').textContent=wave; el('oKills').textContent=kills; el('oMined').textContent=mined;
  el('oNew').classList.toggle('hidden',rank!==0);
  renderHS(el('oList'));
  show(UI.over);
  unlockPointer();
}

function startGame(m){
  mode=m;
  show(UI.loading); UI.hud.classList.add('hidden');
  el('barFill').style.width='8%';
  setTimeout(()=>{
    world.generate();
    el('barFill').style.width='40%';
    setTimeout(()=>{
      world.buildAll(p=>{ el('barFill').style.width=(40+p*60)+'%'; });
      finishStart();
    },30);
  },30);
}
function finishStart(){
  mobs.clear(); particles.reset();
  score=0;mined=0;kills=0;placed=0;wave=1;dayT=DAY*0.12;waveTimer=0;spawnTimer=0;
  const sx=WS/2, sz=WS/2;
  const sy=world.surfaceY(Math.floor(sx),Math.floor(sz))+1;
  player.reset(sx+0.5,sy,sz+0.5,mode==='creative');
  player.yaw=Math.PI*0.25;
  setSlot(0);
  UI.score.textContent='0'; UI.wave.textContent='1'; UI.mode.textContent=mode.toUpperCase();
  el('waveChip').style.display=mode==='creative'?'none':'flex';
  renderHearts();
  UI.hud.classList.remove('hidden'); show(null);
  state=S.PLAY; running=true; last=performance.now();
  // friendly starter mobs so there's something to do in second one
  if(mode==='survival'){
    for(let i=0;i<3;i++) mobs.spawnRing('cubit',player.pos.x,player.pos.z,6,12,WS);
    for(let i=0;i<3;i++) mobs.spawnRing('clashling',player.pos.x,player.pos.z,10,16,WS);
    toast('CLASH THEM ALL');
  }else{
    for(let i=0;i<5;i++) mobs.spawnRing('cubit',player.pos.x,player.pos.z,6,16,WS);
    toast('BUILD SOMETHING HUGE');
  }
  if(!input.touch)lockPointer();
  sfx.init(); sfx.level();
}

/* ================= actions ================= */
let mineTarget=null, mineProg=0;
function currentHit(){
  const o=player.eye(), d=player.dir();
  const reach = mode==='creative'?7:5;
  const mobHit=mobs.pick(o,d,reach);
  const blockHit=world.raycast(o,d,reach);
  if(mobHit && (!blockHit || mobHit.dist<blockHit.dist)) return {mob:mobHit.mob};
  if(blockHit) return {block:blockHit};
  return {};
}
function swing(){ player.swing=1; }

function attackMob(m){
  swing();
  const dmg = mode==='creative'?99:2;
  const d=player.dir();
  const died=m.hurt(dmg,d.x,d.z);
  particles.spawn(m.pos.x,m.pos.y+m.h*0.6,m.pos.z,m.def.color,10,{speed:4,up:3,life:0.5,size:0.13});
  UI.hitmark.classList.remove('on'); void UI.hitmark.offsetWidth; UI.hitmark.classList.add('on');
  shake.add(0.10,0.14); sfx.hit();
  if(died){
    m.dead=true; kills++;
    particles.spawn(m.pos.x,m.pos.y+m.h*0.5,m.pos.z,m.def.color,26,{speed:6,up:5,life:0.9,size:0.17,spread:0.6});
    particles.spawn(m.pos.x,m.pos.y+m.h*0.5,m.pos.z,0xffffff,8,{speed:7,up:6,life:0.5,size:0.1});
    addScore(m.def.score, m.def.name.toUpperCase()+' DOWN');
    shake.add(0.22,0.3); sfx.kill();
  }
}
function mineTick(dt){
  const hit=currentHit();
  if(hit.mob){
    highlight.visible=false; crack.visible=false; mineTarget=null;
    if(attackCd<=0){ attackCd=0.32; attackMob(hit.mob); }
    return;
  }
  if(!hit.block){ mineTarget=null; mineProg=0; crack.visible=false; return; }
  const b=hit.block;
  const key=b.x+','+b.y+','+b.z;
  if(mineTarget!==key){ mineTarget=key; mineProg=0; }
  const def=BLOCKS[b.id];
  if(def.hard===Infinity){ toast('UNBREAKABLE'); mineProg=0; return; }
  const rate = mode==='creative'? 6 : 1.0;
  mineProg += dt*rate/def.hard;
  swing();
  if(Math.random()<dt*18){
    particles.spawn(b.x+0.5,b.y+0.5,b.z+0.5,def.color,2,{speed:1.6,up:1.6,life:0.4,size:0.1,spread:1.0});
    sfx.mine();
  }
  crack.visible=true; crack.position.set(b.x+0.5,b.y+0.5,b.z+0.5);
  crack.material.opacity=Math.min(0.55,mineProg*0.55);
  const s=1.01-mineProg*0.06; crack.scale.set(s,s,s);
  if(mineProg>=1){
    breakBlock(b.x,b.y,b.z,b.id);
    mineProg=0; mineTarget=null; crack.visible=false;
  }
}
function breakBlock(x,y,z,id){
  const def=BLOCKS[id];
  world.set(x,y,z,AIR);
  world.flush();
  particles.spawn(x+0.5,y+0.5,z+0.5,def.color,22,{speed:4,up:3.5,life:0.8,size:0.16,spread:0.9});
  mined++;
  const pts=def.score|| (mode==='creative'?1:2);
  addScore(pts, def.score?def.name.toUpperCase()+'!':null);
  shake.add(def.score?0.18:0.07,def.score?0.28:0.12);
  sfx.break_();
}
function placeBlock(){
  const o=player.eye(), d=player.dir();
  const reach=mode==='creative'?7:5;
  const hit=world.raycast(o,d,reach);
  if(!hit)return;
  const x=hit.x+hit.nx, y=hit.y+hit.ny, z=hit.z+hit.nz;
  if(!world.inside(x,y,z))return;
  if(world.get(x,y,z)!==AIR && world.get(x,y,z)!==WATER)return;
  // don't place inside player
  const r=0.32;
  if(x+1>player.pos.x-r && x<player.pos.x+r && z+1>player.pos.z-r && z<player.pos.z+r &&
     y+1>player.pos.y && y<player.pos.y+1.8) return;
  const id=HOTBAR[slot];
  world.set(x,y,z,id); world.flush();
  placed++;
  const def=BLOCKS[id];
  particles.spawn(x+0.5,y+0.5,z+0.5,def.color,8,{speed:2,up:2,life:0.4,size:0.12,spread:0.9});
  addScore(mode==='creative'?2:1);
  swing(); shake.add(0.045,0.1); sfx.place();
}

let attackCd=0;
const api={
  damagePlayer(dmg){
    if(mode==='creative'||state!==S.PLAY)return;
    if(player.invuln>0)return;
    player.invuln=0.65;
    player.hp-=dmg; renderHearts();
    UI.hearts.classList.remove('hurt'); void UI.hearts.offsetWidth; UI.hearts.classList.add('hurt');
    UI.dmg.classList.remove('on'); void UI.dmg.offsetWidth; UI.dmg.classList.add('on');
    shake.add(0.3,0.32); sfx.hurt();
    particles.spawn(player.pos.x,player.pos.y+1.2,player.pos.z,0xe5484d,10,{speed:3,up:3,life:0.5,size:0.12});
    if(player.hp<=0) gameOver();
  },
  onLand(v){ shake.add(Math.min(0.28,v*0.012),0.2); sfx.noise(0.08,0.05,500);
    if(mode!=='creative'&&v>22){api.damagePlayer(Math.floor((v-22)/4)+1);} },
  onJump(){ sfx.blip(320,0.06,'square',0.03,120); },
};

/* ================= waves & day cycle ================= */
function updateWorldTime(dt){
  dayT=(dayT+dt)%DAY;
  const t=dayT/DAY;                       // 0 dawn .. 0.5 dusk
  const nightF=Math.max(0,Math.min(1,(Math.cos(t*Math.PI*2)* -1)*1.4+0.3)); // 0 day, 1 night
  const c=_cA.setHex(0x9fd2ff).lerp(_cB.setHex(0x0b1626),nightF);
  renderer.setClearColor(c);
  scene.fog.color.copy(c);
  skyMat.uniforms.top.value.copy(_cC.setHex(0x2f7fe0).lerp(_cD.setHex(0x060b18),nightF));
  skyMat.uniforms.bot.value.copy(c);
  skyMat.uniforms.night.value=nightF;
  const light=1-nightF*0.62;
  world.matOpaque.color.setScalar(light);
  world.matAlpha.color.setScalar(light);
  sun.intensity=0.2+0.7*(1-nightF);
  return nightF;
}
const _cA=new THREE.Color(),_cB=new THREE.Color(),_cC=new THREE.Color(),_cD=new THREE.Color();
let wasNight=false;
function updateWaves(dt,nightF){
  if(mode!=='survival')return;
  const night=nightF>0.55;
  if(night&&!wasNight){
    toast('NIGHT '+wave+' — THEY COME');
    sfx.level(); aliveTarget=Math.min(18,3+wave*2); spawnTimer=0;
  }
  if(!night&&wasNight){
    const bonus=60*wave;
    addScore(bonus,'WAVE '+wave+' SURVIVED');
    wave++; UI.wave.textContent=wave;
    if(player.hp<player.maxhp){player.hp=Math.min(player.maxhp,player.hp+2);renderHearts();}
    sfx.level();
  }
  wasNight=night;

  const hostiles=mobs.mobs.filter(m=>m.def.hostile&&!m.dead).length;
  spawnTimer-=dt;
  if(spawnTimer<=0){
    spawnTimer = night? 1.6 : 6.5;
    const cap = night? aliveTarget : 5;
    if(hostiles<cap){
      let type='clashling';
      const r=Math.random();
      if(wave>=3&&r<0.18+wave*0.02)type='brute';
      else if(wave>=2&&r<0.45)type='spiker';
      mobs.spawnRing(type,player.pos.x,player.pos.z,14,26,WS);
    }
    if(mobs.mobs.filter(m=>!m.def.hostile).length<4 && !night)
      mobs.spawnRing('cubit',player.pos.x,player.pos.z,12,24,WS);
  }
  // burn hostiles in daylight (keeps things clean + juicy)
  if(nightF<0.2){
    for(const m of mobs.mobs){
      if(m.def.hostile&&!m.dead&&Math.random()<dt*0.25){
        m.dead=true; kills++; addScore(Math.floor(m.def.score*0.4));
        particles.spawn(m.pos.x,m.pos.y+m.h*0.5,m.pos.z,0xffb347,14,{speed:2,up:5,life:0.7,size:0.14});
      }
    }
  }
}

/* ================= loop ================= */
let last=performance.now(), fpsAcc=0, fpsN=0, fpsT=0;
function frame(now){
  requestAnimationFrame(frame);
  let dt=(now-last)/1000; last=now;
  if(dt>0.1)dt=0.1;
  if(state===S.PLAY){ step(dt,now/1000); }
  else if(state===S.TITLE){ titleCam(dt,now/1000); }
  renderer.render(scene,camera);
  // fps
  fpsAcc+=dt;fpsN++;
  if(fpsAcc>0.5){ UI.fps.textContent=Math.round(fpsN/fpsAcc); 
    const f=fpsN/fpsAcc; fpsAcc=0;fpsN=0;
    if(f<45&&PX<5){PX++;resize();} }
}
function titleCam(dt,t){
  camera.position.set(WS/2+Math.cos(t*0.12)*26, 26+Math.sin(t*0.2)*3, WS/2+Math.sin(t*0.12)*26);
  camera.lookAt(WS/2,10,WS/2);
  particles.update(dt,null);
}
function step(dt,t){
  // look
  const [lx,ly]=input.consumeLook();
  const sens=0.0022;
  player.yaw-=lx*sens; player.pitch-=ly*sens;
  player.pitch=Math.max(-1.5,Math.min(1.5,player.pitch));

  player.update(dt,world,input,mode==='creative',api);
  if(player.pos.y<-4 && mode!=='creative') api.damagePlayer(99);
  if(player.pos.y<-4 && mode==='creative'){ player.pos.y=30; player.vel.set(0,0,0); }

  attackCd-=dt;
  if(input.mine||tapMineOnce){ mineTick(tapMineOnce?0.5:dt); tapMineOnce=false; }
  else { mineProg=0; mineTarget=null; crack.visible=false; }

  // highlight
  const hit=world.raycast(player.eye(),player.dir(),mode==='creative'?7:5);
  if(hit){ highlight.visible=true; highlight.position.set(hit.x+0.5,hit.y+0.5,hit.z+0.5); }
  else highlight.visible=false;

  mobs.update(dt,player,api);
  particles.update(dt,world);
  const nightF=updateWorldTime(dt);
  updateWaves(dt,nightF);
  shake.update(dt,t);

  // camera
  const hv=Math.hypot(player.vel.x,player.vel.z);
  const bobY=Math.sin(player.bob*2)*0.045*Math.min(1,hv/5);
  const bobX=Math.cos(player.bob)*0.03*Math.min(1,hv/5);
  camera.position.set(player.pos.x+shake.off.x+bobX, player.eyeY()+bobY+shake.off.y, player.pos.z+shake.off.z);
  camera.rotation.set(player.pitch,player.yaw,shake.roll,'YXZ');
  sky.position.copy(camera.position);

  // hand animation
  const sw=player.swing;
  handGroup.position.set(0,0,0);
  handBlock.position.set(0.42-sw*0.1,-0.32-Math.sin(sw*Math.PI)*0.16,-0.62+Math.sin(sw*Math.PI)*0.12);
  handBlock.rotation.set(sw*1.4,0.6+sw*0.6,sw*0.5);
  handArm.position.set(0.46,-0.45-Math.sin(sw*Math.PI)*0.12,-0.42+Math.sin(sw*Math.PI)*0.1);
  handArm.rotation.set(-0.4-sw*0.9,0,0);
  const ib=Math.sin(player.bob*2)*0.012*Math.min(1,hv/5);
  handGroup.position.y+=ib;
}

/* ================= go ================= */
addEventListener('resize',resize);
resize();
world.generate(12345);
world.buildAll();
camera.position.set(WS/2,26,WS/2+26);
toTitle();
requestAnimationFrame(frame);
addEventListener('pointerdown',()=>sfx.init(),{once:true});
addEventListener('touchstart',()=>sfx.init(),{once:true});
