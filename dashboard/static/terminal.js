/* ═══════════════════════════════════════════════════════════════════
   SSC TERMINAL — shared JS for all dashboard pages
   Header clock, status dots, ticker, popup manager, sound toggle (SSE)
   ═══════════════════════════════════════════════════════════════════ */
(function(){
  'use strict';

  function $(sel,root){return (root||document).querySelector(sel)}

  // ═══ MATRIX RAIN ═══
  // Global falling-gold-chars background, auto-created on every page that
  // links terminal.js. Pages can opt out with <body data-no-matrix>.
  //
  // Per-drop state model (replaces the flat number-array that kept every
  // column permanently active):
  //   { y, speed, active, delay }
  // Drops have variable speeds so columns desync visually. When a drop
  // runs off the bottom it flips inactive and queues a random 1.6–16s
  // pause, then wakes up at the top with a fresh speed. Result: at any
  // moment ~55-70% of columns are drawing, the rest are "breathing" —
  // the effect keeps its fresh look instead of filling up over time.
  function initMatrix(){
    if(document.body.getAttribute('data-no-matrix')!==null)return;
    var cvs=$('#matrixRain');
    if(!cvs){
      cvs=document.createElement('canvas');
      cvs.id='matrixRain';
      document.body.insertBefore(cvs,document.body.firstChild);
    }
    // Belt-and-suspenders: enforce fixed-position layout even if the page did
    // not link terminal.css (e.g. dashboard.html which has its own inline CSS).
    cvs.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;width:100vw;height:100vh;'+
      'z-index:0;opacity:0.11;pointer-events:none;display:block';
    var ctx=cvs.getContext('2d');
    function resize(){cvs.width=window.innerWidth;cvs.height=window.innerHeight}
    resize();window.addEventListener('resize',resize);
    var chars=('01 {} [] <> ABCDEF 0123456789 $ '+String.fromCharCode(0x2716,0x25A0,0x25B2,0x25BC)).split('');
    var font=14;
    var drops=[];
    function makeDrop(startActive){
      return {
        y: startActive ? -Math.random()*30 : 0,
        speed: 0.4 + Math.random()*0.9,       // 0.4 – 1.3 rows/frame
        active: startActive,
        delay: startActive ? 0 : Math.floor(Math.random()*120)
      };
    }
    function buildDrops(){
      drops.length = 0;
      var cols = Math.floor(cvs.width/font);
      for(var i=0;i<cols;i++){
        drops.push(makeDrop(Math.random() < 0.55));
      }
    }
    buildDrops();
    window.addEventListener('resize', buildDrops);

    function draw(){
      // Slightly stronger trail fade (0.11 vs old 0.08) so old glyphs clear
      // before the next frame stacks on top. Keeps the canvas from
      // saturating over time.
      ctx.fillStyle='rgba(6,6,10,0.11)';
      ctx.fillRect(0,0,cvs.width,cvs.height);
      ctx.font=font+'px "Fira Code",monospace';
      for(var i=0;i<drops.length;i++){
        var d=drops[i];
        if(!d.active){
          if(d.delay>0){d.delay--;continue}
          // Wake up: new random speed each cycle so the column feels fresh
          d.active=true;
          d.y=-1;
          d.speed=0.4+Math.random()*0.9;
          continue;
        }
        var ch=chars[Math.floor(Math.random()*chars.length)];
        // Head of the drop in gold-bright for a subtle lead-char highlight
        if(d.y >= 0){
          ctx.fillStyle='rgba(228,200,104,0.75)';
          ctx.fillText(ch, i*font, d.y*font);
        }
        d.y += d.speed;
        if(d.y*font > cvs.height){
          // Finished its run — park it with a random pause before restart
          d.active=false;
          d.delay=20 + Math.floor(Math.random()*180);  // 1.6 – 16s at 80ms tick
        }
      }
    }
    setInterval(draw,80);
  }

  // ═══ DIGITAL CLOCK ═══
  function initClock(){
    var el=$('#digiClock');if(!el)return;
    function tick(){
      var d=new Date();
      var hh=String(d.getHours()).padStart(2,'0');
      var mm=String(d.getMinutes()).padStart(2,'0');
      var ss=String(d.getSeconds()).padStart(2,'0');
      el.textContent=hh+':'+mm+':'+ss;
    }
    tick();setInterval(tick,1000);
  }

  // ═══ STATUS DOTS ═══
  function initStatusDots(){
    var botDot=$('#stBot'),apiDot=$('#stApi'),pollDot=$('#stPoll');
    if(!botDot&&!apiDot&&!pollDot)return;
    async function poll(){
      try{
        var r=await fetch('/api/upgrade/status');
        if(r.ok){
          [botDot,apiDot,pollDot].forEach(function(d){if(d){d.classList.remove('off');d.classList.add('on')}});
        }else{throw new Error('not ok')}
      }catch(e){
        [botDot,apiDot,pollDot].forEach(function(d){if(d){d.classList.remove('on');d.classList.add('off')}});
      }
    }
    poll();setInterval(poll,10000);
  }

  // ═══ TICKER ═══
  // Shows the same content as the /copy dashboard ticker: sport-detected open
  // trades with #ID, emoji+label, market question, side badge, and unrealized P&L.
  // Uses dashboard's dSp() keyword map for sport/category detection.
  var DSP_MAP=[
    // Esports first
    ['dota 2:','\uD83E\uDDD9','DOTA'],['dota','\uD83E\uDDD9','DOTA'],['nigma','\uD83E\uDDD9','DOTA'],['pipsqueak','\uD83E\uDDD9','DOTA'],['virtus.pro','\uD83E\uDDD9','DOTA'],
    ['counter-strike','\uD83C\uDFAE','CS'],['csgo','\uD83C\uDFAE','CS'],['cs2-','\uD83C\uDFAE','CS'],['3dmax','\uD83C\uDFAE','CS'],['fokus','\uD83C\uDFAE','CS'],['nrg','\uD83C\uDFAE','CS'],['inner circle','\uD83C\uDFAE','CS'],['fut esport','\uD83C\uDFAE','CS'],
    ['lol:','\u2694\uFE0F','LOL'],['lol','\u2694\uFE0F','LOL'],['league of legends','\u2694\uFE0F','LOL'],['drx','\u2694\uFE0F','LOL'],['t1 ','\u2694\uFE0F','LOL'],['hanwha','\u2694\uFE0F','LOL'],['sk gaming','\u2694\uFE0F','LOL'],['top esports','\u2694\uFE0F','LOL'],
    ['valorant','\uD83D\uDD2B','VAL'],['paper rex','\uD83D\uDD2B','VAL'],
    // Sports
    ['mlb','\u26BE','MLB'],['nba','\uD83C\uDFC0','NBA'],['nhl','\uD83C\uDFD2','NHL'],['nfl','\uD83C\uDFC8','NFL'],
    ['ufc','\uD83E\uDD4A','UFC'],['mma','\uD83E\uDD4A','MMA'],['atp','\uD83C\uDFBE','ATP'],['wta','\uD83C\uDFBE','WTA'],
    // MLB teams
    ['rays','\u26BE','MLB'],['brewers','\u26BE','MLB'],['astros','\u26BE','MLB'],['braves','\u26BE','MLB'],['angels','\u26BE','MLB'],['mariners','\u26BE','MLB'],['athletics','\u26BE','MLB'],['cardinals','\u26BE','MLB'],['tigers','\u26BE','MLB'],['phillies','\u26BE','MLB'],['nationals','\u26BE','MLB'],['marlins','\u26BE','MLB'],['rockies','\u26BE','MLB'],['reds','\u26BE','MLB'],['mets','\u26BE','MLB'],['giants','\u26BE','MLB'],['orioles','\u26BE','MLB'],['pirates','\u26BE','MLB'],['padres','\u26BE','MLB'],['yankees','\u26BE','MLB'],['dodgers','\u26BE','MLB'],['cubs','\u26BE','MLB'],['guardians','\u26BE','MLB'],['twins','\u26BE','MLB'],['rangers','\u26BE','MLB'],['royals','\u26BE','MLB'],['diamondback','\u26BE','MLB'],['white sox','\u26BE','MLB'],['blue jays','\u26BE','MLB'],['red sox','\u26BE','MLB'],
    // NBA teams
    ['celtics','\uD83C\uDFC0','NBA'],['bucks','\uD83C\uDFC0','NBA'],['76ers','\uD83C\uDFC0','NBA'],['knicks','\uD83C\uDFC0','NBA'],['bulls','\uD83C\uDFC0','NBA'],['hawks','\uD83C\uDFC0','NBA'],['nets','\uD83C\uDFC0','NBA'],['magic','\uD83C\uDFC0','NBA'],['mavericks','\uD83C\uDFC0','NBA'],['timberwolves','\uD83C\uDFC0','NBA'],['pelicans','\uD83C\uDFC0','NBA'],['kings','\uD83C\uDFC0','NBA'],['raptors','\uD83C\uDFC0','NBA'],['grizzlies','\uD83C\uDFC0','NBA'],['lakers','\uD83C\uDFC0','NBA'],['warriors','\uD83C\uDFC0','NBA'],['heat','\uD83C\uDFC0','NBA'],['spurs','\uD83C\uDFC0','NBA'],['suns','\uD83C\uDFC0','NBA'],['thunder','\uD83C\uDFC0','NBA'],['cavaliers','\uD83C\uDFC0','NBA'],['pacers','\uD83C\uDFC0','NBA'],['pistons','\uD83C\uDFC0','NBA'],['rockets','\uD83C\uDFC0','NBA'],['clippers','\uD83C\uDFC0','NBA'],['nuggets','\uD83C\uDFC0','NBA'],['blazers','\uD83C\uDFC0','NBA'],['jazz','\uD83C\uDFC0','NBA'],['wizards','\uD83C\uDFC0','NBA'],['hornets','\uD83C\uDFC0','NBA'],
    // NHL teams
    ['flyers','\uD83C\uDFD2','NHL'],['islanders','\uD83C\uDFD2','NHL'],['blues','\uD83C\uDFD2','NHL'],['ducks','\uD83C\uDFD2','NHL'],['penguins','\uD83C\uDFD2','NHL'],['canadiens','\uD83C\uDFD2','NHL'],['maple leafs','\uD83C\uDFD2','NHL'],['oilers','\uD83C\uDFD2','NHL'],['flames','\uD83C\uDFD2','NHL'],['canucks','\uD83C\uDFD2','NHL'],['blackhawks','\uD83C\uDFD2','NHL'],['predators','\uD83C\uDFD2','NHL'],['lightning','\uD83C\uDFD2','NHL'],['panthers','\uD83C\uDFD2','NHL'],['hurricanes','\uD83C\uDFD2','NHL'],['avalanche','\uD83C\uDFD2','NHL'],['capitals','\uD83C\uDFD2','NHL'],
    // Soccer
    ['soccer','\u26BD','SOC'],['bundesliga','\u26BD','BL'],['epl','\u26BD','EPL'],['ucl','\u26BD','UCL'],['mls','\u26BD','MLS'],
    ['arsenal','\u26BD','EPL'],['liverpool','\u26BD','EPL'],['man city','\u26BD','EPL'],['manchester','\u26BD','EPL'],['tottenham','\u26BD','EPL'],['chelsea','\u26BD','EPL'],['newcastle','\u26BD','EPL'],
    ['bayern','\u26BD','BL'],['dortmund','\u26BD','BL'],['leipzig','\u26BD','BL'],['borussia','\u26BD','BL'],
    ['barcelona','\u26BD','LAL'],['madrid','\u26BD','LAL'],['atletico','\u26BD','LAL'],['sevilla','\u26BD','LAL'],
    ['juventus','\u26BD','SA'],['napoli','\u26BD','SA'],['milan','\u26BD','SA'],['roma','\u26BD','SA'],['inter','\u26BD','SA'],
    ['psg','\u26BD','FL1'],['marseille','\u26BD','FL1'],['lyon','\u26BD','FL1'],
    // Tennis
    ['wimbledon','\uD83C\uDFBE','ATP'],['roland garros','\uD83C\uDFBE','ATP'],['indian wells','\uD83C\uDFBE','ATP'],['miami open','\uD83C\uDFBE','ATP'],['monte carlo','\uD83C\uDFBE','ATP'],['barcelona open','\uD83C\uDFBE','ATP'],['bmw open','\uD83C\uDFBE','ATP'],['challenger','\uD83C\uDFBE','ATP'],
    ['sinner','\uD83C\uDFBE','ATP'],['djokovic','\uD83C\uDFBE','ATP'],['alcaraz','\uD83C\uDFBE','ATP'],['medvedev','\uD83C\uDFBE','ATP'],['rublev','\uD83C\uDFBE','ATP'],['zverev','\uD83C\uDFBE','ATP'],['tsitsipas','\uD83C\uDFBE','ATP'],['fritz','\uD83C\uDFBE','ATP'],
    ['swiatek','\uD83C\uDFBE','WTA'],['sabalenka','\uD83C\uDFBE','WTA'],['gauff','\uD83C\uDFBE','WTA'],
    // CS teams
    ['faze','\uD83C\uDFAE','CS'],['heroic','\uD83C\uDFAE','CS'],['vitality','\uD83C\uDFAE','CS'],['g2 ','\uD83C\uDFAE','CS'],['mouz','\uD83C\uDFAE','CS'],['spirit','\uD83C\uDFAE','CS'],['natus','\uD83C\uDFAE','CS'],
    // Geopolitics
    ['iran','\uD83D\uDE80','GEO'],['israel','\uD83D\uDE80','GEO'],['gaza','\uD83D\uDE80','GEO'],['ukraine','\uD83D\uDE80','GEO'],['russia','\uD83D\uDE80','GEO'],['ceasefire','\uD83D\uDE80','GEO'],['nuclear','\uD83D\uDE80','GEO'],
    // Politics
    ['trump','\uD83C\uDFDB\uFE0F','POL'],['biden','\uD83C\uDFDB\uFE0F','POL'],['congress','\uD83C\uDFDB\uFE0F','POL'],['senate','\uD83C\uDFDB\uFE0F','POL'],['election','\uD83C\uDFDB\uFE0F','POL'],['president','\uD83C\uDFDB\uFE0F','POL'],['tariff','\uD83C\uDFDB\uFE0F','POL']
  ];
  function dSp(t){
    var s=((t.market_slug||t.slug||'')+(t.market_question||'')+(t.title||'')+(t.detail||'')).toLowerCase();
    for(var i=0;i<DSP_MAP.length;i++){
      var k=DSP_MAP[i][0];
      if(s.indexOf(k)>=0)return{e:DSP_MAP[i][1]||'\uD83C\uDFAE',l:DSP_MAP[i][2]||'E-Sport'};
    }
    return{e:'',l:''};
  }
  async function loadTicker(){
    var el=$('#sscTicker');if(!el)return;
    function emptyState(msg){
      el.textContent='';
      var frag=document.createElement('div');frag.className='tk-i dim';
      frag.textContent=msg;el.appendChild(frag);
    }
    function buildOpenTradeItem(t){
      // Matches dashboard.html ticker format exactly: #ID  🎾 ATP  Market  [SIDE @ PRICE]  +$PnL
      var item=document.createElement('span');item.className='tk-i';
      // #ID
      var idEl=document.createElement('span');
      idEl.style.color='var(--text)';idEl.style.fontWeight='600';
      idEl.textContent='#'+(t.id||'?');
      // Sport emoji + label
      var sp=dSp(t);
      var spEl=document.createElement('span');
      spEl.style.color='var(--text)';spEl.style.fontWeight='600';
      spEl.textContent=(sp.e?sp.e+' '+sp.l:'');
      // Market question (gold)
      var q=document.createElement('span');
      q.style.color='var(--gold)';
      q.textContent=String(t.market_question||'').substring(0,35);
      // Side + price badge (gold outline)
      var badge=document.createElement('span');
      badge.style.background='var(--gold-dim)';badge.style.color='var(--gold)';
      badge.style.border='1px solid rgba(201,168,76,0.25)';
      badge.style.padding='1px 8px';badge.style.borderRadius='3px';
      badge.style.fontWeight='600';badge.style.fontSize='.78em';
      var entry=Math.round((t.entry_price||0)*100);
      badge.textContent=(t.side||'')+' @ '+entry+'\u00A2';
      // P&L (up/dn)
      var pnl=Number(t.pnl_unrealized||0);
      var pnlEl=document.createElement('span');pnlEl.className=pnl>=0?'up':'dn';
      pnlEl.textContent=(pnl>=0?'+':'-')+'$'+Math.abs(pnl).toFixed(2);
      item.appendChild(idEl);item.appendChild(spEl);item.appendChild(q);item.appendChild(badge);item.appendChild(pnlEl);
      return item;
    }
    function buildFunItem(e){
      var item=document.createElement('span');item.className='tk-i';
      var icon=document.createElement('span');icon.className='s';
      icon.textContent=e.type==='buy'?'\uD83D\uDCB0':e.type==='sell'?'\uD83D\uDCB8':e.type==='smart_sell'?'\uD83E\uDDE0':e.type==='redeem'?'\uD83D\uDCB5':'\u26A1';
      var sp=dSp({market_question:e.title||'',title:e.title||'',detail:e.detail||''});
      var spEl=document.createElement('span');
      spEl.style.color='var(--text)';spEl.style.fontWeight='600';
      spEl.textContent=(sp.e?sp.e+' '+sp.l:'');
      var title=document.createElement('span');
      title.style.color='var(--gold)';
      title.textContent=String(e.title||'').substring(0,40);
      item.appendChild(icon);item.appendChild(spEl);item.appendChild(title);
      if(e.pnl){
        var pnlEl=document.createElement('span');
        pnlEl.className=e.pnl>0?'up':'dn';
        pnlEl.textContent=(e.pnl>0?'+':'-')+'$'+Math.abs(Number(e.pnl)).toFixed(2);
        item.appendChild(pnlEl);
      }
      return item;
    }
    try{
      var r=await fetch('/api/live-data');
      if(r.ok){
        var d=await r.json();
        var trades=(d&&d.open_trades)||[];
        if(trades.length){
          // normalize null current_price → entry_price
          trades.forEach(function(t){if(t.current_price==null)t.current_price=t.entry_price||0});
          // active-range filter + cap
          var usable=trades.filter(function(t){return t.current_price>0.01&&t.current_price<0.99}).slice(0,14);
          if(usable.length){
            el.textContent='';
            for(var pass=0;pass<2;pass++){usable.forEach(function(t){el.appendChild(buildOpenTradeItem(t))})}
            return;
          }
        }
      }
    }catch(e){}
    try{
      var r2=await fetch('/api/fun/ticker');
      if(r2.ok){
        var d2=await r2.json();
        if(d2&&d2.events&&d2.events.length){
          el.textContent='';
          for(var p=0;p<2;p++){d2.events.forEach(function(e){el.appendChild(buildFunItem(e))})}
          return;
        }
      }
    }catch(e){}
    emptyState('no open trades');
  }
  function initTicker(){
    if(!$('#sscTicker'))return;
    loadTicker();
    setInterval(loadTicker,30000);
  }

  // ═══ WIDE/DESKTOP TOGGLE ═══
  // Matches the dashboard's togWide(): toggles `html.wide` class, persists in localStorage.
  // The button lives in the header (#sscWide). Label flips between "Desktop" and "Mobile".
  var WideMode={
    init:function(){
      var self=this;
      var saved=localStorage.getItem('ssc_wide');
      if(saved===null){
        // First visit: default to Desktop mode on wide screens, Mobile on narrow
        if(window.innerWidth>900)document.documentElement.classList.add('wide');
      }else if(saved==='1'){
        document.documentElement.classList.add('wide');
      }
      var btn=$('#sscWide');
      if(!btn)return;
      this.updateBtn(btn);
      btn.addEventListener('click',function(){
        document.documentElement.classList.toggle('wide');
        var w=document.documentElement.classList.contains('wide');
        localStorage.setItem('ssc_wide',w?'1':'0');
        self.updateBtn(btn);
      });
    },
    updateBtn:function(btn){
      var w=document.documentElement.classList.contains('wide');
      btn.textContent=w?'\u25A4 Mobile':'\u25A4 Desktop';
      btn.classList.toggle('on',w);
    }
  };

  // ═══ SOUND ═══
  var Sound={
    enabled:localStorage.getItem('ssc_sound')!=='off',
    ctx:null,
    init:function(){
      var self=this;
      var btn=$('#sscSound');
      if(btn){
        this.updateBtn(btn);
        btn.addEventListener('click',function(){
          self.enabled=!self.enabled;
          localStorage.setItem('ssc_sound',self.enabled?'on':'off');
          self.updateBtn(btn);
          if(self.enabled)self.tone(660,0.06,'sine',0.12);
        });
      }
    },
    updateBtn:function(btn){
      btn.textContent=this.enabled?'\u{1F50A} SOUND':'\u{1F507} SOUND';
      btn.classList.toggle('on',this.enabled);
    },
    _ensureCtx:function(){
      if(!this.ctx){
        try{this.ctx=new (window.AudioContext||window.webkitAudioContext)()}catch(e){return null}
      }
      return this.ctx;
    },
    tone:function(freq,dur,type,vol){
      if(!this.enabled)return;
      var c=this._ensureCtx();if(!c)return;
      try{
        var o=c.createOscillator(),g=c.createGain();
        o.type=type||'sine';o.frequency.value=freq;g.gain.value=vol||0.1;
        g.gain.exponentialRampToValueAtTime(0.001,c.currentTime+dur);
        o.connect(g);g.connect(c.destination);
        o.start();o.stop(c.currentTime+dur);
      }catch(e){}
    },
    win:function(){this.tone(523,0.08);var self=this;setTimeout(function(){self.tone(784,0.1)},80)},
    loss:function(){this.tone(220,0.18,'sawtooth',0.1)},
    blip:function(){this.tone(440,0.05,'square',0.08)},
  };

  // ═══ POPUP MANAGER (single slot, new overwrites old, via SSE) ═══
  // Built with DOM methods — no innerHTML with external data.
  var PopupManager={
    slot:null,current:null,dismissTimer:null,
    init:function(){
      this.slot=$('.popup-slot');
      if(!this.slot){
        this.slot=document.createElement('div');
        this.slot.className='popup-slot';
        document.body.appendChild(this.slot);
      }
      this.connectSSE();
    },
    connectSSE:function(){
      if(typeof EventSource==='undefined')return;
      var self=this;
      try{
        var es=new EventSource('/api/stream');
        es.addEventListener('new_trade',function(ev){
          var d=safeParse(ev.data);if(!d)return;
          self.show({
            severity:'success',
            head:'NEW BUY',
            title:(d.trader||'?')+' → '+String(d.market||'unknown').slice(0,60),
            body:'$'+fmt(d.size)+' @ '+pct(d.price)+(d.category?'  '+String(d.category).slice(0,20):''),
          });
          Sound.blip();
        });
        es.addEventListener('trade_closed',function(ev){
          var d=safeParse(ev.data);if(!d)return;
          var pnl=d.pnl||0;
          self.show({
            severity:pnl>=0?'success':'alert',
            head:'CLOSE · '+(pnl>=0?'WIN':'LOSS'),
            title:(d.trader||'?')+' → '+String(d.market||'unknown').slice(0,60),
            body:(pnl>=0?'+':'')+'$'+fmt(pnl)+'  size $'+fmt(d.size||0),
          });
          if(pnl>=0)Sound.win();else Sound.loss();
        });
        es.addEventListener('brain_decision',function(ev){
          var d=safeParse(ev.data);if(!d)return;
          self.show({
            severity:'info',
            head:'BRAIN · '+String(d.action||'DECISION').toUpperCase(),
            title:String(d.target||'—')+(d.category?' · '+String(d.category):''),
            body:String(d.reason||'').slice(0,80),
          });
          Sound.blip();
        });
        es.addEventListener('smart_sell',function(ev){
          var d=safeParse(ev.data);if(!d)return;
          var pnl=d.pnl||0;
          self.show({
            severity:pnl>=0?'success':'alert',
            head:'SMART SELL',
            title:(d.trader||'?')+' → '+String(d.market||'').slice(0,60),
            body:(pnl>=0?'+':'')+'$'+fmt(pnl),
          });
          if(pnl>=0)Sound.win();else Sound.loss();
        });
        es.onerror=function(){
          try{es.close()}catch(e){}
          setTimeout(function(){self.connectSSE()},3000);
        };
      }catch(e){}
    },
    show:function(p){
      var self=this;
      if(this.current){
        try{this.current.remove()}catch(e){}
        this.current=null;
      }
      if(this.dismissTimer){clearTimeout(this.dismissTimer);this.dismissTimer=null}

      var div=document.createElement('div');
      div.className='popup '+(p.severity||'info');

      var head=document.createElement('div');head.className='popup-head';
      var dot=document.createElement('span');dot.className='dot';
      head.appendChild(dot);
      head.appendChild(document.createTextNode(String(p.head||'EVENT')));

      var title=document.createElement('div');title.className='popup-title';
      title.textContent=String(p.title||'');

      var body=document.createElement('div');body.className='popup-body';
      body.textContent=String(p.body||'');

      var time=document.createElement('div');time.className='popup-time';
      var now=new Date();
      time.textContent=
        String(now.getHours()).padStart(2,'0')+':'+
        String(now.getMinutes()).padStart(2,'0')+':'+
        String(now.getSeconds()).padStart(2,'0');

      div.appendChild(head);
      div.appendChild(title);
      div.appendChild(body);
      div.appendChild(time);
      this.slot.appendChild(div);
      this.current=div;

      void div.offsetWidth;  // force reflow
      div.classList.add('show');

      this.dismissTimer=setTimeout(function(){
        if(self.current===div){
          div.classList.remove('show');
          setTimeout(function(){
            try{div.remove()}catch(e){}
            if(self.current===div)self.current=null;
          },400);
        }
      },8000);
    },
  };

  function safeParse(s){try{return JSON.parse(s)}catch(e){return null}}
  function fmt(v,d){if(v==null)return '0.00';return Number(v).toFixed(d==null?2:d)}
  function pct(v){if(v==null)return '--';return Math.round(Number(v)*100)+'c'}

  window.SauseTerminal={
    PopupManager:PopupManager,
    Sound:Sound,
    WideMode:WideMode,
    initClock:initClock,
    initStatusDots:initStatusDots,
    initTicker:initTicker,
  };

  function bootstrap(){
    initMatrix();
    initClock();
    initStatusDots();
    initTicker();
    Sound.init();
    WideMode.init();
    PopupManager.init();
  }
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',bootstrap);
  }else{
    bootstrap();
  }
})();
