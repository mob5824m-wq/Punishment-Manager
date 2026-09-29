// Particle system (GPU points pool) + screen shake + tiny audio synth.
import * as THREE from 'three';

const MAX=900;

export class Particles{
  constructor(scene){
    this.n=MAX;
    this.pos=new Float32Array(MAX*3);
    this.col=new Float32Array(MAX*3);
    this.vel=new Float32Array(MAX*3);
    this.life=new Float32Array(MAX);
    this.max=new Float32Array(MAX);
    this.size=new Float32Array(MAX);
    this.head=0;
    const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.BufferAttribute(this.pos,3));
    g.setAttribute('color',new THREE.BufferAttribute(this.col,3));
    g.setAttribute('psize',new THREE.BufferAttribute(this.size,1));
    const m=new THREE.PointsMaterial({size:0.16,vertexColors:true,sizeAttenuation:true});
    m.onBeforeCompile=(sh)=>{
      sh.vertexShader='attribute float psize;\n'+sh.vertexShader
        .replace('gl_PointSize = size;','gl_PointSize = psize;');
    };
    this.points=new THREE.Points(g,m);
    this.points.frustumCulled=false;
    this.points.renderOrder=2;
    scene.add(this.points);
    for(let i=0;i<MAX;i++){this.pos[i*3+1]=-999;}
  }
  spawn(x,y,z,color,count=10,opt={}){
    const spd=opt.speed??3, up=opt.up??2.5, life=opt.life??0.7, sz=opt.size??0.14, spread=opt.spread??0.35;
    const r=(color>>16&255)/255, g=(color>>8&255)/255, b=(color&255)/255;
    for(let i=0;i<count;i++){
      const k=this.head; this.head=(this.head+1)%MAX;
      this.pos[k*3]=x+(Math.random()-.5)*spread;
      this.pos[k*3+1]=y+(Math.random()-.5)*spread;
      this.pos[k*3+2]=z+(Math.random()-.5)*spread;
      this.vel[k*3]=(Math.random()-.5)*spd;
      this.vel[k*3+1]=Math.random()*up;
      this.vel[k*3+2]=(Math.random()-.5)*spd;
      const t=0.65+Math.random()*0.7;
      this.col[k*3]=Math.min(1,r*t);this.col[k*3+1]=Math.min(1,g*t);this.col[k*3+2]=Math.min(1,b*t);
      this.life[k]=this.max[k]=life*(0.7+Math.random()*0.6);
      this.size[k]=sz*(0.6+Math.random()*0.9);
    }
  }
  update(dt,world){
    const p=this.pos,v=this.vel;
    for(let i=0;i<MAX;i++){
      if(this.life[i]<=0){ if(this.size[i]!==0){this.size[i]=0;} continue; }
      this.life[i]-=dt;
      if(this.life[i]<=0){this.size[i]=0;p[i*3+1]=-999;continue;}
      v[i*3+1]-=14*dt;
      const nx=p[i*3]+v[i*3]*dt, ny=p[i*3+1]+v[i*3+1]*dt, nz=p[i*3+2]+v[i*3+2]*dt;
      if(world && world.get(Math.floor(nx),Math.floor(ny),Math.floor(nz))>0 && world.get(Math.floor(nx),Math.floor(ny),Math.floor(nz))!==9){
        v[i*3]*=0.4; v[i*3+2]*=0.4; v[i*3+1]=Math.abs(v[i*3+1])*0.25;
      }else{ p[i*3]=nx;p[i*3+1]=ny;p[i*3+2]=nz; }
      // shrink at the end
      const f=this.life[i]/this.max[i];
      if(f<0.35) this.size[i]*=0.93;
    }
    this.points.geometry.attributes.position.needsUpdate=true;
    this.points.geometry.attributes.color.needsUpdate=true;
    this.points.geometry.attributes.psize.needsUpdate=true;
  }
  reset(){for(let i=0;i<MAX;i++){this.life[i]=0;this.size[i]=0;this.pos[i*3+1]=-999;}}
}

export class Shake{
  constructor(){this.t=0;this.amp=0;this.freq=32;this.off=new THREE.Vector3();this.roll=0;}
  add(amp,dur=0.25){ this.amp=Math.max(this.amp,amp); this.t=Math.max(this.t,dur); this.dur=this.t; }
  update(dt,time){
    if(this.t>0){
      this.t-=dt;
      const k=Math.max(0,this.t/(this.dur||1));
      const a=this.amp*k*k;
      this.off.set(Math.sin(time*this.freq)*a, Math.sin(time*this.freq*1.7+1.3)*a, Math.cos(time*this.freq*0.9)*a*0.6);
      this.roll=Math.sin(time*this.freq*0.8)*a*0.12;
      if(this.t<=0){this.amp=0;this.off.set(0,0,0);this.roll=0;}
    }
  }
}

/* --------- tiny web-audio blips (no assets) --------- */
export class Sfx{
  constructor(){this.ctx=null;this.on=true;}
  init(){ if(!this.ctx){ try{this.ctx=new (window.AudioContext||window.webkitAudioContext)();}catch(e){} }
    if(this.ctx&&this.ctx.state==='suspended')this.ctx.resume(); }
  blip(freq=440,dur=0.08,type='square',gain=0.08,slide=0){
    if(!this.ctx||!this.on)return;
    const t=this.ctx.currentTime;
    const o=this.ctx.createOscillator(), g=this.ctx.createGain();
    o.type=type;o.frequency.setValueAtTime(freq,t);
    if(slide)o.frequency.exponentialRampToValueAtTime(Math.max(40,freq+slide),t+dur);
    g.gain.setValueAtTime(gain,t);
    g.gain.exponentialRampToValueAtTime(0.0008,t+dur);
    o.connect(g);g.connect(this.ctx.destination);o.start(t);o.stop(t+dur+0.02);
  }
  noise(dur=0.12,gain=0.07,filt=900){
    if(!this.ctx||!this.on)return;
    const t=this.ctx.currentTime, n=Math.floor(this.ctx.sampleRate*dur);
    const buf=this.ctx.createBuffer(1,n,this.ctx.sampleRate), d=buf.getChannelData(0);
    for(let i=0;i<n;i++)d[i]=(Math.random()*2-1)*(1-i/n);
    const s=this.ctx.createBufferSource();s.buffer=buf;
    const f=this.ctx.createBiquadFilter();f.type='lowpass';f.frequency.value=filt;
    const g=this.ctx.createGain();g.gain.value=gain;
    s.connect(f);f.connect(g);g.connect(this.ctx.destination);s.start(t);
  }
  mine(){this.noise(0.07,0.05,1600);}
  break_(){this.noise(0.18,0.1,800);this.blip(180,0.1,'square',0.04,-80);}
  place(){this.blip(300,0.07,'square',0.05,120);}
  hit(){this.blip(660,0.07,'square',0.07,-260);this.noise(0.06,0.05,2200);}
  kill(){this.blip(520,0.09,'square',0.08,240);setTimeout(()=>this.blip(780,0.12,'square',0.07,300),70);}
  hurt(){this.blip(170,0.22,'sawtooth',0.09,-90);}
  die(){[440,330,240,150].forEach((f,i)=>setTimeout(()=>this.blip(f,0.28,'square',0.09,-40),i*140));}
  level(){[520,660,880].forEach((f,i)=>setTimeout(()=>this.blip(f,0.14,'square',0.07),i*90));}
  ui(){this.blip(700,0.05,'square',0.05);}
}
