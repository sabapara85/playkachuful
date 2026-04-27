import os, json

# Use gevent on Render (production), eventlet locally
if os.environ.get('RENDER'):
    from gevent import monkey
    monkey.patch_all()
else:
    import eventlet
    eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random, string

# ── Upstash Redis (optional — for persistence across Render restarts) ──
try:
    import redis as redis_lib
    _redis_url = os.environ.get('UPSTASH_REDIS_URL') or os.environ.get('REDIS_URL')
    _redis = redis_lib.from_url(_redis_url, decode_responses=True) if _redis_url else None
    if _redis:
        _redis.ping()
        print('Redis connected ✅')
    else:
        print('Redis not configured — using file storage')
except Exception as _e:
    print(f'Redis unavailable ({_e}) — using file storage')
    _redis = None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'kachuful-2024-secret')

async_mode = 'gevent' if os.environ.get('RENDER') else 'eventlet'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode)

# Unified sleep compatible with both gevent (Render) and eventlet (local dev)
def _sleep(secs):
    if os.environ.get('RENDER'):
        from gevent import sleep as _gs; _gs(secs)
    else:
        eventlet.sleep(secs)

SUITS     = ['S','D','C','H']
SUIT_SYM  = {'S':'♠','D':'♦','C':'♣','H':'♥'}
SUIT_NAME = {'S':'Spades','D':'Diamonds','C':'Clubs','H':'Hearts'}
SUIT_COL  = {'S':'black','D':'red','C':'black','H':'red'}
RANKS     = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
RANK_VAL  = {r:i for i,r in enumerate(RANKS)}
TRUMP_CYC = ['S','D','C','H']

def build_deck(nd):
    return [{'rank':r,'suit':s} for _ in range(nd) for s in SUITS for r in RANKS]

def balance_deck(nd, np_):
    deck = build_deck(nd)
    removal = [{'rank':r,'suit':s} for r in ['2','3','4','5','6'] for s in SUITS for _ in range(nd)]
    while len(deck) % np_ != 0:
        c = removal.pop(0)
        deck.remove(c)
    random.shuffle(deck)
    return deck, len(deck)//np_

def round_seq(mx):
    return list(range(1,mx+1)) + list(range(mx-1,0,-1))

def beats(c, w, led, trump):
    cs,ws = c['suit'],w['suit']
    # same_card: exact duplicate — same rank AND same suit (only possible with 2+ decks)
    # Rule: later throw of identical card beats the earlier one
    same_card = (c['rank']==w['rank'] and cs==ws)
    if cs==trump and ws!=trump: return True
    if cs==trump and ws==trump: return RANK_VAL[c['rank']]>RANK_VAL[w['rank']] or same_card
    if cs==led   and ws!=trump: return RANK_VAL[c['rank']]>RANK_VAL[w['rank']] or same_card
    return False

games = {}

# ── Persistence ───────────────────────────────────────────────────
SAVE_FILE = '/tmp/kachuful_games.json' if os.environ.get('RENDER') else 'kachuful_games.json'

def save_games():
    try:
        data={}
        for code,g in games.items():
            data[code]={'code':g.code,'host_sid':g.host_sid,'status':g.status,'skipped':list(g.skipped),'waiting':list(g.waiting),
                'num_decks':g.num_decks,'rseq':g.rseq,'ridx':g.ridx,
                'didx':g.didx,'cidx':g.cidx,'trump':g.trump,
                'trick':g.trick,'tlead':g.tlead,'history':g.history,'maxc':g.maxc,
                'players':[{'sid':p['sid'],'name':p['name'],'is_host':p['is_host'],
                    'score':p['score'],'bid':p['bid'],'won':p['won'],
                    'cards':p['cards'],'connected':False} for p in g.players]}
        payload = json.dumps(data)
        if _redis:
            _redis.set('kachuful_games', payload, ex=86400)
        else:
            with open(SAVE_FILE,'w') as f: f.write(payload)
    except Exception as e: print(f'Save error: {e}')

def load_games():
    try:
        if _redis:
            raw = _redis.get('kachuful_games')
            if not raw: return {}
            data = json.loads(raw)
        else:
            if not os.path.exists(SAVE_FILE): return {}
            with open(SAVE_FILE,'r') as f: data=json.load(f)
        restored={}
        for code,d in data.items():
            if d['status'] in ('lobby','game_over'): continue
            g=Game.__new__(Game)
            g.code=d['code']; g.host_sid=d['host_sid']; g.status=d['status']
            g.num_decks=d['num_decks']; g.rseq=d['rseq']; g.ridx=d['ridx']
            g.didx=d['didx']; g.cidx=d['cidx']; g.trump=d['trump']
            g.trick=d['trick']; g.tlead=d['tlead']; g.history=d['history']
            g.maxc=d['maxc']; g.players=d['players']
            g.skipped=set(d.get('skipped',[])); g.waiting=set(d.get('waiting',[]))
            restored[code]=g
            print(f'Restored game {code} — {len(g.players)} players, round {g.ridx+1}')
        return restored
    except Exception as e: print(f'Load error: {e}'); return {}

def init_games():
    global games
    games=load_games()
    print(f'Loaded {len(games)} saved game(s) from disk')


def gcode():
    c=''.join(random.choices(string.ascii_uppercase,k=5))
    return c if c not in games else gcode()

class Game:
    def __init__(self,code,hsid,hname):
        self.code=code; self.host_sid=hsid; self.status='lobby'
        self.num_decks=2; self.players=[]; self.rseq=[]; self.ridx=0
        self.didx=0; self.cidx=0; self.trump=None; self.trick=[]
        self.tlead=0; self.history=[]; self.maxc=0
        self.skipped=set(); self.waiting=set()
        self.add_player(hsid,hname,True)

    def add_player(self,sid,name,host=False):
        self.players.append({'sid':sid,'name':name,'is_host':host,
            'score':0,'bid':None,'won':0,'cards':[],'connected':True})

    def P(self,sid): return next((p for p in self.players if p['sid']==sid),None)
    def I(self,sid): return next((i for i,p in enumerate(self.players) if p['sid']==sid),-1)
    def N(self): return len(self.players)

    def start(self):
        deck,self.maxc = balance_deck(self.num_decks,self.N())
        self.rseq=round_seq(self.maxc); self.ridx=0; self.didx=0
        for p in self.players: p['score']=0
        self._deal(deck)

    def _deal(self,deck=None):
        nc=self.rseq[self.ridx]; self.trump=TRUMP_CYC[self.ridx%4]
        self.trick=[]
        # Restore waiting players BEFORE dealing cards
        for p in self.players:
            if p['sid'] in self.waiting:
                self.waiting.discard(p['sid'])
                self.skipped.discard(p['sid'])
        for p in self.players: p['bid']=None; p['won']=0; p['cards']=[]
        if deck is None:
            deck,_=balance_deck(self.num_decks,self.N())
        active=[p for p in self.players if p['sid'] not in self.skipped]
        for i,p in enumerate(active): p['cards']=deck[i*nc:(i+1)*nc]
        self.tlead=self._next_active((self.didx+1)%self.N()); self.cidx=self.tlead
        self.status='bidding'

    def advance(self):
        self.ridx+=1; self.didx=(self.didx+1)%self.N(); self._deal()

    def bid(self,sid,amt):
        p=self.P(sid); pi=self.I(sid)
        if not p or p['sid'] in self.skipped: return False,"Player is skipped"
        if pi!=self.cidx or p['bid'] is not None: return False,"Not your turn"
        nc=self.rseq[self.ridx]
        if amt<0 or amt>nc: return False,"Invalid bid"
        if pi==self.didx:
            others=sum(x['bid'] for x in self.players if x['bid'] is not None)
            if others+amt==nc: return False,f"Hook rule: cannot bid {amt}"
        p['bid']=amt; self.cidx=(self.cidx+1)%self.N()
        # Skip over skipped players
        while self.status=='bidding' and self.players[self.cidx]['sid'] in self.skipped:
            self.cidx=(self.cidx+1)%self.N()
        active=[x for x in self.players if x['sid'] not in self.skipped]
        if active and all(x['bid'] is not None for x in active):
            self.status='playing'; self.cidx=self._next_active(self.tlead)
        return True,"ok" 

    def play(self,sid,ci):
        p=self.P(sid); pi=self.I(sid)
        if not p or p['sid'] in self.skipped: return False,"Player is skipped"
        if pi!=self.cidx: return False,"Not your turn"
        if ci<0 or ci>=len(p['cards']): return False,"Invalid card"
        card=p['cards'][ci]
        if self.trick:
            led=self.trick[0]['card']['suit']
            if any(c['suit']==led for c in p['cards']) and card['suit']!=led:
                return False,f"Must follow {SUIT_SYM[led]}"
        p['cards'].pop(ci)
        self.trick.append({'sid':sid,'name':p['name'],'card':card})
        # Advance to next non-skipped player
        self.cidx=self._next_active((self.cidx+1)%self.N())
        active_count=sum(1 for x in self.players if x['sid'] not in self.skipped)
        return True,('trick_done' if len(self.trick)==active_count else 'ok')

    def resolve(self):
        led=self.trick[0]['card']['suit']; w=self.trick[0]
        for e in self.trick[1:]:
            if beats(e['card'],w['card'],led,self.trump): w=e
        wp=self.P(w['sid']); wp['won']+=1
        wi=self.I(w['sid']); self.tlead=wi; self.cidx=wi
        t=self.trick[:]; self.trick=[]; return w['sid'],w['name'],t

    def done(self): return all(len(p['cards'])==0 for p in self.players if p['sid'] not in self.skipped)
    def last(self): return self.ridx>=len(self.rseq)-1

    def _next_active(self,idx):
        for _ in range(self.N()):
            if self.players[idx]['sid'] not in self.skipped: return idx
            idx=(idx+1)%self.N()
        return idx

    def active_count(self):
        return sum(1 for p in self.players if p['sid'] not in self.skipped)

    def check_pause(self):
        return self.active_count()<2 and self.status in ('bidding','playing')

    def skip_player(self,sid):
        p=self.P(sid)
        if not p: return False
        self.skipped.add(sid)
        p['cards']=[]; p['bid']=None; p['won']=0
        if self.status=='bidding':
            # Auto advance past skipped in bidding
            while self.status=='bidding' and self.players[self.cidx]['sid'] in self.skipped:
                self.cidx=(self.cidx+1)%self.N()
            active=[x for x in self.players if x['sid'] not in self.skipped]
            if active and all(x['bid'] is not None for x in active):
                self.status='playing'; self.cidx=self._next_active(self.tlead)
        elif self.status=='playing':
            if self.players[self.cidx]['sid'] in self.skipped:
                self.cidx=self._next_active((self.cidx+1)%self.N())
        return True

    def score(self):
        nc=self.rseq[self.ridx]; res=[]
        for p in self.players:
            pts=(10 if p['bid']==0 else 20+p['bid']) if p['bid']==p['won'] else 0
            p['score']+=pts
            res.append({'name':p['name'],'bid':p['bid'],'won':p['won'],'pts':pts,'total':p['score']})
        self.history.append({'round':self.ridx+1,'cards':nc,'trump':self.trump,'scores':res})
        self.status='round_end'; return res

    def final(self):
        return sorted([{'name':p['name'],'score':p['score']} for p in self.players],key=lambda x:-x['score'])

    def forbidden(self):
        if self.status!='bidding' or self.cidx!=self.didx: return None
        nc=self.rseq[self.ridx]
        others=sum(x['bid'] for x in self.players if x['bid'] is not None)
        fb=nc-others
        return fb if 0<=fb<=nc else None

    def snap(self,fsid=None):
        nc=self.rseq[self.ridx] if self.rseq else 0
        ps=[]
        for p in self.players:
            pd={'sid':p['sid'],'name':p['name'],'score':p['score'],
                'bid':p['bid'],'won':p['won'],'card_count':len(p['cards']),
                'is_host':p['is_host'],'connected':p['connected'],
                'skipped':p['sid'] in self.skipped,'waiting':p['sid'] in self.waiting}
            if fsid and p['sid']==fsid: pd['cards']=p['cards']
            ps.append(pd)
        return {'code':self.code,'status':self.status,'num_decks':self.num_decks,
                'ridx':self.ridx,'rseq':self.rseq,'cur_cards':nc,
                'trump':self.trump,'trump_sym':SUIT_SYM.get(self.trump,''),
                'trump_name':SUIT_NAME.get(self.trump,''),
                'trump_col':SUIT_COL.get(self.trump,'black'),
                'cidx':self.cidx,'cur_name':self.players[self.cidx]['name'] if self.players else '',
                'didx':self.didx,'dealer_name':self.players[self.didx]['name'] if self.status!='lobby' and self.players else '',
                'host_sid':self.host_sid,'trick':self.trick,'players':ps,'history':self.history,
                'forbidden':self.forbidden(),'maxc':self.maxc,
                'total_rounds':len(self.rseq)}

def bcast(g):
    save_games()
    for p in g.players:
        socketio.emit('state',g.snap(p['sid']),room=p['sid'])

def bcast_all(g):
    save_games()
    # Send personalised state to each player so everyone gets correct me.is_host etc
    for p in g.players:
        socketio.emit('state', g.snap(p['sid']), room=p['sid'])
    # Also broadcast to room for any listeners
    socketio.emit('state', g.snap(), room=g.code)

@socketio.on('create')
def on_create(d):
    name=d.get('name','Host').strip()[:20]
    code=gcode(); g=Game(code,request.sid,name)
    games[code]=g; join_room(code); join_room(request.sid)
    emit('joined',{'code':code,'you':name})
    emit('state',g.snap(request.sid))

@socketio.on('join')
def on_join(d):
    name=d.get('name','').strip()[:20]; code=d.get('code','').upper().strip()
    if code not in games: emit('err',{'msg':f'Room "{code}" not found'}); return
    g=games[code]
    if g.status!='lobby': emit('err',{'msg':'Game already started'}); return
    if g.N()>=15: emit('err',{'msg':'Room full (15 max)'}); return
    if any(p['name'].lower()==name.lower() for p in g.players):
        emit('err',{'msg':f'Name "{name}" taken'}); return
    g.add_player(request.sid,name); join_room(code); join_room(request.sid)
    emit('joined',{'code':code,'you':name})
    # Notify every existing player individually (especially host)
    for p in g.players:
        socketio.emit('state', g.snap(p['sid']), room=p['sid'])
    socketio.emit('player_joined', {'name':name,'count':g.N()}, room=g.code)

@socketio.on('set_decks')
def on_decks(d):
    code=d.get('code')
    if code not in games: return
    g=games[code]
    if request.sid!=g.host_sid: return
    g.num_decks=max(1,min(3,int(d.get('decks',2))))
    bcast_all(g)

@socketio.on('start')
def on_start(d):
    code=d.get('code')
    if code not in games: emit('err',{'msg':'Room not found'}); return
    g=games[code]
    if request.sid!=g.host_sid: emit('err',{'msg':'Only host can start'}); return
    if g.N()<2: emit('err',{'msg':'Need at least 2 players'}); return
    # Emit shuffle to all players, then deal after short delay via background task
    socketio.emit('shuffle_cards', {}, room=g.code)
    def _do_start():
        _sleep(1.8)
        g.start(); bcast(g)
        socketio.emit('toast',{'msg':f'Game on! Round 1 — {g.rseq[0]} card. Trump: {SUIT_SYM[g.trump]} {SUIT_NAME[g.trump]}','t':'info'},room=g.code)
    socketio.start_background_task(_do_start)

@socketio.on('bid')
def on_bid(d):
    code=d.get('code'); amt=d.get('amount')
    if code not in games: return
    g=games[code]
    if not isinstance(amt,int): emit('err',{'msg':'Invalid bid'}); return
    # If SID not in game, player may have reconnected with new SID — try to fix
    if not g.P(request.sid):
        name=d.get('name','')
        p=next((x for x in g.players if x['name'].lower()==name.lower()),None)
        if p:
            old=p['sid']; p['sid']=request.sid
            if g.host_sid==old: g.host_sid=request.sid
            join_room(code); join_room(request.sid)
    ok,msg=g.bid(request.sid,amt)
    if not ok: emit('err',{'msg':msg}); return
    if g.status=='playing':
        socketio.emit('toast',{'msg':f'All bids in! {g.players[g.tlead]["name"]} leads first.','t':'success'},room=g.code)
    bcast(g)

@socketio.on('play')
def on_play(d):
    code=d.get('code'); ci=d.get('card_idx')
    if code not in games: return
    g=games[code]
    # If SID not in game, player may have reconnected with new SID — fix silently
    if not g.P(request.sid):
        name=d.get('name','')
        p=next((x for x in g.players if x['name'].lower()==name.lower()),None)
        if p:
            old=p['sid']; p['sid']=request.sid
            if g.host_sid==old: g.host_sid=request.sid
            join_room(code); join_room(request.sid)
    ok,msg=g.play(request.sid,ci)
    if not ok: emit('err',{'msg':msg}); return
    if msg=='trick_done':
        wsid,wname,trick=g.resolve()
        socketio.emit('trick_result',{'winner':wname,'trick':trick},room=g.code)
        _sleep(2.5)
        if g.done():
            res=g.score()
            bcast(g)
            if g.last(): socketio.emit('game_over',{'final':g.final()},room=g.code)
            else: socketio.emit('round_end',{'result':res,'round':g.ridx+1},room=g.code)
        else: bcast(g)
    else: bcast(g)

@socketio.on('next_round')
def on_next(d):
    code=d.get('code')
    if code not in games: return
    g=games[code]
    if request.sid!=g.host_sid: return
    if g.last(): socketio.emit('game_over',{'final':g.final()},room=g.code); return
    # Emit shuffle event to ALL players, then advance after short delay via background task
    socketio.emit('shuffle_cards', {}, room=g.code)
    def _do_next():
        _sleep(1.8)
        g.advance(); bcast(g)
        nc=g.rseq[g.ridx]
        socketio.emit('toast',{'msg':f'Round {g.ridx+1} — {nc} card(s). Trump: {SUIT_SYM[g.trump]} {SUIT_NAME[g.trump]}','t':'info'},room=g.code)
    socketio.start_background_task(_do_next)

@socketio.on('close_room')
def on_close_room(d):
    code=d.get('code')
    if code not in games: return
    g=games[code]
    if request.sid!=g.host_sid:
        emit('err',{'msg':'Only host can close room'}); return
    socketio.emit('room_closed',{'msg':'Host has closed the room'},room=code)
    del games[code]
    try:
        if _redis: _redis.delete('kachuful_games')
    except: pass
    save_games()
    print(f'Room {code} closed by host')

@socketio.on('reconnect_player')
def on_reconnect(d):
    code=d.get('code'); name=d.get('name','')
    if code not in games: emit('err',{'msg':'Room not found'}); return
    g=games[code]
    p=next((x for x in g.players if x['name'].lower()==name.lower()),None)
    if not p: emit('err',{'msg':'Player not found'}); return
    old=p['sid']; p['sid']=request.sid; p['connected']=True
    if g.host_sid==old: g.host_sid=request.sid
    join_room(code); join_room(request.sid)
    save_games()
    emit('joined',{'code':code,'you':name,'reconnected':True})
    emit('state',g.snap(request.sid))
    socketio.emit('toast',{'msg':f'{name} rejoined!','t':'success'},room=code)
    bcast_all(g)

@socketio.on('skip_player')
def on_skip(d):
    code=d.get('code'); name=d.get('name','')
    if code not in games: return
    g=games[code]
    if request.sid!=g.host_sid: emit('err',{'msg':'Only host can skip'}); return
    p=next((x for x in g.players if x['name'].lower()==name.lower()),None)
    if not p: emit('err',{'msg':'Player not found'}); return
    ok=g.skip_player(p['sid'])
    if not ok: emit('err',{'msg':'Cannot skip'}); return
    save_games()
    socketio.emit('toast',{'msg':f'{name} skipped for this round','t':'warn'},room=code)
    if g.check_pause():
        socketio.emit('toast',{'msg':'Game paused — need at least 2 active players','t':'warn'},room=code)
    bcast(g)

@socketio.on('chat_msg')
def on_chat(d):
    code=d.get('code'); msg=d.get('msg','').strip()[:200]
    if not code or code not in games or not msg: return
    g=games[code]; p=g.P(request.sid)
    if not p: return
    socketio.emit('chat_msg',{'name':p['name'],'msg':msg},room=code)

@socketio.on('disconnect')
def on_dc():
    sid = request.sid
    def delayed_disconnect():
        # Wait 6 seconds — if player reconnected with new SID, their old SID
        # will still be in players but connected=True (updated by reconnect)
        # Only mark disconnected if SID still matches (no reconnect happened)
        _sleep(6)
        for code,g in games.items():
            p=g.P(sid)
            if p and not p['connected']:
                socketio.emit('toast',{'msg':f'{p["name"]} disconnected','t':'warn'},room=code)
                bcast_all(g)
                break
    for code,g in games.items():
        p=g.P(sid)
        if p:
            p['connected']=False
            # Spawn delayed check instead of immediate broadcast
            socketio.start_background_task(delayed_disconnect)
            break

@app.route('/')
def index(): return render_template('index.html')

@app.route('/join/<code>')
def joinlink(code): return render_template('index.html',join_code=code)

# Load saved games when server starts (handles Render restarts)
init_games()

if __name__=='__main__':
    print("\n  KACHUFUL — Judgement of Cards")
    print("  http://localhost:5000\n")
    socketio.run(app,host='0.0.0.0',port=5000,debug=False)
