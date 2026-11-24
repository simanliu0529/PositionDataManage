"""Account and session management; no default passwords or public credentials."""
import hashlib
import secrets
import time
import uuid
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from backend.control import APIError

HASHER=PasswordHasher()
DUMMY=HASHER.hash(secrets.token_urlsafe(24))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def create_user(store,username,password,role):
    if role not in ('admin','viewer') or not username.strip() or len(username)>80 or len(password)<12:
        raise ValueError('账号必须非空，角色为 admin/viewer，密码至少 12 位。')
    with store.db() as db:
        db.execute('INSERT INTO users VALUES (?,?,?,?,1)',(uuid.uuid4().hex,username,HASHER.hash(password),role))

def login(store,username,password,ip):
    now=time.time();keys=['user:'+username,'ip:'+ip]
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for key in keys:
            row=db.execute('SELECT * FROM login_limits WHERE key=?',(key,)).fetchone()
            if row and now-row['since']<300 and row['attempts']>=8:
                raise APIError(429,'LOGIN_RATE_LIMIT','登录失败次数过多，请在 5 分钟后重试。')
            db.execute('INSERT INTO login_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN since<? THEN 1 ELSE attempts+1 END,since=CASE WHEN since<? THEN ? ELSE since END',(key,now,now-300,now-300,now))
        user=db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
    try:
        valid=HASHER.verify(user['password_hash'] if user else DUMMY,password)
    except VerificationError:
        valid=False
    if not valid or user is None or not user['enabled']:
        raise APIError(401,'INVALID_CREDENTIALS','账号或密码错误，或账号已停用。')
    token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    with store.db() as db:
        db.execute('DELETE FROM sessions WHERE expires_at<?',(now,))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(digest(token),user['user_id'],csrf,now+8*3600))
        db.execute('DELETE FROM login_limits WHERE key=?',(keys[0],))
        db.execute('UPDATE login_limits SET attempts=MAX(0,attempts-1) WHERE key=?',(keys[1],))
        if HASHER.check_needs_rehash(user['password_hash']):
            db.execute('UPDATE users SET password_hash=? WHERE user_id=?',(HASHER.hash(password),user['user_id']))
    return token,{'username':user['username'],'role':user['role'],'csrf_token':csrf}

def session(store,token):
    with store.db() as db:
        row=db.execute('''SELECT u.user_id,u.username,u.role,s.csrf,s.expires_at FROM sessions s JOIN users u ON u.user_id=s.user_id
                       WHERE s.session_key_hash=? AND s.expires_at>? AND u.enabled=1''',(digest(token or ''),time.time())).fetchone()
    if row is None: raise APIError(401,'AUTH_REQUIRED','请先登录。')
    return dict(row)
