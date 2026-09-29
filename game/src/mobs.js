// Blocky mobs: hostile Clashlings & Spikers, passive Cubits.
import * as THREE from 'three';
import {isSolid} from './blocks.js';

const box=(w,h,d,color)=>new THREE.Mesh(new THREE.BoxGeometry(w,h,d),new THREE.MeshLambertMaterial({color}));

export const TYPES={
  clashling:{hp:3, speed:2.6, dmg:1, score:25, w:0.62, h:1.75, color:0x4caf50, hostile:true, name:'Clashling'},
  spiker:   {hp:5, speed:3.4, dmg:2, score:60, w:0.7,  h:1.3,  color:0x9a4bff, hostile:true, name:'Spiker'},
  brute:    {hp:10,speed:2.0, dmg:3, score:140,w:1.0,  h:2.3,  color:0xd0492e, hostile:true, name:'Brute'},
  cubit:    {hp:2, speed:1.4, dmg:0, score:10, w:0.7,  h:0.8,  color:0xffb3c7, hostile:false,name:'Cubit'},
};

function buildBody(type){
  const g=new THREE.Group();
  const t=TYPES[type], c=t.color;
  const dark=new THREE.Color(c).multiplyScalar(0.7).getHex();
  if(type==='cubit'){
    const body=box(0.8,0.6,1.0,c); body.position.y=0.45; g.add(body);
    const head=box(0.5,0.45,0.4,new THREE.Color(c).multiplyScalar(1.1).getHex()); head.position.set(0,0.62,0.6); g.add(head);
    const snout=box(0.24,0.2,0.1,0xff8fae); snout.position.set(0,0.58,0.82); g.add(snout);
    g.legs=[];
    for(const [x,z] of [[-0.26,0.32],[0.26,0.32],[-0.26,-0.32],[0.26,-0.32]]){
      const l=box(0.2,0.35,0.2,dark); l.position.set(x,0.17,z); g.add(l); g.legs.push(l);
    }
    g.head=head;
  }else{
    const bw=t.w, bh=t.h;
    const body=box(bw,bh*0.45,bw*0.55,c); body.position.y=bh*0.52; g.add(body);
    const head=box(bw*0.85,bw*0.85,bw*0.85,new THREE.Color(c).multiplyScalar(1.15).getHex());
    head.position.y=bh*0.86; g.add(head);
    // pixel eyes
    for(const s of [-1,1]){
      const e=box(bw*0.2,bw*0.16,0.06,0x120b16);
      e.position.set(s*bw*0.2, bh*0.9, bw*0.44); g.add(e);
    }
    const arms=[],legs=[];
    for(const s of [-1,1]){
      const a=box(bw*0.24,bh*0.42,bw*0.24,dark);
      a.position.set(s*(bw*0.62), bh*0.56, 0); a.geometry.translate(0,-bh*0.2,0); a.position.y=bh*0.72; g.add(a); arms.push(a);
      const l=box(bw*0.28,bh*0.32,bw*0.28,dark);
      l.geometry.translate(0,-bh*0.16,0); l.position.set(s*bw*0.24, bh*0.3,0); g.add(l); legs.push(l);
    }
    if(type==='spiker'){
      for(let i=0;i<4;i++){
        const sp=box(0.12,0.3,0.12,0xffe36b);
        sp.position.set((i%2?1:-1)*0.18, bh*0.78+0.3, (i<2?1:-1)*0.18); g.add(sp);
      }
    }
    if(type==='brute'){
      const crown=box(bw*0.9,0.16,bw*0.9,0xffc63f); crown.position.y=bh*1.0; g.add(crown);
    }
    g.arms=arms; g.legs=legs; g.head=head;
  }
  return g;
}

export class Mob{
  constructor(type,x,y,z){
    const t=TYPES[type];
    this.type=type; this.def=t;
    this.hp=t.hp; this.maxhp=t.hp;
    this.pos=new THREE.Vector3(x,y,z);
    this.vel=new THREE.Vector3();
    this.w=t.w; this.h=t.h;
    this.onGround=false;
    this.mesh=buildBody(type);
    this.mesh.position.copy(this.pos);
    this.yaw=Math.random()*6.28;
    this.wander=new THREE.Vector3(Math.random()-.5,0,Math.random()-.5).normalize();
    this.wanderT=1+Math.random()*2;
    this.atkCd=0; this.hurtT=0; this.anim=Math.random()*10; this.dead=false; this.deathT=0;
    this.jumpCd=0;
  }
  aabbFree(world,x,y,z){
    const r=this.w/2;
    for(let bx=Math.floor(x-r);bx<=Math.floor(x+r);bx++)
    for(let by=Math.floor(y);by<=Math.floor(y+this.h-0.02);by++)
    for(let bz=Math.floor(z-r);bz<=Math.floor(z+r);bz++)
      if(isSolid(world.get(bx,by,bz))) return false;
    return true;
  }
  update(dt,world,player,api){
    if(this.dead){
      this.deathT+=dt;
      const k=1-Math.min(1,this.deathT/0.32);
      this.mesh.scale.set(k,k*0.6,k);
      this.mesh.rotation.z+=dt*8;
      this.mesh.position.y=this.pos.y+ (1-k)*0.4;
      return this.deathT<0.34;
    }
    this.anim+=dt; this.atkCd-=dt; this.hurtT-=dt; this.jumpCd-=dt;
    const dx=player.pos.x-this.pos.x, dz=player.pos.z-this.pos.z;
    const dist=Math.hypot(dx,dz);
    const hostile=this.def.hostile;
    let mx=0,mz=0, speed=this.def.speed;

    if(hostile && dist<26){
      const inv=1/(dist||1); mx=dx*inv; mz=dz*inv;
      if(dist<1.25 && Math.abs(player.pos.y-this.pos.y)<2.2){
        if(this.atkCd<=0){
          this.atkCd=0.9;
          api.damagePlayer(this.def.dmg, this);
          this.attackT=0.25;
        }
        speed*=0.3;
      }
    }else{
      this.wanderT-=dt;
      if(this.wanderT<=0){this.wanderT=1.5+Math.random()*3;
        this.wander.set(Math.random()-.5,0,Math.random()-.5).normalize();
        if(Math.random()<0.3)this.wander.set(0,0,0);}
      mx=this.wander.x;mz=this.wander.z;speed=this.def.speed*0.45;
      if(!hostile && dist<4){ mx=-dx/dist; mz=-dz/dist; speed=this.def.speed*1.2; } // cubits flee
    }

    // horizontal move with collision + auto step/jump
    this.vel.x=mx*speed; this.vel.z=mz*speed;
    this.vel.y-=26*dt; if(this.vel.y<-30)this.vel.y=-30;

    const tryMove=(ax,az)=>{
      const nx=this.pos.x+ax, nz=this.pos.z+az;
      if(this.aabbFree(world,nx,this.pos.y,this.pos.z)) this.pos.x=nx;
      else if(this.onGround && this.jumpCd<=0 && this.aabbFree(world,nx,this.pos.y+1.05,this.pos.z)){this.vel.y=8.2;this.jumpCd=0.45;}
      if(this.aabbFree(world,this.pos.x,this.pos.y,nz)) this.pos.z=nz;
      else if(this.onGround && this.jumpCd<=0 && this.aabbFree(world,this.pos.x,this.pos.y+1.05,nz)){this.vel.y=8.2;this.jumpCd=0.45;}
    };
    tryMove(this.vel.x*dt,this.vel.z*dt);

    const ny=this.pos.y+this.vel.y*dt;
    if(this.aabbFree(world,this.pos.x,ny,this.pos.z)){this.pos.y=ny;this.onGround=false;}
    else{
      if(this.vel.y<0){this.onGround=true;this.pos.y=Math.floor(this.pos.y)+ (this.aabbFree(world,this.pos.x,Math.floor(this.pos.y),this.pos.z)?0:1);}
      this.vel.y=0;
    }
    if(this.pos.y<-6){this.hp=0;}

    // orient + animate
    if(mx||mz) this.yaw=Math.atan2(mx,mz);
    this.mesh.position.set(this.pos.x,this.pos.y,this.pos.z);
    this.mesh.rotation.y=this.yaw;
    const moving=(Math.abs(mx)+Math.abs(mz))>0.05;
    const sw=moving?Math.sin(this.anim*9)*0.7:0;
    if(this.mesh.legs){
      if(this.type==='cubit'){
        this.mesh.legs.forEach((l,i)=>{l.rotation.x=Math.sin(this.anim*11+(i%2?3.1:0))*(moving?0.6:0);});
      }else{
        this.mesh.legs[0].rotation.x=sw; this.mesh.legs[1].rotation.x=-sw;
      }
    }
    if(this.mesh.arms){
      const atk=this.attackT>0? (this.attackT-=dt, 1):0;
      this.mesh.arms[0].rotation.x=atk?-2.2:-1.4-sw*0.4;
      this.mesh.arms[1].rotation.x=atk?-2.2:-1.4+sw*0.4;
    }
    if(this.mesh.head) this.mesh.head.position.y+= 0; // reserved
    const bob=moving?Math.abs(Math.sin(this.anim*9))*0.06:Math.sin(this.anim*2)*0.02;
    this.mesh.position.y+=bob;

    // hurt flash
    const flash=this.hurtT>0;
    this.mesh.traverse(o=>{ if(o.material&&o.material.emissive) o.material.emissive.setHex(flash?0xff4444:0x000000); });
    return true;
  }
  hurt(dmg,dirX,dirZ){
    this.hp-=dmg; this.hurtT=0.18;
    this.vel.y=Math.max(this.vel.y,5.5);
    this.pos.x+=dirX*0.35; this.pos.z+=dirZ*0.35;
    return this.hp<=0;
  }
}

export class MobManager{
  constructor(scene,world){
    this.scene=scene; this.world=world; this.mobs=[];
    this.group=new THREE.Group(); scene.add(this.group);
  }
  clear(){ for(const m of this.mobs) this.group.remove(m.mesh); this.mobs.length=0; }
  spawn(type,x,y,z){
    const m=new Mob(type,x,y,z); this.mobs.push(m); this.group.add(m.mesh); return m;
  }
  spawnRing(type,px,pz,rMin,rMax,WS){
    for(let i=0;i<30;i++){
      const a=Math.random()*6.283, r=rMin+Math.random()*(rMax-rMin);
      const x=Math.floor(px+Math.cos(a)*r), z=Math.floor(pz+Math.sin(a)*r);
      if(x<2||z<2||x>WS-3||z>WS-3)continue;
      const y=this.world.surfaceY(x,z)+1;
      if(y<2||y>34)continue;
      const m=this.spawn(type,x+0.5,y,z+0.5);
      return m;
    }
    return null;
  }
  update(dt,player,api){
    for(let i=this.mobs.length-1;i>=0;i--){
      const m=this.mobs[i];
      const alive=m.update(dt,this.world,player,api);
      if(!alive){ this.group.remove(m.mesh); this.mobs.splice(i,1); }
    }
  }
  /** ray vs mob AABB, returns nearest hit within dist */
  pick(origin,dir,maxDist){
    let best=null,bt=maxDist;
    for(const m of this.mobs){
      if(m.dead)continue;
      const r=m.w/2+0.15;
      const min=[m.pos.x-r,m.pos.y,m.pos.z-r], max=[m.pos.x+r,m.pos.y+m.h,m.pos.z+r];
      const o=[origin.x,origin.y,origin.z], d=[dir.x,dir.y,dir.z];
      let t0=0,t1=bt,ok=true;
      for(let a=0;a<3;a++){
        if(Math.abs(d[a])<1e-6){ if(o[a]<min[a]||o[a]>max[a]){ok=false;break;} continue; }
        let ta=(min[a]-o[a])/d[a], tb=(max[a]-o[a])/d[a];
        if(ta>tb){const s=ta;ta=tb;tb=s;}
        t0=Math.max(t0,ta); t1=Math.min(t1,tb);
        if(t0>t1){ok=false;break;}
      }
      if(ok&&t0<bt){bt=t0;best=m;}
    }
    return best?{mob:best,dist:bt}:null;
  }
}
