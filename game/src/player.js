// Player physics + input (keyboard/mouse + touch).
import * as THREE from 'three';
import {isSolid, WATER} from './blocks.js';

export const PW=0.6, PH=1.8, EYE=1.62;

export class Player{
  constructor(){
    this.pos=new THREE.Vector3();
    this.vel=new THREE.Vector3();
    this.yaw=0; this.pitch=0;
    this.onGround=false; this.flying=false;
    this.hp=10; this.maxhp=10;
    this.bob=0; this.swing=0; this.inWater=false;
    this.invuln=0; this.coyote=0; this.jumpBuf=0; this.stepOff=0;
  }
  reset(x,y,z,creative){
    this.pos.set(x,y,z); this.vel.set(0,0,0);
    this.yaw=0;this.pitch=-0.15;this.hp=this.maxhp=10;
    this.flying=!!creative; this.invuln=0; this.swing=0; this.stepOff=0;
  }
  free(world,x,y,z){
    const r=PW/2;
    for(let bx=Math.floor(x-r);bx<=Math.floor(x+r-0.001);bx++)
    for(let by=Math.floor(y);by<=Math.floor(y+PH-0.001);by++)
    for(let bz=Math.floor(z-r);bz<=Math.floor(z+r-0.001);bz++)
      if(isSolid(world.get(bx,by,bz)))return false;
    return true;
  }
  update(dt,world,input,creative,api){
    const wish=new THREE.Vector3();
    const f=input.forward, s=input.strafe;
    if(f||s){
      const sy=Math.sin(this.yaw), cy=Math.cos(this.yaw);
      wish.x = (-sy*f + cy*s);
      wish.z = (-cy*f - sy*s);
      if(wish.lengthSq()>1)wish.normalize();
      const l=Math.min(1,Math.hypot(f,s)); wish.multiplyScalar(l);
    }
    // water check
    const hx=Math.floor(this.pos.x),hy=Math.floor(this.pos.y+0.4),hz=Math.floor(this.pos.z);
    this.inWater = world.get(hx,hy,hz)===WATER;

    const sprint = input.sprint && f>0.1 ? 1.45 : 1;
    let speed = (this.flying?11:(this.inWater?3.6:5.0))*sprint;
    const accel = this.onGround||this.flying ? 44 : 14;
    const target=wish.multiplyScalar(speed);
    this.vel.x+= (target.x-this.vel.x)*Math.min(1,accel*dt);
    this.vel.z+= (target.z-this.vel.z)*Math.min(1,accel*dt);

    if(this.flying){
      let vy=0;
      if(input.jump)vy+=1; if(input.crouch)vy-=1;
      this.vel.y += (vy*9 - this.vel.y)*Math.min(1,20*dt);
    }else{
      const g=this.inWater?9:28;
      this.vel.y-=g*dt;
      if(this.inWater) this.vel.y=Math.max(this.vel.y,-4);
      if(this.vel.y<-42)this.vel.y=-42;
      this.coyote = this.onGround?0.12:Math.max(0,this.coyote-dt);
      this.jumpBuf = input.jump?0.14:Math.max(0,this.jumpBuf-dt);
      if(this.jumpBuf>0 && (this.coyote>0||this.inWater)){
        this.vel.y=this.inWater?5.0:9.2; this.coyote=0; this.jumpBuf=0; this.onGround=false;
        if(api&&api.onJump)api.onJump();
      }
    }

    // integrate with per-axis collision + auto-step
    const step=(axis,amount)=>{
      if(amount===0)return;
      const p=this.pos.clone();
      p[axis]+=amount;
      if(this.free(world,p.x,p.y,p.z)){this.pos.copy(p);return;}
      // auto step-up (up to a full block) when grounded — arcade friendly
      if(!this.flying&&(this.onGround||this.inWater)){
        for(const rise of [0.55,1.05]){
          const up=p.clone(); up.y=Math.floor(this.pos.y+rise)+0.0001;
          if(up.y<=this.pos.y+0.001) up.y=this.pos.y+rise;
          if(this.free(world,up.x,up.y,up.z)&&this.free(world,this.pos.x,up.y,this.pos.z)){
            this.stepOff-=(up.y-this.pos.y);
            this.pos.copy(up); return;
          }
        }
      }
      this.vel[axis]=0;
    };
    const dx=this.vel.x*dt, dz=this.vel.z*dt, dy=this.vel.y*dt;
    step('x',dx); step('z',dz);
    const py=this.pos.y;
    const ny=py+dy;
    if(this.free(world,this.pos.x,ny,this.pos.z)){ this.pos.y=ny; this.onGround=false; }
    else{
      if(this.vel.y<0){
        // settle on top of block
        this.pos.y=Math.ceil(ny);
        let guard=0;
        while(!this.free(world,this.pos.x,this.pos.y,this.pos.z)&&guard++<8)this.pos.y+=1;
        const fall=-this.vel.y;
        if(!this.onGround && fall>16 && !this.flying && !this.inWater && api&&api.onLand) api.onLand(fall);
        this.onGround=true;
      }
      this.vel.y=0;
    }
    // world bounds
    const WSx=world.constructor?0:0;
    this.pos.x=Math.max(0.4,Math.min(world.size-0.4,this.pos.x));
    this.pos.z=Math.max(0.4,Math.min(world.size-0.4,this.pos.z));

    // head bob & swing decay
    const hv=Math.hypot(this.vel.x,this.vel.z);
    this.bob += dt*hv*1.7;
    if(this.swing>0)this.swing=Math.max(0,this.swing-dt*5.5);
    if(this.invuln>0)this.invuln-=dt;
    if(this.stepOff!==0){
      const k=Math.min(1,dt*14);
      this.stepOff+= -this.stepOff*k;
      if(Math.abs(this.stepOff)<0.002)this.stepOff=0;
    }
  }
  eye(){ return new THREE.Vector3(this.pos.x, this.pos.y+EYE+this.stepOff, this.pos.z); }
  eyeY(){ return this.pos.y+EYE+this.stepOff; }
  dir(){
    return new THREE.Vector3(
      -Math.sin(this.yaw)*Math.cos(this.pitch),
      Math.sin(this.pitch),
      -Math.cos(this.yaw)*Math.cos(this.pitch)
    );
  }
}

/* ---------------- input ---------------- */
export class Input{
  constructor(canvas,cbs){
    this.keys=new Set();
    this.forward=0;this.strafe=0;this.jump=false;this.crouch=false;this.sprint=false;
    this.mine=false;this.place=false;
    this.lookX=0;this.lookY=0;
    this.locked=false;
    this.cbs=cbs;
    this.touch = matchMedia('(pointer:coarse)').matches || 'ontouchstart' in window;
    this.canvas=canvas;
    this._bindKeys(); this._bindMouse(); this._bindTouch();
  }
  _bindKeys(){
    addEventListener('keydown',e=>{
      if(e.repeat){return;}
      const k=e.code;
      this.keys.add(k);
      if(k==='Space')e.preventDefault();
      if(k==='KeyF')this.cbs.toggleFly?.();
      if(k==='KeyP'||k==='Escape')this.cbs.pause?.();
      if(k==='KeyR')this.cbs.restartKey?.();
      if(/^Digit[1-8]$/.test(k))this.cbs.selectSlot?.(+k.slice(5)-1);
      if(k==='KeyQ')this.cbs.cycle?.(-1);
      if(k==='KeyE')this.cbs.cycle?.(1);
      this._sync();
    });
    addEventListener('keyup',e=>{this.keys.delete(e.code);this._sync();});
    addEventListener('blur',()=>{this.keys.clear();this._sync();});
  }
  _sync(){
    const k=this.keys;
    if(this.touch && this._stickActive) return;
    this.forward=(k.has('KeyW')||k.has('ArrowUp')?1:0)-(k.has('KeyS')||k.has('ArrowDown')?1:0);
    this.strafe=(k.has('KeyD')||k.has('ArrowRight')?1:0)-(k.has('KeyA')||k.has('ArrowLeft')?1:0);
    this.jump=k.has('Space')||this._tJump;
    this.crouch=k.has('ShiftLeft')||k.has('ShiftRight')||this._tCrouch;
    this.sprint=k.has('ControlLeft')||k.has('KeyR')===false&&k.has('ShiftLeft')&&false||k.has('ControlLeft');
    this.sprint=k.has('ControlLeft')||k.has('KeyZ');
  }
  _bindMouse(){
    const cv=this.canvas;
    cv.addEventListener('mousedown',e=>{
      if(!this.locked){ this.cbs.requestLock?.(); return; }
      if(e.button===0)this.mine=true;
      if(e.button===2){this.place=true;this.cbs.placeOnce?.();}
    });
    addEventListener('mouseup',e=>{ if(e.button===0)this.mine=false; if(e.button===2)this.place=false; });
    addEventListener('contextmenu',e=>e.preventDefault());
    document.addEventListener('pointerlockchange',()=>{
      this.locked=document.pointerLockElement===cv;
      this.cbs.lockChange?.(this.locked);
      if(!this.locked){this.mine=false;this.place=false;}
    });
    addEventListener('mousemove',e=>{
      if(!this.locked)return;
      this.lookX+=e.movementX; this.lookY+=e.movementY;
    });
    addEventListener('wheel',e=>{this.cbs.cycle?.(Math.sign(e.deltaY));},{passive:true});
  }
  _bindTouch(){
    const stick=document.getElementById('stick'), nub=document.getElementById('stickNub');
    let sid=null, sx=0, sy=0;
    const setNub=(dx,dy)=>{nub.style.transform=`translate(${dx}px,${dy}px)`;};
    stick.addEventListener('touchstart',e=>{
      e.preventDefault(); const t=e.changedTouches[0]; sid=t.identifier;
      const r=stick.getBoundingClientRect(); sx=r.left+r.width/2; sy=r.top+r.height/2;
      this._stickActive=true;
    },{passive:false});
    const move=e=>{
      for(const t of e.changedTouches){
        if(t.identifier!==sid)continue;
        let dx=t.clientX-sx, dy=t.clientY-sy;
        const m=Math.hypot(dx,dy), lim=48;
        if(m>lim){dx=dx/m*lim;dy=dy/m*lim;}
        setNub(dx,dy);
        this.strafe=dx/lim; this.forward=-dy/lim;
      }
    };
    stick.addEventListener('touchmove',e=>{e.preventDefault();move(e);},{passive:false});
    const end=e=>{
      for(const t of e.changedTouches) if(t.identifier===sid){
        sid=null;this._stickActive=false;this.forward=0;this.strafe=0;setNub(0,0);}
    };
    stick.addEventListener('touchend',end); stick.addEventListener('touchcancel',end);

    // look drag anywhere on the right side
    let lid=null,lx=0,ly=0,moved=0,startT=0;
    this.canvas.addEventListener('touchstart',e=>{
      const t=e.changedTouches[0];
      if(lid!==null)return;
      lid=t.identifier;lx=t.clientX;ly=t.clientY;moved=0;startT=performance.now();
    },{passive:true});
    this.canvas.addEventListener('touchmove',e=>{
      for(const t of e.changedTouches){
        if(t.identifier!==lid)continue;
        const dx=t.clientX-lx, dy=t.clientY-ly; lx=t.clientX;ly=t.clientY;
        moved+=Math.abs(dx)+Math.abs(dy);
        this.lookX+=dx*1.4; this.lookY+=dy*1.4;
      }
    },{passive:true});
    const lend=e=>{
      for(const t of e.changedTouches) if(t.identifier===lid){
        if(moved<10 && performance.now()-startT<250) this.cbs.tapMine?.();
        lid=null;
      }
    };
    this.canvas.addEventListener('touchend',lend);
    this.canvas.addEventListener('touchcancel',lend);

    const hold=(el,on,off)=>{
      el.addEventListener('touchstart',e=>{e.preventDefault();on();},{passive:false});
      el.addEventListener('touchend',e=>{e.preventDefault();off&&off();},{passive:false});
      el.addEventListener('touchcancel',()=>off&&off());
    };
    hold(document.getElementById('tbJump'),()=>{this._tJump=true;this.jump=true;},()=>{this._tJump=false;this.jump=false;});
    hold(document.getElementById('tbBreak'),()=>{this.mine=true;},()=>{this.mine=false;});
    document.getElementById('tbPlace').addEventListener('touchstart',e=>{e.preventDefault();this.cbs.placeOnce?.();},{passive:false});
    document.getElementById('tbFly').addEventListener('touchstart',e=>{e.preventDefault();this.cbs.toggleFly?.();},{passive:false});
    document.getElementById('tbPrev').addEventListener('touchstart',e=>{e.preventDefault();this.cbs.cycle?.(-1);},{passive:false});
    document.getElementById('tbNext').addEventListener('touchstart',e=>{e.preventDefault();this.cbs.cycle?.(1);},{passive:false});
  }
  consumeLook(){ const x=this.lookX,y=this.lookY; this.lookX=0;this.lookY=0; return [x,y]; }
}
