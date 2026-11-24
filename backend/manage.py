"""Local administrator commands. Passwords are prompted or randomly generated."""
import argparse
import getpass
import json
import secrets
from backend.control import Store
from backend.security import create_user


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    add=sub.add_parser('add-user');add.add_argument('username');add.add_argument('--role',choices=['admin','viewer'],default='viewer')
    for name in ('disable-user','enable-user','reset-password'):
        command=sub.add_parser(name);command.add_argument('username')
    args=parser.parse_args();store=Store()
    if args.command=='init':
        credentials={}
        with store.db() as db: existing={row[0] for row in db.execute('SELECT username FROM users')}
        for username,role in [('admin','admin'),('viewer','viewer')]:
            if username in existing: continue
            password=secrets.token_urlsafe(18);create_user(store,username,password,role);credentials[username]=password
        if credentials:
            path=store.root/'local-accounts.json'
            if path.exists():
                old=json.loads(path.read_text(encoding='utf-8'));old.update(credentials);credentials=old
            path.write_text(json.dumps(credentials,ensure_ascii=False,indent=2),encoding='utf-8')
            print('Accounts created. Local passwords: '+str(path))
        else: print('Accounts already exist; passwords unchanged.')
    elif args.command=='add-user':
        create_user(store,args.username,getpass.getpass('Password (12+ characters): '),args.role);print('Account created.')
    else:
        with store.db() as db:
            user=db.execute('SELECT user_id FROM users WHERE username=?',(args.username,)).fetchone()
            if not user: parser.error('Account not found')
            if args.command=='reset-password':
                from backend.security import HASHER
                password=getpass.getpass('New password (12+ characters): ')
                if len(password)<12: parser.error('Password must have at least 12 characters')
                db.execute('UPDATE users SET password_hash=? WHERE user_id=?',(HASHER.hash(password),user[0]))
            else: db.execute('UPDATE users SET enabled=? WHERE user_id=?',(int(args.command=='enable-user'),user[0]))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user[0],))
        print('Account updated; old sessions revoked.')

if __name__=='__main__': main()
