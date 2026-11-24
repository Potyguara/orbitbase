"""Gestão de contas pelo administrador (o autocadastro pelo site está desligado).

Na VPS, dentro de /opt/orbitbase/deploy:

    docker compose exec backend python -m app.manage_users list
    docker compose exec backend python -m app.manage_users create --name "Fulano" --email fulano@exemplo.com
    docker compose exec backend python -m app.manage_users set-password --email fulano@exemplo.com
    docker compose exec backend python -m app.manage_users deactivate --email fulano@exemplo.com
    docker compose exec backend python -m app.manage_users activate --email fulano@exemplo.com

Sem --password, a senha é pedida no terminal (use `docker compose exec -it ...`).
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.db import Base, SessionLocal, engine
from app.models import User
from app.security import hash_password
from app.services.billing import PLAN_LIMITS, get_or_create_usage_record

PLANS = tuple(PLAN_LIMITS)


def _ask_password(given: str | None) -> str:
    if given:
        password = given
    else:
        password = getpass.getpass("Senha: ")
        if password != getpass.getpass("Repita a senha: "):
            sys.exit("As senhas não conferem.")
    if not 6 <= len(password) <= 100:
        sys.exit("A senha precisa ter de 6 a 100 caracteres.")
    return password


def _find(db, email: str) -> User:
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user:
        sys.exit(f"Usuário não encontrado: {email}")
    return user


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.manage_users")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="lista as contas")

    create = sub.add_parser("create", help="cria uma conta")
    create.add_argument("--name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--password", help="se omitida, é pedida no terminal")
    create.add_argument("--plan", default="free", choices=PLANS)

    pwd = sub.add_parser("set-password", help="troca a senha de uma conta")
    pwd.add_argument("--email", required=True)
    pwd.add_argument("--password")

    for name in ("activate", "deactivate"):
        p = sub.add_parser(name, help=f"{'reativa' if name == 'activate' else 'bloqueia'} uma conta")
        p.add_argument("--email", required=True)

    args = parser.parse_args(argv)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if args.cmd == "list":
            for u in db.query(User).order_by(User.id).all():
                status = "ativo" if u.is_active else "INATIVO"
                print(f"{u.id:>4}  {u.email:<40} {u.name:<30} {u.plan:<10} {status}")
            return

        if args.cmd == "create":
            email = args.email.strip().lower()
            if db.query(User).filter(User.email == email).first():
                sys.exit(f"E-mail já cadastrado: {email}")
            name = args.name.strip()
            if len(name) < 2:
                sys.exit("Informe o nome (mínimo 2 caracteres).")
            user = User(
                name=name,
                email=email,
                password_hash=hash_password(_ask_password(args.password)),
                plan=args.plan,
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            get_or_create_usage_record(db, user)
            print(f"Conta criada: {user.email} (id {user.id}, plano {user.plan})")
            return

        user = _find(db, args.email)
        if args.cmd == "set-password":
            user.password_hash = hash_password(_ask_password(args.password))
            print(f"Senha alterada: {user.email}")
        else:
            user.is_active = args.cmd == "activate"
            print(f"{user.email}: {'ativo' if user.is_active else 'bloqueado'}")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
